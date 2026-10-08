"""Verify asset integrity and retry behavior without network or model inference."""
import hashlib
import io

import pytest

from tools import setup_roformer as setup


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status


def test_local_weights_must_match_official_hash_before_copy(tmp_path):
    source = tmp_path / "download.ckpt"
    source.write_bytes(b"wrong checkpoint")
    destination = tmp_path / "installed.ckpt"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        setup.copy_checked(source, destination, digest(b"official checkpoint"))
    assert not destination.exists()


def test_existing_asset_is_checked_instead_of_trusted(tmp_path, monkeypatch):
    destination = tmp_path / "installed.ckpt"
    destination.write_bytes(b"corrupt")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: pytest.fail("No network expected"))
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        setup.fetch("https://example.invalid/weight", destination, digest(b"official"))


def test_resume_partial_asset_and_check_complete_hash(tmp_path, monkeypatch):
    data = b"the official checkpoint bytes"
    destination = tmp_path / "installed.ckpt"
    destination.with_name(destination.name + ".part").write_bytes(data[:5])

    def resume(request, timeout):
        assert request.get_header("Range") == "bytes=5-"
        return Response(data[5:], 206, {"Content-Range": "bytes 5-28/29"})

    monkeypatch.setattr(setup.urllib.request, "urlopen", resume)
    setup.fetch("https://example.invalid/weight", destination, digest(data))
    assert destination.read_bytes() == data
    assert not destination.with_name(destination.name + ".part").exists()


def test_server_ignoring_range_restarts_download(tmp_path, monkeypatch):
    data = b"complete asset"
    destination = tmp_path / "weight.ckpt"
    destination.with_name(destination.name + ".part").write_bytes(b"old partial")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: Response(data))
    setup.fetch("https://example.invalid/weight", destination, digest(data))
    assert destination.read_bytes() == data


def test_hash_mismatch_is_discarded_and_retried(tmp_path, monkeypatch):
    data = b"official bytes"
    responses = iter([Response(b"wrong bytes"), Response(data)])
    destination = tmp_path / "weight.ckpt"
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **k: next(responses))
    monkeypatch.setattr(setup.time, "sleep", lambda *_: None)
    setup.fetch("https://example.invalid/weight", destination, digest(data), retries=2)
    assert destination.read_bytes() == data


def test_vendored_architecture_and_config_match_pinned_sources():
    for name, expected in setup.UPSTREAM_SHA256.items():
        assert setup.sha(setup.VENDOR / name) == expected, name


def test_code_record_paths_are_project_relative_and_exclude_bytecode():
    records, code_hash = setup.code_records()
    assert "vendor/RoFormer/models/bs_roformer/mel_band_roformer.py" in records
    assert "tools/setup_roformer.py" in records
    assert all("__pycache__" not in name for name in records)
    assert code_hash == digest(__import__("json").dumps(records, sort_keys=True, separators=(",", ":")).encode())


def test_registry_display_changes_do_not_invalidate_inference_but_validation_changes_do(tmp_path):
    from malody_studio.deployment_integrity import registry_inference_hash
    source = (setup.ROOT / 'malody_studio' / 'separation_models.py').read_text(encoding='utf-8')
    path = tmp_path / 'registry.py'
    path.write_text(source, encoding='utf-8')
    expected = registry_inference_hash(path)
    path.write_text(source.replace("label='Kim MelBand RoFormer'", "label='New display label'"), encoding='utf-8')
    assert registry_inference_hash(path) == expected
    path.write_text(source.replace("'overlap_count': 4", "'overlap_count': 2"), encoding='utf-8')
    assert registry_inference_hash(path) != expected
