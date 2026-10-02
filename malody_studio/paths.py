import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name in ('cache', 'uploads', 'outputs', 'logs'):
    (ROOT / name).mkdir(exist_ok=True)
os.environ['TEMP'] = str(ROOT / 'cache')
os.environ['TMP'] = str(ROOT / 'cache')
os.environ['HF_HOME'] = str(ROOT / 'cache' / 'huggingface')
os.environ['TORCH_HOME'] = str(ROOT / 'cache' / 'torch')
os.environ['NUMBA_CACHE_DIR'] = str(ROOT / 'cache' / 'numba')
VENDOR = ROOT / 'vendor' / 'Mug-Diffusion'
MODEL_ROOT = ROOT / 'models'
WEIGHTS = MODEL_ROOT / 'mug-diffusion' / 'v1.0.0' / 'model.ckpt'
MUG_CONFIG = WEIGHTS.parent / 'model.yaml'

from .difficulty import PRESETS
