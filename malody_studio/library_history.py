"""Small, file-versioned metadata for the package list, never chart event data.

Reports can contain tens of MB of diagnostics. Keep only list fields in memory
and a disposable checkpoint so changing pages or restarting does not parse all
of those reports again. Source stats and delivery existence are checked on each
request; publication/deletion do not wait for a cache expiry.
"""
from collections import OrderedDict
import copy
import json
from pathlib import Path
import threading
import uuid


SCHEMA = 1
LIMIT = 1024


def _signature(path):
    info = path.stat()
    return [info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino]


def _summary(data, kind):
    if not isinstance(data, dict):
        raise ValueError('Invalid library metadata')
    if kind == 'report':
        charts = data.get('charts', [])
        if not isinstance(charts, list) or any(not isinstance(row, dict) for row in charts):
            raise ValueError('Invalid library chart metadata')
        result = {key: data[key] for key in ('title', 'artist', 'created') if key in data}
        result['charts'] = [{key: row[key] for key in ('engine', 'difficulty') if key in row} for row in charts]
        return result
    if kind != 'project':
        raise ValueError('Unknown library metadata kind')
    assemblies = data.get('assemblies', [])
    if not isinstance(assemblies, list):
        raise ValueError('Invalid library assemblies')
    result = {key: data[key] for key in ('id', 'title', 'artist', 'background') if key in data}
    result['assemblies'] = []
    for row in assemblies:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in ('id', 'title', 'artist', 'created', 'duration', 'download') if key in row}
        item['segment_count'] = len(row.get('mapping', []))
        result['assemblies'].append(item)
    return result


class SummaryCache:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.rows = OrderedDict()
        self.dirty = False
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
            if document.get('schema') == SCHEMA and isinstance(document.get('rows'), dict):
                for key, row in list(document['rows'].items())[-LIMIT:]:
                    if (isinstance(row, dict) and isinstance(row.get('signature'), list)
                            and isinstance(row.get('summary'), dict)):
                        self.rows[key] = row
        except (OSError, ValueError, AttributeError):
            pass  # A checkpoint is optional; originals remain authoritative.

    def get(self, path, kind, reader):
        path = Path(path).absolute()
        key = kind + ':' + str(path)
        with self.lock:
            signature = _signature(path)
            cached = self.rows.get(key)
            if cached is not None and cached['signature'] == signature:
                self.rows.move_to_end(key)
                return copy.deepcopy(cached['summary'])
            # A project/report may be atomically replaced during export. Never
            # attach data from one file version to the stat of another version.
            for _ in range(2):
                summary = _summary(reader(path), kind)
                after = _signature(path)
                if after == signature:
                    break
                signature = after
            else:
                raise OSError('Library metadata changed while reading')
            self.rows[key] = {'signature': signature, 'summary': summary}
            self.rows.move_to_end(key)
            while len(self.rows) > LIMIT:
                self.rows.popitem(last=False)
            self.dirty = True
            return copy.deepcopy(summary)

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps({'schema': SCHEMA, 'rows': self.rows}, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.path)
                self.dirty = False
            except OSError:
                pass  # Read-only storage must not prevent browsing packages.
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


_lock = threading.Lock()
_caches = OrderedDict()


def summary_cache(root):
    path = Path(root).absolute() / 'runtime' / 'library-history-summaries.json'
    with _lock:
        if path not in _caches:
            _caches[path] = SummaryCache(path)
        _caches.move_to_end(path)
        while len(_caches) > 8:
            _caches.popitem(last=False)
        return _caches[path]
