"""Submit frozen Advanced requests serially; resume accepted batches without reruns."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value) + b"\n")
    temporary.replace(path)


def load_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("jobs"), list) or not manifest["jobs"]:
        raise ValueError("Expected schema=1 and a nonempty jobs list")
    url = urlparse(manifest.get("service_url", "http://127.0.0.1:8765"))
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("service_url must point to the local HTTP service")
    keys, requests = set(), set()
    for job in manifest["jobs"]:
        key, pid, payload = job.get("key"), job.get("project_id"), job.get("payload")
        if not isinstance(key, str) or not key.strip() or key in keys:
            raise ValueError("Each job needs a unique nonempty key")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{32}", pid):
            raise ValueError(f"Invalid project_id: {key}")
        if not isinstance(payload, dict) or any(k.startswith("_") for k in payload):
            raise ValueError(f"Invalid public generation payload: {key}")
        rid, segments = payload.get("request_id"), payload.get("segment_ids")
        if not isinstance(rid, str) or not re.fullmatch(r"[a-f0-9]{32}", rid):
            raise ValueError(f"Expected a frozen 32-character request_id: {key}")
        if (not isinstance(segments, list) or not 1 <= len(segments) <= 100
                or any(not isinstance(s, str) or not re.fullmatch(r"[a-f0-9]{32}", s) for s in segments)
                or len(set(segments)) != len(segments)):
            raise ValueError(f"Invalid segment_ids: {key}")
        if (pid, rid) in requests:
            raise ValueError("Duplicate project/request identity")
        if type(payload.get("expected_revision")) is not int or payload["expected_revision"] < 0:
            raise ValueError(f"Freeze expected_revision before submitting: {key}")
        keys.add(key)
        requests.add((pid, rid))
    canonical(manifest)
    return manifest


@contextmanager
def writer_lock(path):
    """OS lock is released on process exit; never remove another runner's lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another controller owns this state file") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BatchRunner:
    def __init__(self, manifest, state_path, client, service_root=ROOT, sleep=time.sleep):
        self.manifest, self.path, self.client = manifest, state_path, client
        self.root, self.sleep = Path(service_root), sleep
        fingerprint = hashlib.sha256(canonical(manifest)).hexdigest()
        self.state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
            "schema": 1, "manifest_sha256": fingerprint, "jobs": {}}
        if self.state.get("manifest_sha256") != fingerprint:
            raise ValueError("Manifest changed: preserve this run and create a new state file for a new candidate")

    def save(self):
        atomic(self.path, self.state)

    def call(self, method, path, payload=None):
        response = self.client.request(method, path, **({"json": payload} if payload is not None else {}))
        response.raise_for_status()
        return response.json()

    def reconcile(self, job):
        """Read the deployed service's durable receipt after an uncertain POST."""
        rid, pid = job["payload"]["request_id"], job["project_id"]
        path = self.root / "outputs" / "advanced" / pid / "batches" / (rid + ".json")
        if not path.is_file():
            raise RuntimeError(f"Uncertain submission for {job['key']}: inspect service jobs before a new request; not resubmitting")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        expected = hashlib.sha256(canonical(job["payload"])).hexdigest()
        if receipt.get("request_id") != rid or receipt.get("request_hash") != expected:
            raise RuntimeError("Service receipt does not match the frozen request")
        return receipt

    def run(self, interval=5):
        health = self.call("GET", "/api/health")
        self.state.setdefault("initial_service", {k: health[k] for k in (
            "api_version", "advanced_workflow_version", "nps_star_mapping_id", "artifact_retention_version") if k in health})
        self.save()
        for job in self.manifest["jobs"]:
            key, pid = job["key"], job["project_id"]
            row = self.state["jobs"].setdefault(key, {"project_id": pid, "payload": job["payload"], "status": "planned"})
            if row["status"] in TERMINAL:
                if row["status"] != "completed":
                    return False
                continue
            if not row.get("receipt"):
                if row["status"] in {"posting", "uncertain"}:
                    receipt = self.reconcile(job)
                else:
                    row["status"] = "posting"
                    self.save()  # intent reaches disk before any persistent submission
                    try:
                        receipt = self.call("POST", f"/api/advanced/projects/{pid}/generation-batches", job["payload"])
                    except Exception as exc:
                        row.update(status="uncertain", error=str(exc))
                        self.save()
                        raise
                if (not isinstance(receipt.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", receipt["id"])
                        or receipt.get("request_id") != job["payload"]["request_id"]):
                    raise RuntimeError("Invalid service batch receipt; submission remains uncertain")
                row.update(receipt=receipt, status="submitted")
                row.pop("error", None)
                self.save()
            bid = row["receipt"]["id"]
            while True:
                detail = self.call("GET", f"/api/advanced/projects/{pid}/generation-batches/{bid}")
                status = detail.get("status")
                if status not in TERMINAL | {"queued", "running", "paused"}:
                    raise RuntimeError(f"Unknown batch status: {status}")
                # Retain the latest compact receipt, not an unbounded polling trace.
                row.update(status=status, detail=detail)
                self.save()
                print(json.dumps({"key": key, "batch_id": bid, "status": status}, ensure_ascii=True), flush=True)
                if status in TERMINAL:
                    break
                if status == "paused":
                    raise RuntimeError("Service queue is paused; resume it explicitly, then rerun this controller")
                self.sleep(interval)
            if status != "completed":
                return False
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--state", type=Path, help="Default: state.json beside the frozen manifest")
    parser.add_argument("--service-root", type=Path, default=ROOT, help="Checkout running the local service (receipt recovery)")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--check", action="store_true", help="Validate only; no service calls or submissions")
    args = parser.parse_args()
    if not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 1 and 60")
    manifest = load_manifest(args.manifest)
    if args.check:
        print(json.dumps({"valid": True, "requests": len(manifest["jobs"])}))
        return 0
    state_path = (args.state or args.manifest.parent / "state.json").resolve()
    if state_path == args.manifest.resolve():
        parser.error("State must not overwrite the manifest")
    with writer_lock(state_path.with_suffix(".lock")):
        with httpx.Client(base_url=manifest.get("service_url", "http://127.0.0.1:8765"), timeout=180,
                          trust_env=False) as client:
            runner = BatchRunner(manifest, state_path, client, args.service_root)
            return 0 if runner.run(args.poll_seconds) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
