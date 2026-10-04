"""Server-owned undo for segment edits, retaining immutable chart files."""
import copy
from .advanced import atomic, now, read

LIMIT = 20


def _path(store, pid):
    return store.directory(pid) / 'segment-edits.json'


def _history(store, pid):
    path = _path(store, pid)
    data = read(path) if path.is_file() else {'schema': 1, 'entries': []}
    if data.get('schema') != 1 or not isinstance(data.get('entries'), list):
        raise ValueError('片段撤销记录不可用；当前音乐和谱面没有被修改')
    return data


def status(store, pid):
    with store.lock:
        p = store.load(pid)
        entries = _history(store, pid)['entries']
        last = entries[-1] if entries else None
        matching = bool(last and last['after'] == p['segments'])
        return {'can_undo': matching, 'label': last['label'] if last else '',
                'count': len(entries), 'project_revision': p['revision'],
                'reason': '' if matching or not last else '片段版本已变化；为保留新结果，不能撤销这项较早的操作'}


def perform(store, pid, operation, label):
    """Record only successful edits; never trust a client-supplied snapshot."""
    with store.lock:
        before = copy.deepcopy(store.load(pid)['segments'])
        history = _history(store, pid)
        result = operation()
        if before != result['segments']:
            history['entries'].append({'label': label, 'created': now(),
                                       'before': before, 'after': copy.deepcopy(result['segments'])})
            history['entries'] = history['entries'][-LIMIT:]
            atomic(_path(store, pid), history)
        return result


def undo(store, pid, expected=None):
    with store.lock:
        p = store.load(pid)
        store.check(p, expected)
        history = _history(store, pid)
        if not history['entries']:
            raise ValueError('没有可以撤销的片段操作')
        entry = history['entries'][-1]
        if entry['after'] != p['segments']:
            raise ValueError('片段版本已变化；为保留新结果，不能撤销这项较早的操作')
        # Only the segment list is restored: settings, tempo, reviews, source
        # audio and all revision files remain exactly where they are.
        p['segments'] = copy.deepcopy(entry['before'])
        result = store.save(p)
        history['entries'].pop()
        atomic(_path(store, pid), history)
        return result
