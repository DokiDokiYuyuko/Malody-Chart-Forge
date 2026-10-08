"""Public YouTube search and local audio library, using the deployed yt-dlp."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4
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


class MusicSourceError(ValueError):
    """User-safe failure with the original subprocess evidence kept separately."""
    def __init__(self, message, category, diagnostic_id):
        super().__init__(message)
        self.category = category
        self.diagnostic_id = diagnostic_id


def _failure_category(stderr):
    fatal = [line for line in stderr.splitlines() if re.match(r'^\s*ERROR(?:\s|:)', line)]
    error = ('\n'.join(fatal) if fatal else stderr).lower()
    if any(value in error for value in ('could not copy chrome cookie database',
                                       'could not copy edge cookie database',
                                       'database is locked', 'database is busy')):
        return 'cookie_locked', '浏览器占用登录凭据文件，请保存工作并完全退出对应浏览器后重试'
    if any(value in error for value in ('dpapi', 'failed to decrypt cookie',
                                       'could not decrypt cookie', 'unable to decrypt cookie')):
        return 'local_cookie_decryption', 'Windows 无法解密浏览器登录凭据，可导出 YouTube 的 Netscape 格式 cookies 文件到项目目录，并在音乐源配置中设置 cookies_file 后重试'
    if 'cookie database' in error or 'cookies database' in error:
        return 'local_cookie', '本机浏览器 Cookie 数据库读取失败，请检查当前 Windows 用户及浏览器登录状态'
    if re.search(r'sign\s+in\s+to\s+confirm.*not\s+a\s+bot', error):
        return 'bot_verification', 'YouTube 要求验证您不是机器人，请在已登录的浏览器中完成验证，并显式配置该浏览器的 Cookie 后重试'
    if any(value in error for value in ('winerror 10013', 'permission denied', 'access is denied')):
        return 'local_permission', '音乐读取组件被本机权限阻止，请检查应用权限后重试'
    if 'requested format is not available' in error or 'no video formats found' in error:
        return 'format', '音乐源没有提供所需音频格式，错误详情已记录'
    if 'sign in' in error and not any(value in error for value in (
            'private video', 'video has been removed', 'copyright', 'not available in your country')):
        return 'authentication_required', 'YouTube 要求登录后读取该音源，请配置浏览器登录凭据或本地 cookies 文件后重试'
    if any(value in error for value in ('sign in', 'private video', 'video unavailable',
                                       'this video is not available', 'not available in your country',
                                       'video has been removed', 'copyright')):
        return 'source_restricted', '该音乐需要登录、限制访问或已下架，请换一个公开音源'
    if any(value in error for value in ('timed out', 'timeout')):
        return 'timeout', '音乐源响应超时，请稍后重试'
    if any(value in error for value in ('unable to extract', 'extractorerror', 'unsupported url',
                                       'no supported javascript runtime', 'javascript challenge')):
        return 'extractor', '音乐信息提取组件无法解析该结果，错误详情已记录'
    if any(value in error for value in ('name or service not known', 'getaddrinfo failed',
                                       'temporary failure in name resolution', 'connection refused',
                                       'connection reset', 'network is unreachable',
                                       'certificate_verify_failed', 'failed to establish a new connection')):
        return 'network', '连接音乐源失败，请稍后重试，错误详情已记录'
    return 'unknown', '获取音乐源信息失败，错误详情已记录，请稍后重试'


def _command_failure(args, *, category, message, stderr='', stdout='', exit_code=None,
                     exception=None):
    diagnostic_id = uuid4().hex
    def text(value):
        return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or '')
    phase = 'search' if '--flat-playlist' in args else 'metadata' if '--dump-single-json' in args else 'download'
    record = {'schema': 1, 'id': diagnostic_id, 'created': datetime.now(timezone.utc).isoformat(),
              'stage': phase, 'category': category, 'arguments': list(args),
              'exit_code': exit_code, 'stderr': text(stderr), 'stdout': text(stdout),
              'exception_type': type(exception).__name__ if exception is not None else None,
              'exception': str(exception) if exception is not None else None}
    try:
        directory = ROOT / 'logs' / 'music-source-errors'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (diagnostic_id + '.json')).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        # Diagnostic storage must not replace the actual subprocess failure.
        pass
    return MusicSourceError(message, category, diagnostic_id)


def _source_options(args, *, credentials=False):
    options = []
    node = shutil.which('node')
    if node is None:
        bundled = ROOT / 'runtime' / 'bin' / 'node.exe'
        if bundled.is_file():
            node = str(bundled)
    if node is not None:
        options.extend(['--js-runtimes', 'node:' + str(Path(node).resolve())])
    if not credentials:
        return options
    settings_path = ROOT / 'runtime' / 'music-source-settings.json'
    try:
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return options
    except (OSError, ValueError):
        raise _command_failure(args, category='local_config',
                               message='音乐源配置 runtime/music-source-settings.json 无法读取或不是有效的 UTF-8 JSON 对象') from None
    if (not isinstance(settings, dict) or set(settings) - {'cookies_from_browser', 'cookies_file'}
            or ('cookies_from_browser' in settings
                and settings['cookies_from_browser'] not in ('edge', 'chrome', 'firefox'))):
        raise _command_failure(args, category='local_config',
                               message='音乐源配置必须为 JSON 对象，仅支持 cookies_file 和 cookies_from_browser；浏览器值为 edge、chrome 或 firefox（不支持 profile）')
    if 'cookies_file' in settings and 'cookies_from_browser' in settings:
        raise _command_failure(args, category='local_config',
                               message='音乐源配置 cookies_file 与 cookies_from_browser 互斥，请只保留其中一个')
    if 'cookies_file' in settings:
        value = settings['cookies_file']
        if not isinstance(value, str) or not value.strip():
            raise _command_failure(args, category='local_config',
                                   message='音乐源配置 cookies_file 必须为项目目录内的 .txt 文件路径字符串')
        try:
            cookie_file = (ROOT / value).resolve()
            cookie_file.relative_to(ROOT.resolve())
            if cookie_file.suffix.lower() != '.txt' or not cookie_file.is_file():
                raise ValueError('invalid cookie file')
            with cookie_file.open(encoding='utf-8-sig') as stream:
                header = stream.readline(4096).rstrip('\r\n')
            if header not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
                raise ValueError('invalid Netscape header')
        except (OSError, ValueError, RuntimeError):
            raise _command_failure(args, category='local_config',
                                   message='cookies_file 必须是项目目录内存在且可读取的 .txt 文件，首行须为 Netscape Cookie 格式标识；请检查路径、文件和格式') from None
        options.extend(['--cookies', str(cookie_file)])
    if 'cookies_from_browser' in settings:
        options.extend(['--cookies-from-browser', settings['cookies_from_browser']])
    return options


def _run_source(args, options, timeout):
    exe = ROOT / 'runtime' / 'bin' / 'yt-dlp.exe'
    if not exe.is_file():
        raise _command_failure(args, category='component_missing', message='音乐下载组件缺失')
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        result = subprocess.run([str(exe), '--ignore-config', '--no-cache-dir', '--no-playlist',
                                 '--socket-timeout', '15', '--retries', '1', '--fragment-retries', '1',
                                 *options, *args], capture_output=True, encoding='utf-8', errors='replace',
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise _command_failure(args, category='timeout', message='音乐源响应超时，请稍后重试',
                               stderr=exc.stderr, stdout=exc.stdout, exception=exc) from exc
    except FileNotFoundError as exc:
        raise _command_failure(args, category='component_missing', message='音乐下载组件缺失', exception=exc) from exc
    except PermissionError as exc:
        raise _command_failure(args, category='local_permission', message='音乐读取组件被本机权限阻止，请检查应用权限后重试', exception=exc) from exc
    except OSError as exc:
        raise _command_failure(args, category='local_startup', message='音乐读取组件启动失败，错误详情已记录', exception=exc) from exc
    if result.returncode:
        category, message = _failure_category(result.stderr)
        raise _command_failure(args, category=category, message=message,
                               stderr=result.stderr, stdout=result.stdout, exit_code=result.returncode)
    return result.stdout.strip()


def command(*args, timeout=75):
    # Public results should remain available even if the selected browser is
    # open, its encrypted credentials are unreadable, or a local config is bad.
    started = time.monotonic()
    try:
        return _run_source(args, _source_options(args), timeout)
    except MusicSourceError as initial:
        if initial.category not in ('bot_verification', 'authentication_required'):
            raise
        options = _source_options(args, credentials=True)
        if not any(flag in options for flag in ('--cookies', '--cookies-from-browser')):
            raise
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise _command_failure(args, category='timeout',
                                   message='音乐源响应超时，请稍后重试') from initial
        # One authenticated retry shares the original request's time budget.
        return _run_source(args, options, remaining)

def summarize(entry):
    video_id = valid_id(entry.get('id'))
    return {'id': video_id, 'title': str(entry.get('title') or '未命名音乐')[:120],
            'artist': str(entry.get('artist') or entry.get('channel') or entry.get('uploader') or '')[:120],
            'channel': str(entry.get('channel') or entry.get('uploader') or '')[:120],
            'duration': entry.get('duration'), 'url': url_for(video_id),
            'thumbnail': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
            'channel_verified': bool(entry.get('channel_is_verified'))}

def search_page(query, cursor=0, limit=20, min_duration=5, max_duration=600, exclude_ids=()):
    query = query.strip()
    if not query or len(query) > 200:
        raise ValueError('请填写曲名、音乐人或 YouTube 单曲链接（最多 200 字）')
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or cursor > 10000:
        raise ValueError('搜索页码无效，请重新搜索')
    if not 1 <= limit <= 24 or not 5 <= min_duration <= 600 or not 5 <= max_duration <= 600 or min_duration > max_duration:
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
        scan_limit = min(96 if limit == 24 else limit, 10000 - cursor)
        if not scan_limit:
            return {'results': [], 'source': 'YouTube', 'next_cursor': None, 'has_more': False,
                    'cursor': cursor, 'page_size': limit, 'scanned': 0,
                    'filtered': {'duration': 0, 'live': 0, 'invalid': 0, 'duplicate': 0},
                    'duration_range': {'min': min_duration, 'max': max_duration}}
        end = cursor + scan_limit
        data = json.loads(command('--flat-playlist', '--skip-download', '--dump-single-json',
                                  '--playlist-start', str(cursor + 1), '--playlist-end', str(end),
                                  f'ytsearch{end}:' + query))
        entries = data.get('entries', [])
    results, seen = [], set(exclude_ids)
    filtered = {'duration': 0, 'live': 0, 'invalid': 0, 'duplicate': 0}
    scanned = 0
    for entry in entries[:96]:
        scanned += 1
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
        if len(results) >= limit:
            break
    with lock:
        candidates.update({item['id']: item for item in results})
        if len(candidates) > 400:
            for key in list(candidates)[:len(candidates) - 400]:
                candidates.pop(key)
    return {'results': results, 'source': 'YouTube',
            'next_cursor': cursor + scanned if cursor + scanned < 10000 and (scanned < len(entries) or len(entries) >= (96 if limit == 24 else limit)) and not query.startswith(('http://', 'https://')) else None,
            'has_more': cursor + scanned < 10000 and (scanned < len(entries) or len(entries) >= (96 if limit == 24 else limit)) and not query.startswith(('http://', 'https://')),
            'cursor': cursor, 'page_size': limit, 'scanned': scanned, 'filtered': filtered,
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
