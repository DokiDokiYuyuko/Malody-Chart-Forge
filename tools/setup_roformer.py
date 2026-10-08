"""Install the isolated, pinned Kim MelBand RoFormer inference deployment.

This command validates imports and the CUDA build, but never runs inference.
Use --weights-path to install a separately downloaded official checkpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TARGET = ROOT / "models" / "separation" / "melband-roformer-kim"
VENDOR = ROOT / "vendor" / "RoFormer"
RUNTIME = ROOT / "runtime" / "roformer-venv"
LOCK = ROOT / "runtime" / "roformer-requirements-lock.txt"
VERSION = "melband-roformer-kim-adapter-v1"
COMMIT = "e247dfe4abc1f17c69dff719207fe045dc04413a"
REPOSITORY = "https://github.com/ZFTurbo/Music-Source-Separation-Training"
RAW_SOURCE = "https://raw.githubusercontent.com/ZFTurbo/Music-Source-Separation-Training/" + COMMIT + "/"
WEIGHT_REPOSITORY = "KimberleyJSN/melbandroformer"
WEIGHT_REVISION = "ac9b0614ab3cd7f77219e18ba494dfd93956c348"
WEIGHT_NAME = "MelBandRoformer.ckpt"
WEIGHT_SHA256 = "87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e"
CONFIG_NAME = "config_vocals_mel_band_roformer_kj.yaml"
CONFIG_PATH = "configs/KimberleyJensen/" + CONFIG_NAME
UPSTREAM_SHA256 = {
    "LICENSE": "3282dc057695ef5b9a64909a7092ca40b2c292c232580fc6ace6e5d665cc0207",
    "models/bs_roformer/attend.py": "0459d799ade55541df2994b0becf7aec12214491360c5a06e346f6d615eaed15",
    "models/bs_roformer/mel_band_roformer.py": "3b4a57ab268933900172e05fb76dd9e8acc1eb770c745d583a4e42b94b79ec15",
    CONFIG_PATH: "f63f38eb1e6e40a7db0dade714a5ae257555dd8748f4e774eae8679275a81926",
}


def sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def require_hash(path: Path, expected: str) -> str:
    digest = sha(path)
    if digest != expected:
        raise RuntimeError("SHA-256 mismatch for " + str(path) + ": expected " + expected + ", got " + digest)
    return digest


def fetch(url: str, destination: Path, expected: str, retries: int = 3) -> None:
    """Resume interrupted downloads; only publish a complete, checked file."""
    if destination.is_file():
        require_hash(destination, expected)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    for attempt in range(1, retries + 1):
        try:
            offset = temporary.stat().st_size if temporary.exists() else 0
            headers = {"User-Agent": "malody-roformer-setup/1", "Cache-Control": "no-cache"}
            if offset:
                headers["Range"] = "bytes=" + str(offset) + "-"
            print("Downloading " + destination.name + " (attempt " + str(attempt) + ")", flush=True)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                status = response.getcode()
                if status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith("bytes " + str(offset) + "-"):
                        raise RuntimeError("Unexpected resume range for " + destination.name)
                else:
                    offset = 0
                written = offset
                last_report = time.monotonic()
                with temporary.open("ab" if offset else "wb") as output:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        output.write(chunk)
                        written += len(chunk)
                        if time.monotonic() - last_report >= 20:
                            print(destination.name + ": " + str(written // (1024 * 1024)) + " MiB", flush=True)
                            last_report = time.monotonic()
            try:
                require_hash(temporary, expected)
            except RuntimeError:
                temporary.unlink(missing_ok=True)
                raise
            temporary.replace(destination)
            return
        except (OSError, http.client.HTTPException, urllib.error.URLError, RuntimeError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 416:
                # A prior attempt might have finished before the connection failed.
                if temporary.is_file() and sha(temporary) == expected:
                    temporary.replace(destination)
                    return
                temporary.unlink(missing_ok=True)
            if attempt == retries:
                raise RuntimeError("Download failed for " + destination.name + ". Use --weights-path for a local checkpoint. " + str(exc)) from exc
            print("Retrying " + destination.name + ": " + str(exc), flush=True)
            time.sleep(min(2 ** attempt, 8))


def copy_checked(source: Path, destination: Path, expected: str) -> None:
    source = source.expanduser().resolve()
    require_hash(source, expected)
    if source == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    shutil.copyfile(source, temporary)
    require_hash(temporary, expected)
    temporary.replace(destination)


def vendor_assets(retries: int) -> None:
    for name, expected in UPSTREAM_SHA256.items():
        fetch(RAW_SOURCE + name, VENDOR / name, expected, retries)
    (VENDOR / "models" / "__init__.py").write_text(
        '"""Minimal inference-only subset of Music-Source-Separation-Training."""\n', encoding="utf-8")
    (VENDOR / "models" / "bs_roformer" / "__init__.py").write_text(
        'from .mel_band_roformer import MelBandRoformer\n\n__all__ = ["MelBandRoformer"]\n', encoding="utf-8")
    upstream = {
        name: {"sha256": expected, "bytes": (VENDOR / name).stat().st_size, "url": RAW_SOURCE + name}
        for name, expected in UPSTREAM_SHA256.items()
    }
    metadata = {
        "schema": 1, "repository": REPOSITORY, "commit": COMMIT, "license": "MIT",
        "upstream_files": upstream,
        "local_changes": ["Add an inference-only models/__init__.py.",
                          "Reduce models/bs_roformer/__init__.py to the retained MelBandRoformer class; omit other model families."],
        "execution": "Uses native PyTorch attention. No torch.compile, external flash-attn, PoPE, or training dependencies are installed.",
    }
    (VENDOR / "SOURCE.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    copy_checked(VENDOR / CONFIG_PATH, TARGET / CONFIG_NAME, UPSTREAM_SHA256[CONFIG_PATH])
    copy_checked(VENDOR / "LICENSE", TARGET / "LICENSE", UPSTREAM_SHA256["LICENSE"])


def environment_python() -> Path:
    return RUNTIME / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_environment(base_python: Path | None = None) -> None:
    if not LOCK.is_file():
        raise RuntimeError("Missing exact dependency lock: " + str(LOCK))
    if base_python is None:
        base_python = ROOT / "runtime" / "python" / "cpython-3.10.19-windows-x86_64-none" / "python.exe"
    if not base_python.is_file():
        raise RuntimeError("Local Python 3.10 is missing; pass --python with a Python 3.10 executable.")
    version = subprocess.check_output([str(base_python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"], text=True).strip()
    if version != "3.10":
        raise RuntimeError("RoFormer requires the isolated Python 3.10 runtime; got " + version)
    python = environment_python()
    uv = ROOT / "cache" / "separation-bootstrap" / "uv" / "uv.exe"
    uv_command = str(uv) if uv.is_file() else shutil.which("uv")
    env = dict(os.environ, UV_CACHE_DIR=str(ROOT / "cache" / "uv"))
    if uv_command:
        if not python.is_file():
            subprocess.run([uv_command, "venv", "--python", str(base_python), str(RUNTIME)], env=env, check=True)
        subprocess.run([uv_command, "pip", "sync", "--python", str(python), "--index-strategy", "unsafe-best-match", str(LOCK)], env=env, check=True)
    else:
        if not python.is_file():
            subprocess.run([str(base_python), "-m", "venv", str(RUNTIME)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(LOCK)], env=env, check=True)


def lock_versions() -> dict[str, str]:
    versions = {}
    for line in LOCK.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line.strip())
        if match:
            versions[re.sub(r"[-_.]+", "-", match[1]).lower()] = match[2]
    if not versions or versions.get("torch") != "2.5.1+cu124":
        raise RuntimeError("Dependency lock must pin torch==2.5.1+cu124 and every inference dependency.")
    return versions


def probe_environment() -> dict:
    python = environment_python()
    if not python.is_file():
        raise RuntimeError("Missing isolated RoFormer runtime: " + str(python))
    probe = r'''
import importlib.metadata as m, json, platform, re, sys
sys.path.insert(0, sys.argv[1])
import torch, numpy, scipy, librosa, soundfile, einops, rotary_embedding_torch, beartype, yaml
from models.bs_roformer.mel_band_roformer import MelBandRoformer
packages = {re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(): d.version for d in m.distributions()}
available = torch.cuda.is_available()
print(json.dumps({"python": sys.version, "python_version": platform.python_version(), "python_executable": sys.executable,
 "torch": torch.__version__, "cuda_build": torch.version.cuda, "cuda_available": available,
 "cuda_device": torch.cuda.get_device_name(0) if available else None, "packages": packages,
 "model_import_verified": True, "inference_run": False}))
'''
    process = subprocess.run([str(python), "-c", probe, str(VENDOR)], capture_output=True, text=True, check=True)
    environment = json.loads(process.stdout)
    if not environment["python_version"].startswith("3.10."):
        raise RuntimeError("The isolated runtime is not Python 3.10.")
    if environment["torch"] != "2.5.1+cu124" or environment["cuda_build"] != "12.4":
        raise RuntimeError("Expected PyTorch 2.5.1 with its CUDA 12.4 build.")
    for name, expected in lock_versions().items():
        actual = environment["packages"].get(name)
        if actual != expected:
            raise RuntimeError("Runtime dependency differs from lock: " + name + " expected " + expected + ", got " + str(actual))
    return environment


def code_records() -> tuple[dict, str]:
    paths = [ROOT / "tools" / "setup_roformer.py", ROOT / "malody_studio" / "deployment_integrity.py"]
    paths += [path for path in VENDOR.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    # Include integration code if present so every deployed adapter file has a hash.
    paths += [path for path in (ROOT / "tools" / "roformer_worker.py", ROOT / "malody_studio" / "roformer.py",
                              ROOT / "malody_studio" / "separation_models.py") if path.is_file()]
    records = {path.relative_to(ROOT).as_posix(): {"bytes": path.stat().st_size, "sha256": sha(path)} for path in sorted(paths)}
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return records, digest


def write_manifest(environment: dict, download_url: str) -> dict:
    official_url = "https://huggingface.co/" + WEIGHT_REPOSITORY + "/resolve/" + WEIGHT_REVISION + "/" + WEIGHT_NAME
    files = {}
    for name, url, expected in ((WEIGHT_NAME, official_url, WEIGHT_SHA256), (CONFIG_NAME, RAW_SOURCE + CONFIG_PATH, UPSTREAM_SHA256[CONFIG_PATH]), ("LICENSE", RAW_SOURCE + "LICENSE", UPSTREAM_SHA256["LICENSE"])):
        path = TARGET / name
        files[name] = {"bytes": path.stat().st_size, "sha256": require_hash(path, expected), "url": url}
    from malody_studio.deployment_integrity import registry_inference_hash
    code_files, code_hash = code_records()
    manifest = {
        "schema": 1, "adapter_version": VERSION, "code_version": COMMIT,
        "code_source": REPOSITORY + "/tree/" + COMMIT, "license": "MIT",
        "weight_source": {"repository": WEIGHT_REPOSITORY, "revision": WEIGHT_REVISION,
                          "filename": WEIGHT_NAME, "download_url": download_url, "expected_sha256": WEIGHT_SHA256},
        "models": {"melband_roformer_kim": [WEIGHT_NAME, CONFIG_NAME]}, "files": files,
        "environment": environment, "dependency_lock_sha256": sha(LOCK),
        "code_files": code_files, "code_hash": code_hash, "inference_verified": False,
        "registry_inference_sha256": registry_inference_hash(ROOT / "malody_studio" / "separation_models.py"),
    }
    destination = TARGET / "manifest.json"
    temporary = TARGET / "manifest.partial.json"
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights-path", type=Path, help="Official checkpoint downloaded elsewhere; SHA-256 is always checked.")
    parser.add_argument("--hf-endpoint", default=os.environ.get("MALODY_ROFORMER_HF_ENDPOINT", os.environ.get("HF_ENDPOINT", "https://huggingface.co")), help="Optional Hugging Face mirror; checkpoint hash remains mandatory.")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--python", type=Path, help="Existing Python 3.10 executable used to create the isolated runtime.")
    parser.add_argument("--skip-install", action="store_true", help="Validate an already installed isolated runtime instead of installing dependencies.")
    args = parser.parse_args()
    if not 1 <= args.retries <= 10:
        parser.error("--retries must be between 1 and 10")
    endpoint = args.hf_endpoint.rstrip("/")
    if not endpoint.startswith("https://"):
        parser.error("--hf-endpoint must use HTTPS")
    vendor_assets(args.retries)
    if not args.skip_install:
        install_environment(args.python)
    url = endpoint + "/" + WEIGHT_REPOSITORY + "/resolve/" + WEIGHT_REVISION + "/" + WEIGHT_NAME + "?download=true"
    if args.weights_path:
        copy_checked(args.weights_path, TARGET / WEIGHT_NAME, WEIGHT_SHA256)
    else:
        fetch(url, TARGET / WEIGHT_NAME, WEIGHT_SHA256, args.retries)
    environment = probe_environment()
    manifest = write_manifest(environment, url)
    print(json.dumps({"ready_assets": True, "manifest": str(TARGET / "manifest.json"),
                      "models": list(manifest["models"]), "environment": environment,
                      "inference_verified": False}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
