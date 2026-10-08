import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import librosa
import pytest
import soundfile as sf

from malody_studio.density_timing_fallback import (
    resolve_artifact,
    stage_artifact,
    timing_facts,
    timing_rows,
    validate_staged_reference,
)
from malody_studio.beat_analysis import mono_audio
from malody_studio.mapperatorinator import PYTHON, build_worker_request
from malody_studio.separation import pcm_hash
from malody_studio.v32_recovery import recovery_reference


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path):
    root = tmp_path / "workspace"
    outputs = root / "outputs"
    project_id = "a" * 32
    job_id = "b" * 32
    source = outputs / "advanced" / project_id / "source.wav"
    source.parent.mkdir(parents=True)
    samples = (np.sin(np.arange(44100, dtype=np.float32) / 80) * .2).reshape(-1, 1)
    sf.write(source, samples, 44100, subtype="FLOAT")
    source_pcm = pcm_hash(samples)
    source_bytes = source.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    descriptor = {"path": str(source), "source_id": "original", "source_role": "mix",
                  "pcm_sha": source_pcm, "sample_rate": 44100, "frame_count": len(samples)}

    job_root = outputs / job_id
    prepared_audio = librosa.resample(mono_audio(samples)[0], orig_sr=44100, target_sr=22050)
    prepared_path = job_root / "density-input.wav"
    prepared_path.parent.mkdir(parents=True)
    sf.write(prepared_path, prepared_audio, 22050, subtype="FLOAT")
    prepared_sha = hashlib.sha256(prepared_path.read_bytes()).hexdigest()
    preset_key = "condition-5_9__window-0"
    preset = {"key": preset_key, "label": "whole", "sr": 5.9, "seed": 17,
              "start_time": 0, "end_time": 1000}
    trial = {"source": descriptor, "bounds": [0, len(samples)], "active_seconds": 1.,
             "conditions": [5.9], "seed": 17, "pattern": "balanced", "split": "development",
             "reference_mode": "none", "reference": None, "windows": [[0, len(samples)]],
             "experimental_only": True}
    prepared = {"source_stop_sample_exclusive": len(samples), "sample_rate": 22050,
                "channels": 1, "subtype": "FLOAT", "pcm_sha256": pcm_hash(prepared_audio),
                "wav_sha256": prepared_sha}
    execution = {"input_file_sha256": prepared_sha, "trial": trial,
                 "input_pcm_identity": {"source": {"source_id": "original", "source_role": "mix",
                     "file_sha256": source_sha, "pcm_sha256": source_pcm,
                     "sample_rate": 44100, "frames": len(samples)}, "prepared": prepared},
                 "presets": [preset]}
    _write_json(job_root / "trial-execution.json", execution)
    _write_json(job_root / "density-trial.json", {
        "trial": trial,
        "observations": [{"project_id": project_id, "condition": 5.9, "seed": 17,
                          "status": "completed"}],
        "metadata": {"errors": {}},
    })

    chart = (b"osu file format v14\n\n[TimingPoints]\n"
             b"100,500,3,1,0,100,1,0\n500,500,3,1,0,100,1,0\n\n[HitObjects]\n"
             b"64,192,200,1,0,0:0:0:0:\n")
    attempt_dir = job_root / "v32-original" / preset_key / "inference-attempts" / "0001"
    attempt_dir.mkdir(parents=True)
    archived_chart = attempt_dir / "returned-chart.osu"
    archived_chart.write_bytes(chart)
    native = attempt_dir / "v32-generation-diagnostics.json"
    native.write_text("{}", encoding="utf-8")
    output_dir = job_root / "v32-original" / preset_key
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(attempt_dir / "attempt-result.json", {
        "status": "completed", "attempt": 1, "original_output_path": str(output_dir),
        "artifacts": [
            {"kind": "returned_chart", "path": str(output_dir / "beatmap.osu"),
             "archived_path": str(archived_chart), "sha256": hashlib.sha256(chart).hexdigest(),
             "bytes": len(chart)},
            {"kind": "native_trace", "path": str(native), "archived_path": str(native),
             "sha256": hashlib.sha256(native.read_bytes()).hexdigest(), "updated_by_this_attempt": True},
        ],
    })
    _write_json(attempt_dir / "resolved-parameters.json", {
        "request": {"seed": 17, "experimental_parameter_snapshot": True,
                    "audio": str(prepared_path), "output": str(job_root / "v32-original"),
                    "presets": [preset]},
    })
    return root, project_id, job_id, preset_key, descriptor, source_sha, source_pcm


@pytest.mark.skipif(not PYTHON.is_file(), reason='V32 parser integration requires its isolated environment')
def test_resolve_stage_and_worker_request_preserve_model_timing_rows(tmp_path):
    root, project_id, job_id, preset_key, source, source_sha, source_pcm = _fixture(tmp_path)
    manifest = resolve_artifact(root, project_id,
        {"job_id": job_id, "preset_key": preset_key, "attempt": 1}, source)
    assert manifest["classification"] == "model-derived-uncertain"
    assert manifest["experimental_only"] and not manifest["user_confirmed"]
    assert manifest["timing_rows"]["meters"] == [3]
    assert manifest["timing_rows"]["row_count"] == 2

    output = root / "outputs" / "new-density-trial"
    output.mkdir()
    reference, identity = stage_artifact(root, manifest, project_id=project_id, source=source,
        source_file_sha=source_sha, source_pcm_sha=source_pcm, source_rate=44100,
        source_frames=44100, prepared_pcm_sha=manifest["prepared_identity"]["pcm_sha256"],
        prepared_wav_sha="d" * 64,
        directory=output)
    assert timing_rows(reference.read_bytes()) == timing_rows(
        Path(manifest["source_chart"]["path"]).read_bytes())
    assert validate_staged_reference(reference, identity, root) == str(reference.resolve())
    parser_check = '''import sys
import rosu_pp_py
from slider import Beatmap
raw=rosu_pp_py.Beatmap(path=sys.argv[1])
upstream=Beatmap.from_path(sys.argv[1])
assert raw.n_objects==0 and upstream.circle_size==4
assert len(upstream.timing_points)==2
assert [row.meter for row in upstream.timing_points]==[3,3]
'''
    subprocess.run([str(PYTHON), "-c", parser_check, str(reference)], check=True,
                   capture_output=True, text=True)

    request = build_worker_request(Path("density-input.wav"), Path("out"), {
        "title": "song", "artist": "artist", "seed": 17, "ln_ratio": .15,
        "timing_fallback_reference": str(reference), "timing_fallback_identity": identity,
    })
    assert request["timing_fallback_reference"] == str(reference)
    assert request["timing_fallback_identity"] == identity
    assert "timing_reference" not in request
    exact_error = AssertionError("No timing points found in beatmap.")
    attempted = set()
    assert recovery_reference(exact_error, preset_key, request["timing_fallback_reference"], attempted) == str(reference)
    with pytest.raises(ValueError, match="恢复失败"):
        recovery_reference(exact_error, preset_key, request["timing_fallback_reference"], attempted)

    reference.write_bytes(reference.read_bytes() + b"// changed\n")
    with pytest.raises(ValueError, match="身份已变化"):
        validate_staged_reference(reference, identity, root)


@pytest.mark.parametrize("mutate", ["wrong_source", "wrong_project", "bad_id"])
def test_resolver_rejects_stale_or_untrusted_artifact_request(tmp_path, mutate):
    root, project_id, job_id, preset_key, source, _, _ = _fixture(tmp_path)
    request = {"job_id": job_id, "preset_key": preset_key, "attempt": 1}
    target = dict(source)
    expected_project = project_id
    if mutate == "wrong_source":
        target["pcm_sha"] = "0" * 64
    elif mutate == "wrong_project":
        expected_project = "c" * 32
    else:
        request["job_id"] = "..\\outside"
    with pytest.raises(ValueError):
        resolve_artifact(root, expected_project, request, target)


def test_timing_rows_reject_invalid_clock_and_tampered_chart(tmp_path):
    with pytest.raises(ValueError, match="无效"):
        timing_facts([b"-1,500,3,1,0,100,1,0\n"])
    root, project_id, job_id, preset_key, source, source_sha, source_pcm = _fixture(tmp_path)
    manifest = resolve_artifact(root, project_id,
        {"job_id": job_id, "preset_key": preset_key, "attempt": 1}, source)
    chart = Path(manifest["source_chart"]["path"])
    chart.write_bytes(chart.read_bytes() + b"// changed\n")
    out = root / "outputs" / "stale"
    out.mkdir()
    with pytest.raises(ValueError, match="身份已变化"):
        stage_artifact(root, manifest, project_id=project_id, source=source,
            source_file_sha=source_sha, source_pcm_sha=source_pcm, source_rate=44100,
            source_frames=44100, prepared_pcm_sha=manifest["prepared_identity"]["pcm_sha256"],
            prepared_wav_sha="d" * 64,
            directory=out)
