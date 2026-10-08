import json
from pathlib import Path

import pytest

from malody_studio import advanced


def test_transient_windows_read_denial_returns_the_complete_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'project.json'
    path.write_text(json.dumps({'revision':54,'events':[{'id':'original'}]}), encoding='utf-8')
    read_text = Path.read_text
    attempts = []; pauses = []

    def temporarily_denied(target, *args, **kwargs):
        attempts.append(target)
        if len(attempts) < 3:
            raise PermissionError(13, 'Permission denied', str(target))
        return read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', temporarily_denied)
    monkeypatch.setattr(advanced.time, 'sleep', pauses.append)
    assert advanced.read(path) == {'revision':54,'events':[{'id':'original'}]}
    assert len(attempts) == 3 and len(pauses) == 2


def test_persistent_read_denial_and_invalid_json_remain_errors(tmp_path, monkeypatch):
    path = tmp_path / 'project.json'
    denied = PermissionError(13, 'Permission denied', str(path))
    attempts = []
    def permanently_denied(*args, **kwargs):
        attempts.append(1)
        raise denied
    monkeypatch.setattr(Path, 'read_text', permanently_denied)
    monkeypatch.setattr(advanced.time, 'sleep', lambda _:None)
    with pytest.raises(PermissionError) as caught:
        advanced.read(path)
    assert caught.value is denied and len(attempts) == 10
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k:'{broken')
    with pytest.raises(json.JSONDecodeError):
        advanced.read(path)
