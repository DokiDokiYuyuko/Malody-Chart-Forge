import io
import json
from pathlib import Path
import zipfile

import numpy as np
from PIL import Image
import pytest
import soundfile as sf

from malody_studio.charts import Note, serialize
from tools.import_chart_packs import digest, inspect_pack, install, matches, selected_packs


def package(path):
    audio = io.BytesIO()
    sf.write(audio, np.zeros((44100 * 3, 2)), 44100, format="OGG", subtype="VORBIS")
    image = io.BytesIO(); Image.new("RGB", (16, 16)).save(image, format="PNG")
    chart = serialize([Note(500, 0), Note(1000, 1), Note(1500, 2, 2000)], "Demo", "Demo artist", "Expert", 120)
    chart["meta"]["background"] = "background.png"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0/audio.ogg", audio.getvalue())
        archive.writestr("0/background.png", image.getvalue())
        archive.writestr("0/balanced--expert.mc", json.dumps(chart))
    return path


def test_readback_metrics_preserve_source_and_do_not_claim_playtest(tmp_path):
    path = package(tmp_path / "demo.mcz"); before = path.read_bytes()
    contents, report = inspect_pack(path)
    assert path.read_bytes() == before
    assert report["charts"]["0/balanced--expert.mc"]["average_nps"] == 2
    assert report["human_playtest"] == "not_performed" and report["edits"] == {}
    assert contents["0/audio.ogg"]


def test_reimport_reads_actual_bytes_and_preserves_previous_folder(tmp_path):
    path = package(tmp_path / "demo.mcz"); contents, _ = inspect_pack(path)
    root, history = tmp_path / "game/chart", tmp_path / "history"
    first = install("song-1", contents, root, history)
    assert first["status"] == "imported"
    assert install("song-1", contents, root, history)["status"] == "unchanged"
    folder = Path(first["folder"])
    (folder / "0/balanced--expert.mc").write_bytes(b"changed outside importer")
    result = install("song-1", contents, root, history)
    assert result["status"] == "imported" and result["previous_version"]
    assert matches(folder, contents)
    assert (next(history.iterdir()) / "0/balanced--expert.mc").read_bytes() == b"changed outside importer"


@pytest.mark.parametrize("member", ["../outside", "0/../../outside", "0/C:bad", "0\\outside"])
def test_unsafe_archive_member_is_rejected_without_extracting(tmp_path, member):
    path = package(tmp_path / "bad.mcz")
    with zipfile.ZipFile(path, "a") as archive:
        entry = zipfile.ZipInfo("placeholder")
        entry.filename = member
        archive.writestr(entry, b"bad")
    with pytest.raises(ValueError, match="Unsafe"):
        inspect_pack(path)
    assert not (tmp_path / "outside").exists()


def test_missing_artwork_or_unknown_artist_cannot_be_delivered(tmp_path):
    path = package(tmp_path / "demo.mcz")
    contents, _ = inspect_pack(path)
    chart = json.loads(contents["0/balanced--expert.mc"])
    chart["meta"]["song"]["artist"] = "Unknown"
    contents["0/balanced--expert.mc"] = json.dumps(chart).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="artist"):
        inspect_pack(path)


def test_only_explicit_hash_matched_approved_manifest_entries_are_selected(tmp_path):
    path = package(tmp_path / "selected.mcz")
    package(tmp_path / "unselected-attempt.mcz")
    manifest = {"schema": 1, "deliveries": [{"id": "song-1", "path": path.name,
                  "sha256": digest(path.read_bytes()), "approved": True}]}
    selection = tmp_path / "deliveries.json"; selection.write_text(json.dumps(manifest))
    assert [row["path"] for row in selected_packs(selection)] == [path]
    manifest["deliveries"][0]["sha256"] = "0" * 64
    selection.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity mismatch"):
        selected_packs(selection)
