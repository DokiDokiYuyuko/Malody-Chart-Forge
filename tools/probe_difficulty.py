import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import soundfile as sf
from malody_studio.mapperatorinator import read_osu
from malody_studio.difficulty import attacks, calibrate, PRESETS
from malody_studio.charts import chart_stats

root = Path(__file__).resolve().parents[1]
directory = root / 'outputs' / '9bbf9ef3d73443feb578a06d1ae8e34f'
request = json.loads((directory / 'v32-original' / 'worker-result.json').read_text())
master = read_osu(request['charts']['expert'])[0]
y, sr = sf.read(directory / 'analysis.wav', dtype='float32')
candidates = attacks(y, sr, master)
print('candidate attacks', len(candidates), flush=True)
for key in PRESETS:
    notes, adjustment = calibrate(candidates, len(y)/sr*1000, key, .15, 20261001)
    stats = chart_stats(notes, len(y)/sr)
    stats.pop('density')
    print(key, stats, adjustment, flush=True)
