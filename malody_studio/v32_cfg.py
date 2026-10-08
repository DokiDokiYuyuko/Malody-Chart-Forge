"""Keep the local V32 two-stream decoder order compatible with HF CFG."""
from functools import wraps


def install_cfg_batch_order(model):
    """Upstream prepares [negative, positive]; HF consumes [positive, negative]."""
    if getattr(model, '_malody_cfg_batch_order', False):
        return
    original = model.prepare_inputs_for_generation

    @wraps(original)
    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        if kwargs.get('negative_prompt') is not None:
            import torch
            # Encoder outputs are identical copies. Only decoder streams and their
            # masks differ; put their caches in the correct order from prefill on.
            for key in ('input_ids', 'decoder_input_ids', 'decoder_attention_mask'):
                value = prepared.get(key)
                if value is not None:
                    if value.shape[0] % 2:
                        raise ValueError('V32 CFG requires an even decoder batch')
                    first, second = value.chunk(2, dim=0)
                    prepared[key] = torch.cat((second, first), dim=0)
        return prepared

    model.prepare_inputs_for_generation = prepare
    model._malody_cfg_batch_order = True
