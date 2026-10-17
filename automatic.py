"""一键自动作图：粘贴资料 → 采集1688采购SKU → 采集亚马逊竞品 → 生成 → 保存本地。

默认全程自动：只有首次配置密钥、以及网页要求登录/验证时需要人工处理。
失败或需要改动的任务会停在任务卡片上，点“继续”即可接着跑，已完成的部分不会重做。
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from importer import parse_import, bundle_parts, purchase_skus

STAGES = [
    ('queued', '排队中', '等待自动采集'),
    ('supplier', '读取1688拿货款式', '按拿货规格匹配采购SKU的高清产品图'),
    ('competitor', '读取亚马逊竞品', '抓取标题、五点描述与主副图，取最大像素图作风格参考'),
    ('planning', '规划画面', '把产品身份与竞品风格分配到每张图'),
    ('generating', '生成图片', '逐张生成并立即保存到本机'),
    ('done', '完成', '图片、ZIP与记录已保存到成品文件夹'),
]
STAGE_INDEX = {key: index for index, (key, _, _) in enumerate(STAGES)}
SUB_SCENES = [
    ('使用场景', '把产品放进资料支持的真实使用场景，交代用途与氛围，产品是画面主角。'),
    ('使用效果', '展示产品在使用中或使用后的实际效果，保持可验证的真实状态。'),
    ('操作动作', '成年人按资料允许的方式取用或操作产品，手部自然，不编造安装步骤。'),
    ('套装内容', '完整展示这套商品的实际组成与数量，各款外观以对应实物图为准。'),
    ('材质细节', '近景特写真实材质、表面纹理与可见结构，光影干净。'),
    ('结构角度', '用侧面或背面等不同角度展示参考图能确认的结构。'),
    ('搭配展示', '在同一使用场景中展示产品与常见搭配物的关系，道具从简。'),
    ('收纳整理', '展示产品收纳或整理后的状态，画面整洁，结构真实。'),
]


class Workflow:
    def __init__(self, backend):
        self.w = backend
        self.lock = threading.RLock()
        self.requests = {}
        self.last_seen = 0.0
        self.launch_attempt = 0.0
        self.pool = ThreadPoolExecutor(max_workers=2)

    @property
    def delivery(self):
        """成品文件夹：默认在程序目录下的“成品”，可在“更多设置”里改成任意位置。"""
        custom = str(self.w.settings().get('output_root') or '').strip()
        path = Path(custom) if custom else self.w.ROOT / '成品'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def delete_task(self, body):
        tid = body['task']
        with self.w.LOCK:
            task = self.w.get_task(tid)
            if tid in self.w.RUNNING:
                # 正在跑的任务：先请求停止，再删记录；后台线程的回写会被忽略。
                task['cancel'] = True
                self.w.save_task(task)
                self.w.RUNNING.discard(tid)
            self.w.DB.execute('DELETE FROM tasks WHERE id=?', (tid,))
            self.w.DB.commit()
        shutil.rmtree(self.w.OUT / tid, ignore_errors=True)
        return {'deleted': tid, 'name': task.get('name', ''), 'delivery': task.get('delivery_path', '')}

    # ---------------------------------------------------------------- 浏览器助手
    def bridge_state(self):
        found = self.browser_info()
        return {
            'connected': time.time() - self.last_seen < 65,
            'browser': (found or {}).get('browser', ''),
            'profile': (found or {}).get('profile', ''),
            'has_browser': bool(found and found.get('exe')),
            'helper_version': getattr(self, 'helper_version', ''),
            'output_root': str(self.delivery),
        }

    def browser_info(self):
        if getattr(self, '_browser_cache', None) is None:
            self._browser_cache = self.w.find_bridge_browser()
        return self._browser_cache

    def open_browser(self):
        """打开已装好采集助手的浏览器；十几秒内助手会自动连上。"""
        self._browser_cache = None
        found = self.w.open_bridge_browser(self.w.port)
        if found:
            self._browser_cache = found
        return found

    def bridge(self, action, body):
        with self.lock:
            self.last_seen = time.time()
            if action == 'heartbeat':
                self.helper_version = str(body.get('version') or '')
                return {'ok': True}
            if action == 'next':
                for request in self.requests.values():
                    if request['state'] == 'pending':
                        request.update(state='leased', lease=secrets.token_urlsafe(24))
                        # 除了内部字段，作业参数原样下发（新增参数不用再改这里）。
                        payload = {key: value for key, value in request.items()
                                   if key not in ('tid', 'state', 'lease', 'data', 'error', 'timeout', 'timeout_sec')}
                        payload['lease'] = request['lease']
                        payload['timeout_sec'] = request.get('timeout_sec') or request.get('timeout') or 780
                        return payload
                return {}
            if action == 'result':
                request = self.requests.get(body.get('id'))
                if not request or request['state'] != 'leased' or body.get('lease') != request.get('lease'):
                    raise ValueError('采集请求已失效，请回到工作台继续')
                if body.get('error'):
                    request.update(state='error', error=str(body['error'])[:600])
                else:
                    data = body.get('data')
                    if not isinstance(data, dict):
                        raise ValueError('采集数据不完整')
                    if data.get('progress') and not data.get('images') and not data.get('pending_question'):
                        # 进度心跳：只更新卡片上的提示，作业继续等你出图。
                        self.update(request['tid'], auto_message=str(data['progress'])[:200])
                        return {'ok': True}
                    if self.page_identity(data.get('source', '')) != self.page_identity(request['url']):
                        raise ValueError('浏览器当前页面是另一个商品，已停止使用它的资料')
                    if request.get('notes'):
                        data = {**data, 'notes': request['notes']}
                    request.update(state='done', data=data)
                return {'ok': True}
        raise ValueError('未知采集操作')

    @staticmethod
    def page_identity(url):
        parsed = urlparse(url)
        match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})|/offer/(\d+)\.html', parsed.path, re.I)
        return parsed.hostname, (match.group(1) or match.group(2)).upper() if match else parsed.path

    # ---------------------------------------------------------------- 任务读写
    def update(self, tid, **fields):
        with self.w.LOCK:
            try:
                task = self.w.get_task(tid)
            except ValueError:
                return None  # 任务已被删除：忽略这次回写
            task.update(fields)
            self.w.save_task(task)
        return task

    def checkpoint(self, tid, stage, message=''):
        task = self.update(tid)
        if task is None:
            raise ValueError('任务已删除')
        if task.get('cancel'):
            raise ValueError('任务已停止；已发送到模型的请求无法撤回')
        self.update(tid, auto_stage=stage, auto_message=message or dict(
            (key, text) for key, _, text in STAGES).get(stage, ''))

    # ---------------------------------------------------------------- 采集
    def request_job(self, tid, kind, payload, timeout=300):
        """把一个作业交给浏览器助手并等待结果；采集和 Lovart 出图都走这里。"""
        request = {'id': uuid.uuid4().hex, 'tid': tid, 'kind': kind, 'state': 'pending', **payload}
        with self.lock:
            self.requests[request['id']] = request
        opened = False
        try:
            start = time.time()
            while time.time() - start < timeout:
                if tid and self.w.get_task(tid).get('cancel'):
                    raise ValueError('任务已停止')
                if request['state'] == 'done':
                    return request['data']
                if request['state'] == 'error':
                    raise ValueError(request['error'])
                if not self.bridge_state()['connected'] and not opened:
                    opened = True
                    if tid:
                        self.update(tid, auto_message='正在打开已登录的浏览器…')
                    self.open_browser()
                if time.time() - start > max(90, min(180, timeout / 3)) and not self.bridge_state()['connected']:
                    raise ValueError('自动采集助手没有连上。请在装好助手的浏览器里打开 http://127.0.0.1:%d ，再点“继续”。' % self.w.port)
                time.sleep(0.5)
            raise ValueError('等待浏览器超过 %d 秒还没完成。请检查浏览器里的页面，处理完点“继续”。' % int(timeout))
        finally:
            with self.lock:
                self.requests.pop(request['id'], None)

    def capture(self, tid, kind, url, skus, subset=False):
        parsed = urlparse(url)
        allowed = (parsed.hostname == 'detail.1688.com' and re.search(r'/offer/\d+\.html', parsed.path)) if kind == 'supplier' else (
            re.fullmatch(r'(?:[\w-]+\.)?amazon\.(?:de|co\.uk|com|fr|it|es|nl|se|pl|ca|co\.jp)', parsed.hostname or '')
            and re.search(r'/(?:dp|gp/product)/[A-Z0-9]{10}', parsed.path, re.I))
        if parsed.scheme != 'https' or not allowed:
            raise ValueError('缺少有效的' + ('1688拿货链接' if kind == 'supplier' else '亚马逊竞品链接') + '，请修改输入资料后重新开始')
        direct = self.w.direct_capture(kind, url, skus)
        if direct:
            return direct
        return self.request_job(tid, kind, {'url': url, 'skus': skus, 'subset': bool(subset)}, timeout=300)

    # ---------------------------------------------------------------- Lovart 通道
    def reference_files(self, task, card=None, limit=10):
        products, styles = self.w.references(task, card)
        files, slots = [], limit
        for index, asset in enumerate(products):
            if slots <= 0:
                break
            files.append({'id': asset['id'], 'name': f'基准图{index + 1}.jpg', 'url': asset['url'], 'role': 'product'})
            slots -= 1
        for index, asset in enumerate(styles):
            if slots <= 0:
                break
            files.append({'id': asset['id'], 'name': f'竞品参考图{index + 1}.jpg', 'url': asset['url'], 'role': 'style'})
            slots -= 1
        return files

    def export_reference_files(self, tid, files):
        """把这次要用的参考图复制到一个文件夹，方便手动拖进 Lovart。"""
        folder = self.w.OUT / tid / '参考图'
        folder.mkdir(parents=True, exist_ok=True)
        for item in files:
            source = self.w.ASSETS / (item['id'] + '.jpg')
            if source.exists():
                shutil.copy2(source, folder / item['name'])
        return folder

    def generate_lovart(self, tid, config, card_id=None):
        """用浏览器里的 Lovart 网页（会员额度）出图，整批一次技能调用。"""
        task = self.w.get_task(tid)
        card = next((c for c in task.get('cards', []) if c['id'] == card_id), None) if card_id else None
        if card is not None:
            card['prompt'] = self.w.build_prompt(task, card)
        else:
            whole = self.w.build_set_prompt(task)
            for item in task.get('cards', []):
                if not item.get('current'):
                    item['prompt'] = self.w.build_prompt(task, item)
        self.w.save_task(task)
        prompt = card['prompt'] if card is not None else whole
        files = self.reference_files(task, card, int(config.get('lovart_ref_limit', 10)))
        answer = str(task.get('lovart_answer') or '')
        project = str(task.get('lovart_project') or '') or 'new'
        timeout = int(config.get('lovart_timeout_sec', 900))
        # 已经建过项目、并且是回答 Agent 的追问：只把回答发过去，不重复上传参考图。
        follow_up = bool(answer) and project not in ('', 'new')
        sending_files = [] if follow_up else files
        sending_prompt = answer if follow_up else prompt
        skip_upload = bool(task.get('lovart_skip_upload')) or follow_up
        if sending_files:
            # 参考图另外存一份到任务目录，方便你手动拖进 Lovart（自动化挂图失败时的兜底）。
            self.export_reference_files(tid, sending_files)
        # 手动挂图模式（默认）：Lovart 不接受程序合成的拖放，所以先把画布开好、参考图备好，
        # 你拖进去点一下“继续”，剩下的发送与收图全自动。
        manual_mode = str(config.get('lovart_upload_mode') or 'manual') == 'manual'
        if manual_mode and not follow_up and not skip_upload and sending_files:
            self.request_job(tid, 'lovart-probe', {
                'url': 'https://www.lovart.ai/canvas?newProject=true' if project in ('', 'new') else project,
                'mode': 'open', 'task_id': tid, 'files': [],
            }, timeout=180)
            folder = self.w.OUT / tid / '参考图'
            self.update(tid, status='attention', auto_stage='generating', lovart_skip_upload=False,
                        auto_message=f'{len(sending_files)} 张参考图已备好在 {folder}；'
                                     '拖进 Lovart 输入框后点“参考图已挂好，继续”，之后全自动',
                        error='')
            return -1        # -1 = 正在等你在 Lovart 里挂好参考图，不算失败
        self.checkpoint(tid, 'generating', f'Lovart 正在出图：已准备 {len(files)} 张参考图，发送技能 /{config.get("lovart_skill", "product-shoot")}')
        try:
            result = self.request_job(tid, 'lovart', {
                'url': 'https://www.lovart.ai/canvas?newProject=true' if project in ('', 'new') else project,
                'project': project,
                'skill': str(config.get('lovart_skill') or 'product-shoot'),
                'prompt': sending_prompt,
                'answer': answer,
                'files': sending_files,
                'skip_upload': skip_upload,
                'timeout_sec': timeout,
            }, timeout=timeout)
        except ValueError as error:
            raise self.bridge_hint(error)
        images = [item for item in (result.get('images') or []) if isinstance(item, str)]
        if not images:
            question = str(result.get('pending_question') or '').strip()
            if question:
                self.update(tid, lovart_question=question, lovart_project=result.get('project_url') or '',
                            status='attention', auto_stage='generating',
                            auto_message='Lovart 反问了一句，回答后继续', error='')
                raise ValueError('Lovart 反问：' + question + '（在卡片上回答后点“继续”）')
            raise ValueError(result.get('note') or 'Lovart 没有返回图片，请在浏览器里看一次画布状态')
        saved = self.w.save_lovart_images(tid, images)
        if not saved:
            raise ValueError('Lovart 返回的图片无法解码')
        with self.w.LOCK:
            task = self.w.get_task(tid)
            task['lovart_project'] = result.get('project_url') or task.get('lovart_project') or ''
            task['lovart_question'] = ''
            task['lovart_answer'] = ''
            task['lovart_skip_upload'] = False
            cards = task.get('cards') or []
            if card is not None:
                target = next((c for c in cards if c['id'] == card['id']), None)
                targets = [target] + [None] * (len(saved) - 1)
            else:
                targets = list(cards)
            for index, item in enumerate(saved):
                version = {'file': item['file'], 'url': f"/outputs/{tid}/{item['file']}", 'created': self.w.now(),
                           'model': 'lovart:' + str(config.get('lovart_skill') or ''), 'prompt': prompt,
                           'note': item.get('note', '')}
                if index < len(targets) and targets[index]:
                    target = targets[index]
                else:
                    target = {'id': uuid.uuid4().hex[:12], 'title': f'Lovart 成品 {index + 1}', 'purpose': 'Lovart 技能出图',
                              'scene': '', 'prompt': prompt, 'status': 'draft', 'versions': [], 'reviewed': False}
                    cards.append(target)
                target.setdefault('versions', []).append(version)
                target['current'] = item['file']
                target['status'], target['error'] = 'done', ''
            for item in cards:
                # 技能包出图张数由 Lovart 决定：没出到的计划张数标成跳过，不算失败。
                if not item.get('current') and item.get('status') != 'error':
                    item['status'] = 'skipped'
            task['cards'] = cards
            task['actual_count'] = len(saved)
            task['status'] = 'finished'
            self.w.save_task(task)
        self.update(tid, auto_message=f'Lovart 已出图 {len(saved)} 张')
        return len(saved)

    def redo_lovart(self, tid, config, card_id):
        """单张重做：Lovart 通道下一次技能调用，结果挂回这张卡片。"""
        try:
            self.generate_lovart(tid, config, card_id)
        except Exception as error:
            self.update(tid, status='attention', auto_message=str(error)[:700], error=str(error)[:700])
        finally:
            with self.w.LOCK:
                self.w.RUNNING.discard(tid)

    def probe_lovart(self, url='https://www.lovart.ai/canvas?newProject=true', mode='', files=None, task_id=''):
        """体检 Lovart 通道：打开画布，确认登录状态与输入框（不发送任何生成请求）。"""
        try:
            result = self.request_job(None, 'lovart-probe',
                                      {'url': url, 'mode': mode, 'files': files or [], 'task_id': task_id}, timeout=600)
        except ValueError as error:
            raise self.bridge_hint(error)
        if isinstance(result, dict) and not result.get('version'):
            # 顺手把扩展管理页打开，省得你自己找
            raise ValueError('浏览器里的助手后台还是旧版（没有加载新的转发逻辑）：'
                             '请在“更多设置”点“助手更新到最新版”，在扩展页点一次“重新加载”。'
                             '我已经帮你打开扩展管理页了；这一次重载之后，Lovart 相关的调整就不需要再重载了。')
        return result

    @staticmethod
    def bridge_hint(error):
        text = str(error)
        if '采集网址不在允许' in text or '未知采集操作' in text or '采集数据不完整' in text:
            return ValueError('浏览器里的采集助手还是旧版：请在“更多设置”里点“助手更新到最新版”，'
                              '在扩展页对“商品素材采集助手”点一次“重新加载”，然后再试（只做一次）。')
        return error

    def import_photo(self, tid, item, role, referer=''):
        """下载候选并保留实际像素最大的一张，原图另存一份备查。"""
        raw_urls = list(dict.fromkeys([item.get('url', '')] + list(item.get('alternatives', []))))
        urls = []
        for url in raw_urls:
            for candidate in self.w.image_candidates(url):
                if candidate not in urls:
                    urls.append(candidate)
        urls = urls[:12]
        candidates = []
        errors = []
        for url in urls:
            host = urlparse(url).hostname or ''
            allowed = any(host == domain or host.endswith('.' + domain) for domain in (
                ('alicdn.com',) if role == 'product' else ('media-amazon.com', 'ssl-images-amazon.com', 'images-amazon.com')))
            if not allowed:
                continue
            try:
                raw = self.w.download(url, headers=self.w.image_headers(referer))
                with Image.open(io.BytesIO(raw)) as photo:
                    width, height = photo.size
                    image_format = photo.format
                    if width * height > 40_000_000 or min(width, height) < 250:
                        errors.append(f'{url.rsplit("/", 1)[-1][-28:]} 只有{width}×{height}')
                        continue
                candidates.append((width * height, url, raw, width, height, image_format))
            except Exception as error:
                errors.append(f'{type(error).__name__}: {str(error)[:70]}')
                continue
        if not candidates:
            detail = '；'.join(errors[:3]) if errors else '没有可用的图片地址'
            raise ValueError('无法下载“' + str(item.get('name', '参考图')) + '”的清晰原图（' + detail + '）')
        _, url, raw, width, height, image_format = max(candidates, key=lambda row: row[0])
        # 送进模型的参考图用适度缩小的副本，保证“竞品图全带上”时请求体积不超限。
        asset = self.w.store_image(raw, item.get('name', '来源图片'), url, max_side=1400 if role == 'style' else 1800)
        asset.update(role=role, selected=True, sku=item.get('sku', ''), original_width=width,
                     original_height=height, pixels=width * height)
        directory = self.w.OUT / tid / '采集原图'
        directory.mkdir(parents=True, exist_ok=True)
        extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(image_format, '.img')
        filename = asset['id'] + extension
        (directory / filename).write_bytes(raw)
        asset['original_file'] = filename
        asset['original_url'] = f'/outputs/{tid}/采集原图/{filename}'
        return asset

    # ---------------------------------------------------------------- 启动
    def start(self, body):
        if not body.get('paid_ack'):
            raise ValueError('请点击“开始自动作图”，一次授权本批采集与模型生成')
        config = self.w.settings(True)
        if not config['has_key']:
            raise ValueError('第一次使用请先在“需要修改时”里保存火山方舟 API Key，之后不用再填')
        model = str(body.get('model') or config['image_model']).strip()
        if not model:
            raise ValueError('请选择作图模型')
        products = parse_import({'text': body.get('text', '')})['products']
        if len(products) > 30:
            raise ValueError('一次最多30个销售商品，请分批粘贴')
        options = {
            'auto_model': model,
            'main_count': 1 if str(body.get('main', '1')) not in ('0', 'false', 'False') else 0,
            'sub_count': min(8, max(1, int(body.get('sub_count') or config.get('sub_count', 6)))),
            'style_count': max(0, min(9, int(body.get('style_count', config.get('style_count', 0)) or 0))),
            'image_backend': 'lovart' if str(body.get('image_backend') or config.get('image_backend')) == 'lovart' else 'ark',
            'auto_prompt': str(body.get('prompt', '')).strip()[:4000] or config.get('auto_prompt', ''),
            'auto_notes': str(body.get('notes', ''))[:3000],
        }
        if str(body.get('prompt', '')).strip() and str(body['prompt']).strip() != config.get('auto_prompt', ''):
            try:
                self.w.update_settings({'auto_prompt': body['prompt']})
            except Exception:
                pass
        created, reused = [], []
        with self.w.LOCK:
            existing = self.w.all_tasks()
            for record in products:
                mark = record.get('asin') or record.get('erp') or record['name'] + '|' + record.get('identity', '')
                digest = hashlib.sha256(mark.strip().upper().encode()).hexdigest()
                old = next((task for task in existing if task.get('auto_identity') == digest), None)
                if old:
                    retryable = old['id'] not in self.w.RUNNING and old.get('auto_stage') not in ('generating', 'done')
                    if retryable:
                        for key in ('name', 'facts', 'identity', 'notes', 'erp', 'asin', 'source_url',
                                    'reference_url', 'own_url', 'raw_import'):
                            old[key] = str(record.get(key, ''))[:18000]
                        old['source_urls'] = list(record.get('source_urls') or [])
                        old['identity_from_competitor'] = bool(record.get('identity_from_competitor'))
                        if not old.get('cards'):
                            # 还没出图的旧任务重新粘贴时，竞品素材按最新规则重采一次（张数/去重会更新）。
                            old.update(competitor_capture=None, style_anchor='', supplier_capture=None,
                                       assets=[a for a in old.get('assets', []) if a.get('role') == 'product'])
                        old.update(status='automatic', cancel=False, error='', auto_stage='queued',
                                   auto_message='等待自动采集', **options)
                        old['bundle_components'] = bundle_parts(old['identity'])
                        if old.get('supplier_capture') and not old.get('cards'):
                            old['assets'] = [a for a in old.get('assets', []) if a.get('role') == 'product']
                        self.w.save_task(old)
                        self.w.RUNNING.add(old['id'])
                        created.append(old)
                    else:
                        reused.append(old)
                    continue
                task = {key: str(record.get(key, ''))[:18000] for key in (
                    'name', 'facts', 'identity', 'notes', 'erp', 'asin', 'source_url', 'reference_url', 'own_url', 'raw_import')}
                task['source_urls'] = list(record.get('source_urls') or [])
                task['identity_from_competitor'] = bool(record.get('identity_from_competitor'))
                if not task['facts']:
                    task['facts'] = '产品名称：' + task['name'] + ('\n拿货规格：' + task['identity'] if task['identity'] else '')
                task.update(id=uuid.uuid4().hex, created=self.w.now(), status='automatic', cards=[], assets=[],
                            primary='', warnings=[], automatic=True, auto_identity=digest, auto_stage='queued',
                            auto_message='等待自动采集', cancel=False, paid_ack_at=self.w.now(), **options)
                task['bundle_components'] = bundle_parts(task['identity'])
                self.w.save_task(task)
                existing.append(task)
                self.w.RUNNING.add(task['id'])
                created.append(task)
            for task in created:
                self.pool.submit(self.run, task['id'])
        return {'created': created, 'reused': reused}

    def resume(self, body):
        tid = body['task']
        with self.w.LOCK:
            task = self.w.get_task(tid)
            if tid in self.w.RUNNING:
                raise ValueError('任务正在执行')
            if task.get('auto_stage') == 'done':
                raise ValueError('这个任务已经完成，可以在成品文件夹里取图')
            if task.get('auto_stage') in ('generating', 'interrupted') and not body.get('paid_ack'):
                raise ValueError('失败或中断的模型请求可能已经计费，请确认后点“继续”')
            if body.get('model'):
                task['auto_model'] = str(body['model']).strip()[:160]
            task.update(status='automatic', error='', auto_message='继续自动执行', cancel=False)
            self.w.save_task(task)
            self.w.RUNNING.add(tid)
            self.pool.submit(self.run, tid)
        return task

    def cancel(self, body):
        self.update(body['task'], cancel=True, auto_message='已请求停止，未开始的图片会跳过')
        return {'ok': True}

    # ---------------------------------------------------------------- 主流程
    def run(self, tid):
        try:
            task = self.w.get_task(tid)
            # 先取竞品：亚马逊通常一次就能读到，之后1688即使需要人工验证也不会重复抓竞品。
            if not any(a.get('role') == 'style' for a in task.get('assets', [])):
                data = task.get('competitor_capture')
                if not data:
                    self.checkpoint(tid, 'competitor')
                    data = self.capture(tid, 'amazon', task['reference_url'], [])
                    if not data.get('title') or not data.get('bullets'):
                        raise ValueError('竞品标题或五点描述没读到，已停止')
                    gallery = data.get('images', [])[:12]
                    if not gallery:
                        raise ValueError('没有采集到竞品主副图')
                    self.update(tid, competitor_capture=data)
                gallery = data.get('images', [])[:12]
                styles = []
                for index, image in enumerate(gallery):
                    self.checkpoint(tid, 'competitor', f'下载竞品主副图 {index + 1}/{len(gallery)}，比较实际像素取最大图')
                    asset = self.import_photo(tid, image, 'style', data.get('source', ''))
                    fingerprint = asset.get('fingerprint')
                    twin = next((a for a in styles if a.get('fingerprint')
                                 and self.w.fingerprint_distance(a['fingerprint'], fingerprint) <= 6), None)
                    if twin is not None:
                        # 同一张图的不同尺寸（常见的是页面缩略图）：只留清晰的那一张，缩略图不下载保留。
                        if (asset.get('pixels') or 0) > (twin.get('pixels') or 0):
                            position = styles.index(twin)
                            asset['order'] = twin.get('order', position)
                            styles[position] = asset
                        continue
                    asset['order'] = len(styles)
                    styles.append(asset)
                if not styles:
                    raise ValueError('竞品主副图都下载失败，已停止')
                anchor = max(styles, key=lambda asset: asset.get('pixels', 0))
                for asset in styles:
                    # 竞品主副图全部作为风格参考；请求时按像素从大到小取，钩子图排最前。
                    asset['selected'] = True
                self.update(tid, competitor_capture=data, assets=[a for a in task['assets'] if a['role'] == 'product'] + styles,
                            style_anchor=anchor['id'],
                            competitor_pixels={'width': anchor.get('original_width'), 'height': anchor.get('original_height'),
                                               'total': len(styles), 'gallery': data.get('gallery_count', len(gallery))})
            task = self.w.get_task(tid)
            if not any(a.get('role') == 'product' for a in task.get('assets', [])):
                skus = task.get('purchase_skus') or purchase_skus(task.get('identity', ''))
                style_assets = [a for a in task['assets'] if a.get('role') == 'style']
                if task.get('identity_from_competitor'):
                    # 拿货规格写的是“和竞品一样”：直接用竞品主图当产品基准图。
                    if not style_assets:
                        raise ValueError('资料写的是“和竞品一样”，但没采到竞品主图')
                    self.checkpoint(tid, 'supplier', '按“和竞品一样”，用竞品主图作产品基准图')
                    main = min(style_assets, key=lambda a: a.get('order', 99))
                    base = {**main, 'role': 'product', 'name': '竞品主图（基准）', 'selected': True}
                    remaining = [a for a in style_assets if a['id'] != main['id']]
                    for asset in remaining:
                        asset['selected'] = True
                    self.update(tid, supplier_capture={'source': task.get('reference_url', ''), 'title': '按“和竞品一样”使用竞品主图',
                                                       'images': [], 'mode': 'competitor', 'skus': []},
                                assets=[base] + remaining, primary=base['id'], purchase_skus=['和竞品一样'],
                                supplier_mode='competitor')
                else:
                    data = task.get('supplier_capture')
                    if not data:
                        links = list(dict.fromkeys([u for u in (task.get('source_urls') or [task.get('source_url', '')]) if u]))
                        images, seen = [], set()
                        for index, link in enumerate(links):
                            self.checkpoint(tid, 'supplier', f'读取1688拿货款式（链接{index + 1}/{len(links)}）')
                            part = self.capture(tid, 'supplier', link, skus, subset=len(links) > 1)
                            if len(links) > 1 and not part.get('capture_version'):
                                raise ValueError('采集助手版本过旧：请在“更多设置”里点“助手更新到最新版”，重新加载后再点继续（只做一次）')
                            for image in part.get('images', []):
                                key = image.get('url', '')
                                if key and key not in seen:
                                    seen.add(key)
                                    images.append(image)
                            data = data or {}
                            data = {'source': link, 'title': part.get('title', ''), 'images': images,
                                    'skus': [i.get('sku', '') for i in images if i.get('sku')],
                                    'mode': part.get('mode', 'sku'), 'capture_version': part.get('capture_version', ''),
                                    'links': links}
                        self.update(tid, supplier_capture=data, purchase_skus=skus)
                    images = data.get('images', [])
                    if not images:
                        raise ValueError('这个1688链接没有读到款式图，也没有主副图可用')
                    gallery_warning = []
                    if data.get('mode') == 'gallery' and skus:
                        gallery_warning = ['拿货规格里写了款式（' + '、'.join(skus[:8]) + '），'
                                           '但在1688页面上没匹配到，已改用该链接的主副图；'
                                           '请核对款式名是否和页面一致，或把那几款在页面上展开']
                    product_assets = []
                    failures = []
                    for image in images[:12]:
                        label = image.get('sku') or image.get('name') or '主副图'
                        self.checkpoint(tid, 'supplier', '下载拿货图 ' + str(label))
                        try:
                            asset = self.import_photo(tid, image, 'product', data.get('source', ''))
                        except Exception as error:
                            failures.append(str(error)[:120])
                            continue
                        if any(a.get('fingerprint') and a.get('fingerprint') == asset.get('fingerprint') for a in product_assets):
                            continue
                        product_assets.append(asset)
                    if not product_assets:
                        # 1688这边一张可用图都没有：按用户要求，退而使用竞品主图当基准图。
                        if not style_assets:
                            raise ValueError('1688没有可用产品图，竞品图也没有，无法确定产品外观')
                        self.checkpoint(tid, 'supplier', '1688没有可用产品图，改用竞品主图作基准图')
                        main = min(style_assets, key=lambda a: a.get('order', 99))
                        base = {**main, 'role': 'product', 'name': '竞品主图（1688无图，自动替代）', 'selected': True}
                        remaining = [a for a in style_assets if a['id'] != main['id']]
                        self.update(tid, assets=[base] + remaining, primary=base['id'], purchase_skus=skus,
                                    supplier_mode='competitor-fallback',
                                    warnings=list(task.get('warnings') or []) + [
                                        '1688没有可用产品图，已自动改用竞品主图作基准图' + ('（' + failures[0] + '）' if failures else '')])
                    else:
                        # 多张基准图拼成一张大图，给竞品风格参考省出名额。
                        composite = self.w.compose_base(tid, product_assets)
                        for asset in product_assets:
                            asset['selected'] = False
                        self.update(tid, supplier_capture=data, assets=product_assets + [composite] + style_assets,
                                    primary=composite['id'], purchase_skus=skus,
                                    supplier_mode=data.get('mode', 'sku'), supplier_composite=composite['id'],
                                    warnings=list(task.get('warnings') or []) + gallery_warning + (
                                        ['1688有' + str(len(failures)) + '张图下载失败，已用其余图片'] if failures else []))
            task = self.w.get_task(tid)
            if not task['cards']:
                self.checkpoint(tid, 'planning')
                task = self.plan(tid)
            self.checkpoint(tid, 'generating')
            task = self.w.get_task(tid)
            # 生成前按当前素材与设置重算提示词，改了参考图数量也能立刻生效。
            for card in task['cards']:
                if not card.get('current'):
                    card['prompt'] = self.w.build_prompt(task, card)
            self.w.save_task(task)
            ids = [card['id'] for card in task['cards'] if not card.get('current')]
            if ids:
                config = self.w.settings(True)
                config['image_model'] = task.get('auto_model') or config['image_model']
                backend = 'lovart' if str(task.get('image_backend') or config.get('image_backend')) == 'lovart' else 'ark'
                if backend == 'lovart':
                    # Lovart 走网页会话，一次技能调用出整套图，必须串行。
                    self.checkpoint(tid, 'generating', 'Lovart 技能包正在出整套图（用你的会员额度）')
                    task['status'] = 'generating'
                    self.w.save_task(task)
                    produced = self.generate_lovart(tid, config)
                    if produced == -1:
                        return  # 等你在 Lovart 里挂好参考图后点“继续”
                else:
                    for card in task['cards']:
                        if card['id'] in ids:
                            card['status'] = 'queued'
                    task['status'] = 'generating'
                    self.w.save_task(task)
                    self.w.generate_job(tid, ids, config, release=False)
            task = self.w.get_task(tid)
            expected = [card for card in task['cards'] if card.get('status') != 'skipped']
            done = sum(bool(card.get('current')) for card in expected)
            if done:
                folder = self.deliver(task)
                self.update(tid, delivery_path=str(folder))
            if task.get('cancel'):
                raise ValueError('已停止；完成的图片已经保存在本机')
            if done != len(expected):
                raise ValueError(f'已保存{done}张，还有{len(expected) - done}张没有生成。失败请求可能已经计费，看完错误可点“继续”只补这几张')
            self.update(tid, status='finished', auto_stage='done', error='',
                        auto_message=f'{done}张图片与记录已保存到成品文件夹')
        except Exception as error:
            self.update(tid, status='attention', auto_message=str(error)[:700], error=str(error)[:700])
        finally:
            with self.w.LOCK:
                self.w.RUNNING.discard(tid)

    def plan(self, tid):
        task = self.w.get_task(tid)
        products = [a for a in task['assets'] if a.get('role') == 'product']
        styles = [a for a in task['assets'] if a.get('role') == 'style']
        anchor = next((a for a in styles if a['id'] == task.get('style_anchor')), styles[0] if styles else None)
        if not products or not anchor:
            raise ValueError('缺少产品图或风格参考图，无法规划')
        cards = []
        if task.get('main_count', 1):
            cards.append({
                'title': '白底主图',
                'purpose': '平台主图：清楚展示真实销售内容',
                'scene': '纯白背景，只展示真实套装内容，无人物、无道具、无额外文字，保留产品原有印刷。',
            })
        titles = SUB_SCENES[:max(1, int(task.get('sub_count', 4)))]
        for title, scene in titles:
            cards.append({'title': title, 'purpose': title, 'scene': scene})
        for card in cards:
            card.update(id=uuid.uuid4().hex[:12], status='draft', versions=[], reviewed=False,
                        style_id=anchor['id'], prompt='')
            card['prompt'] = self.w.build_prompt(task, card)
        task['cards'] = cards
        task['plan_source'] = '一键默认画面（可用“需要修改时”调整）'
        self.w.save_task(task)
        return task

    def deliver(self, task):
        label = self.w.delivery_label(task)
        folder = self.delivery / (label + '_' + task['id'][:8])
        folder.mkdir(exist_ok=True)
        for index, card in enumerate(task['cards'], 1):
            if card.get('current'):
                shutil.copy2(self.w.OUT / task['id'] / card['current'],
                             folder / f'{index:02d}_{self.w.file_label(task)}.jpg')
        (folder / f'{label}_商品图.zip').write_bytes(self.w.make_zip(task))
        record = {key: task.get(key) for key in (
            'name', 'erp', 'asin', 'identity', 'bundle_components', 'source_url', 'reference_url',
            'supplier_capture', 'competitor_capture', 'delivery_path')}
        (folder / '采集与生成记录.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
        return folder

    def open_folder(self, body):
        task = self.w.get_task(body['task']) if body.get('task') else None
        path = Path(task.get('delivery_path')) if task and task.get('delivery_path') else (
            self.w.OUT / task['id'] if task else self.delivery)
        resolved = path.resolve()
        if not (resolved.is_relative_to(self.delivery.resolve()) or resolved.is_relative_to(self.w.OUT.resolve())):
            raise ValueError('目录无效')
        resolved.mkdir(parents=True, exist_ok=True)
        os.startfile(str(resolved))
        return {'path': str(resolved)}
