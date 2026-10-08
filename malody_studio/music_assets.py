"""Independent, persistent source audio downloads. No generation or audio analysis."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4
from datetime import datetime, timezone
import copy
import json
import re
import shutil
import subprocess
import threading
import os
from .paths import ROOT
from . import music

MAX_BYTES = 200 * 1024 * 1024
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='independent-music')


def now():
    return datetime.now(timezone.utc).isoformat()


def units(value):
    return len(value.encode('utf-16-le')) // 2


def trim(value, maximum):
    while units(value) > maximum:
        value = value[:-1]
    return value


def safe(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip().rstrip('. ')
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    return value


def filename(title, artist, extension, suffix=''):
    song, author = safe(title) or '未命名音乐', safe(artist)
    budget = 180 - units(suffix)
    if author:
        author = trim(author, max(0, budget - 4))
        song = trim(song, max(1, budget - units(author) - 3))
        stem = song + ' - ' + author
    else:
        stem = trim(song, budget)
    return stem.rstrip('. ') + suffix + '.' + extension


def diagnostic(message, exc=None, response=''):
    return music._command_failure(['kipfel'], category='third_party', message=message,
                                  stdout=response, exception=exc)


def json_request(url):
    with urlopen(Request(url, headers={'User-Agent': 'MalodyStudio/1.0'}), timeout=30) as response:
        raw = response.read(2 * 1024 * 1024).decode('utf-8')
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise diagnostic('第三方解析返回了无效响应，请重试', exc, raw) from exc


def bili_input(value):
    match = re.fullmatch(r'BV[A-Za-z0-9]{10}', value)
    if match:
        return 'https://www.bilibili.com/video/' + value, value, 1
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'):
        raise ValueError('B站仅支持视频链接、BV号和 b23.tv 分享短链')
    match = re.search(r'/video/(BV[A-Za-z0-9]{10})(?:/|$)', parsed.path)
    if not match and parsed.hostname != 'b23.tv':
        raise ValueError('请填写 B站视频链接或 BV号')
    try:
        part = int(parse_qs(parsed.query).get('p', ['1'])[0])
    except ValueError:
        raise ValueError('B站分P编号无效')
    if not 1 <= part <= 1000:
        raise ValueError('B站分P编号无效')
    return value, match[1] if match else None, part


def resolve_bili(value, previous=None):
    url, expected, part = bili_input(value)
    previous = previous or {}
    errors = []
    remembered = dict(previous)
    for route in ('vrc-json', 'kfc-json'):
        endpoint = 'https://api.kipfel.link/v1/' + route + '?' + urlencode({'url': url})
        try:
            payload = json_request(endpoint)
            data = payload.get('data') or {}
            if payload.get('error') or payload.get('code', 0) != 0:
                raise diagnostic('B站解析失败：' + str(payload.get('message') or payload.get('code')), response=json.dumps(payload, ensure_ascii=False))
            content = data.get('content') or {}
            # Preserve all nonempty metadata even if this route lacks a resource.
            author = data.get('author') or {}
            if data.get('title'): remembered['title'] = data['title']
            if author.get('nickname'): remembered['artist'] = author['nickname']
            cover=data.get('cover') or data.get('cover_url') or content.get('cover_url')
            if cover: remembered['thumbnail'] = cover
            returned_id=data.get('video_id')
            bvid = data.get('bvid') or (returned_id if isinstance(returned_id,str) and re.fullmatch(r'BV[A-Za-z0-9]{10}',returned_id) else None) or expected
            if not bvid or not re.fullmatch(r'BV[A-Za-z0-9]{10}', bvid) or (expected and bvid != expected):
                raise ValueError('第三方未返回一致的视频身份，请重试')
            reported = data.get('page') or data.get('part') or content.get('page')
            if (reported is not None and int(reported) != part) or (part != 1 and reported is None):
                raise ValueError('第三方未验证所选分P，无法安全下载该分P')
            resource = content.get('audio_url') or data.get('audio_url') or content.get('play_url') or data.get('play_url')
            if not resource or urlparse(resource).scheme not in ('https', 'http'):
                raise ValueError('第三方未返回可下载的音轨或视频地址')
            return {**remembered, 'id': f'bili-{bvid}-p{part}', 'platform': 'bilibili',
                    'video_id': bvid, 'part': part, 'title': remembered.get('title') or '未命名音乐',
                    'artist': remembered.get('artist', ''), 'channel': remembered.get('artist', ''),
                    'duration': data.get('duration') or previous.get('duration'), 'url': url,
                    'resource_url': resource, 'extension': 'm4a', 'parser_route': route}
        except Exception as exc:
            errors.append(str(exc))
    raise diagnostic('B站解析失败：' + '；'.join(errors), response=json.dumps(errors, ensure_ascii=False))


def resolve_youtube(value):
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        value = music.url_for(value)
    if not value.startswith(('https://', 'http://')):
        raise ValueError('请填写有效的 YouTube 视频链接')
    page = music.search_page(value, limit=1)
    if not page['results']:
        raise ValueError('请填写有效的 YouTube 视频链接')
    row = page['results'][0]
    info = json.loads(music.command('--skip-download', '--dump-single-json', row['url']))
    formats = [f for f in info.get('formats', []) if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    extension = formats[-1].get('ext') if formats else info.get('ext')
    return {**row, 'platform': 'youtube', 'video_id': row['id'], 'part': 1, 'extension': extension or 'webm'}


def probe(path):
    """Inspect and decode without producing a converted music file."""
    from .audio import ffmpeg
    result = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path), '-map', '0:a:0',
                             '-vn', '-f', 'null', '-'], capture_output=True, text=True, errors='replace', timeout=180)
    text = result.stderr
    if result.returncode:
        raise ValueError('音轨验证失败：' + text[-600:])
    duration = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', text)
    audio = re.search(r'Stream #0:\d+[^\n]*Audio: ([a-zA-Z0-9_]+)', text)
    container = re.search(r'Input #0, ([^,]+)', text)
    if not duration or not audio or not container:
        raise ValueError('无法确认实际音频格式或时长')
    seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
    if not 5 <= seconds <= 600:
        raise ValueError('请选择 5 秒至 10 分钟的音乐')
    codec, fmt = audio[1], container[1]
    has_video = bool(re.search(r'Stream #0:\d+[^\n]*Video:', text))
    ext = {'aac': 'm4a', 'alac': 'm4a', 'mp3': 'mp3', 'flac': 'flac', 'vorbis': 'ogg', 'opus': 'webm'}.get(codec)
    if fmt == 'aac': ext = 'aac'
    elif fmt in ('wav', 'flac', 'mp3', 'ogg', 'webm', 'matroska'): ext = {'matroska': 'webm'}.get(fmt, fmt)
    if not ext:
        raise ValueError('暂不支持该源音轨格式：' + codec + ' / ' + fmt)
    return {'duration': seconds, 'codec': codec, 'format': fmt, 'extension': ext, 'has_video': has_video}


def source_audio(source, directory):
    if Path(source).stat().st_size > MAX_BYTES:
        raise ValueError('音乐文件超过 200 MB')
    meta = probe(source)
    if not meta['has_video']:
        target = directory / ('audio.' + meta['extension'])
        if source.resolve() != target.resolve(): shutil.copy2(source, target)
        return target, meta
    from .audio import ffmpeg
    target = directory / ('audio.' + meta['extension'])
    result = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                             '-map', '0:a:0', '-vn', '-c:a', 'copy', str(target)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('无损提取音轨失败：' + result.stderr[-600:])
    verified = probe(target)
    if verified['has_video'] or verified['codec'] != meta['codec']:
        raise ValueError('音轨提取校验失败')
    return target, verified


class ResourceExpired(ValueError):
    pass


def fetch_resource(url, target):
    try:
        with urlopen(Request(url, headers={'User-Agent': 'MalodyStudio/1.0'}), timeout=30) as response:
            if int(response.headers.get('Content-Length') or 0) > MAX_BYTES:
                raise ValueError('音乐文件超过 200 MB')
            count = 0
            with target.open('wb') as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    count += len(chunk)
                    if count > MAX_BYTES: raise ValueError('音乐文件超过 200 MB')
                    output.write(chunk)
            if not count: raise ValueError('第三方返回的资源为空')
    except HTTPError as exc:
        raw = exc.read(1024 * 1024).decode('utf-8', errors='replace')
        error = diagnostic('CDN下载失败：HTTP ' + str(exc.code), exc, raw)
        if exc.code in (400, 401, 403, 404, 410):
            expired = ResourceExpired('CDN资源地址失效：HTTP ' + str(exc.code))
            expired.diagnostic_id = error.diagnostic_id
            raise expired from exc
        raise error from exc


class AssetStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.directory = self.root / 'data' / '音乐'
        self.registry = self.root / 'data' / 'music-library'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry.mkdir(parents=True, exist_ok=True)
        self.index = self.registry / 'index.json'
        self.lock = threading.RLock()
        self.source_locks = {}
        self.state = {'schema': 1, 'assets': {}, 'tasks': {}, 'candidates': {}, 'sources': {}}
        if self.index.exists(): self.state.update(json.loads(self.index.read_text(encoding='utf-8')))
        for task in self.state['tasks'].values():
            if task['status'] in ('queued', 'downloading', 'saving'):
                task.update(status='failed', message='下载被服务重启中断，请重试', retryable=True)
        self.persist()

    def persist(self):
        with self.lock:
            temporary = self.index.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.index)

    def resolve(self, value):
        value = str(value).strip()
        if not value or len(value) > 2048: raise ValueError('请填写视频链接')
        row = resolve_bili(value) if ('bilibili.com' in value or 'b23.tv' in value or value.startswith('BV')) else resolve_youtube(value)
        with self.lock:
            self.state['candidates'][row['id']] = row
            self.persist()
        return copy.deepcopy(row)

    def assets(self):
        with self.lock: return copy.deepcopy(list(self.state['assets'].values()))

    def tasks(self):
        with self.lock: return copy.deepcopy(list(self.state['tasks'].values()))

    def task(self, task_id):
        with self.lock:
            if task_id not in self.state['tasks']: raise ValueError('下载任务不存在')
            return copy.deepcopy(self.state['tasks'][task_id])

    def identify(self, path, original_filename=None):
        digest = music._file_hash(path)
        matches=[row for row in self.assets() if row['sha256']==digest]
        # Renamed copies can deliberately carry different confirmed metadata.
        # Prefer the chosen file's registered name within the verified hash set.
        if original_filename:
            name=Path(original_filename).name.casefold()
            selected=next((row for row in matches if row['filename'].casefold()==name),None)
            if selected:return selected
        return matches[0] if matches else None

    def begin(self, source_id, title, artist):
        if not isinstance(source_id, str): raise ValueError('音乐来源编号无效')
        if not isinstance(title, str) or not title.strip() or not isinstance(artist, str):
            raise ValueError('请确认曲名和音乐人')
        title, artist = title.strip(), artist.strip()
        if len(title) > 10000 or len(artist) > 10000: raise ValueError('曲名或音乐人过长')
        with self.lock:
            for task in self.state['tasks'].values():
                if (task['source_id'], task['title'], task['artist']) == (source_id, title, artist) and task['status'] != 'failed':
                    asset = self.state['assets'].get(task.get('asset_id'))
                    if task['status'] != 'ready': return copy.deepcopy(task)
                    if asset and Path(asset['path']).is_file():
                        if music._file_hash(asset['path']) != asset['sha256']:
                            raise ValueError('已登记音乐文件完整性校验失败，请检查该文件')
                        return copy.deepcopy(task)
            if sum(t['status'] in ('queued', 'downloading', 'saving') for t in self.state['tasks'].values()) >= 20:
                raise ValueError('独立音乐下载队列已满，请等待当前下载完成')
            row = self.state['candidates'].get(source_id)
            if row is None and source_id in music.candidates:
                row = {**music.candidates[source_id], 'platform': 'youtube', 'video_id': source_id, 'part': 1}
                self.state['candidates'][source_id] = row
            if row is None: raise ValueError('来源已过期，请重新解析或搜索')
            task_id = uuid4().hex
            task = {'id': task_id, 'source_id': source_id, 'title': title, 'artist': artist,
                    'status': 'queued', 'message': '等待下载', 'created': now(), 'retryable': False}
            self.state['tasks'][task_id] = task
            self.persist()
            pool.submit(self.run, task_id)
            return copy.deepcopy(task)

    def update(self, task_id, **values):
        with self.lock:
            self.state['tasks'][task_id].update(values, updated=now())
            self.persist()

    def download_source(self, row):
        directory = self.registry / 'sources' / row['id']
        directory.mkdir(parents=True, exist_ok=True)
        if row['platform'] == 'youtube':
            video_id = music.valid_id(row['video_id'])
            # Original validated legacy downloads are eligible for cache reuse.
            if video_id in music.tracks and music.tracks[video_id]['status'] == 'ready':
                path, legacy = music.ready_original_audio(video_id)
                if legacy['source_provenance']['direct_original']:
                    return source_audio(path, directory)
            output = music.command('-f', music.YOUTUBE_AUDIO_FORMAT, '--max-filesize', '200M', '--no-progress',
                                   '-o', str(directory / 'source.%(ext)s'), '--print', 'after_move:filepath',
                                   music.url_for(video_id), timeout=240)
            source = Path(output.splitlines()[-1]).resolve() if output else None
            if source is None or source.parent != directory.resolve() or not source.is_file():
                raise ValueError('音乐下载失败或文件超过 200 MB')
            if source.stat().st_size > MAX_BYTES: raise ValueError('音乐文件超过 200 MB')
        else:
            source = directory / 'resource.bin'
            try:
                fetch_resource(row['resource_url'], source)
            except ResourceExpired:
                refreshed = resolve_bili(row['url'], previous=row)
                if refreshed['id'] != row['id']: raise ValueError('重新解析后视频身份发生变化')
                row.update(refreshed)
                # Exactly one refresh; second expiry surfaces to the user.
                fetch_resource(row['resource_url'], source)
        return source_audio(source, directory)

    def save(self, row, title, artist, source, meta):
        with self.lock:
            for asset in self.state['assets'].values():
                if (asset['source_id'], asset['title'], asset['artist']) == (row['id'], title, artist) and Path(asset['path']).is_file():
                    if music._file_hash(asset['path']) != asset['sha256']:
                        raise ValueError('已登记音乐文件完整性校验失败，请检查该文件')
                    return copy.deepcopy(asset)
            temporary = self.registry / ('save-' + uuid4().hex + '.tmp')
            shutil.copy2(source, temporary)
            number = 1
            try:
                while True:
                    name = filename(title, artist, meta['extension'], '' if number == 1 else f'（{number}）')
                    target = self.directory / name
                    try:
                        # Linking a fully written file atomically publishes it, without overwrite.
                        os.link(temporary, target)
                        break
                    except FileExistsError:
                        number += 1
            finally:
                temporary.unlink(missing_ok=True)
            aid = uuid4().hex
            asset = {'id': aid, 'source_id': row['id'], 'platform': row['platform'], 'video_id': row['video_id'],
                     'part': row.get('part', 1), 'title': title, 'artist': artist, 'channel': row.get('channel', ''),
                     'thumbnail': row.get('thumbnail', ''), 'filename': name, 'path': str(target.resolve()),
                     'duration': meta['duration'], 'format': meta['format'], 'codec': meta['codec'],
                     'extension': meta['extension'], 'sha256': music._file_hash(target), 'bytes': target.stat().st_size,
                     'url': row['url'], 'created': now(), 'file_url': f'/api/music-assets/{aid}/file'}
            self.state['assets'][aid] = asset
            self.persist()
            return copy.deepcopy(asset)

    def run(self, task_id):
        task = self.task(task_id)
        try:
            self.update(task_id, status='downloading', message='正在下载源音轨')
            with self.lock:
                row = copy.deepcopy(self.state['candidates'][task['source_id']])
                source_lock = self.source_locks.setdefault(row['id'], threading.Lock())
            # Multiple confirmed filenames share one complete source download.
            with source_lock:
                with self.lock:
                    cached = copy.deepcopy(self.state['sources'].get(row['id']))
                if cached:
                    path = Path(cached['path']).resolve()
                    allowed = path.parent == (self.registry / 'sources' / row['id']).resolve() and path.name.startswith('audio.')
                    if row['platform'] == 'youtube':
                        allowed = allowed or (path.parent == (music.LIBRARY / music.valid_id(row['video_id'])).resolve() and path.name.startswith('source.'))
                    if not allowed:
                        raise ValueError('下载缓存路径无效')
                if cached and Path(cached['path']).is_file() and music._file_hash(cached['path']) == cached['sha256']:
                    source, meta = Path(cached['path']), cached['metadata']
                else:
                    source, meta = self.download_source(row)
                    with self.lock:
                        self.state['sources'][row['id']] = {'path': str(source), 'sha256': music._file_hash(source), 'metadata': meta}
                        self.persist()
            self.update(task_id, status='saving', message='正在保存音乐')
            asset = self.save(row, task['title'], task['artist'], source, meta)
            self.update(task_id, status='ready', message='音乐已保存', asset_id=asset['id'], asset=asset, retryable=False)
        except Exception as exc:
            error = exc if isinstance(exc, music.MusicSourceError) else diagnostic('音乐下载失败：' + str(exc), exc)
            self.update(task_id, status='failed', message=str(error), retryable=True,
                        category=error.category, diagnostic_id=error.diagnostic_id,
                        source_diagnostic_id=getattr(exc, 'diagnostic_id', error.diagnostic_id))

    def migrate_legacy(self):
        report = {'copied': [], 'reused': [], 'failed': []}
        for video_id, track in list(music.tracks.items()):
            if track.get('status') != 'ready': continue
            try:
                source, original = music.ready_original_audio(video_id)
                if not original['source_provenance']['direct_original']:
                    raise ValueError('旧记录缺少下载原始音轨，不复制转码替代品')
                row = {**track, 'id': video_id, 'video_id': video_id, 'platform': 'youtube', 'part': 1,
                       'url': track.get('url') or music.url_for(video_id)}
                existing = len(self.assets())
                directory = self.registry / 'sources' / video_id
                directory.mkdir(parents=True, exist_ok=True)
                meta = probe(source)
                saved = source
                if meta['has_video']:
                    saved, meta = source_audio(source, directory)
                asset = self.save(row, track.get('title') or '未命名音乐', track.get('artist') or '', saved, meta)
                with self.lock:
                    self.state['candidates'][row['id']] = row
                    self.state['sources'][row['id']] = {'path': str(saved), 'sha256': music._file_hash(saved), 'metadata': meta}
                    self.persist()
                report['copied' if len(self.assets()) > existing else 'reused'].append(asset)
            except Exception as exc:
                report['failed'].append({'id': video_id, 'reason': str(exc)})
        return report


store = AssetStore()


def identify(path, original_filename=None):
    return store.identify(path,original_filename)


def migrate_legacy():
    return store.migrate_legacy()


def get_asset(asset_id):
    with store.lock:
        asset = store.state['assets'].get(asset_id)
        if asset is None: raise ValueError('音乐素材不存在')
        return copy.deepcopy(asset)


def asset_path(asset_id):
    row = get_asset(asset_id)
    path = Path(row['path']).resolve()
    if path.parent != store.directory.resolve() or not path.is_file():
        raise ValueError('登记音轨文件不存在或路径无效')
    if music._file_hash(path) != row['sha256']:
        raise ValueError('登记音轨完整性校验失败')
    return path


def asset_cover(asset_id):
    row = get_asset(asset_id)
    url = row.get('thumbnail', '')
    if urlparse(url).scheme not in ('http', 'https'): return None
    directory = store.registry / 'covers'
    directory.mkdir(exist_ok=True)
    path = directory / (asset_id + '.jpg')
    if path.is_file(): return path
    try:
        with urlopen(Request(url, headers={'User-Agent': 'MalodyStudio/1.0'}), timeout=10) as response:
            data = response.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024: return None
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as cover:
            if cover.width * cover.height > 25000000: return None
            cover.convert('RGB').save(path, 'JPEG', quality=90)
        return path
    except Exception:
        return None
