import copy
import json
from pathlib import Path
import subprocess

import pytest

from malody_studio.density_trials import write_experimental_reference
from malody_studio.mapperatorinator import PYTHON


@pytest.mark.parametrize('points',[[[0,150],[1000,180]],[[119500,121.4]]])
def test_experimental_reference_parses_in_deployed_rosu_and_upstream_slider(tmp_path,points):
    if not PYTHON.is_file():pytest.skip('Deployed V32 parser runtime unavailable')
    reference={'points':points,'qualified':False,'experimental_only':True}
    before=copy.deepcopy(reference)
    path=write_experimental_reference(tmp_path/'reference.osu',reference)
    # Both real parsers matter: rosu accepts some defaults that the pinned
    # upstream slider parser requires explicitly (HP, AR, slider multiplier).
    script='''import json,sys
import rosu_pp_py
from slider import Beatmap
raw=rosu_pp_py.Beatmap(path=sys.argv[1])
upstream=Beatmap.from_path(sys.argv[1])
assert raw.cs==4 and raw.n_objects==0
assert upstream.circle_size==4 and upstream.hp_drain_rate==5
assert upstream.approach_rate==9 and upstream.slider_multiplier==1.4
assert upstream.slider_tick_rate==1
print(json.dumps({'mode':str(raw.mode),'points':[[p.offset.total_seconds()*1000,60000/p.ms_per_beat] for p in upstream.timing_points]}))
'''
    result=subprocess.run([str(PYTHON),'-c',script,str(path)],capture_output=True,text=True,check=True)
    parsed=json.loads(result.stdout)
    assert parsed['mode']=='GameMode.Mania'
    assert len(parsed['points'])==len(points)
    for actual,expected in zip(parsed['points'],points):assert actual==pytest.approx(expected)
    assert reference==before and not reference['qualified']


def test_experiment_freezes_non_clock_metadata_and_leaves_ordinary_requests_unchanged():
    from types import SimpleNamespace
    from malody_studio.mapperatorinator import build_worker_request
    from tools.mapperatorinator_worker import apply_experimental_conditioning
    options={'title':'Frozen song','artist':'Artist','seed':20261001,'ln_ratio':.15,
             'difficulties':['expert'],'_advanced_presets':[{'key':'trial','label':'trial','sr':5.9}]}
    ordinary=build_worker_request(Path('audio.wav'),Path('result'),options)
    assert 'experimental_conditioning' not in ordinary
    args=SimpleNamespace(hitsounded=False,approach_rate=5,bpm=121.4,offset=119500,beatmap_id=123)
    apply_experimental_conditioning(args,ordinary)
    assert not args.hitsounded and args.approach_rate==5 and args.beatmap_id==123
    request=build_worker_request(Path('audio.wav'),Path('result'),{**options,'experimental_parameter_snapshot':True})
    apply_experimental_conditioning(args,request)
    assert args.hitsounded and args.approach_rate==9 and args.beatmap_id is None
    assert args.bpm==121.4 and args.offset==119500
    assert args.hold_note_ratio==.15 and args.difficulty==5.9
    assert args.title_unicode=='Frozen song' and args.artist_unicode=='Artist'
    with pytest.raises(ValueError):
        apply_experimental_conditioning(args,{**request,'experimental_conditioning':{'bpm':120}})
