import json
import random
from pathlib import Path

import pytest
import torch
import torch.nn.functional as F

from malody_studio import v32_fast_decode as fd
from malody_studio import v32_grammar_mask as gm

LAYOUT = json.loads((Path(__file__).parent / 'fixtures' / 'v32_mania_token_layout.json').read_text(encoding='utf-8'))
R = LAYOUT['ranges']
TS0, TS1 = R['t']
V = LAYOUT['vocab_out']
SOS_IDS = [LAYOUT['sos'], *LAYOUT['context_sos'].values()]


def upstream_sample(logits, top_p):
    """The pinned upstream nucleus arithmetic (compiled_decode._sample, top_k=0, temperature=1)."""
    if 0 < top_p < 1.0:
        sorted_logits, sorted_idx = torch.sort(logits, descending=True, dim=-1)
        cum_probs = F.softmax(sorted_logits, dim=-1).cumsum(dim=-1)
        remove = cum_probs > top_p
        remove[..., 1:] = remove[..., :-1].clone()
        remove[..., 0] = False
        sorted_logits = sorted_logits.masked_fill(remove, -float('inf'))
        logits = torch.full_like(logits, -float('inf'))
        logits.scatter_(-1, sorted_idx, sorted_logits)
    probs = F.softmax(logits, dim=-1)
    return torch.multinomial(probs.view(-1, probs.size(-1)), num_samples=1).view(probs.size(0), -1)


def upstream_monotonic(ids, scores):
    """The pinned upstream MonotonicTimeShiftLogitsProcessor for one row."""
    is_ts = [TS0 <= t < TS1 for t in ids]
    last_ts = max((i for i, hit in enumerate(is_ts) if hit), default=-1)
    last_sos = max((i for i, t in enumerate(ids) if t in SOS_IDS), default=-1)
    if last_ts != -1 and last_ts > last_sos:
        scores[:, TS0:ids[last_ts]] = float('-inf')
    return scores


def bf16_like_logits(generator):
    # Model logits arrive as bf16 values, so exact ties are common and must sort the same way.
    return (torch.randn((1, V), generator=generator) * 4).to(torch.bfloat16).float()


def random_ids(rng, length):
    pool = [TS0, TS0 + 3, TS0 + 40, TS1 - 1, *SOS_IDS, LAYOUT['eos'], R['snap'][0], R['column'][0],
            R['column'][0] + 2, R['hitsound'][0], R['volume'][0], R['circle'][0], R['hold_note'][0],
            R['hold_note_end'][0], R['hold_note_sustain'][0]]
    return [rng.choice(pool) for _ in range(length)]


@pytest.mark.parametrize('top_p', [0.9, 0.5, 1.0])
def test_nucleus_sampling_draws_the_upstream_tokens(top_p):
    generator = torch.Generator().manual_seed(7)
    for step in range(200):
        logits = bf16_like_logits(generator)
        logits[:, TS0:TS0 + step] = float('-inf')
        torch.manual_seed(1000 + step)
        expected = upstream_sample(logits.clone(), top_p)
        torch.manual_seed(1000 + step)
        assert torch.equal(fd.sample_top_p(logits.clone(), top_p), expected)


def test_time_shift_floor_tracks_the_upstream_monotonic_mask():
    rng = random.Random(3)
    for _ in range(300):
        ids = random_ids(rng, rng.randint(0, 12))
        floor = fd.TimeShiftFloor(TS0, TS1, SOS_IDS).reset(ids[:len(ids) // 2])
        for token in ids[len(ids) // 2:]:
            floor.update(token)
        expected = upstream_monotonic(ids, torch.zeros((1, V)))
        actual, _ = fd.masked_scores(torch.zeros((1, V)), 1.0, TS0, floor.bound())
        assert torch.equal(actual, expected), ids


@pytest.mark.parametrize('lookback_end', [TS0, TS0 + 25])
def test_masked_scores_equal_the_upstream_processor_chain(lookback_end):
    tables = gm.Tables(LAYOUT, 4)
    reference = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    illegal = ~reference.legal
    rng, generator = random.Random(11), torch.Generator().manual_seed(11)
    states, tops = [], []
    for _ in range(400):
        ids = random_ids(rng, rng.randint(1, 9))
        logits = bf16_like_logits(generator)
        # Upstream order: monotonic on raw logits, temperature, lookback mask, grammar mask.
        expected = upstream_monotonic(ids, logits.clone()) / 0.9
        expected[:, TS0:lookback_end] = float('-inf')
        expected = reference(torch.tensor([ids]), expected)
        floor = fd.TimeShiftFloor(TS0, TS1, SOS_IDS).reset(ids)
        state = tables.state(ids[-5:])
        actual, top = fd.masked_scores(logits, 0.9, TS0, max(lookback_end, floor.bound()), illegal[state])
        assert torch.equal(actual, expected), ids
        states.append(state)
        tops.append(top)
    steps, top_masked, first, first_top, fallbacks, mismatch = reference.counts.tolist()
    bad = illegal[torch.tensor(states), torch.cat(tops)].long()
    assert (steps, top_masked, first, first_top) == (len(states), int(bad.sum()), 1, int(bad[0]))
    assert fallbacks == mismatch == 0 and 0 < top_masked < steps


def test_every_grammar_state_keeps_a_legal_token_outside_the_time_shift_range():
    # masked_scores omits upstream's "nothing legal left" fallback; _plan relies on this property.
    tables = gm.Tables(LAYOUT, 4)
    assert not any(all(TS0 <= i < TS1 for i in ids) for ids in tables.legal_ids.values())


def test_environment_switch_restores_the_upstream_loop(monkeypatch):
    assert fd.enabled()
    monkeypatch.setenv(fd.DISABLE_ENV, '0')
    assert not fd.enabled()
    calls = []
    result = fd.generate('model', 'tokenizer', {'decoder_input_ids': torch.zeros((1, 3), dtype=torch.long)}, {},
                         upstream=lambda *args: calls.append(args) or 'upstream')
    assert result == 'upstream' and len(calls) == 1
