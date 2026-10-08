import copy
import json
from pathlib import Path

import pytest

from malody_studio.charts import Note
from malody_studio.v32_rhythm import read_rhythm, attach_notes, hydrate_cache, transfer_notes


def sidecar(path, events, *, types_first=False, timing=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload={'types_first':types_first,'keycount':4,'events':events,
             'timing':timing if timing is not None else ['0,500,4,2,0,100,1,0']}
    path.write_text(json.dumps(payload),encoding='utf-8')
    return payload


@pytest.mark.parametrize('types_first',[False,True])
def test_native_snap_owned_by_each_head_not_neighbor_hold_tail(tmp_path,types_first):
    groups=[('hold_note',100,4,64),('circle',120,2,192),('hold_note_end',300,1,64),('circle',310,8,320)]
    events=[]
    for kind,t,snap,x in groups:
        attrs=[['t',t],['snap',snap],['pos_x',x]]
        events.extend([[kind,0],*attrs] if types_first else [*attrs,[kind,0]])
    path=tmp_path/'model-events.json';before=sidecar(path,events,types_first=types_first)
    rows,missing=read_rhythm(path)
    assert missing is None
    assert [(r['start_ms'],r['lane'],r['model_rhythm']['snap_divisor']) for r in rows]==[(100,0,4),(120,1,2),(310,2,8)]
    assert rows[0]['model_rhythm']['grid_time_ms']==125 # snap4 excludes snap2 ticks
    assert rows[1]['model_rhythm']['grid_time_ms']==250 # snap2 excludes whole beats
    assert rows[0]['model_rhythm']['grid_error_ms']==-25 # preserve large error
    notes=[Note(100,0,300),Note(120,1),Note(310,2)]
    original=[(n.start,n.lane,n.end) for n in notes]
    attach_notes(notes,rows)
    from malody_studio.advanced_generation import owned
    events,_=owned(notes,0,1000,1000)
    assert [(e['start_ms'],e['lane'],e['end_ms']) for e in events]==original
    assert events[0]['model_rhythm']==rows[0]['model_rhythm']
    assert not any(k in events[0]['model_rhythm'] for k in ('certified','model_group_id'))
    assert json.loads(path.read_text())==before


def test_native_timing_seams_inherited_points_and_fingerprint(tmp_path):
    path=tmp_path/'model-events.json'
    sidecar(path,[['t',740],['snap',4],['pos_x',64],['circle',0],
                  ['t',1090],['snap',4],['pos_x',192],['circle',0]],
            timing=['0,500,4,2,0,100,1,0','600,-50,4,2,0,100,0,0','1000,400,3,2,0,100,1,0'])
    rows,_=read_rhythm(path)
    assert rows[0]['model_rhythm']['beat_length_ms']==500
    assert rows[0]['model_rhythm']['timing_anchor_ms']==0
    assert rows[1]['model_rhythm']['grid_time_ms']==1100
    assert rows[1]['model_rhythm']['beat_length_ms']==400
    assert rows[1]['model_rhythm']['timing_anchor_ms']==1000
    assert rows[0]['model_rhythm']['timing_fingerprint']==rows[1]['model_rhythm']['timing_fingerprint']
    assert rows[0]['model_rhythm']['timing_source']=='model_output'


def test_missing_sidecar_or_tag_is_explicit_and_never_guessed(tmp_path):
    rows,missing=read_rhythm(tmp_path/'absent.json')
    notes=attach_notes([Note(100,0)],rows,missing)
    assert notes[0].model_rhythm['reason']=='model_events_sidecar_missing'
    path=tmp_path/'model-events.json'
    sidecar(path,[['t',100],['pos_x',64],['circle',0]])
    rows,_=read_rhythm(path)
    assert rows[0]['model_rhythm']['snap_divisor'] is None
    assert rows[0]['model_rhythm']['grid_time_ms'] is None
    assert rows[0]['model_rhythm']['available'] is False


def test_parallel_processor_clock_keeps_inherited_head_evidence(tmp_path):
    path = tmp_path/'model-events.json'
    payload = sidecar(path,[['t',100],['snap',4],['pos_x',64],['circle',0],
                           ['snap',4],['pos_x',192],['circle',0]])
    payload['event_times'] = [100]*7
    path.write_text(json.dumps(payload),encoding='utf-8')
    rows,missing = read_rhythm(path)
    assert missing is None
    assert [(r['start_ms'],r['lane']) for r in rows] == [(100,0),(100,1)]
    payload['event_times'] = [100]
    path.write_text(json.dumps(payload),encoding='utf-8')
    rows,missing = read_rhythm(path)
    assert rows == [] and missing['reason'] == 'model_events_sidecar_invalid'


def test_worker_reader_to_raw_and_candidate_events_preserves_evidence(tmp_path):
    chart=tmp_path/'master'/'master.osu'
    sidecar(chart.parent/'model-events.json',[['t',100],['snap',4],['pos_x',64],['hold_note',0],
            ['t',300],['snap',1],['pos_x',64],['hold_note_end',0]])
    chart.write_text('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n0,500,4,2,0,100,1,0\n[HitObjects]\n64,192,100,128,0,300:0:0:0:0:\n',encoding='utf-8')
    from malody_studio.mapperatorinator import read_worker_charts
    from malody_studio.advanced_generation import _stable_events
    from malody_studio.quality_workflow import candidate_events
    charts,_=read_worker_charts({'charts':{'master':str(chart)}})
    notes=charts['master'][0]
    raw,_=_stable_events(notes,0,1000,1000,'stable-recipe')
    candidate=candidate_events([Note(100,2,250)],[],raw,source_role='vocals')
    assert candidate[0]['model_rhythm']==raw[0]['model_rhythm']
    assert candidate[0]['start_ms']==100 and candidate[0]['lane']==2
    assert candidate[0]['model_rhythm']['model_time_ms']==100
    assert raw[0]['end_ms']==300


def test_old_cache_selected_retry_uses_only_its_sidecar_without_mutation(tmp_path):
    paths={}
    for key,snap in [('span',2),('span__density_retry1',4),('span__density_retry2',8)]:
        chart=tmp_path/key/'out.osu';paths[key]=str(chart)
        sidecar(chart.parent/'model-events.json',[['t',100],['snap',snap],['pos_x',64],['circle',0]])
    cache={'raw':{'hard':[[100,0,None]]},'metadata':{'charts':paths},'source':{'source_role':'vocals'},
           'records':[{'difficulty_key':'hard','inference_group':'span','core':[0,44100],
                       'selected_round':1,'density_policy':{'frozen':True}}]}
    before=copy.deepcopy(cache)
    raw={'hard':[Note(*row) for row in cache['raw']['hard']]}
    hydrate_cache(cache,raw,tmp_path)
    assert raw['hard'][0].model_rhythm['snap_divisor']==4
    assert raw['hard'][0].model_rhythm['source_role']=='vocals'
    assert cache==before
    selected=transfer_notes([Note(100,3)],raw['hard'])
    assert selected[0].model_rhythm==raw['hard'][0].model_rhythm


def test_legacy_quality_freeze_validates_its_original_contract(monkeypatch):
    from malody_studio import chart_quality
    from malody_studio.advanced_generation import validate_frozen_policies
    legacy={'version':'chart-quality-v2','hash':'original-frozen-v2-hash'}
    calls=[]
    def contract(version=None):
        calls.append(version)
        return legacy if version=='chart-quality-v2' else {'version':'chart-quality-v3','hash':'current'}
    monkeypatch.setattr(chart_quality,'contract',contract)
    validate_frozen_policies({'quality_policy':copy.deepcopy(legacy)})
    assert calls==['chart-quality-v2']
    with pytest.raises(ValueError):
        validate_frozen_policies({'quality_policy':{**legacy,'hash':'corrupted'}})


def test_rule_revision_preserves_absent_and_existing_rhythm_without_backfill():
    original=[Note(100,0),Note(200,1,400)]
    proof={'version':'v32-rhythm-v1','snap_divisor':4,'model_time_ms':200,'source_role':'vocals'}
    object.__setattr__(original[1],'model_rhythm',copy.deepcopy(proof))
    revised=transfer_notes([Note(100,0),Note(200,1,350)],original,unavailable_on_missing=False)
    from malody_studio.advanced_generation import owned
    events,_=owned(revised,0,1000,1000)
    assert 'model_rhythm' not in events[0]
    assert events[1]['model_rhythm']==proof


def test_real_v2_and_v3_quality_freezes_validate_and_unknown_version_fails():
    from malody_studio.chart_quality import contract
    from malody_studio.advanced_generation import validate_frozen_policies
    for version in ('chart-quality-v2','chart-quality-v3'):
        frozen=contract(version)
        validate_frozen_policies({'quality_policy':frozen})
        with pytest.raises(ValueError):
            validate_frozen_policies({'quality_policy':{**frozen,'hash':'bad'}})
    with pytest.raises(ValueError):
        validate_frozen_policies({'quality_policy':{'version':'unknown-quality','hash':'bad'}})


@pytest.mark.parametrize('strategy',['native','fast'])
def test_sectioned_raw_revision_and_selected_cache_keep_rhythm(tmp_path,monkeypatch,strategy):
    import numpy as np
    import soundfile as sf
    from malody_studio import section_plan as sp, mapperatorinator, advanced_generation as gen
    from malody_studio.advanced import defaults
    monkeypatch.setattr(sp,'rhythm_features',lambda data:([
        {'start_sample':i*sp.SR,'end_sample':(i+1)*sp.SR,'active_fraction':1.,'onset_rate':8.,'rms':.1}
        for i in range(8)],np.ones(1600),[],.005))
    source=tmp_path/'source.wav'
    sf.write(source,np.full((8*sp.SR,2),.1,np.float32),sp.SR,subtype='FLOAT')
    settings=defaults();settings.update(strategy=strategy,dynamic_enabled=False,title='test',artist='test')
    plan=sp.build_plan(source,settings,{'points':[[0,150]],'manual':True,'bpm':150})
    def infer(source,folder,options,progress):
        charts={}
        for request in options['_advanced_presets']:
            path=folder/request['key']/'out.osu'
            sidecar(path.parent/'model-events.json',[['t',100],['snap',4],['pos_x',64],['circle',0]])
            path.write_text('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n0,500,4,2,0,100,1,0\n[HitObjects]\n64,192,100,1,0,0:0:0:0:\n',encoding='utf-8')
            charts[request['key']]=str(path)
        from malody_studio.mapperatorinator import read_worker_charts
        return read_worker_charts({'charts':charts})
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    result=gen.generate_sectioned(source,tmp_path/'out',settings,
        [{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],plan,lambda *_:None,raw_only=True)
    assert result['advanced_result'][0]['events'][0]['model_rhythm']['snap_divisor']==4
    cache=json.loads((tmp_path/'out'/'balanced-mother.json').read_text(encoding='utf-8'))
    assert cache['model_rhythm']['hard'][0]['model_rhythm']['model_time_ms']==100
    # The separate classic path also plans lanes/holds from the native head;
    # metadata must survive that planning, not just the dynamic raw-only path.
    settings.pop('density_policy',None)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}]}
    legacy=gen.run(source,tmp_path/'classic',{'_advanced':snapshot},lambda *_:None)
    assert legacy['advanced_result'][0]['events'][0]['model_rhythm']['snap_divisor']==4
    assert legacy['advanced_result'][0]['events'][0]['start_ms']==100
