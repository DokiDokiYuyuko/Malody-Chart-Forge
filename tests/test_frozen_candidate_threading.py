import pytest

from malody_studio.advanced_generation import frozen_candidate_options, validate_frozen_policies
from malody_studio.quality_workflow import candidate_contract


@pytest.mark.parametrize('version', ['evidenced-single-heads-v1', 'evidenced-single-heads-v2', 'model-heads-only-v3'])
def test_supported_frozen_candidate_contracts_remain_exact(version):
    policy = candidate_contract(version)
    validate_frozen_policies({'candidate_policy': policy})
    changed = {**policy, 'extra_holds': True}
    with pytest.raises(ValueError):
        validate_frozen_policies({'candidate_policy': changed})


def test_unknown_candidate_version_rejected():
    with pytest.raises(ValueError):
        validate_frozen_policies({'candidate_policy': {'version': 'future'}})


def test_frozen_candidate_options_preserve_legacy_and_new_selection():
    assert frozen_candidate_options({}) == {'selection_policy': 'model_only', 'audio_vote_cap': None}
    assert frozen_candidate_options({'candidate_policy': candidate_contract('evidenced-single-heads-v1')}) == {
        'selection_policy': 'ranked_beam', 'audio_vote_cap': None}
    assert frozen_candidate_options({'candidate_policy': candidate_contract()}) == {
        'selection_policy': 'model_only', 'audio_vote_cap': 0.0}
    assert frozen_candidate_options({'candidate_policy': candidate_contract('evidenced-single-heads-v2')}) == {
        'selection_policy': 'model_skeleton_then_audio', 'audio_vote_cap': 1.0}
