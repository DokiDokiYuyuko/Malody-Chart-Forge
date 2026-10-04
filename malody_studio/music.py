"""Public YouTube search and local audio library, using the deployed yt-dlp."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import hashlib
import json
import re
import subprocess
import threading
from .paths import ROOT

LIBRARY = ROOT / 'uploads' / 'library'
LIBRARY.mkdir(exist_ok=True)
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='music-download')
lock = threading.Lock()
tracks = {}
candidates = {}
YOUTUBE_AUDIO_FORMAT = 'bestaudio'
for state in LIBRARY.glob('*/track.json'):
    try:
        track = json.loads(state.read_text(encoding='utf-8'))
        if track['status'] in ('downloading', 'converting'):
            track.update(status='failed', message='下载被服务重启中断，请重试')
        if track['status'] == 'ready' and not (state.parent / 'audio.ogg').is_file():
            track.update(status='failed', message='音乐文件缺失，请重新下载')
        tracks[track['id']] = track
    except (ValueError, KeyError):
        pass

def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('音乐编号无效，请重新搜索')
    return value

def url_for(video_id):
    return 'https://www.youtube.com/watch?v=' + valid_id(video_id)

def command(*args, timeout=75):
    exe = ROOT / 'runtime' / 'bin' / 'yt-dlp.exe'
    if not exe.is_file():
        raise ValueError('音乐下载组件缺失')
    try:
        result = subprocess.run([str(exe), '--ignore-config', '--no-cache-dir', '--no-playlist',
                                 '--socket-timeout', '15', '--retries', '1', '--fragment-retries', '1',
                                 *args], capture_output=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('音乐源响应超时，请稍后重试')
    if result.returncode:
        error = result.stderr
        if 'Sign in' in error or 'not available' in error or 'Private video' in error:
            raise ValueError('该音乐需要登录、限制访问或已下架，请换一个公开音源')
        raise ValueError('暂时无法访问音乐源，请检查网络连接或换一个结果')
    return result.stdout.strip()

def summarize(entry):
    video_id = valid_id(entry.get('id'))
    return {'id': video_id, 'title': str(entry.get('title') or '未命名音乐')[:120],
            'artist': str(entry.get('artist') or entry.get('channel') or entry.get('uploader') or '')[:120],
            'channel': str(entry.get('channel') or entry.get('uploader') or '')[:120],
            'duration': entry.get('duration'), 'url': url_for(video_id),
            'thumbnail': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
            'channel_verified': bool(entry.get('channel_is_verified'))}

def search_page(query, cursor=0, limit=20, min_duration=5, max_duration=600):
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError('请填写曲名、音乐人或 YouTube 单曲链接（最多 200 字）')
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or cursor > 500:
        raise ValueError('搜索页码无效，请重新搜索')
    if not 1 <= limit <= 20 or not 5 <= min_duration <= 600 or not 5 <= max_duration <= 600 or min_duration > max_duration:
        raise ValueError('时长筛选范围无效（5 秒至 10 分钟）')
    if query.startswith(('http://', 'https://')):
        url = urlparse(query)
        if url.scheme != 'https' or url.hostname not in ('youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'):
            raise ValueError('目前支持曲名搜索或 YouTube 单曲链接')
        video_id = url.path.strip('/') if url.hostname == 'youtu.be' else parse_qs(url.query).get('v', [''])[0]
        if url.path.startswith('/shorts/'):
            video_id = url.path.split('/')[2]
        target = url_for(video_id)
        data = json.loads(command('--skip-download', '--dump-single-json', target))
        entries = [data]
    else:
        end = cursor + limit
        data = json.loads(command('--flat-playlist', '--skip-download', '--dump-single-json',
                                  '--playlist-start', str(cursor + 1), '--playlist-end', str(end),
                                  f'ytsearch{end}:' + query))
        entries = data.get('entries', [])
    results, seen = [], set()
    filtered = {'duration': 0, 'live': 0, 'invalid': 0, 'duplicate': 0}
    for entry in entries:
        if not entry:
            continue
        if entry.get('live_status') in ('is_live', 'is_upcoming') or entry.get('is_live'):
            filtered['live'] += 1
            continue
        duration = entry.get('duration')
        if duration is None or not min_duration <= duration <= max_duration:
            filtered['duration'] += 1
            continue
        try:
            item = summarize(entry)
        except ValueError:
            filtered['invalid'] += 1
            continue
        if item['id'] in seen:
            filtered['duplicate'] += 1
            continue
        seen.add(item['id'])
        results.append(item)
    with lock:
        candidates.update({item['id']: item for item in results})
        if len(candidates) > 400:
            for key in list(candidates)[:len(candidates) - 400]:
                candidates.pop(key)
    return {'results': results, 'source': 'YouTube',
            'next_cursor': cursor + limit if len(entries) >= limit and not query.startswith(('http://', 'https://')) else None,
            'has_more': len(entries) >= limit and not query.startswith(('http://', 'https://')),
            'cursor': cursor, 'page_size': limit, 'scanned': len(entries), 'filtered': filtered,
            'duration_range': {'min': min_duration, 'max': max_duration}}

def search(query):
    """Compatibility helper for callers that only need the first result page."""
    return search_page(query)['results']

def update(video_id, **changes):
    with lock:
        tracks[video_id].update(changes)
        path = LIBRARY / video_id / 'track.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(tracks[video_id], ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

def download(video_id):
    directory = LIBRARY / video_id
    try:
        info = json.loads(command('--skip-download', '--dump-single-json', url_for(video_id)))
        duration = info.get('duration')
        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming') or duration is None or not 5 <= duration <= 600:
            raise ValueError('请选择 5 秒至 10 分钟的非直播音乐')
        item = summarize(info)
        update(video_id, **{k: v for k, v in item.items() if k != 'id'}, message='下载音乐中…')
        # Do not prefer M4A by extension: yt-dlp's bestaudio selector chooses
        # its best available audio-only stream, which may be Opus/WebM.
        result = command('-f', YOUTUBE_AUDIO_FORMAT, '--max-filesize', '200M', '--no-progress',
                         '-o', str(directory / 'source.%(ext)s'), '--print', 'after_move:filepath',
                         url_for(video_id), timeout=240)
        source = Path(result.splitlines()[-1]).resolve() if result else None
        if source is None or source.parent != directory.resolve() or not source.is_file():
            raise ValueError('音乐未下载成功，文件可能超过 200 MB')
        if source.stat().st_size > 200 * 1024 * 1024:
            raise ValueError('音乐文件超过 200 MB')
        update(video_id, status='converting', message='转换音乐并检查时间轴…',
               source_original={'file':source.name,'bytes':source.stat().st_size,'sha256':_file_hash(source),
                                'format_selector':YOUTUBE_AUDIO_FORMAT})
        from .audio import convert
        _, _, seconds, _ = convert(source, directory)
        update(video_id, status='ready', message='音乐已导入，可以生成谱面', duration=seconds,
               preview=f'/api/music/{video_id}/audio', file=f'/api/music/{video_id}/download')
    except Exception as exc:
        update(video_id, status='failed', message=str(exc))
        (ROOT / 'logs' / f'music-{video_id}.log').write_text(str(exc), encoding='utf-8')

def begin(video_id):
    valid_id(video_id)
    with lock:
        if video_id in tracks and tracks[video_id]['status'] != 'failed':
            return dict(tracks[video_id])
        if video_id not in candidates and video_id not in tracks:
            raise ValueError('搜索结果已过期，请重新搜索后选择')
        if sum(t['status'] in ('downloading', 'converting') for t in tracks.values()) >= 3:
            raise ValueError('音乐下载队列已满，请等待当前下载完成')
        directory = LIBRARY / video_id
        directory.mkdir(exist_ok=True)
        tracks[video_id] = {**candidates.get(video_id, tracks.get(video_id, {})),
                            'id': video_id, 'status': 'downloading', 'message': '正在获取音源…'}
    update(video_id)
    pool.submit(download, video_id)
    return dict(tracks[video_id])

def get(video_id):
    valid_id(video_id)
    with lock:
        if video_id not in tracks:
            raise ValueError('音乐不存在，请先搜索并下载')
        return dict(tracks[video_id])

def ready_audio(video_id):
    track = get(video_id)
    if track['status'] != 'ready':
        raise ValueError('音乐尚未下载完成')
    return LIBRARY / video_id / 'audio.ogg', track


def _file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def ready_original_audio(video_id):
    """Advanced imports decode the direct download, with explicit legacy fallback."""
    track=get(video_id)
    if track['status'] != 'ready':raise ValueError('音乐尚未下载完成')
    directory=(LIBRARY/valid_id(video_id)).resolve()
    extensions=('.webm','.m4a','.opus','.mp3','.flac','.wav','.ogg','.aac','.mp4')
    record=track.get('source_original')
    source=None
    if record is not None:
        if not isinstance(record,dict) or not isinstance(record.get('file'),str):raise ValueError('下载原始音源记录无效')
        candidate=(directory/record['file']).resolve()
        if candidate.parent != directory or candidate.name != record['file'] or candidate.suffix.lower() not in extensions or not candidate.name.lower().startswith('source.'):
            raise ValueError('下载原始音源路径无效')
        if candidate.is_file():
            if candidate.stat().st_size != record.get('bytes') or _file_hash(candidate) != record.get('sha256'):
                raise ValueError('下载原始音源完整性校验失败，请重新下载')
            source=candidate
    if source is None:
        # Older downloads did not record the selected filepath. Only exact
        # source.<audio extension> files are eligible; incomplete files are not.
        for suffix in extensions:
            candidate=(directory/('source'+suffix)).resolve()
            if candidate.parent == directory and candidate.is_file():
                source=candidate;break
    direct=source is not None
    if source is None:
        source=(directory/'audio.ogg').resolve()
        if source.parent != directory or not source.is_file():raise ValueError('音乐文件缺失，请重新下载')
    if source.stat().st_size < 1:raise ValueError('音乐文件为空，请重新下载')
    track['source_provenance']={'kind':'downloaded_original' if direct else 'converted_fallback',
                                'direct_original':direct,'filename':source.name,'sha256':_file_hash(source),
                                'bytes':source.stat().st_size,'url':track.get('url',url_for(video_id)),
                                'format_selector':record.get('format_selector',YOUTUBE_AUDIO_FORMAT) if isinstance(record,dict) else YOUTUBE_AUDIO_FORMAT}
    return source,track
