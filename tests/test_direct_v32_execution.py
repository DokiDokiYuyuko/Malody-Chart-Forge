"""CPU adapter/packaging checks; model and Beat This inference are mocked."""
import copy
import json
import zipfile
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from malody_studio import direct_v32, simple_generation, nps_star_calibration


def test_simple_freezes_new_route_but_preserves_legacy_snapshot():
    new = simple_generation.freeze({'engine':'v32'})
    assert new['simple_generation_policy'] == simple_generation.direct_contract()
    assert new['strategy'] == 'independent'
    assert simple_generation.freeze(new) == new
    old = simple_generation.freeze({'engine':'v32','simple_generation_policy':simple_generation.contract()})
    assert 'direct_v32_policy' not in old
    assert simple_generation.freeze(old) == old


@pytest.mark.parametrize('trim',[False,True])
@pytest.mark.parametrize('creator',[None, 'Startrail', '  Custom chart author  '])
def test_simple_direct_adapter_and_archive_readback(tmp_path,monkeypatch,trim,creator):
    from malody_studio import mapperatorinator,resident,library,artwork,pipeline
    sr=44100
    pcm=np.zeros((sr*8,2),np.float32)
    pcm[:sr*6]=.1
    source=tmp_path/'input.wav'
    sf.write(source,pcm,sr,subtype='FLOAT')
    calls=[]
    def plan(directory,settings,variants,policy,progress):
        assert policy['beat_analysis_policy']['adapter']=='beat_this_final1'
        assert sf.info(Path(directory)/'source.wav').frames==8*sr
        return dict(id='frozen-plan',samples=8*sr,sections=[dict(id='full',core=[0,8*sr])]),dict(
            source=dict(effective_end_sample=6*sr),provenance=dict(adapter='beat_this_final1'))
    def worker(engine,message,progress):
        request=json.loads(Path(message['request_path']).read_text(encoding='utf-8'))
        calls.append(request)
        assert sf.info(request['audio']).duration==pytest.approx(8)
        assert len(request['presets'])==2
        assert 'timing_reference' not in request and 'timing_fallback_reference' not in request
        paths={}
        for i,r in enumerate(request['presets']):
            assert r['key']!='master'
            path=Path(request['output'])/(r['key']+'.osu')
            path.write_text('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n0,500,4,2,0,100,1,0\n[HitObjects]\n'+
                            '\n'.join(f'{64+(j%4)*128},192,{500+j*400},1,0,0:0:0:0:' for j in range(8+i*4))+'\n',encoding='utf-8')
            paths[r['key']]=str(path)
        return dict(charts=paths)
    monkeypatch.setattr(direct_v32,'make_simple_plan',plan)
    monkeypatch.setattr(direct_v32,'quality_events',lambda events,*args:dict(events=copy.deepcopy(events),summary={}))
    monkeypatch.setattr(mapperatorinator,'ready',lambda:True)
    monkeypatch.setattr(resident,'call',worker)
    monkeypatch.setattr(library,'publish',lambda *a,**k:None)
    monkeypatch.setattr(artwork,'prepare_artwork',lambda *_:(None,{}))
    options=simple_generation.freeze(dict(engine='v32',title='test',artist='test',seed=42,ln_ratio=.15,
        patterns=['balanced'],difficulties=['hard','expert'],tail_trim_enabled=trim))
    if creator is not None:
        options['creator']=creator
    report,archive=pipeline.run(source,tmp_path/'out',options,lambda *_:None)
    assert len(calls)==1
    assert len(report['charts'])==2
    assert not report['mother_chart'] and not report['density_thinning']
    assert [r['model_raw_notes'] for r in report['charts']]==[8,12]
    assert all(r['removed_notes']==0 for r in report['charts'])
    assert report['duration']==pytest.approx(6 if trim else 8)
    with zipfile.ZipFile(archive) as z:
        charts=[json.loads(z.read(name)) for name in z.namelist() if name.endswith('.mc')]
    assert sorted(sum('column' in n for n in c['note']) for c in charts)==[8,12]
    if creator is not None:
        assert report['creator']==report['generation_settings']['creator']==creator.strip()
        assert all(c['meta']['creator']==creator.strip() for c in charts)
        assert options['creator']==creator  # Caller snapshot stays frozen.
    else:
        assert 'creator' not in report
        assert all('Mapperatorinator' in c['meta']['creator'] for c in charts)


def test_advanced_direct_dispatch_bypasses_legacy_dynamic(monkeypatch):
    from malody_studio import advanced_generation
    sentinel=object()
    monkeypatch.setattr(direct_v32,'advanced_run',lambda *args:sentinel)
    monkeypatch.setattr(advanced_generation,'_run_dynamic',lambda *args:pytest.fail('entered mother route'))
    assert advanced_generation._run_untrimmed(None,None,{'_advanced':{'direct_v32_policy':{'frozen':True}}},None) is sentinel


def test_queue_finalizer_never_recalibrates_direct_raw_failure(tmp_path,monkeypatch):
    from malody_studio import quality_workflow
    policy=nps_star_calibration.freeze_policy()
    result=dict(direct_v32_policy=policy,advanced_result=[dict(kind='model_raw',events=[],provenance={})])
    monkeypatch.setattr(quality_workflow,'evidence_for',lambda *args:pytest.fail('legacy finalizer entered'))
    assert quality_workflow.finalize_generated(None,tmp_path,{'_advanced':dict(direct_v32_policy=policy,quality_policy={})},result) is result


def test_assembly_uses_chart_span_not_old_budget():
    from malody_studio import quality_workflow
    events=[dict(id='a',start_ms=1000,lane=0,end_ms=None),dict(id='b',start_ms=2000,lane=1,end_ms=3000)]
    density=nps_star_calibration.chart_span_density(__import__('malody_studio.advanced',fromlist=['as_notes']).as_notes(events),dict(min=.5,max=2))
    result=quality_workflow.assembly_density([dict(provenance=dict(density_validation=density))],events,10)
    assert result['measured_nps']==1 and result['status']=='in_range'


def test_advanced_keeps_raw_parent_and_all_candidate_heads(tmp_path,monkeypatch):
    policy=nps_star_calibration.freeze_policy()
    events=[dict(id='head-a',start_ms=1000,lane=0,end_ms=None),dict(id='head-b',start_ms=2000,lane=1,end_ms=None)]
    snapshot=dict(direct_v32_policy=policy,settings=dict(nps_ranges={'expert':dict(min=.5,max=5)}),
        segment=dict(start_sample=0,end_sample=44100*4),project=dict(duration=4),
        variants=[dict(key='balanced--expert',pattern='balanced',difficulty='expert')],section_plan=dict(id='plan'))
    monkeypatch.setattr(direct_v32,'infer',lambda *args:({'balanced--expert':events},[],[],set(),{}))
    monkeypatch.setattr(direct_v32,'quality_events',lambda raw,*args:dict(events=copy.deepcopy(raw),summary={}))
    result=direct_v32.advanced_run(None,tmp_path,dict(_advanced=snapshot),lambda *_:None)
    raw,candidate=result['advanced_result']
    assert raw['kind']=='model_raw' and not raw['activate_initial']
    assert candidate['kind']=='direct' and candidate['activate_initial']
    assert candidate['provenance']['parent']==raw['id']
    assert [e['id'] for e in raw['events']]==[e['id'] for e in candidate['events']]
    assert all(e['origins'][0]['revision_id']==raw['id'] for e in candidate['events'])


def test_incomplete_song_reports_successful_requests_and_keeps_complete_song_guard(tmp_path,monkeypatch):
    records = [dict(key=f'region{i}',status='generated' if i<4 else 'failed') for i in range(5)]
    errors = [dict(variant='balanced--expert',request_key='region4',core=[7880670,12657153],error='bad native tail')]
    monkeypatch.setattr(direct_v32,'infer',lambda *args:({'balanced--expert':[]},records,errors,{'balanced--expert'},{}))
    snapshot = dict(direct_v32_policy={},settings={},segment=dict(start_sample=0,end_sample=12657153),
                    variants=[dict(key='balanced--expert',pattern='balanced',difficulty='expert')])
    monkeypatch.setattr(direct_v32,'quality_events',lambda *args:pytest.fail('Incomplete song must not become a finished chart'))
    with pytest.raises(ValueError,match='4/5 个请求已生成') as error:
        direct_v32.advanced_run(None,tmp_path,dict(_advanced=snapshot),lambda *_:None)
    assert 'region4' in str(error.value) and '7880670' in str(error.value)


def test_one_token_boundary_hold_overlap_is_repaired_only_in_candidate():
    events=[dict(id='a',start_ms=168686,lane=2,end_ms=168866),dict(id='b',start_ms=168856,lane=2,end_ms=169036)]
    records=[dict(variant='v',status='generated',core=[0,7446285]),dict(variant='v',status='generated',core=[7446285,12657153])]
    repaired,decisions=direct_v32.stitch_boundary_tails(events,records,'v')
    assert events[0]['end_ms']==168866 and repaired[0]['end_ms']==168856
    assert [(e['id'],e['start_ms'],e['lane']) for e in repaired]==[(e['id'],e['start_ms'],e['lane']) for e in events]
    assert decisions[0]['overlap_ms']==10
    events[0]['end_ms']=168900
    unchanged,decisions=direct_v32.stitch_boundary_tails(events,records,'v')
    assert unchanged==events and decisions==[]
    events[0]['end_ms']=168866
    unchanged,decisions=direct_v32.stitch_boundary_tails(events,[dict(variant='v',status='generated',core=[0,12657153])],'v')
    assert unchanged==events and decisions==[]


def test_native_cache_reuse_requires_same_frozen_source_settings_and_requests(tmp_path,monkeypatch):
    monkeypatch.setattr(direct_v32,'ROOT',tmp_path)
    parent='a'*32;folder=tmp_path/'outputs'/parent;folder.mkdir(parents=True)
    snapshot=dict(retry_of=parent,settings={'seed':1},direct_v32_policy={'version':'test'},variants=[dict(key='v')])
    requests=[dict(key='r',variant='v',seed=1)]
    cache=dict(policy=snapshot['direct_v32_policy'],source_pcm_sha256='pcm',failed_variants=[],errors=[],
               requests=[dict(**requests[0],status='generated',owned_heads=1)],raw={'v':[dict(id='head')]})
    (folder/'queue-worker.json').write_text(json.dumps(dict(options=dict(_advanced=snapshot))),encoding='utf-8')
    (folder/'direct-generation-cache.json').write_text(json.dumps(cache),encoding='utf-8')
    assert direct_v32.reusable_cache(snapshot,requests,'pcm') is not None
    assert direct_v32.reusable_cache(snapshot,requests,'other') is None
    assert direct_v32.reusable_cache({**snapshot,'settings':{'seed':2}},requests,'pcm') is None
    assert direct_v32.reusable_cache(snapshot,[{**requests[0],'seed':2}],'pcm') is None
