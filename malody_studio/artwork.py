"""Cache the exact public YouTube thumbnail as a packaged JPEG background."""
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from PIL import Image
from .paths import ROOT

def video_id_from_url(url):
    if not url or not url.strip():
        return None
    from .music import valid_id
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https' or parsed.hostname not in ('www.youtube.com', 'youtube.com', 'music.youtube.com', 'youtu.be'):
        raise ValueError('封面链接请使用 YouTube 单曲 HTTPS 链接')
    value = parsed.path.strip('/') if parsed.hostname == 'youtu.be' else parse_qs(parsed.query).get('v', [''])[0]
    if parsed.path.startswith('/shorts/'):
        value = parsed.path.split('/')[2]
    return valid_id(value)

def check_jpeg(data):
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('封面图片超过 5 MB')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'JPEG' or image.width < 320 or image.height < 180 or image.width * image.height > 16000000:
            raise ValueError('封面图片格式或尺寸无效')
        dimensions = [image.width, image.height]
        image.verify()
    return dimensions

def prepare_artwork(video_id):
    if not video_id:
        return None, {'status': 'unlinked'}
    from .music import valid_id
    valid_id(video_id)
    directory = ROOT / 'uploads' / 'library' / video_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'background.jpg'
    manifest = directory / 'artwork.json'
    if target.is_file() and manifest.is_file():
        try:
            data = target.read_bytes()
            dimensions = check_jpeg(data)
            record = json.loads(manifest.read_text(encoding='utf-8'))
            if record['sha256'] == hashlib.sha256(data).hexdigest():
                return target, {**record, 'dimensions': dimensions}
        except (ValueError, KeyError, OSError):
            pass
    for resolution in ('maxresdefault', 'hqdefault'):
        source = f'https://i.ytimg.com/vi/{video_id}/{resolution}.jpg'
        try:
            with urlopen(Request(source, headers={'User-Agent': 'MalodyStudio/1.0'}), timeout=8) as response:
                data = response.read(5 * 1024 * 1024 + 1)
            dimensions = check_jpeg(data)
            record = {'status': 'ready', 'video_id': video_id, 'source': source,
                      'dimensions': dimensions, 'sha256': hashlib.sha256(data).hexdigest(),
                      'file': 'background.jpg', 'usage': '封面与背景共用 YouTube 原始缩略图'}
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
            manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            return target, record
        except Exception:
            continue
    return None, {'status': 'failed', 'video_id': video_id, 'message': 'YouTube 缩略图不可用'}
