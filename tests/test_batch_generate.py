import copy
import hashlib
import json

import httpx
import pytest

from tools.batch_generate import BatchRunner, atomic, canonical, load_manifest, writer_lock


def manifest():
    return {"schema": 1, "jobs": [{"key": "recording-1", "project_id": "a" * 32,
            "payload": {"request_id": "b" * 32, "expected_revision": 1, "segment_ids": ["c" * 32]}}]}


def client_for(handler):
    return httpx.Client(base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(handler))


def test_serial_submissions_wait_and_completed_resume_never_posts(tmp_path):
    frozen = manifest()
    second = copy.deepcopy(frozen["jobs"][0])
    second["key"] = "recording-2"
    second["payload"]["request_id"] = "d" * 32
    frozen["jobs"].append(second)
    calls = []
    state_path = tmp_path / "state.json"
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"api_version": 2})
        if request.method == "POST":
            state = json.loads(state_path.read_text())
            assert sum(row["status"] == "posting" for row in state["jobs"].values()) == 1
            assert not any(row["status"] == "running" for row in state["jobs"].values())
            return httpx.Response(200, json={"id": "e" * 32, "request_id": json.loads(request.content)["request_id"]})
        return httpx.Response(200, json={"status": "completed", "results": []})
    with client_for(handler) as client:
        assert BatchRunner(frozen, state_path, client).run()
        assert BatchRunner(frozen, state_path, client).run()
    assert [m for m, p in calls if p != "/api/health"] == ["POST", "GET", "POST", "GET"]


def test_partial_stops_batch_and_is_not_resubmitted(tmp_path):
    frozen = manifest()
    second = copy.deepcopy(frozen["jobs"][0]); second["key"] = "second"
    frozen["jobs"].append(second)
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": "e" * 32, "request_id": "b" * 32})
        return httpx.Response(200, json={} if request.url.path == "/api/health" else {"status": "partial"})
    with client_for(handler) as client:
        runner = BatchRunner(frozen, tmp_path / "state.json", client)
        assert runner.run() is False
        assert runner.run() is False
    assert len(posts) == 1 and "second" not in runner.state["jobs"]


def test_lost_post_response_recovers_local_receipt_without_second_post(tmp_path):
    frozen = manifest(); state_path = tmp_path / "state.json"
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
            atomic(tmp_path / "outputs/advanced" / ("a" * 32) / "batches" / ("b" * 32 + ".json"),
                   {"id": "e" * 32, "request_id": "b" * 32,
                    "request_hash": hashlib.sha256(canonical(frozen["jobs"][0]["payload"])).hexdigest()})
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={} if request.url.path == "/api/health" else {"status": "completed"})
    with client_for(handler) as client:
        with pytest.raises(httpx.ReadTimeout):
            BatchRunner(frozen, state_path, client, tmp_path).run()
        assert BatchRunner(frozen, state_path, client, tmp_path).run()
    assert len(posts) == 1


def test_uncertain_request_without_receipt_never_reposts(tmp_path):
    frozen = manifest()
    with client_for(lambda _: httpx.Response(200, json={})) as client:
        runner = BatchRunner(frozen, tmp_path / "state.json", client, tmp_path)
        runner.state["jobs"]["recording-1"] = {"status": "uncertain"}
        runner.save()
        with pytest.raises(RuntimeError, match="not resubmitting"):
            runner.run()


def test_changed_manifest_cannot_reuse_state(tmp_path):
    frozen = manifest()
    with client_for(lambda _: httpx.Response(200, json={})) as client:
        runner = BatchRunner(frozen, tmp_path / "state.json", client); runner.save()
        frozen["jobs"][0]["payload"]["settings"] = {"seed": 4}
        with pytest.raises(ValueError, match="Manifest changed"):
            BatchRunner(frozen, runner.path, client)


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(service_url="https://example.com"),
    lambda m: m["jobs"].append(copy.deepcopy(m["jobs"][0])),
    lambda m: m["jobs"][0]["payload"].pop("expected_revision"),
    lambda m: m["jobs"][0]["payload"].update(segment_ids=["../source"]),
])
def test_invalid_manifest_fails_before_any_submission(tmp_path, mutation):
    frozen = manifest(); mutation(frozen)
    path = tmp_path / "batch.json"; atomic(path, frozen)
    with pytest.raises(ValueError):
        load_manifest(path)


def test_second_controller_cannot_acquire_state_lock(tmp_path):
    with writer_lock(tmp_path / "state.lock"):
        with pytest.raises(RuntimeError, match="Another controller"):
            with writer_lock(tmp_path / "state.lock"):
                pass
