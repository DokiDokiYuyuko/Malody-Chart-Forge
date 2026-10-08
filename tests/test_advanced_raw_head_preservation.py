from copy import deepcopy

import pytest

from malody_studio.adaptive_difficulty import calibrate_adaptive, model_candidates
from malody_studio.advanced import valid_events
from malody_studio.advanced_generation import _stable_events
from malody_studio.charts import Note
from malody_studio.quality_workflow import candidate_events


def test_planned_hold_shortening_can_keep_a_real_model_head_with_its_raw_origin():
    # The real Vitamins failure, reduced to the overlapping model LN and tap.
    originals = [Note(93808, 0, 94438), Note(94278, 0)]
    before = deepcopy(originals)
    acoustic = {'source': {'sample_rate': 1000}, 'onset_samples': [], 'onset_strengths': []}
    local = [Note(n.start - 93800, n.lane, None if n.end is None else n.end - 93800) for n in originals]
    candidates = model_candidates(local, acoustic, selection_policy='model_only')
    selected, _ = calibrate_adaptive(candidates, 1000, 'expert', 1., 42,
        {'hold_ms': 400, 'gap': 35, 'peak': 10, 'nps': 10, 'chord': 4},
        selection_policy='model_only', audio_vote_cap=0)
    assert selected == [Note(8, 0, 408), Note(478, 0)]
    old_raw, _ = _stable_events(originals, 93800, 94800, 95000, 'frozen')
    with pytest.raises(ValueError, match='原谱'):
        candidate_events(selected, candidates, old_raw, origin_ms=93800)
    raw, diagnostics = _stable_events(originals, 93800, 94800, 95000, 'frozen', preserve_raw_heads=True)
    assert len(raw) == 2 and raw[0]['end_ms'] == 94438
    assert len(diagnostics['retained_model_head_conflicts']) == 1
    playable = candidate_events(selected, candidates, raw, origin_ms=93800, raw_revision_id='raw-version')
    assert [e['start_ms'] for e in playable] == [93808, 94278]
    assert playable[0]['end_ms'] == 94208 and playable[1]['end_ms'] is None
    assert playable[1]['origins'][0]['note_id'] == raw[1]['id']
    assert playable[1]['origins'][0]['original_start_ms'] == 94278
    assert playable[1]['origins'][0]['original_lane'] == 0
    assert all(e['candidate_provenance']['kind'] == 'model' for e in playable)
    valid_events(playable, 93800, 94800, 95000)
    with pytest.raises(ValueError, match='占轨'):
        valid_events(raw, 93800, 94800, 95000)
    assert originals == before


def test_raw_head_preservation_keeps_invalid_data_and_duplicate_rejection():
    raw = [Note(100, 0, 800), Note(300, 0), Note(300, 0), Note(400, 5),
           Note(500, 1, 400), Note(1000, 2)]
    events, diagnostics = _stable_events(raw, 0, 1000, 1000, 'frozen', preserve_raw_heads=True)
    assert [(e['start_ms'], e['lane'], e['end_ms']) for e in events] == [(100., 0, 800.), (300., 0, None)]
    assert len(diagnostics['discarded_invalid']) == 3
    assert len(diagnostics['retained_model_head_conflicts']) == 1
