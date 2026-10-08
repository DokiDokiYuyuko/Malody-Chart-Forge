import copy

import numpy as np
import pytest
import soundfile as sf

from malody_studio import advanced, advanced_generation, audio_bounds, direct_v32, nps_star_calibration, paired_timing, section_plan, stem_generation


@pytest.fixture
def direct_stem_case(tmp_path, monkeypatch):
    project_directory=tmp_path/'project'
    project_directory.mkdir()
    original=project_directory/'source.wav'
    sf.write(original,np.full((advanced.SR*2,2),.1,dtype=np.float32),advanced.SR,subtype='FLOAT')
    stem_id='b'*64
    stem_directory=project_directory/'stems'/stem_id
    stem_directory.mkdir(parents=True)
    stems=[];descriptors=[]
    for role,source_id in (('vocals','v-source'),('accompaniment','a-source')):
        path=stem_directory/(role+'.wav')
        sf.write(path,np.full((advanced.SR*2,2),.05,dtype=np.float32),advanced.SR,subtype='FLOAT')
        row={'role':role,'source_id':source_id,'file':path.name,'path':str(path),'pcm_sha':role[0]*64,
             'file_sha256':role[0]*64,'frames':advanced.SR*2}
        stems.append(row)
        descriptors.append({'source_role':role,'source_id':source_id,'pcm_sha':row['pcm_sha']})
    manifest={'id':stem_id,'recipe_hash':'d'*64,'source_pcm_sha':'c'*64,'frame_count':advanced.SR*2,
              'sample_rate':advanced.SR,'stems':stems}
    advanced.atomic(stem_directory/'manifest.json',manifest)
    settings=advanced.defaults()
    settings.update(engine='v32',strategy='independent',seed=123)
    # A frozen historical snapshot has no fusion mode and must keep the strict union.
    for key in ('fusion_mode','fusion_primary'):settings.pop(key)
    policy={'version':nps_star_calibration.VERSION,'mapping_id':'frozen-map','mapping_sha256':'e'*64}
    variants=[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'},
              {'key':'balanced--expert','pattern':'balanced','difficulty':'expert'}]
    project={'id':'a'*32,'samples':advanced.SR*2,'duration':2.,'source_pcm_sha256':'c'*64,
             'tail_trim':{'enabled':False}}
    segment={'id':'f'*32,'start_sample':0,'end_sample':advanced.SR*2,'included':True}
    old_raw={'id':'9'*32,'kind':'stem_raw','variant':'balanced--hard','range':[0,advanced.SR*2],
             'events':[{'id':'old','start_ms':50,'lane':3,'end_ms':None}],
             'provenance':{'source_id':'v-source','pcm_sha':'v'*64,'source_role':'vocals'}}
    snapshot={'project':project,'segment':segment,'settings':settings,'variants':variants,
              'original_source':{'path':str(original)},'input_sources':descriptors,'stem_set_id':stem_id,
              'section_plan':{'id':'plan-id','sections':[],'source_pcm_sha':'c'*64},
              'direct_v32_policy':policy,'density_policy':{'policy_hash':'legacy-density'},
              'quality_policy':{'version':'test-quality','hash':'quality-hash'},
              'candidate_policy':{'selection_policy':'model_only','audio_vote_cap':0.},
              'auto_fuse':True,'retry_of':'8'*32,'reusable_raw_revisions':[old_raw]}
    options={'engine':'v32','_advanced':snapshot}
    monkeypatch.setattr(stem_generation,'ROOT',tmp_path)
    monkeypatch.setattr(stem_generation,'validate_manifest',lambda *_args,**_kwargs:manifest)
    monkeypatch.setattr(stem_generation,'materialize_stems',lambda *_args:pytest.fail('explicit source should not materialize stems'))
    monkeypatch.setattr(stem_generation,'ensure_stems',lambda *_args:pytest.fail('explicit source should not separate'))
    monkeypatch.setattr(stem_generation,'_find_existing',lambda *_args:pytest.fail('direct mode reused an old revision'))
    monkeypatch.setattr(stem_generation,'_checkpoint',lambda *_args:pytest.fail('direct mode reused an old checkpoint'))
    monkeypatch.setattr(stem_generation,'_audio_evidence',lambda *_args:pytest.fail('direct mode should not run legacy evidence fusion'))
    monkeypatch.setattr(stem_generation,'fuse_revisions',lambda *_args,**_kwargs:pytest.fail('direct mode entered legacy budget fusion'))
    monkeypatch.setattr(nps_star_calibration,'load_mapping',lambda _policy:{})
    monkeypatch.setattr(direct_v32,'quality_events',lambda events,*_args,**_kwargs:{
        'events':copy.deepcopy(events),'summary':{'preserved_heads':len(events)},'decisions':[],'evidence_id':'quality-evidence'})
    monkeypatch.setattr(advanced_generation,'validate_frozen_policies',lambda _snapshot:None)
    monkeypatch.setattr(advanced_generation,'frozen_candidate_options',lambda _snapshot:{'selection_policy':'model_only','audio_vote_cap':0.})
    monkeypatch.setattr(section_plan,'validate_plan',lambda *_args:None)
    monkeypatch.setattr(audio_bounds,'content_end',lambda _project:advanced.SR*2)
    monkeypatch.setattr(paired_timing,'missing_model_timing',lambda *_args:pytest.fail('direct mode entered paired-timing recovery'))
    return tmp_path,original,options,manifest,variants


def test_direct_stem_path_keeps_new_raws_and_fuses_every_head_without_legacy_paths(direct_stem_case,monkeypatch):
    tmp_path,original,options,_manifest,variants=direct_stem_case
    calls=[]

    def generate(source,directory,local_options,_progress):
        snapshot=local_options['_advanced'];role=snapshot['source']['source_role']
        calls.append((role,[row['key'] for row in snapshot['variants']]))
        output=[]
        for variant in snapshot['variants']:
            difficulty=variant['key'].split('--')[1]
            if difficulty=='hard':start=120.
            else:start=130. if role=='vocals' else 175.
            lane=0 if role=='vocals' else 1
            output.append({'variant':variant['key'],'events':[{'id':role+'-'+difficulty,'start_ms':start,
                'end_ms':None,'lane':lane}],'settings':snapshot['settings'],'kind':'stem_raw','provenance':{}})
        return {'advanced_result':output,'errors':[]}

    monkeypatch.setattr(advanced_generation,'run',generate)
    directory=tmp_path/'job';directory.mkdir()
    result=stem_generation.run(original,directory,options,lambda *_args:None)

    assert calls==[('vocals',['balanced--hard','balanced--expert']),
                   ('accompaniment',['balanced--hard','balanced--expert'])]
    fused=[row for row in result['advanced_result'] if row['kind']=='fusion']
    raw=[row for row in result['advanced_result'] if row['kind']=='stem_raw']
    assert len(raw)==4 and len(fused)==2
    assert result['reused_revisions']==[] and result['timing_recoveries']==[]
    assert result['errors']==[]
    assert result['direct_v32_policy']==options['_advanced']['direct_v32_policy']
    for row in fused:
        assert len(row['events'])==2
        assert row['provenance']['raw_model_heads']==row['provenance']['fused_model_heads']==2
        assert row['provenance']['preservation_verified'] is True
        assert row['provenance']['structure_validation']=='passed'
        assert row['provenance']['direct_v32_policy']==options['_advanced']['direct_v32_policy']
        assert row['provenance']['quality_policy']==options['_advanced']['quality_policy']
        assert row['provenance']['candidate_policy']==options['_advanced']['candidate_policy']
        assert row['provenance']['density_validation']['denominator']=='first_head_to_last_head_or_tail'
        assert {event['lane'] for event in row['events']}=={0,1}
        assert all(len(event['origins'])==1 for event in row['events'])
    hard=next(row for row in fused if row['variant']=='balanced--hard')
    assert [event['start_ms'] for event in hard['events']]==[120.,120.]


def test_direct_stem_conflict_keeps_raw_candidates_and_reports_fusion_failure(direct_stem_case,monkeypatch):
    tmp_path,original,options,_manifest,variants=direct_stem_case
    options['_advanced']['variants']=[variants[0]]

    def generate(_source,_directory,local_options,_progress):
        role=local_options['_advanced']['source']['source_role']
        return {'advanced_result':[{'variant':'balanced--hard','events':[{'id':role,'start_ms':100.,
                'end_ms':None,'lane':0}],'settings':local_options['_advanced']['settings'],
                'kind':'stem_raw','provenance':{}}],'errors':[]}

    monkeypatch.setattr(advanced_generation,'run',generate)
    directory=tmp_path/'conflict-job';directory.mkdir()
    result=stem_generation.run(original,directory,options,lambda *_args:None)

    assert [row['kind'] for row in result['advanced_result']]==['stem_raw','stem_raw']
    assert len([row for row in result['advanced_result'] if row['kind']=='fusion'])==0
    failure=next(row for row in result['errors'] if row.get('stage')=='direct_fusion')
    assert failure['fusion_failed'] and failure['raw_preserved']
    assert len(failure['raw_revision_ids'])==2


def _conflicting_generate(local_options):
    snapshot=local_options['_advanced'];role=snapshot['source']['source_role']
    output=[]
    for variant in snapshot['variants']:
        if role=='vocals':
            events=[{'id':'v1','start_ms':100.,'end_ms':None,'lane':0},{'id':'v2','start_ms':300.,'end_ms':900.,'lane':1}]
        else:
            events=[{'id':'a1','start_ms':100.,'end_ms':None,'lane':0},{'id':'a2','start_ms':500.,'end_ms':None,'lane':1},
                    {'id':'a3','start_ms':50.,'end_ms':400.,'lane':3},{'id':'a4','start_ms':450.,'end_ms':None,'lane':3}]
        output.append({'variant':variant['key'],'events':events,'settings':snapshot['settings'],'kind':'stem_raw','provenance':{}})
    return {'advanced_result':output,'errors':[]}


def _fused(direct_stem_case,monkeypatch,**fusion):
    tmp_path,original,options,_manifest,variants=direct_stem_case
    options['_advanced']['variants']=[variants[1]]
    options['_advanced']['settings'].update(fusion)
    monkeypatch.setattr(advanced_generation,'run',lambda _s,_d,local,_p:_conflicting_generate(local))
    directory=tmp_path/('job-'+'-'.join(v if isinstance(v,str) else k for k,v in fusion.items()));directory.mkdir()
    result=stem_generation.run(original,directory,options,lambda *_args:None)
    assert result['errors']==[]
    return next(row for row in result['advanced_result'] if row['kind']=='fusion')


def test_legacy_snapshot_without_mode_keeps_strict_union_policy(direct_stem_case,monkeypatch):
    tmp_path,original,options,_manifest,variants=direct_stem_case
    assert 'fusion_mode' not in options['_advanced']['settings']
    options['_advanced']['variants']=[variants[1]]
    monkeypatch.setattr(advanced_generation,'run',lambda _s,_d,local,_p:_conflicting_generate(local))
    directory=tmp_path/'legacy';directory.mkdir()
    result=stem_generation.run(original,directory,options,lambda *_args:None)
    assert not any(row['kind']=='fusion' for row in result['advanced_result'])
    assert any(row.get('stage')=='direct_fusion' and row['fusion_failed'] for row in result['errors'])


def test_new_defaults_select_relane_with_vocal_primary():
    settings=advanced.validate_settings({})
    assert (settings['fusion_mode'],settings['fusion_primary'])==('relane','vocals')
    for bad in ({'fusion_mode':'union'},{'fusion_primary':'mix'},{'fusion_mode':None}):
        with pytest.raises(ValueError):advanced.validate_settings(bad)
    assert advanced.validate_settings({'fusion_mode':'accompaniment_priority'})['fusion_mode']=='accompaniment_priority'


def test_vocals_priority_fusion_end_to_end_records_decisions(direct_stem_case,monkeypatch):
    row=_fused(direct_stem_case,monkeypatch,fusion_mode='vocals_priority')
    provenance=row['provenance']
    assert provenance['fusion_policy_version']=='direct-priority-merge-v3' and provenance['fusion_mode']=='vocals_priority'
    expert_chord=advanced.defaults()['difficulty_rules']['expert']['chord']
    assert provenance['attack_window_ms']==40. and provenance['chord_cap']==expert_chord
    assert provenance['fusion_primary']=='vocals' and provenance['primary_role']=='vocals'
    assert provenance['min_hold_ms']==60. and provenance['gap_ms']==advanced.defaults()['difficulty_rules']['expert']['gap']
    assert provenance['accounting_verified'] is True and provenance['preservation_verified'] is False
    counts=provenance['fusion_counts']
    assert counts['vocals']=={'input':2,'kept':2,'dropped':0,'relaned':0,'shortened':0,'to_tap':0}
    assert counts['accompaniment']['input']==4 and counts['accompaniment']['kept']+counts['accompaniment']['dropped']==4
    decisions=provenance['fusion_decisions']
    assert len({d['event_id'] for d in decisions})==len(decisions)
    assert len(decisions)==sum(counts['accompaniment'].get(k,0) for k in ('dropped','shortened','to_tap','aligned','tail_spacing'))
    by_start={(e['start_ms'],e['lane']):e for e in row['events']}
    assert (100.,0) in by_start and by_start[(100.,0)]['origins'][0]['stem_role']=='vocals'
    aligned={d['event_id'] for d in decisions if d['action']=='aligned'}
    assert all(e['origins'][0]['original_start_ms']==e['start_ms'] for e in row['events'] if e['id'] not in aligned)
    assert provenance['fusion_summary']['mode']=='vocals_priority'
    assert provenance['residual_same_stem_lane_gap']==provenance['fusion_summary']['residual_same_stem_lane_gap']
    assert sum(provenance['residual_same_stem_lane_gap_by_role'].values())==provenance['residual_same_stem_lane_gap']
    assert provenance['density_validation']['denominator']=='first_head_to_last_head_or_tail'


def test_accompaniment_priority_fusion_keeps_accompaniment_untouched(direct_stem_case,monkeypatch):
    row=_fused(direct_stem_case,monkeypatch,fusion_mode='accompaniment_priority')
    counts=row['provenance']['fusion_counts']
    # a3 (50-400) ends 50 ms before a4 in lane 3: the only change to the primary side, recorded as tail_spacing
    assert counts['accompaniment']=={'input':4,'kept':4,'dropped':0,'relaned':0,'shortened':0,'to_tap':0,'tail_spacing':1}
    assert counts['vocals']['dropped']==1  # v1 collides with a1 at 100 ms in lane 0
    assert {d['reason'] for d in row['provenance']['fusion_decisions'] if d['role']=='accompaniment'}=={'tail_spacing'}


def test_relane_fusion_moves_instead_of_dropping_and_honours_primary(direct_stem_case,monkeypatch):
    row=_fused(direct_stem_case,monkeypatch,fusion_mode='relane',fusion_primary='accompaniment')
    provenance=row['provenance']
    assert provenance['primary_role']=='accompaniment' and provenance['fusion_primary']=='accompaniment'
    counts=provenance['fusion_counts']
    assert counts['vocals']['relaned']==2 and counts['vocals']['dropped']==0
    moved=next(d for d in provenance['fusion_decisions'] if d['action']=='relaned' and d['start_ms']==100.)
    assert moved['start_ms']==100. and moved['from_lane']==0 and moved['to_lane']!=0
    event=next(e for e in row['events'] if e['origins'][0]['note_id']==moved['origin']['note_id'])
    assert event['lane']==moved['to_lane'] and event['start_ms']==100. and event['origins'][0]['original_lane']==0
    assert counts['accompaniment']['kept']==4 and len(row['events'])==sum(c['kept'] for c in counts.values())
    assert provenance['fused_model_heads']==len(row['events']) and provenance['raw_model_heads']==6


def test_fusion_mode_does_not_change_raw_generation_identity():
    settings=advanced.defaults()
    args=({'source_id':'s'},{'start_sample':0,'end_sample':10},'balanced--expert',None,{'version':'x'})
    base=stem_generation._raw_key(settings,*args)
    for mode in ('vocals_priority','accompaniment_priority'):
        changed={**settings,'fusion_mode':mode,'fusion_primary':'accompaniment'}
        assert stem_generation._raw_key(changed,*args)==base


def test_both_stems_empty_still_fails_in_every_mode():
    for fusion in ({},{'fusion_mode':'relane'},{'fusion_mode':'vocals_priority'}):
        pair={role:{'id':role[0]*32,'kind':'stem_raw','variant':'balanced--expert','range':[0,10],'events':[],
                    'provenance':{'source_role':role,'stem_set_id':'s','parent_source_id':'p','source_id':role}} for role in ('vocals','accompaniment')}
        with pytest.raises(ValueError,match='均没有模型音符'):
            stem_generation._direct_fuse_revisions(pair,{**advanced.defaults(),**fusion},{'version':'x'},[0,10],1000.)


def test_chord_cap_and_attack_window_are_part_of_the_fusion_recipe(direct_stem_case,monkeypatch):
    base=_fused(direct_stem_case,monkeypatch,fusion_mode='vocals_priority')['provenance']
    rules=copy.deepcopy(advanced.defaults()['difficulty_rules'])
    rules['expert']['chord']=2
    changed=_fused(direct_stem_case,monkeypatch,fusion_mode='vocals_priority',difficulty_rules=rules)['provenance']
    assert changed['chord_cap']==2 and base['chord_cap']!=2
    assert changed['recipe_hash']!=base['recipe_hash']
