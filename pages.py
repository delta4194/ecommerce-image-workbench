"""商品页获取与解析：优先本机浏览器助手，其次直接读取公开HTML。

这里只做两件不含副作用的事：
1. 找出已经装好采集助手扩展的浏览器与配置目录，需要时把它打开；
2. 把亚马逊 / 1688 的页面HTML解析成标题、五点、图片候选。
所有网络请求由 server.py 统一发出，本模块不自行联网。
"""
import json
import os
import re
import subprocess
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

LOCALAPPDATA = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
BROWSERS = [
    {
        'name': 'Edge',
        'exe': [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'],
        'userdata': LOCALAPPDATA / 'Microsoft' / 'Edge' / 'User Data',
    },
    {
        'name': 'Chrome',
        'exe': [r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'],
        'userdata': LOCALAPPDATA / 'Google' / 'Chrome' / 'User Data',
    },
]
PROFILE_NAME = re.compile(r'^(?:Default|Profile \d+)$')


def _load_profile_settings(profile: Path):
    for filename in ('Secure Preferences', 'Preferences'):
        path = profile / filename
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text('utf-8', errors='replace'))
        except Exception:
            continue
    return {}


def find_bridge_browser():
    """返回已安装采集助手的浏览器、配置目录和解压缩路径。"""
    for browser in BROWSERS:
        if not browser['userdata'].is_dir():
            continue
        executable = next((p for p in browser['exe'] if Path(p).exists()), '')
        for profile in sorted(browser['userdata'].iterdir()):
            if not profile.is_dir() or not PROFILE_NAME.match(profile.name):
                continue
            settings = _load_profile_settings(profile)
            entries = settings.get('extensions', {}).get('settings', {}) or {}
            for extension_id, config in entries.items():
                path = str(config.get('path', ''))
                if config.get('location') != 4 or 'collector' not in path.lower():
                    continue
                if config.get('disable_reasons'):
                    continue
                return {
                    'browser': browser['name'],
                    'exe': executable,
                    'profile': profile.name,
                    'profile_path': str(profile),
                    'extension_id': extension_id,
                    'extension_path': path,
                }
    return None


def open_bridge_browser(port: int):
    """启动（或激活）带采集助手的浏览器并打开工作台，让助手自动连上。"""
    found = find_bridge_browser()
    if not found or not found['exe']:
        return None
    command = [found['exe'], f"--profile-directory={found['profile']}", f'http://127.0.0.1:{port}/']
    try:
        subprocess.Popen(command, close_fds=True)
    except Exception:
        return None
    return found


def open_extension_page():
    """打开浏览器的扩展管理页，方便点一次“重新加载”把助手换成最新版。"""
    found = find_bridge_browser()
    if not found or not found['exe']:
        return None
    page = 'edge://extensions/' if found['browser'] == 'Edge' else 'chrome://extensions/'
    try:
        subprocess.Popen([found['exe'], f"--profile-directory={found['profile']}", page], close_fds=True)
    except Exception:
        return None
    return found


def open_url(url: str):
    """在已登录的浏览器里打开商品页（用于补登录或人工核对）。"""
    found = find_bridge_browser()
    if not found or not found['exe']:
        return None
    try:
        subprocess.Popen([found['exe'], f"--profile-directory={found['profile']}", url], close_fds=True)
    except Exception:
        return None
    return found


def _clean(value):
    return unescape(re.sub(r'<[^>]+>', ' ', str(value or ''))).strip()


THUMB = r'(?:sum|q\d+|\d+x\d+(?:q\d+)?)'
THUMB_DOUBLE = re.compile(r'\.(jpe?g|png|webp)_' + THUMB + r'\.(?:jpe?g|png|webp)$', re.I)
THUMB_SINGLE = re.compile(r'_' + THUMB + r'\.(jpe?g|png|webp)$', re.I)


def strip_thumb(url: str):
    """去掉图床缩略图后缀，取原始大图地址。"""
    match = THUMB_DOUBLE.search(url)
    if match:
        return url[:match.start()] + '.' + match.group(1)
    match = THUMB_SINGLE.search(url)
    if match:
        return url[:match.start()] + '.' + match.group(1)
    return url


def image_candidates(url: str):
    """把图床的缩略图地址还原成原图，并给出几个常见的大图规格。

    alicdn 的商品图常见形式：`...cib.jpg_sum.jpg`（列表缩略图）；
    去掉 `_sum` 就是原始大图，另有 `...cib.jpg_800x800.jpg` 之类的大图规格。
    """
    if not url:
        return []
    plain = re.sub(r'\?.*$', '', url)
    base = strip_thumb(plain)
    variants = [base, plain]
    match = re.match(r'^(.*)\.(jpe?g|png|webp)$', base, re.I)
    if match:
        root, ext = match.group(1), match.group(2)
        if not re.search(r'_\d+x\d+$', root):
            variants += [f'{root}_800x800.{ext}', f'{root}_q75.{ext}', f'{root}_400x400.{ext}']
    seen, result = set(), []
    for item in variants:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _first(pattern, text, flags=re.S):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ''


def looks_blocked(html: str) -> bool:
    if not html:
        return True
    sample = html[:4000] + html[-4000:]
    return bool(re.search(r'action"\s*:\s*"captcha"|请输入验证码|请完成验证|Robot Check|captcha|punish\?x5secdata', sample, re.I))


def amazon_images(html: str, url: str, limit=12):
    """按原图ID去重，返回可下载的高清地址（同ID可直接换尺寸后缀取最大）。"""
    found = {}
    order = []

    def add(raw):
        if not raw:
            return
        full = raw if raw.startswith('http') else urljoin(url, raw)
        full = full.replace('\\u002F', '/').replace('\\/', '/')
        match = re.search(r'https://m\.media-amazon\.com/images/I/([A-Za-z0-9+%-]+)(?:\._[^/]*_)?\.(?:jpg|jpeg|png|webp)', full, re.I)
        if not match:
            return
        key = match.group(1)
        if key in found:
            return
        found[key] = True
        order.append({
            'url': f'https://m.media-amazon.com/images/I/{key}.jpg',
            'alternatives': [full],
            'name': f'竞品图片 {len(order) + 1}',
        })

    for match in re.finditer(r'"hiRes"\s*:\s*"(https://[^"]+)"', html):
        add(match.group(1))
    for match in re.finditer(r'"large"\s*:\s*"(https://[^"]+)"', html):
        add(match.group(1))
    for match in re.finditer(r'data-a-dynamic-image="([^"]+)"', html):
        for candidate in re.findall(r'(https://[^"\s]+?\.(?:jpg|jpeg|png))', unescape(match.group(1))):
            add(candidate)
    for match in re.finditer(r'https://m\.media-amazon\.com/images/I/[A-Za-z0-9+%._-]+\.(?:jpg|jpeg|png|webp)', html):
        add(match.group(0))
    return order[:limit]


def amazon_bullets(html: str):
    block = ''
    start = html.find('id="feature-bullets"')
    if start >= 0:
        block = html[start:start + 20000]
    if not block:
        start = html.find('id="productFactsDesktopExpander"')
        block = html[start:start + 20000] if start >= 0 else html
    rows = [_clean(item) for item in re.findall(r'<span class="a-list-item">(.*?)</span>', block, re.S)]
    rows = [row for row in rows if row and not re.search(r'Make sure this fits|See more product details|^About this item$', row, re.I)]
    seen, bullets = set(), []
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        bullets.append(row[:600])
    return bullets[:10]


def amazon_page(html: str, url: str):
    title = _clean(_first(r'id="productTitle"[^>]*>(.*?)</span>', html)) or _clean(_first(r'<title>(.*?)</title>', html))
    bullets = amazon_bullets(html)
    images = amazon_images(html, url)
    detail = _first(r'id="productDescription"[^>]*>(.*?)</div>', html)
    text = '\n'.join(filter(None, [title, '\n'.join(bullets), _clean(detail)[:4000]]))
    return {
        'kind': 'competitor',
        'source': url,
        'title': title[:500],
        'bullets': bullets,
        'images': images,
        'text': text[:12000],
    }


def supplier_page(html: str, url: str):
    """1688: 公开HTML通常只给到图片候选；款式与名称仍需浏览器助手确认。"""
    title = _clean(_first(r'<title>(.*?)</title>', html)) or _clean(_first(r'property="og:title"\s+content="(.*?)"', html))
    pairs = []
    for block in re.findall(r'"skuProps"\s*:\s*(\[.*?\])\s*,\s*"', html, re.S)[:4]:
        for name, image in re.findall(r'"name"\s*:\s*"([^"]{1,60})"\s*,\s*"imageUrl"\s*:\s*"([^"]+)"', block):
            pairs.append((name, image))
    if not pairs:
        for name, image in re.findall(r'"name"\s*:\s*"([^"]{1,60})"\s*,\s*"imageUrl"\s*:\s*"([^"]+)"', html)[:40]:
            pairs.append((name, image))
    images, seen = [], set()
    for name, image in pairs:
        full = image if image.startswith('http') else 'https:' + image if image.startswith('//') else urljoin(url, image)
        full = re.sub(r'_\d+x\d+(?:q\d+)?\.(jpg|png|webp)$', r'.\1', full, flags=re.I)
        if full in seen:
            continue
        seen.add(full)
        images.append({'url': full, 'alternatives': [full], 'name': name[:60], 'sku': name[:60], 'matched': True})
    if not images:
        for match in re.finditer(r'https://cbu01\.alicdn\.com/[^\s"\'\\<>]+?\.(?:jpg|png|webp)', html):
            full = re.sub(r'_\d+x\d+(?:q\d+)?\.(jpg|png|webp)$', r'.\1', match.group(0), flags=re.I)
            if full in seen or len(images) >= 30:
                continue
            seen.add(full)
            images.append({'url': full, 'alternatives': [full], 'name': f'页面图片 {len(images) + 1}'})
    return {
        'kind': 'supplier',
        'source': url,
        'title': title[:500],
        'images': images,
        'text': '',
        'skus': [item.get('sku', '') for item in images if item.get('sku')],
    }
