"""Headless inference adapter: no upstream WebUI, external update checks or training."""
import sys
import os
import math
import random
import gc
import numpy as np
import librosa
import torch
from omegaconf import OmegaConf
from .paths import VENDOR, WEIGHTS, MUG_CONFIG
from .charts import parse_osu_objects

sys.path.insert(0, str(VENDOR))

def mug_condition_features(options):
    ln_ratio = options['ln_ratio']
    features = {'sr': options.get('mug_difficulty', 8),
                'rank_status': options.get('mug_style', 'ranked'), 'ln_ratio': ln_ratio,
                'rc': int(ln_ratio < .1), 'hb': int(.1 <= ln_ratio < .4),
                'ln': int(ln_ratio >= .4)}
    pattern = options.get('pattern', 'balanced')
    # MuG's feature schema has no standalone Speed token. Speed uses the
    # supported Stream condition and is distinguished by the local lane planner.
    if pattern == 'speed':
        pattern = 'stream'
    if pattern != 'balanced':
        features[pattern] = 1
        features[pattern + '_ett'] = options.get('pattern_strength', 20)
    return features

class Engine:
    def __new__(cls):
        if os.environ.get('STARTRAIL_GPU_RESIDENT') != '1' and os.environ.get('STARTRAIL_RESIDENT_DISABLED') != '1':
            from .resident_mug import RemoteEngine
            return RemoteEngine()
        return super().__new__(cls)

    def __init__(self):
        self.model = None
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.config = None

    def load(self, progress):
        if self.model is not None:
            return
        if not WEIGHTS.exists() or WEIGHTS.stat().st_size != 1839231053:
            raise RuntimeError('模型权重缺失或下载未完成')
        from mug.util import instantiate_from_config
        config = OmegaConf.load(MUG_CONFIG)
        config.model.params.cond_stage_config.params.path_to_yaml = str(VENDOR / 'configs' / 'mug' / 'mania_beatmap_features.yaml')
        progress('加载生成模型', 12)
        torch.set_num_threads(4)
        model = instantiate_from_config(config.model)
        # This checksum-verified public checkpoint includes the Lightning state dictionary.
        state = torch.load(WEIGHTS, map_location='cpu', weights_only=False)
        incompatible = model.load_state_dict(state['state_dict'], strict=False)
        trainable_names = {name for name, _ in model.named_parameters()}
        missing = trainable_names.intersection(incompatible.missing_keys)
        if missing:
            raise RuntimeError('模型参数与配置不兼容：' + ', '.join(sorted(missing)[:5]))
        del state
        gc.collect()
        model.eval()
        model.to(self.device)
        model.model.wave_model.to('cpu')
        self.model, self.config = model, config

    def unload(self):
        self.model = None
        self.config = None
        gc.collect()
        if self.device == 'cuda':
            torch.cuda.empty_cache()

    def prepare(self, y, sr, progress):
        self.load(progress)
        params = self.config.data.params.common_params
        if sr != params.sr:
            y = librosa.resample(y, orig_sr=sr, target_sr=params.sr)
        # Exact feature transform used by the upstream inference code.
        mel = librosa.feature.melspectrogram(y=y, sr=params.sr, n_mels=params.n_mels,
                                             hop_length=params.n_fft // 4, n_fft=params.n_fft)
        mel = np.log1p(mel).astype(np.float16)
        ratio = 64  # audio frames per latent frame; note decoder upsamples latent by 8
        latent_length = (int(mel.shape[1] / ratio / 32) + 1) * 32
        self.model.z_length = latent_length
        mel = np.pad(mel, ((0, 0), (0, latent_length * ratio - mel.shape[1])))
        progress('提取音乐特征', 20)
        self.model.model.wave_model.to(self.device)
        try:
            with torch.no_grad():
                wave = self.model.model.wave_model(torch.from_numpy(mel.astype(np.float32))[None].to(self.device))
        finally:
            self.model.model.wave_model.to('cpu')
            if self.device == 'cuda':
                torch.cuda.empty_cache()
        return wave

    def generate(self, wave, preset, options, progress):
        from mug.util import feature_dict_to_embedding_ids
        from mug.diffusion.ddim import DDIMSampler
        from mug.data.convertor import BeatmapMeta, OsuManiaConvertor
        import yaml
        seed = options['seed']
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        features = mug_condition_features(options)
        schema = yaml.safe_load((VENDOR / 'configs' / 'mug' / 'mania_beatmap_features.yaml').read_text())
        ids = torch.tensor([feature_dict_to_embedding_ids(features, schema)], device=self.device)
        with torch.no_grad():
            c = self.model.model.cond_stage_model(ids)
            uc = self.model.model.cond_stage_model(torch.tensor([feature_dict_to_embedding_ids({}, schema)], device=self.device))
            sampler = DDIMSampler(self.model)
            steps = options['steps']
            samples, _ = sampler.sample(S=steps, c=c, w=wave, batch_size=1, verbose=False,
                                       unconditional_guidance_scale=options.get('mug_guidance', 1.5),
                                       unconditional_conditioning=uc, eta=options.get('mug_eta', 0),
                                       callback=lambda i: progress((i + 1) / steps))
            decoded = self.model.model.decode(samples)[0].cpu().numpy()
        params = self.config.data.params.common_params
        converter = OsuManiaConvertor(frame_ms=params.n_fft / 4 / params.sr * params.audio_note_window_ratio * 1000,
                                     max_frame=decoded.shape[-1], from_logits=False)
        meta = BeatmapMeta(path='', cs=4, game_mode=3, convertor=converter)
        notes = parse_osu_objects(converter.array_to_objects(decoded, meta))
        del samples, decoded
        if self.device == 'cuda':
            torch.cuda.empty_cache()
        return notes
