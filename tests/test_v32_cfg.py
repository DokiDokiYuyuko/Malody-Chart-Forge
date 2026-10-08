"""CPU check of the actual deployed HF formula and upstream preparation method."""
from pathlib import Path
import subprocess

import pytest


def test_v32_cfg_stream_order_with_actual_upstream_and_hf():
    root = Path(__file__).resolve().parents[1]
    python = root / 'runtime/mapperatorinator-venv/Scripts/python.exe'
    if not python.is_file():
        pytest.skip('deployed V32 CPU dependency runtime unavailable')
    script = r'''
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, 'vendor/Mapperatorinator')
from types import SimpleNamespace, MethodType
import torch
from transformers import ClassifierFreeGuidanceLogitsProcessor
from osuT5.osuT5.model.modeling_mapperatorinator import Mapperatorinator
from malody_studio.v32_cfg import install_cfg_batch_order

class Transformer:
    def prepare_inputs_for_generation(self, **kwargs):
        return {k: kwargs[k] for k in ('decoder_input_ids', 'decoder_attention_mask')}

model = SimpleNamespace(transformer=Transformer())
model.prepare_inputs_for_generation = MethodType(Mapperatorinator.prepare_inputs_for_generation, model)
positive = torch.tensor([[20, 21], [30, 31]])
negative = torch.tensor([[10, 11], [40, 41]])
mask = torch.ones_like(positive)
negative_mask = torch.tensor([[0, 1], [1, 0]])
kw = dict(decoder_attention_mask=mask, negative_prompt=negative, negative_prompt_attention_mask=negative_mask)
broken = model.prepare_inputs_for_generation(positive, **kw)
assert torch.equal(broken['decoder_input_ids'], torch.cat((negative, positive)))
hf = ClassifierFreeGuidanceLogitsProcessor(3.)
neg_logits, pos_logits = torch.tensor([[2., 0.]]), torch.tensor([[0., 2.]])
assert torch.equal(hf(torch.tensor([[1]]), torch.cat((neg_logits, pos_logits))), torch.tensor([[6., -4.]]))

install_cfg_batch_order(model)
wrapper = model.prepare_inputs_for_generation
install_cfg_batch_order(model)
assert model.prepare_inputs_for_generation is wrapper
fixed = model.prepare_inputs_for_generation(positive, **kw)
assert torch.equal(fixed['decoder_input_ids'], torch.cat((positive, negative)))
assert torch.equal(fixed['decoder_attention_mask'], torch.cat((mask, negative_mask)))
assert torch.equal(hf(torch.tensor([[1]]), torch.cat((pos_logits, neg_logits))), torch.tensor([[-4., 6.]]))
# cfg=1 has no negative_prompt; retain the original tensor and mask unchanged.
plain = model.prepare_inputs_for_generation(positive, decoder_attention_mask=mask)
assert plain['decoder_input_ids'] is positive and plain['decoder_attention_mask'] is mask
# Same default prompts cannot explain any claimed difficulty amplification.
assert torch.equal(hf(torch.tensor([[1]]), torch.cat((pos_logits, pos_logits))), pos_logits)
print('CFG CPU audit passed')
'''
    result = subprocess.run([str(python), '-'], input=script, text=True, capture_output=True,
                            cwd=root, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CFG CPU audit passed' in result.stdout


def test_cfg_adapter_is_part_of_deployed_execution_identity():
    from malody_studio.density_calibration import v32_execution_identity
    identity = v32_execution_identity()
    assert len(identity['adapter_sha256']['cfg_batch_order']) == 64
