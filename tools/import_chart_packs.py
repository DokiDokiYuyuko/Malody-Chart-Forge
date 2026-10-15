"""Read back selected classic 4K MCZ deliveries and import them into Malody V."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
import soundfile as sf
from malody_studio.advanced import chart_events
from malody_studio.charts import validate_chart
from malody_studio.micro_chord_alignment import normalize_chart
from malody_studio.lane_burden import repair_chart


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_member(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and len(path.parts) == 2 and path.parts[0] == "0"
            and path.parts[1] not in {"", ".", ".."} and not any(c in name for c in "\\:\x00"))


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def inspect_pack(path, *, align=False, limit_lanes=False):
    contents, edits = {}, {}
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(e.file_size for e in entries) > 1_500_000_000:
            raise ValueError("Package exceeds importer extraction limits")
        for entry in entries:
            if entry.is_dir():
                if entry.filename != "0/":
                    raise ValueError("Only classic 0/ package paths are supported")
                continue
            if (not safe_member(entry.orig_filename) or not safe_member(entry.filename)
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                    or entry.filename.casefold() in {n.casefold() for n in contents}):
                raise ValueError(f"Unsafe or duplicate package member: {entry.filename}")
            contents[entry.filename] = archive.read(entry)
    if "0/audio.ogg" not in contents:
        raise ValueError("Missing 0/audio.ogg")
    audio = sf.info(io.BytesIO(contents["0/audio.ogg"]))
    if audio.format != "OGG" or audio.subtype != "VORBIS" or audio.frames <= 0:
        raise ValueError("Game audio must be nonempty OGG Vorbis")
    duration = audio.frames / audio.samplerate
    charts = {}
    identity = set()
    for name, data in list(contents.items()):
        if not name.lower().endswith(".mc"):
            continue
        chart = json.loads(data.decode("utf-8-sig"))
        artist = str(chart.get("meta", {}).get("song", {}).get("artist", "")).strip()
        title = str(chart.get("meta", {}).get("song", {}).get("title", "")).strip()
        background = chart.get("meta", {}).get("background")
        if not artist or artist.casefold() == "unknown" or not title:
            raise ValueError(f"Verified title and artist required: {name}")
        if not isinstance(background, str) or not safe_member("0/" + background) or "0/" + background not in contents:
            raise ValueError(f"Packaged artwork required: {name}")
        with Image.open(io.BytesIO(contents["0/" + background])) as cover:
            cover.verify()
        validate_chart(chart, duration * 1000)
        if align:
            chart, alignment = normalize_chart(chart)
            edits.setdefault(name, {})["alignment"] = alignment
        if limit_lanes:
            chart, burden = repair_chart(chart)
            edits.setdefault(name, {})["lane_burden"] = burden
        validate_chart(chart, duration * 1000)
        if align or limit_lanes:
            contents[name] = json.dumps(chart, ensure_ascii=False, allow_nan=False).encode("utf-8")
        events = chart_events(chart)
        if not events:
            raise ValueError(f"Empty playable chart: {name}")
        span = max(1.0, (max(e.get("end_ms") or e["start_ms"] for e in events)
                         - min(e["start_ms"] for e in events)) / 1000)
        timestamps = sorted(e["start_ms"] for e in events)
        left = peak = 0
        for right, timestamp in enumerate(timestamps):
            while timestamp - timestamps[left] >= 1000:
                left += 1
            peak = max(peak, right - left + 1)
        charts[name] = {"heads": len(events), "holds": sum(e.get("end_ms") is not None for e in events),
                        "average_nps": len(events) / span, "active_span_seconds": span, "peak_1s_heads": peak,
                        "lanes": [sum(e["lane"] == lane for e in events) for lane in range(4)]}
        identity.add((title, artist))
    if not charts or len(identity) != 1:
        raise ValueError("Expected classic 4K charts for one identified recording")
    return contents, {"package_sha256": digest(path.read_bytes()), "audio_sha256": digest(contents["0/audio.ogg"]), "charts": charts, "edits": edits,
                      "audio_duration_seconds": duration, "musical_review": "not_established",
                      "human_playtest": "not_performed"}


def selected_packs(source):
    if source.suffix.lower() == ".mcz":
        return [{"id": None, "path": source.resolve()}]
    manifest = json.loads(source.read_text(encoding="utf-8-sig"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("deliveries"), list) or not manifest["deliveries"]:
        raise ValueError("Select one MCZ or a schema=1 manifest with nonempty deliveries")
    rows, keys = [], set()
    for row in manifest["deliveries"]:
        key = row.get("id")
        if not isinstance(key, str) or not key.strip() or key in keys or row.get("approved") is not True:
            raise ValueError("Each delivery needs a unique id and an explicit approved=true decision")
        path = Path(row["path"])
        path = (source.parent / path).resolve() if not path.is_absolute() else path.resolve()
        if path.suffix.lower() != ".mcz" or digest(path.read_bytes()) != row.get("sha256"):
            raise ValueError(f"Selected package identity mismatch: {key}")
        keys.add(key)
        rows.append({"id": key, "path": path})
    return rows


def matches(folder, contents):
    if not folder.is_dir() or linked(folder):
        return False
    files = [p for p in folder.rglob("*") if p.is_file()]
    return (not any(linked(p) for p in folder.rglob("*"))
            and {p.relative_to(folder).as_posix() for p in files} == set(contents)
            and all((folder / name).read_bytes() == data for name, data in contents.items()))


def install(key, contents, chart_root, history_root):
    folder_name = "Startrail-" + digest(key.encode("utf-8"))[:20]
    target = chart_root / folder_name
    if linked(target) or not target.resolve().is_relative_to(chart_root.resolve()):
        raise ValueError("Import target escapes the selected game directory")
    if matches(target, contents):
        return {"status": "unchanged", "folder": str(target), "files_readback": True}
    if target.exists() and (not target.is_dir() or any(linked(p) for p in target.rglob("*"))):
        raise ValueError("Refusing to replace a non-directory or linked game folder")
    chart_root.mkdir(parents=True, exist_ok=True)
    stage = chart_root / (".startrail-stage-" + uuid.uuid4().hex)
    backup = None
    stage.mkdir()
    try:
        for name, data in contents.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if not matches(stage, contents):
            raise ValueError("Staged file readback failed")
        if target.exists():
            history_root.mkdir(parents=True, exist_ok=True)
            backup = history_root / (folder_name + "-" + uuid.uuid4().hex)
            shutil.move(str(target), str(backup))
        try:
            stage.replace(target)
        except OSError:
            if backup:
                shutil.move(str(backup), str(target))
            raise
        if not matches(target, contents):
            raise ValueError("Installed file readback failed")
        return {"status": "imported", "folder": str(target), "previous_version": str(backup) if backup else None,
                "files_readback": True}
    finally:
        if stage.exists() and stage.resolve().is_relative_to(chart_root.resolve()) and not linked(stage):
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="One final MCZ or an explicit delivery manifest; no recursive discovery")
    parser.add_argument("--chart-dir", type=Path, help="Actual Malody V chart directory; required for installation")
    parser.add_argument("--history-dir", type=Path, default=ROOT / "outputs" / "import-history")
    parser.add_argument("--dry-run", action="store_true", help="Read back and report without writing any game files")
    parser.add_argument("--align-micro-chords", action="store_true", help="Opt-in fixed-120-BPM correction in staging only")
    parser.add_argument("--limit-lane-burden", action="store_true", help="Opt-in comfort rule; avoid for intentional Jack patterns")
    args = parser.parse_args()
    if not args.dry_run and args.chart_dir is None:
        parser.error("--chart-dir is required for installation")
    if args.chart_dir and args.history_dir.resolve().is_relative_to(args.chart_dir.resolve()):
        parser.error("Import history must be outside the game's scanned chart directory")
    # Preflight the complete selection without retaining a playlist's audio in RAM.
    prepared = []
    for row in selected_packs(args.source):
        contents, report = inspect_pack(row["path"], align=args.align_micro_chords, limit_lanes=args.limit_lane_burden)
        prepared.append((row, report))
        del contents
    records = []
    for row, report in prepared:
        key = row["id"] or "audio-" + report["audio_sha256"]
        result = {"id": key, "source": str(row["path"]), **report, "status": "checked"}
        if not args.dry_run:
            contents, checked = inspect_pack(row["path"], align=args.align_micro_chords, limit_lanes=args.limit_lane_burden)
            if checked["package_sha256"] != report["package_sha256"]:
                raise ValueError("Selected package changed after preflight")
            result.update(install(key, contents, args.chart_dir.resolve(), args.history_dir.resolve()))
        records.append(result)
    print(json.dumps({"schema": 1, "dry_run": args.dry_run, "records": records}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
