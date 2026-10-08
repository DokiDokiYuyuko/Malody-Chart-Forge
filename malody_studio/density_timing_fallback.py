"""Identity-bound experimental TIMING fallback for density trials."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .paths import ROOT

SCHEMA = "model-derived-timing-fallback-v1"
STAGED_SCHEMA = "model-derived-timing-fallback-stage-v1"
_JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
_PRESET_KEY = re.compile(r"condition-[0-9]+(?:_[0-9]+)?__window-[0-9]+\Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("模型计时来源记录格式无效")
    return value


def _contained_file(path: Path, parent: Path) -> Path:
    resolved_parent = Path(parent).resolve(strict=True)
    resolved = Path(path).resolve(strict=True)
    try:
        resolved.relative_to(resolved_parent)
    except ValueError as exc:
        raise ValueError("模型计时来源必须位于本地 outputs 目录内") from exc
    if not resolved.is_file():
        raise ValueError("模型计时来源文件不存在")
    return resolved


def timing_rows(raw_chart: bytes) -> list[bytes]:
    active = False
    rows: list[bytes] = []
    for line in raw_chart.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(b"[") and stripped.endswith(b"]"):
            active = stripped == b"[TimingPoints]"
        elif active and stripped and not stripped.startswith(b"//"):
            rows.append(line)
    return rows


def timing_facts(rows: list[bytes]) -> dict:
    parsed = []
    for row in rows:
        try:
            fields = row.strip().decode("ascii").split(",")
            if len(fields) < 7:
                raise ValueError
            time_ms, beat_length = float(fields[0]), float(fields[1])
            meter, uninherited = int(fields[2]), int(fields[6])
            if (not math.isfinite(time_ms) or not math.isfinite(beat_length) or beat_length <= 0
                    or not 20 <= 60000 / beat_length <= 600 or meter < 1 or uninherited not in (0, 1)):
                raise ValueError
        except (ValueError, UnicodeDecodeError, OverflowError) as exc:
            raise ValueError("模型 Timing 行无效") from exc
        parsed.append((time_ms, meter, uninherited))
    if not parsed or parsed[0][0] < 0 or any(b[0] <= a[0] for a, b in zip(parsed, parsed[1:])):
        raise ValueError("模型 Timing 锚点缺失或次序无效")
    return {
        "row_count": len(rows),
        "rows_sha256": hashlib.sha256(b"".join(rows)).hexdigest(),
        "meters": sorted({row[1] for row in parsed}),
        "uninherited_values": sorted({row[2] for row in parsed}),
        "first_time_ms": parsed[0][0],
        "last_time_ms": parsed[-1][0],
    }


def resolve_artifact(root: Path, project_id: str, source_attempt: dict, target_source: dict) -> dict:
    """Resolve a prior successful whole-song model attempt by IDs, never by a client path."""
    if not isinstance(source_attempt, dict) or set(source_attempt) != {"job_id", "preset_key", "attempt"}:
        raise ValueError("模型计时来源须指定任务、预设和尝试编号")
    job_id, preset_key = source_attempt["job_id"], source_attempt["preset_key"]
    attempt = source_attempt["attempt"]
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("模型计时来源任务 ID 无效")
    if not isinstance(preset_key, str) or not _PRESET_KEY.fullmatch(preset_key):
        raise ValueError("模型计时来源预设无效")
    if isinstance(attempt, bool) or not (isinstance(attempt, int) or (isinstance(attempt, str) and attempt.isdigit())):
        raise ValueError("模型计时来源尝试编号无效")
    attempt_number = int(attempt)
    if attempt_number < 1 or attempt_number > 9999:
        raise ValueError("模型计时来源尝试编号无效")

    root = Path(root).resolve(strict=True)
    outputs = root / "outputs"
    job_root = _contained_file(outputs / job_id / "trial-execution.json", outputs).parent
    execution = _json(job_root / "trial-execution.json")
    delivered = _json(job_root / "density-trial.json")
    trial = execution.get("trial")
    if not isinstance(trial, dict) or not isinstance(delivered.get("trial"), dict):
        raise ValueError("模型计时来源不是完整密度实验")
    delivered_trial = delivered["trial"]
    source_identity = execution.get("input_pcm_identity", {}).get("source", {})
    prepared_identity = execution.get("input_pcm_identity", {}).get("prepared", {})
    observations = delivered.get("observations", [])
    matching_observations = [row for row in observations if row.get("project_id") == project_id]
    if not matching_observations or delivered.get("metadata", {}).get("errors"):
        raise ValueError("模型计时来源未在同一项目中完整成功")
    if (trial.get("reference_mode") != "none" or delivered_trial.get("reference_mode") != "none"
            or trial.get("reference") is not None or trial.get("timing_fallback_artifact") is not None):
        raise ValueError("模型计时来源必须来自无参考的整曲首轮实验")
    bounds, windows = trial.get("bounds"), trial.get("windows")
    if (not isinstance(bounds, list) or len(bounds) != 2 or bounds[0] != 0
            or not isinstance(windows, list) or windows != [bounds]
            or bounds[1] != prepared_identity.get("source_stop_sample_exclusive")):
        raise ValueError("模型计时来源必须覆盖同一完整保留音频")

    expected_path = Path(target_source["path"]).resolve(strict=True)
    expected_source_sha = sha256_file(expected_path)
    target_pcm = target_source.get("pcm_sha")
    for key in ("source_id", "source_role", "pcm_sha"):
        if trial.get("source", {}).get(key) != target_source.get(key):
            raise ValueError("模型计时来源原曲身份不匹配")
    if (source_identity.get("file_sha256") != expected_source_sha
            or source_identity.get("pcm_sha256") != target_pcm
            or source_identity.get("sample_rate") != target_source.get("sample_rate")
            or source_identity.get("frames") != target_source.get("frame_count")):
        raise ValueError("模型计时来源原曲文件或 PCM 身份不匹配")
    if (prepared_identity.get("pcm_sha256") is None or prepared_identity.get("sample_rate") != 22050
            or prepared_identity.get("channels") != 1 or prepared_identity.get("subtype") != "FLOAT"):
        raise ValueError("模型计时来源的模型输入身份缺失")

    preset_rows = [row for row in execution.get("presets", []) if row.get("key") == preset_key]
    if len(preset_rows) != 1:
        raise ValueError("模型计时来源预设不在冻结运行记录中")
    preset = preset_rows[0]
    observation = next((row for row in matching_observations
                        if row.get("condition") == preset.get("sr") and row.get("seed") == preset.get("seed")), None)
    if observation is None or observation.get("status") != "completed":
        raise ValueError("模型计时来源预设没有成功完成")

    attempt_dir = job_root / "v32-original" / preset_key / "inference-attempts" / f"{attempt_number:04d}"
    attempt_result = _json(attempt_dir / "attempt-result.json")
    resolved = _json(attempt_dir / "resolved-parameters.json")
    chart = _contained_file(attempt_dir / "returned-chart.osu", outputs)
    if attempt_result.get("status") != "completed" or attempt_result.get("attempt") != attempt_number:
        raise ValueError("模型计时来源推理尝试没有成功完成")
    if Path(attempt_result.get("original_output_path", "")).resolve() != (job_root / "v32-original" / preset_key).resolve():
        raise ValueError("模型计时来源尝试路径与任务不符")
    artifacts = attempt_result.get("artifacts", [])
    chart_record = next((row for row in artifacts if row.get("kind") == "returned_chart"), None)
    if (not chart_record or Path(chart_record.get("archived_path", "")).resolve() != chart
            or chart_record.get("sha256") != sha256_file(chart)
            or chart_record.get("bytes") != chart.stat().st_size):
        raise ValueError("模型計時來源譜面未由該嘗試原生保存")
    trace_record = next((row for row in artifacts if row.get("kind") == "native_trace"
                         and row.get("updated_by_this_attempt") is True), None)
    if not trace_record:
        raise ValueError("模型计时来源缺少本次原生模型事件记录")
    trace_path = _contained_file(Path(trace_record.get("archived_path", "")), outputs)
    if (trace_path.parent != attempt_dir.resolve()
            or trace_record.get("sha256") != sha256_file(trace_path)
            or trace_record.get("archived_sha256", trace_record.get("sha256")) != sha256_file(trace_path)):
        raise ValueError("模型计时来源原生模型事件记录身份不匹配")
    request = resolved.get("request", {})
    request_preset = next((row for row in request.get("presets", []) if row.get("key") == preset_key), None)
    if (request.get("seed") != trial.get("seed") or not request.get("experimental_parameter_snapshot")
            or "timing_reference" in request or request_preset != preset):
        raise ValueError("模型计时来源实际解析参数与冻结记录不一致")
    if Path(request.get("output", "")).resolve() != (job_root / "v32-original").resolve():
        raise ValueError("模型计时来源输出目录与冻结任务不一致")
    original_input = _contained_file(Path(request.get("audio", "")), outputs)
    if (sha256_file(original_input) != prepared_identity.get("wav_sha256")
            or sha256_file(original_input) != execution.get("input_file_sha256")):
        raise ValueError("模型计时来源实际模型输入文件身份不匹配")

    raw_chart = chart.read_bytes()
    rows = timing_rows(raw_chart)
    facts = timing_facts(rows)
    return {
        "schema": SCHEMA,
        "classification": "model-derived-uncertain",
        "experimental_only": True,
        "user_confirmed": False,
        "production_tempo_write": False,
        "project_id": project_id,
        "source_attempt": {
            "job_id": job_id, "preset_key": preset_key, "attempt": f"{attempt_number:04d}",
            "trial_execution_sha256": sha256_file(job_root / "trial-execution.json"),
            "density_trial_sha256": sha256_file(job_root / "density-trial.json"),
            "attempt_result_sha256": sha256_file(attempt_dir / "attempt-result.json"),
            "resolved_parameters_sha256": sha256_file(attempt_dir / "resolved-parameters.json"),
            "native_trace_sha256": sha256_file(trace_path),
        },
        "resolved_parameters": {
            key: request.get(key) for key in (
                "seed", "difficulty", "temperature", "top_p", "mania_column_temperature",
                "cfg_scale", "ln_ratio", "year", "descriptors", "negative_descriptors",
                "experimental_conditioning",
            )
        } | {"preset": request_preset, "timing_reference": None},
        "source_identity": {
            "source_id": target_source.get("source_id"),
            "source_role": target_source.get("source_role"),
            "file_sha256": expected_source_sha,
            "pcm_sha256": target_pcm,
            "sample_rate": target_source.get("sample_rate"),
            "frames": target_source.get("frame_count"),
        },
        "prepared_identity": {
            "pcm_sha256": prepared_identity["pcm_sha256"],
            "wav_sha256": prepared_identity["wav_sha256"],
            "sample_rate": 22050,
            "channels": 1,
            "subtype": "FLOAT",
        },
        "source_chart": {"path": str(chart), "sha256": sha256_file(chart), "bytes": chart.stat().st_size},
        "timing_rows": facts,
    }


def stage_artifact(root: Path, manifest: dict, *, project_id: str, source: dict,
                   source_file_sha: str, source_pcm_sha: str, source_rate: int,
                   source_frames: int, prepared_pcm_sha: str, prepared_wav_sha: str,
                   directory: Path) -> tuple[Path, dict]:
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
            or manifest.get("classification") != "model-derived-uncertain"
            or manifest.get("experimental_only") is not True or manifest.get("user_confirmed") is not False
            or manifest.get("production_tempo_write") is not False or manifest.get("project_id") != project_id):
        raise ValueError("模型计时回退清单无效")
    identity = manifest.get("source_identity", {})
    if any(identity.get(key) != value for key, value in {
        "source_id": source.get("source_id"), "source_role": source.get("source_role"),
        "file_sha256": source_file_sha, "pcm_sha256": source_pcm_sha,
        "sample_rate": source_rate, "frames": source_frames,
    }.items()):
        raise ValueError("模型计时回退原曲身份已变化")
    prepared = manifest.get("prepared_identity", {})
    if (prepared.get("pcm_sha256") != prepared_pcm_sha or prepared.get("sample_rate") != 22050
            or prepared.get("channels") != 1 or prepared.get("subtype") != "FLOAT"):
        raise ValueError("模型计时回退准备后 PCM 身份已变化")
    chart = _contained_file(Path(manifest.get("source_chart", {}).get("path", "")), Path(root).resolve() / "outputs")
    chart_bytes = chart.read_bytes()
    chart_identity = manifest["source_chart"]
    if (sha256_file(chart) != chart_identity.get("sha256") or len(chart_bytes) != chart_identity.get("bytes")):
        raise ValueError("模型计时来源谱面身份已变化")
    rows = timing_rows(chart_bytes)
    facts = timing_facts(rows)
    if facts != manifest.get("timing_rows"):
        raise ValueError("模型 Timing 行身份已变化")
    destination = Path(directory) / "model-derived-timing-fallback.osu"
    header = (b"osu file format v14\n\n[General]\nAudioFilename: density-input.wav\nMode: 3\n\n"
              b"[Metadata]\nTitle: Experimental model timing\nArtist: Local\nCreator: Malody Studio\nVersion: Uncertain\n\n"
              b"[Difficulty]\nCircleSize:4\nOverallDifficulty:8\nHPDrainRate:5\nApproachRate:9\n"
              b"SliderMultiplier:1.4\nSliderTickRate:1\n\n[TimingPoints]\n")
    staged_bytes = header + b"".join(rows) + (b"" if rows[-1].endswith((b"\n", b"\r")) else b"\n") + b"\n[HitObjects]\n"
    if destination.exists():
        if destination.read_bytes() != staged_bytes:
            raise ValueError("本次实验目录已有不同的模型计时文件，拒绝覆盖")
    else:
        destination.write_bytes(staged_bytes)
    staged = {
        "schema": STAGED_SCHEMA,
        "classification": manifest["classification"],
        "experimental_only": True,
        "user_confirmed": False,
        "production_tempo_write": False,
        "project_id": project_id,
        "source_attempt": manifest["source_attempt"],
        "source_chart": chart_identity,
        "source_identity": identity,
        "trial_prepared_input": {"pcm_sha256": prepared_pcm_sha, "wav_sha256": prepared_wav_sha, "sample_rate": 22050},
        "timing_rows": facts,
        "staged_reference": {"path": str(destination.resolve()), "sha256": sha256_file(destination),
                             "bytes": destination.stat().st_size},
    }
    return destination, staged


def validate_staged_reference(reference: str | Path, identity: dict, root: Path = ROOT) -> str:
    if (not isinstance(identity, dict) or identity.get("schema") != STAGED_SCHEMA
            or identity.get("classification") != "model-derived-uncertain"
            or identity.get("experimental_only") is not True or identity.get("user_confirmed") is not False
            or identity.get("production_tempo_write") is not False):
        raise ValueError("模型计时回退解析身份无效")
    staged = identity.get("staged_reference", {})
    path = _contained_file(Path(reference), Path(root).resolve() / "outputs")
    if path != Path(staged.get("path", "")).resolve():
        raise ValueError("模型计时回退文件路径与解析身份不一致")
    content = path.read_bytes()
    if sha256_file(path) != staged.get("sha256") or len(content) != staged.get("bytes"):
        raise ValueError("模型计时回退文件身份已变化")
    facts = timing_facts(timing_rows(content))
    if facts != identity.get("timing_rows"):
        raise ValueError("模型计时回退行身份已变化")
    return str(path)
