"""本地自动作图服务：粘贴资料 → 自动采集 → 自动生成 → 保存本地。只用标准库加 Pillow。"""
import base64
import ctypes
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from PIL import Image, ImageOps

import pages
from automatic import Workflow
from importer import parse_import, purchase_skus

# 采集助手所在的浏览器由 pages 负责查找与打开。
find_bridge_browser = pages.find_bridge_browser
open_bridge_browser = pages.open_bridge_browser
image_candidates = pages.image_candidates

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
ASSETS = DATA / 'assets'
OUT = DATA / 'outputs'
for folder in (DATA, ASSETS, OUT):
    folder.mkdir(parents=True, exist_ok=True)
Image.MAX_IMAGE_PIXELS = 40_000_000
LOCK = threading.RLock()
DB = sqlite3.connect(DATA / 'workbench.sqlite', check_same_thread=False)
DB.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
DB.commit()
TOKEN = secrets.token_urlsafe(32)
RUNNING = set()
POOL = ThreadPoolExecutor(max_workers=3)
ARK = 'https://ark.cn-beijing.volces.com/api/v3'
DEFAULTS = {'image_model': 'doubao-seedream-4-5-251128', 'planner_model': '', 'parallel': 2,
            'main_count': 1, 'sub_count': 6, 'style_count': 0, 'auto_prompt': (
                '亚马逊商品图风格：真实商业摄影质感，主体清晰锐利，光线自然明亮，'
                '背景干净不杂乱，画面里产品是唯一主角；不要出现文字、水印、logo、箭头或拼贴。'),
            'output_root': '', 'image_backend': 'ark', 'lovart_skill': 'product-shoot',
            'lovart_upload_mode': 'manual',
            'lovart_ref_limit': 10, 'lovart_timeout_sec': 900}
RULES = ('产品是画面主角，场景只解释用途，道具少而准确。保持产品基准图中的颜色、款式、形状、结构、图案、'
         '材质、套装数量与真实比例；风格参考不得改变产品外观。专业商业摄影质感，自然光影，主体清晰，'
         '场景必须符合真实用途，安装关系未知时不得编造。除产品原有印刷外不添加任何文字、品牌、徽章、'
         '认证、箭头或拼贴。只在必要时出现成年人，不出现未成年人。')


def now():
    return datetime.now().isoformat(timespec='seconds')


def save_task(task):
    with LOCK:
        task['updated'] = now()
        DB.execute('INSERT OR REPLACE INTO tasks VALUES (?,?)', (task['id'], json.dumps(task, ensure_ascii=False)))
        DB.commit()


def get_task(tid):
    with LOCK:
        row = DB.execute('SELECT body FROM tasks WHERE id=?', (tid,)).fetchone()
    if not row:
        raise ValueError('任务不存在')
    return json.loads(row[0])


def all_tasks():
    with LOCK:
        rows = DB.execute('SELECT body FROM tasks ORDER BY rowid DESC').fetchall()
    return [json.loads(row[0]) for row in rows]


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(value, decrypt=False):
    if os.name != 'nt':
        raise ValueError('密钥加密目前只在 Windows 上可用；其他系统请设置 ARK_API_KEY 环境变量')
    raw = value if isinstance(value, bytes) else value.encode()
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), 'Ecommerce Auto', None, None, None, 1, ctypes.byref(dest))
    if not ok:
        raise ValueError('Windows 密钥加密失败')
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree(dest.data)


def settings(secret=False):
    path = DATA / 'settings.json'
    with LOCK:
        config = {**DEFAULTS, **(json.loads(path.read_text('utf-8')) if path.exists() else {})}
    key = os.environ.get('ARK_API_KEY', '')
    if not key and config.get('protected_key'):
        try:
            key = protect(base64.b64decode(config['protected_key']), True).decode()
        except Exception:
            if secret:
                raise ValueError('已保存的密钥无法解密，请重新填写')
    config.pop('protected_key', None)
    config['has_key'] = bool(key)
    if secret:
        config['key'] = key
    return config


def update_settings(body):
    path = DATA / 'settings.json'
    with LOCK:
        value = json.loads(path.read_text('utf-8')) if path.exists() else dict(DEFAULTS)
        for field in ('image_model', 'planner_model'):
            if field in body:
                value[field] = str(body.get(field, '')).strip()[:160]
        if 'auto_prompt' in body:
            value['auto_prompt'] = str(body.get('auto_prompt', '')).strip()[:4000]
        if 'sub_count' in body:
            value['sub_count'] = min(8, max(1, int(body['sub_count'])))
        if 'style_count' in body:
            value['style_count'] = max(0, min(9, int(body['style_count'])))
        if 'output_root' in body:
            value['output_root'] = str(body.get('output_root', '')).strip()[:260]
        if 'image_backend' in body:
            value['image_backend'] = 'lovart' if str(body.get('image_backend')) == 'lovart' else 'ark'
        if 'lovart_skill' in body:
            value['lovart_skill'] = str(body.get('lovart_skill', '')).strip().lstrip('/')[:60] or 'product-shoot'
        if 'lovart_ref_limit' in body:
            value['lovart_ref_limit'] = min(10, max(1, int(body['lovart_ref_limit'])))
        if 'lovart_timeout_sec' in body:
            value['lovart_timeout_sec'] = min(3600, max(120, int(body['lovart_timeout_sec'])))
        if 'lovart_upload_mode' in body:
            value['lovart_upload_mode'] = 'auto' if str(body.get('lovart_upload_mode')) == 'auto' else 'manual'
        if 'main_count' in body:
            value['main_count'] = 1 if int(body['main_count']) else 0
        value['parallel'] = min(3, max(1, int(body.get('parallel', value.get('parallel', 2)))))
        if body.get('clear_key'):
            value.pop('protected_key', None)
        elif str(body.get('key', '')).strip():
            value['protected_key'] = base64.b64encode(protect(str(body['key']).strip())).decode()
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False), 'utf-8')
        temp.replace(path)
    return settings()


def safe_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('只支持公开的 HTTPS 链接')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('不允许访问本地或内部网络地址')
    return url


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('模型接口发生异常跳转，已停止请求')


BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,de;q=0.7',
}


def download(url, limit=20_000_000, headers=None):
    safe_url(url)
    req = Request(url, headers=headers or {'User-Agent': 'Mozilla/5.0', 'Accept': '*/*'})
    with build_opener(ProxyHandler({}), PublicRedirect()).open(req, timeout=35) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('文件过大')
        return data


def image_headers(referer=''):
    """图片CDN（alicdn / media-amazon）需要正常的浏览器请求头，部分会拒绝没有 Referer 的请求。"""
    headers = {
        'User-Agent': BROWSER_HEADERS['User-Agent'],
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': BROWSER_HEADERS['Accept-Language'],
    }
    if referer:
        headers['Referer'] = referer
    return headers


FOLDER_DIALOG = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.Description='选择成品的保存位置';"
    "$d.ShowNewFolderButton=$true;"
    "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.SelectedPath)}"
)

SEND_FILES_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$pathsFile = $args[0]
$paths = Get-Content -LiteralPath $pathsFile -Encoding UTF8 | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if (-not $paths) { [Console]::Out.Write('NO-FILES'); exit 1 }
$quoted = ($paths | ForEach-Object { '"' + $_ + '"' }) -join ' '
Set-Clipboard -Value $quoted
$sig = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
'@
Add-Type -TypeDefinition $sig
$target = [IntPtr]::Zero
$callback = [W+EnumProc]{
  param($h, $l)
  if ([W]::IsWindowVisible($h)) {
    $buffer = New-Object System.Text.StringBuilder 512
    [void][W]::GetWindowText($h, $buffer, 512)
    $title = $buffer.ToString()
    if ($title -match '打开|Open|选择|上传|Upload|文件') { $script:target = $h }
  }
  return $true
}
[void][W]::EnumWindows($callback, [IntPtr]::Zero)
if ($target -eq [IntPtr]::Zero) { [Console]::Out.Write('NO-DIALOG'); exit 2 }
[void][W]::SetForegroundWindow($target)
Start-Sleep -Milliseconds 600
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 500
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
[Console]::Out.Write('SENT:' + $paths.Count)
'''


def fill_file_dialog(paths):
    """把文件路径粘进已经打开的 Windows 文件选择框并按回车（用于 Lovart 的上传按钮）。"""
    import subprocess
    import tempfile
    if not paths:
        raise ValueError('没有可以填入的文件')
    handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8')
    handle.write('\n'.join(str(Path(p)) for p in paths))
    handle.close()
    script = Path(handle.name).with_suffix('.ps1')
    script.write_text(SEND_FILES_SCRIPT, 'utf-8')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-File', str(script), handle.name],
                                capture_output=True, text=True, timeout=120)
        out = (result.stdout or '').strip()
        if out.startswith('SENT'):
            return {'ok': True, 'detail': out}
        if out == 'NO-DIALOG':
            return {'ok': False, 'detail': '没找到文件选择窗口（可能没点开上传按钮）'}
        return {'ok': False, 'detail': out or (result.stderr or '')[:120] or '未知结果'}
    finally:
        for path in (handle.name, script):
            try:
                Path(path).unlink()
            except Exception:
                pass


def pick_folder():
    """弹出系统的“选择文件夹”窗口，返回用户选中的路径。"""
    import subprocess
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-Command', FOLDER_DIALOG],
                                capture_output=True, text=True, timeout=300)
    except Exception as error:
        raise ValueError('没能打开文件夹选择窗口（' + str(error)[:80] + '），可以直接把路径粘贴到输入框里')
    path = (result.stdout or '').strip()
    if not path:
        return {'path': ''}
    return {'path': path}


PROBE_MODELS = ['doubao-seedream-5-0-260128', 'doubao-seedream-5-0-flash-260915', 'doubao-seedream-5-0-pro-260628',
                'doubao-seedream-4-5-251128', 'doubao-seedream-4-0-250828', 'doubao-seedream-4-0-20260415']


def probe_models(config, models=None):
    """零成本检测：故意用空 prompt。没开通会返回 ModelNotOpen，已开通只会报 prompt 参数错误。"""
    results = []
    for model in (models or PROBE_MODELS):
        payload = {'model': model, 'prompt': '', 'size': '2k', 'response_format': 'url'}
        try:
            api_call('/images/generations', payload, config)
        except ValueError as error:
            text = str(error)
            if 'prompt' in text and ('not valid' in text or 'cannot be empty' in text):
                results.append({'model': model, 'ok': True, 'message': '这个模型已经开通，可以用'})
                break
            results.append({'model': model, 'ok': False, 'message': text[:220]})
            if '还没在你的方舟账号开通' not in text and '模型ID或接入点不存在' not in text:
                break
            continue
        results.append({'model': model, 'ok': True, 'message': '这个模型已经开通，可以用'})
        break
    return results


def api_call(endpoint, payload, config):
    if not config.get('key'):
        raise ValueError('请先在“需要修改时”里保存火山方舟 API Key')
    req = Request(ARK + endpoint, data=json.dumps(payload).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + config['key'], 'Content-Type': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(req, timeout=240) as response:
            return json.loads(response.read(65_000_000))
    except HTTPError as error:
        raw = error.read(12000).decode('utf-8', 'replace')
        try:
            message = json.loads(raw).get('error', {}).get('message', '')
        except Exception:
            message = ''
        message = str(message).replace(config['key'], '[已隐藏]')[:350]
        if 'ModelNotOpen' in raw or 'not activated' in message:
            raise ValueError('这个模型还没在你的方舟账号开通（' + message[:130] + '）。'
                             '请到火山方舟控制台 → 开通管理 里开通「豆包·Seedream」图片生成，再点“继续”。') from None
        if 'InvalidEndpointOrModel' in raw or 'does not exist' in message:
            raise ValueError('模型ID或接入点不存在（' + message[:130] + '）。请在“更多设置”里填账号里已开通的模型ID，或 ep- 开头的接入点ID。') from None
        raise ValueError(f'模型接口返回 {error.code}：{message or "请检查模型权限、额度与配置"}') from None
    except (TimeoutError, socket.timeout):
        raise ValueError('请求超时，计费状态未知。不会自动重试；请先查看方舟控制台调用记录。') from None
    except Exception as error:
        text = str(error)
        if '10061' in text or 'refused' in text.lower() or 'getaddrinfo' in text or 'Name or service' in text:
            raise ValueError('连不上火山方舟接口（' + text[:90] + '）。请确认这台电脑能访问 ark.cn-beijing.volces.com，'
                             '并用 Start.cmd 启动本程序后点“继续自动作图”重试。') from None
        raise ValueError('模型请求失败：' + text[:160]) from None


def image_fingerprint(image):
    """8×8 灰度指纹：同一张照片即使换了尺寸后缀也能认出来，用于去掉重复图。"""
    small = image.convert('L').resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    average = sum(pixels) / len(pixels)
    bits = ''.join('1' if value > average else '0' for value in pixels)
    return f'{int(bits, 2):016x}'


def fingerprint_distance(first, second):
    """两个指纹的汉明距离：0-6 基本是同一张照片的不同尺寸，17 以上是不同图。"""
    try:
        return bin(int(first, 16) ^ int(second, 16)).count('1')
    except Exception:
        return 64


def store_image(raw, name, source='', max_side=2400):
    if len(raw) > 20_000_000:
        raise ValueError('每张图片不能超过20MB')
    with Image.open(io.BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened).convert('RGB')
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        aid = uuid.uuid4().hex
        image.save(ASSETS / (aid + '.jpg'), quality=94)
        return {'id': aid, 'name': str(name)[:160], 'source': source, 'url': '/assets/' + aid + '.jpg',
                'width': image.width, 'height': image.height, 'role': 'product', 'selected': True,
                'fingerprint': image_fingerprint(image)}


def data_url(aid):
    if not re.fullmatch('[a-f0-9]{32}', aid):
        raise ValueError('图片编号无效')
    path = ASSETS / (aid + '.jpg')
    if not path.exists():
        raise ValueError('参考图片已丢失，请重新采集')
    return 'data:image/jpeg;base64,' + base64.b64encode(path.read_bytes()).decode()


def direct_capture(kind, url, skus):
    """不打开浏览器时的兜底：直接读公开HTML。失败就交给浏览器助手。"""
    try:
        html = download(url, 6_000_000, BROWSER_HEADERS).decode('utf-8', 'replace')
    except Exception:
        return None
    if pages.looks_blocked(html):
        return None
    if kind == 'supplier':
        data = pages.supplier_page(html, url)
        images = data.get('images', [])
        wanted = [sku.strip().lower() for sku in (skus or []) if sku.strip()]
        if not images:
            return None
        if wanted:
            labels = {str(image.get('sku', '')).strip().lower() for image in images}
            if not set(wanted).issubset(labels):
                return None
        return data
    data = pages.amazon_page(html, url)
    if not data.get('title') or not data.get('bullets') or not data.get('images'):
        return None
    return data


def references(task, card=None):
    products = [a for a in task['assets'] if a.get('selected') and a.get('role') == 'product']
    products.sort(key=lambda a: (a['id'] != task.get('primary'),))
    # 风格参考默认用竞品全部主副图；只想少用几张时按像素从大到小截取。
    every_style = [a for a in task['assets'] if a.get('role') == 'style']
    limit = int(task.get('style_count') or 0)
    styles = [a for a in every_style if a.get('selected')] if limit else list(every_style)
    if limit:
        styles.sort(key=lambda a: -(a.get('pixels') or 0))
        styles = styles[:limit]
    # 全部参考时按页面顺序（主图在前），限张时按像素从大到小。
    styles.sort(key=lambda a: (a['order'] if isinstance(a.get('order'), int) else 0, -(a.get('pixels') or 0)))
    if not products:
        raise ValueError('缺少产品基准图')
    if not styles:
        raise ValueError('缺少竞品风格参考图')
    products = products[:4]
    return products, styles[:max(1, 10 - len(products))]


def build_prompt(task, card):
    products, styles = references(task, card)
    base_list = '；'.join(
        f'图{i + 1} 1688基准图“{a["name"]}”' + ('（多款拼在一张，逐格对照，不要跨格混色混款）' if '拼图' in str(a.get('name', '')) else '')
        for i, a in enumerate(products))
    style_list = '；'.join(
        f'图{i + len(products) + 1} 竞品主副图（原图{str(a.get("original_width", ""))}×{str(a.get("original_height", ""))}）'
        for i, a in enumerate(styles))
    user_prompt = (task.get('auto_prompt') or '').strip()
    competitor = task.get('competitor_capture', {}) or {}
    notes = task.get('auto_notes') or task.get('notes') or ''
    return f'''生成一张亚马逊商品图，正方形。

【这是什么产品】本品名称：{task['name']}。真实资料：{task.get('facts', '')[:4000]}。规格/套装：{task.get('identity', '')[:1500]}。

【本品外观依据】{base_list}。
产品的外观、颜色、图案、材质、结构、款式数量只认这些基准图；各款必须与对应实物一致，不得把竞品的外形特征搬到本品上。

【竞品资料包】{style_list}，共{len(styles)}张主副图，加上下面的标题与五点描述——这是同类商品在亚马逊上的完整表达方式，请把它们当成一个整体来理解，而不是只看其中某一张：
- 这组图和文字一起说明：这是什么产品、卖给谁、解决什么问题、用在什么场景、这个类目习惯怎么拍；
- 每一张竞品图都可以提供风格、场景、构图、布光、色调、道具和卖点表达上的参考，请综合吸收；
- 竞品标题：{str(competitor.get('title', ''))[:500]}。
- 竞品五点描述（用来理解产品定位、卖点结构和购买理由，属于竞品文字，不得当作本品的参数或承诺）：{json.dumps(competitor.get('bullets', []), ensure_ascii=False)[:3000]}。
- 1688货源标题（本品资料）：{str((task.get('supplier_capture') or {}).get('title', ''))[:600]}。

【本图任务】目的：{card['purpose']}。场景与构图：{card['scene']}。
请从上面的五点描述里挑出与本图目的最匹配的一个卖点来组织画面，让这张图承担一个明确的购买理由。

【用户的AI提示词】{user_prompt[:2000]}。

【作图规范】{RULES}
不得复制竞品的产品造型、品牌、logo、画面里的文字贴纸、赠品、认证或促销话术；画面里不出现任何新增文字。
用户补充要求：{notes[:1500]}。
输出单张完整照片，不拼贴，2K方图。'''


def build_set_prompt(task):
    """给 Lovart 技能包用的整套图提示词：沿用长提示词，只把“本图任务”换成整套清单。"""
    cards = [c for c in (task.get('cards') or []) if not c.get('current')] or (task.get('cards') or [])
    if not cards:
        cards = [{'title': '白底主图', 'purpose': '展示真实销售内容', 'scene': '纯白背景，只展示真实套装内容'},
                 {'title': '使用场景', 'purpose': '交代用途', 'scene': '资料支持的真实使用场景'},
                 {'title': '使用效果', 'purpose': '展示效果', 'scene': '使用中或使用后的真实状态'},
                 {'title': '操作动作', 'purpose': '说明用法', 'scene': '成年人按资料允许的方式使用'},
                 {'title': '套装内容', 'purpose': '说明套装组成', 'scene': '完整展示实际组成与数量'},
                 {'title': '材质细节', 'purpose': '展示材质', 'scene': '真实材质与表面纹理特写'},
                 {'title': '结构角度', 'purpose': '展示结构', 'scene': '侧面或背面的可见结构'}]
    listing = '\n'.join(f'{index + 1}. {card["title"]}——{card.get("purpose", "")}；{card.get("scene", "")}'
                        for index, card in enumerate(cards))
    base = build_prompt(task, cards[0])
    marker = '【本图任务】'
    head = base.split(marker)[0]
    tail = base.split(marker)[1].split('【用户', 1)[-1] if marker in base else ''
    return (head
            + '【本次要出的整套图】请按下面这个清单各出一张成品图，整套风格统一、画面互不重复，'
              '数量以清单为准；如果多出画面也一并给出。\n' + listing + '\n\n【用户' + tail)


def save_lovart_images(tid, images):
    """把 Lovart 回传的图片落盘；方图统一 1600×1600/72DPI，非方图等比缩到长边 1600 并标注。"""
    directory = OUT / tid
    directory.mkdir(parents=True, exist_ok=True)
    saved = []
    for data in images:
        if not isinstance(data, str) or ',' not in data:
            continue
        try:
            raw = base64.b64decode(data.split(',', 1)[1], validate=False)
            with Image.open(io.BytesIO(raw)) as source:
                photo = ImageOps.exif_transpose(source).convert('RGB')
                square = abs(photo.width / photo.height - 1) <= .06
                if square:
                    photo = ImageOps.fit(photo, (1600, 1600), method=Image.Resampling.LANCZOS)
                elif max(photo.size) > 1600:
                    photo.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                filename = uuid.uuid4().hex[:12] + '-lovart.jpg'
                photo.save(directory / filename, quality=95, dpi=(72, 72))
                saved.append({'file': filename, 'note': '' if square else '非方图',
                              'width': photo.width, 'height': photo.height})
        except Exception:
            continue
    return saved


def render_one(tid, cid, config):
    task = get_task(tid)
    if task.get('cancel'):
        return
    with LOCK:
        task = get_task(tid)
        card = next(c for c in task['cards'] if c['id'] == cid)
        card['status'], card['error'] = 'running', ''
        save_task(task)
    try:
        products, styles = references(task, card)
        prompt = card['prompt']
        if card.get('revision'):
            prompt += '\n本次修改要求：' + card['revision']
        payload = {'model': config['image_model'], 'prompt': prompt,
                   'image': [data_url(a['id']) for a in products + styles], 'size': '2K',
                   'response_format': 'url', 'watermark': False, 'sequential_image_generation': 'disabled'}
        result = api_call('/images/generations', payload, config)
        item = result.get('data', [{}])[0]
        if item.get('b64_json'):
            raw = base64.b64decode(item['b64_json'], validate=True)
        elif item.get('url'):
            raw = download(item['url'], 45_000_000)
        else:
            raise ValueError('模型没有返回图片，请查看模型权限或内容审核结果')
        directory = OUT / tid
        directory.mkdir(exist_ok=True)
        filename = cid + '-' + uuid.uuid4().hex[:8] + '.jpg'
        with Image.open(io.BytesIO(raw)) as source:
            photo = ImageOps.exif_transpose(source).convert('RGB')
            if abs(photo.width / photo.height - 1) > .06:
                raise ValueError('返回图片不是方图，已停止导出以免拉伸产品')
            photo = ImageOps.fit(photo, (1600, 1600), method=Image.Resampling.LANCZOS)
            photo.save(directory / filename, quality=95, dpi=(72, 72))
        with LOCK:
            task = get_task(tid)
            card = next(c for c in task['cards'] if c['id'] == cid)
            card['versions'].append({'file': filename, 'url': f'/outputs/{tid}/{filename}', 'created': now(),
                                     'model': config['image_model'], 'prompt': prompt, 'usage': result.get('usage', {})})
            card['current'] = filename
            card['status'], card['error'], card['reviewed'] = 'done', '', False
            save_task(task)
    except Exception as error:
        with LOCK:
            task = get_task(tid)
            card = next(c for c in task['cards'] if c['id'] == cid)
            card['status'], card['error'] = 'error', str(error)[:500]
            save_task(task)


def generate_job(tid, ids, config, release=True):
    try:
        with ThreadPoolExecutor(max_workers=config['parallel']) as pool:
            futures = [pool.submit(render_one, tid, cid, config) for cid in ids]
            for future in futures:
                future.result()
    finally:
        with LOCK:
            task = get_task(tid)
            for card in task['cards']:
                if card['status'] == 'queued':
                    card['status'] = 'draft'
            task['status'] = 'stopped' if task.get('cancel') else 'finished'
            save_task(task)
            if release:
                RUNNING.discard(tid)


def make_zip(task):
    buff = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buff, 'w', zipfile.ZIP_DEFLATED) as archive:
        for index, card in enumerate(task['cards'], 1):
            if not card.get('current'):
                continue
            file = OUT / task['id'] / card['current']
            if not file.is_file():
                continue
            name = f'{index:02d}_{file_label(task)}.jpg'
            archive.write(file, name)
            manifest.append({'file': name, 'title': card['title'], 'purpose': card['purpose']})
        if not manifest:
            raise ValueError('还没有可下载的图片')
        archive.writestr('生成记录.json', json.dumps({'name': task['name'], 'asin': task.get('asin', ''),
                                                  'size': '1600x1600', 'dpi': 72,
                                                  'cards': manifest}, ensure_ascii=False, indent=2))
    return buff.getvalue()


def compose_base(tid, assets, max_side=3200):
    """把多张基准图按原尺寸拼成一张大图，省下模型“最多10张参考图”的名额。"""
    photos = []
    for asset in assets:
        path = ASSETS / (asset['id'] + '.jpg')
        if path.exists():
            photos.append(Image.open(path).convert('RGB'))
    if not photos:
        raise ValueError('没有可拼接的基准图')
    if len(photos) == 1:
        photo = photos[0]
        aid = uuid.uuid4().hex
        photo.save(ASSETS / (aid + '.jpg'), quality=92)
        return {'id': aid, 'name': '基准图（单款）', 'source': '1688', 'url': '/assets/' + aid + '.jpg',
                'width': photo.width, 'height': photo.height, 'role': 'product', 'selected': True,
                'pixels': photo.width * photo.height}
    columns = 2 if len(photos) <= 4 else 3
    rows = (len(photos) + columns - 1) // columns
    gap = 28
    cell_w = max(photo.width for photo in photos)
    cell_h = max(photo.height for photo in photos)
    canvas = Image.new('RGB', (columns * cell_w + gap * (columns + 1), rows * cell_h + gap * (rows + 1)), '#f5f5f5')
    for index, photo in enumerate(photos):
        row, column = divmod(index, columns)
        # 小图放大到格子尺寸，避免拼图里有的格子只有一小块；大图保持原始像素不缩小。
        if max(photo.size) < max(cell_w, cell_h) * 0.75:
            ratio = min(cell_w / photo.width, cell_h / photo.height)
            photo = photo.resize((max(1, int(photo.width * ratio)), max(1, int(photo.height * ratio))),
                                 Image.Resampling.LANCZOS)
        x = gap + column * (cell_w + gap) + (cell_w - photo.width) // 2
        y = gap + row * (cell_h + gap) + (cell_h - photo.height) // 2
        canvas.paste(photo, (x, y))
    if max(canvas.size) > max_side:
        canvas.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    aid = uuid.uuid4().hex
    canvas.save(ASSETS / (aid + '.jpg'), quality=90)
    return {'id': aid, 'name': f'基准图拼图（{len(photos)}款）', 'source': '1688拼接',
            'url': '/assets/' + aid + '.jpg', 'width': canvas.width, 'height': canvas.height,
            'role': 'product', 'selected': True, 'pixels': canvas.width * canvas.height}


def safe_label(value, limit=80):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value or '')).strip(' .')
    value = re.sub(r'\s+', ' ', value)
    return value[:limit] or '未命名'


def delivery_label(task):
    """成品文件夹名：亚马逊商品名 + ASIN（没有ASIN时用ERP编号）。"""
    name = safe_label(task.get('name', ''), 60)
    code = safe_label(task.get('asin') or task.get('erp') or '', 20)
    return f'{name}_{code}' if code else name


def file_label(task):
    """图片文件名：亚马逊商品名 + ASIN。"""
    name = safe_label(task.get('name', ''), 36)
    code = safe_label(task.get('asin') or task.get('erp') or '', 20)
    return f'{name}_{code}' if code else name


def task_view(task):
    """给前端的精简视图：不返回密钥和无关字段。"""
    return {
        'id': task['id'],
        'name': task.get('name', ''),
        'erp': task.get('erp', ''),
        'asin': task.get('asin', ''),
        'identity': task.get('identity', ''),
        'status': task.get('status', ''),
        'stage': task.get('auto_stage', ''),
        'message': task.get('auto_message', ''),
        'error': task.get('error', ''),
        'model': task.get('auto_model', ''),
        'prompt': task.get('auto_prompt', ''),
        'sub_count': task.get('sub_count', 6),
        'main_count': task.get('main_count', 1),
        'style_count': task.get('style_count', 0),
        'backend': task.get('image_backend', 'ark'),
        'lovart_project': task.get('lovart_project', ''),
        'lovart_question': task.get('lovart_question', ''),
        'actual_count': task.get('actual_count', 0),
        'supplier_mode': task.get('supplier_mode', ''),
        'supplier_links': len(task.get('source_urls') or []),
        'supplier_title': (task.get('supplier_capture') or {}).get('title', ''),
        'competitor_title': (task.get('competitor_capture') or {}).get('title', ''),
        'competitor_bullets': (task.get('competitor_capture') or {}).get('bullets', []),
        'anchor': task.get('competitor_pixels') or {},
        'delivery': task.get('delivery_path', ''),
        'folder_name': delivery_label(task),
        'source_url': task.get('source_url', ''),
        'reference_url': task.get('reference_url', ''),
        'supplier_images': [{'name': a.get('name', ''), 'url': a['url'],
                             'pixels': f"{a.get('original_width', '')}×{a.get('original_height', '')}"}
                            for a in task.get('assets', []) if a.get('role') == 'product'][:9],
        'style_images': [{'url': a['url'], 'name': a.get('name', ''),
                          'pixels': f"{a.get('original_width', '')}×{a.get('original_height', '')}"}
                         for a in sorted([x for x in task.get('assets', []) if x.get('role') == 'style'],
                                         key=lambda x: x.get('order', 0))],
        'style_total': len([x for x in task.get('assets', []) if x.get('role') == 'style']),
        'gallery_total': (task.get('competitor_pixels') or {}).get('gallery') or 0,
        'warnings': task.get('warnings', [])[-3:],
        'cards': [{'id': c['id'], 'title': c['title'], 'status': c.get('status', ''),
                   'url': f"/outputs/{task['id']}/{c['current']}" if c.get('current') else '',
                   'error': c.get('error', '')} for c in task.get('cards', [])],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, content='application/json; charset=utf-8', status=200, extra=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            if self.headers.get('Host', '').split(':')[0] not in ('127.0.0.1', 'localhost'):
                return self.send({'error': '只允许本机访问'}, status=403)
            path = unquote(urlparse(self.path).path)
            if path == '/api/state':
                return self.send({'token': TOKEN, 'settings': settings(), 'rules': RULES,
                                  'tasks': [task_view(t) for t in all_tasks()], 'bridge': AUTO.bridge_state(),
                                  'output_root': str(AUTO.delivery)})
            if path == '/api/bridge/state':
                return self.send({'token': TOKEN, 'app': 'ecommerce-auto', 'version': '3.0',
                                  'connected': AUTO.bridge_state()['connected']})
            if path.startswith('/api/export/'):
                return self.send(make_zip(get_task(path.rsplit('/', 1)[1])), 'application/zip',
                                 extra={'Content-Disposition': 'attachment; filename=product-images.zip'})
            bases = {'/assets/': ASSETS, '/outputs/': OUT, '/static/': ROOT / 'static'}
            file = ROOT / 'static' / 'index.html' if path == '/' else None
            if file is None:
                for prefix, directory in bases.items():
                    if path.startswith(prefix):
                        candidate = (directory / path[len(prefix):]).resolve()
                        if candidate.is_relative_to(directory.resolve()):
                            file = candidate
                        break
            if not file or not file.is_file():
                return self.send({'error': '不存在'}, status=404)
            types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                     '.js': 'text/javascript; charset=utf-8', '.jpg': 'image/jpeg', '.png': 'image/png',
                     '.zip': 'application/zip', '.json': 'application/json'}
            return self.send(file.read_bytes(), types.get(file.suffix, 'application/octet-stream'))
        except Exception as error:
            self.send({'error': str(error)[:500]}, status=400)

    def do_POST(self):
        try:
            if self.headers.get('Host', '').split(':')[0] not in ('127.0.0.1', 'localhost') or self.headers.get('X-Workbench-Token') != TOKEN:
                return self.send({'error': '页面会话已失效，请刷新'}, status=403)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32_000_000:
                raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(size))
            path = urlparse(self.path).path
            if path.startswith('/api/bridge/'):
                return self.send(AUTO.bridge(path.rsplit('/', 1)[-1], body))
            if path == '/api/settings':
                return self.send(update_settings(body))
            if path == '/api/auto/start':
                return self.send(AUTO.start(body))
            if path == '/api/auto/resume':
                return self.send(AUTO.resume(body))
            if path == '/api/auto/cancel':
                return self.send(AUTO.cancel(body))
            if path == '/api/auto/folder':
                return self.send(AUTO.open_folder(body))
            if path == '/api/auto/delete':
                return self.send(AUTO.delete_task(body))
            if path == '/api/pick-folder':
                return self.send(pick_folder())
            if path == '/api/lovart/probe':
                target = str(body.get('url') or '').strip()
                if target and not re.fullmatch(r'https://(?:[\w.-]+\.)?lovart\.ai/[^\s]*', target):
                    raise ValueError('只能检查 Lovart 自己的页面地址')
                try:
                    return self.send(AUTO.probe_lovart(url=target or 'https://www.lovart.ai/canvas?newProject=true',
                                                       mode=str(body.get('mode', ''))))
                except ValueError as error:
                    if '旧版' in str(error):
                        try:
                            pages.open_extension_page()
                        except Exception:
                            pass
                    raise
            if path == '/api/lovart/upload-test':
                # 零成本：把某个任务真实要用的参考图试着挂进 Lovart，但不发送提示词。
                tid = str(body.get('task') or '')
                if not tid:
                    usable = [t for t in all_tasks() if any(a.get('role') == 'product' for a in t.get('assets', []))]
                    if not usable:
                        raise ValueError('还没有带参考图的任务：先跑一个商品，或先在卡片上点“重做这张”，再来体检')
                    tid = usable[0]['id']
                task = get_task(tid)
                files = AUTO.reference_files(task, None, 2)
                AUTO.export_reference_files(task['id'], files)
                try:
                    return self.send(AUTO.probe_lovart(mode='upload', files=files, task_id=task['id']))
                except ValueError as error:
                    if '旧版' in str(error):
                        try:
                            pages.open_extension_page()
                        except Exception:
                            pass
                    raise
            if path == '/api/auto/browser':
                return self.send({'opened': bool(AUTO.open_browser())})
            if path == '/api/auto/extension':
                found = pages.open_extension_page()
                return self.send({'opened': bool(found), 'browser': (found or {}).get('browser', '')})
            if path == '/api/auto/open':
                target = str(body.get('url', ''))
                parsed = urlparse(target)
                allowed = parsed.scheme == 'https' and bool(re.search(
                    r'(^|\.)(1688\.com|amazon\.(?:de|co\.uk|com|fr|it|es|nl|se|pl|ca|co\.jp))$', parsed.hostname or '', re.I))
                if not allowed:
                    raise ValueError('只能打开1688或亚马逊的商品链接')
                return self.send({'opened': bool(pages.open_url(target))})
            if path == '/api/models/probe':
                if not body.get('paid_ack'):
                    raise ValueError('检测会生成最多1张1K小图用于确认模型可用，请确认后重试')
                config = settings(True)
                if not config['has_key']:
                    raise ValueError('请先保存火山方舟 API Key')
                found = probe_models(config, body.get('models'))
                working = next((row['model'] for row in found if row['ok']), '')
                if working:
                    update_settings({'image_model': working})
                return self.send({'results': found, 'working': working})
            if path == '/api/auto/parse':
                return self.send(parse_import(body))
            if path == '/api/auto/redo':
                with LOCK:
                    task = get_task(body['task'])
                    card = next(c for c in task['cards'] if c['id'] == body['card'])
                    card['current'] = ''
                    card['status'] = 'queued'
                    if body.get('revision'):
                        card['revision'] = str(body['revision'])[:2000]
                    task['status'] = 'generating'
                    save_task(task)
                    RUNNING.add(task['id'])
                config = settings(True)
                config['image_model'] = task.get('auto_model') or config['image_model']
                backend = 'lovart' if str(task.get('image_backend') or config.get('image_backend')) == 'lovart' else 'ark'
                if backend == 'lovart':
                    AUTO.pool.submit(AUTO.redo_lovart, task['id'], config, body['card'])
                else:
                    POOL.submit(generate_job, task['id'], [body['card']], config)
                return self.send(task_view(task))
            if path == '/api/auto/lovart-answer':
                with LOCK:
                    task = get_task(body['task'])
                    task['lovart_answer'] = str(body.get('answer', '')).strip()[:2000]
                    task['lovart_question'] = ''
                    task['status'], task['error'], task['cancel'] = 'automatic', '', False
                    task['auto_message'] = '把回答发给 Lovart'
                    save_task(task)
                    RUNNING.add(task['id'])
                AUTO.pool.submit(AUTO.run, task['id'])
                return self.send(task_view(task))
            if path == '/api/auto/lovart-manual':
                # 参考图由用户自己在 Lovart 里挂好：程序跳过自动挂图，直接发提示词并收图。
                with LOCK:
                    task = get_task(body['task'])
                    task['lovart_skip_upload'] = True
                    task['lovart_question'] = ''
                    task['status'], task['error'], task['cancel'] = 'automatic', '', False
                    task['auto_message'] = '按你挂好的参考图继续出图'
                    save_task(task)
                    RUNNING.add(task['id'])
                AUTO.pool.submit(AUTO.run, task['id'])
                return self.send(task_view(task))
            if path == '/api/auto/ref-folder':
                task = get_task(body['task'])
                folder = OUT / task['id'] / '参考图'
                folder.mkdir(parents=True, exist_ok=True)
                if not any(folder.iterdir()):
                    raise ValueError('这个任务还没有导出参考图；先在卡片上跑一次作图（或点“重做这张”）就会生成')
                os.startfile(str(folder))
                return self.send({'path': str(folder)})
            return self.send({'error': '未知操作'}, status=404)
        except Exception as error:
            self.send({'error': str(error)[:500]}, status=400)


port = int(os.environ.get('WORKBENCH_PORT', '3788'))


def main():
    for task in all_tasks():
        if task['status'] in ('generating', 'automatic'):
            task['status'] = 'attention'
            task['error'] = '上次运行被中断，计费状态未知，请查看方舟记录后点“继续”。'
            task['auto_message'] = task['error']
            for card in task.get('cards', []):
                if card.get('status') in ('running', 'queued'):
                    card['status'], card['error'] = 'error', '运行中断，未自动重试'
            save_task(task)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Auto workbench ready at http://127.0.0.1:{port}', flush=True)
    server.serve_forever()


AUTO = Workflow(sys.modules[__name__])

if __name__ == '__main__':
    main()
