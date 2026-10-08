import copy
import json
import numpy as np
import pytest
import soundfile as sf
from malody_studio import advanced, advanced_api, advanced_generation as gen, section_plan as sp, mapperatorinator, task_history
from malody_studio.charts import Note
from malody_studio.generation_context import contract,group_entries,owned_rows


def test_continuous_native_executes_one_full_decoder_request_per_difficulty(tmp_path,monkeypatch):
    source=tmp_path/'source.wav'
    sf.write(source,np.full((64*sp.SR,2),.1,np.float32),sp.SR,subtype='FLOAT')
    monkeypatch.setattr(sp,'rhythm_features',lambda data:([
        {'start_sample':i*sp.SR,'end_sample':(i+1)*sp.SR,'active_fraction':1.,'onset_rate':8.,'rms':.1}
        for i in range(64)],np.ones(12800),[],.005))
    settings=advanced.defaults();settings.update(strategy='independent',section_granularity='fine')
    settings['conditions']['v32'].update(hard=4.6,expert=5.9)
    plan=sp.build_plan(source,settings,{'bpm':120,'manual':True})
    assert len(plan['sections'])>=3
    for i,section in enumerate(plan['sections']):
        for key in ('hard','expert'):section['per_difficulty'][key]['model_condition']=6+i*.1
    body={key:value for key,value in plan.items() if key not in ('id','content_hash')}
    plan['id']=plan['content_hash']=sp.canonical_hash(body)
    calls=[]
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']))
        return {row['key']:([Note(15900,0,16300),Note(16000,1),Note(50000,2)],[[0,120]])
                for row in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'id':'a'*32,'title':'test','artist':'test','tempo':{'bpm':120,'uncertain':True}},
              'segment':{'id':'b'*32,'start_sample':0,'end_sample':64*sp.SR},
              'variants':[{'key':'balanced--'+key,'pattern':'balanced','difficulty':key} for key in ('hard','expert')],
              'settings':settings,'section_plan':plan,'_stem_raw_only':True,
              'generation_context_policy':contract()}
    result=gen.run(source,tmp_path/'continuous',{'_advanced':snapshot},lambda *_:None)
    assert len(calls)==2
    assert {row['sr'] for row in calls}=={4.6,5.9}
    assert all(row['start_time']==0 and row['end_time']==64000 for row in calls)
    for output in result['advanced_result']:
        expected=settings['conditions']['v32'][output['variant'].split('--')[1]]
        records=output['provenance']['core_conditions']
        assert all(row['selected_condition']==row['requested_condition']==expected for row in records)
        assert all(row['condition_rationale']=='continuous_requested_difficulty_base' for row in records)
        assert len({row['inference_group'] for row in records})==1
        assert [event['start_ms'] for event in output['events']]==[15900,16000,50000]
    cache=json.loads((tmp_path/'continuous'/'balanced-mother.json').read_text(encoding='utf-8'))
    assert cache['metadata']['generation_context_policy']==contract()
    assert cache['metadata']['decoder_context']['ranges'][0]['core']==[0,64*sp.SR]


def entry(start,end,seed=42,policy=True):
    part={'id':str(start+1).zfill(32),'name':str(start),'start_sample':start,'end_sample':end,'overrides':{},'active':{}}
    snapshot={'project':{'id':'a'*32},'segment':part,'settings':{'seed':seed},'variants':[{'key':'balanced--hard'}]}
    if policy:snapshot['generation_context_policy']=contract()
    return ({'_advanced':snapshot},{'type':'project_file','path':'same.wav'})


def test_span_grouping_preserves_regions_and_rejects_changed_frozen_parameters():
    original=[entry(0,44100),entry(44100,88200),entry(88200,132300,seed=43),entry(132300,176400,policy=False)]
    before=copy.deepcopy(original)
    grouped=group_entries(original)
    assert original==before and len(grouped)==3
    assert [part['start_sample'] for part in grouped[0][0]['_advanced']['member_segments']]==[0,44100]
    assert grouped[0][0]['_advanced']['segment']['end_sample']==88200


def test_partition_owns_heads_once_keeps_cross_boundary_hold_and_raw_ancestry():
    snapshot=group_entries([entry(0,44100),entry(44100,88200)])[0][0]['_advanced']
    events=[{'id':'h','start_ms':900,'lane':0,'end_ms':1300},
            {'id':'t','start_ms':1000,'lane':1,'end_ms':None}]
    raw={'id':'b'*32,'variant':'balanced--hard','kind':'model_raw','settings':{},'events':events,'provenance':{}}
    playable=copy.deepcopy(raw);playable.update(id='c'*32,kind='rules')
    for event in playable['events']:event['origins']=[{'revision_id':raw['id'],'note_id':event['id']}]
    playable['provenance']={'global_budget_applied':True,'playability':{'budget_passes':1},
                            'density_validation':{'actual_heads':2,'bounds':[0,88200],'attempts':2},
                            'quality_summary':{'heads_before':2,'heads_after':2,'ln_before':1,'ln_after':1,'ln_target':.15},
                            'quality_decisions':[]}
    snapshot['section_plan']={'sample_rate':44100,'sections':[
        {'core':[0,44100],'active_seconds':1},{'core':[44100,88200],'active_seconds':1}]}
    result={'bounds':[0,88200],'advanced_result':[raw,playable]}
    rows=owned_rows(snapshot,result)
    assert len(rows)==4 and [len(row['events']) for part,row,bounds in rows]==[1,1,1,1]
    assert rows[0][1]['events'][0]['end_ms']==1300
    assert rows[1][1]['events'][0]['origins'][0]['revision_id']==rows[0][1]['id']
    assert rows[3][1]['events'][0]['origins'][0]['revision_id']==rows[2][1]['id']
    assert len({row['id'] for part,row,bounds in rows})==4
    assert all(row['provenance']['budget_partition_only'] for part,row,bounds in rows)
    assert all(row['provenance']['playability']['budget_passes']==1 for part,row,bounds in rows if row['kind']=='rules')
    assert all(row['provenance']['density_validation']['actual_heads']==1 for part,row,bounds in rows if row['kind']=='rules')
    assert all(row['provenance']['density_validation']['bounds']==bounds for part,row,bounds in rows if row['kind']=='rules')
    assert all(row['provenance']['span_density_validation']['actual_heads']==2 for part,row,bounds in rows if row['kind']=='rules')
    assert all(row['provenance']['quality_summary']['heads_after']==1 for part,row,bounds in rows if row['kind']=='rules')
    assert [row['provenance']['quality_summary']['ln_after'] for part,row,bounds in rows if row['kind']=='rules']==[1,0]


def test_boundary_alignment_keeps_reference_to_original_raw_head_owner():
    snapshot=group_entries([entry(0,44100),entry(44100,88200)])[0][0]['_advanced']
    raw={'id':'b'*32,'variant':'balanced--hard','kind':'model_raw','settings':{},'provenance':{},
         'events':[{'id':'head','start_ms':999.,'lane':0,'end_ms':None}]}
    selected=copy.deepcopy(raw);selected.update(id='c'*32,kind='rules')
    selected['events'][0].update(start_ms=1001.,origins=[{'revision_id':raw['id'],'note_id':'head'}])
    rows=owned_rows(snapshot,{'bounds':[0,88200],'advanced_result':[raw,selected]})
    assert not rows[1][1]['events'] and not rows[2][1]['events']
    assert rows[3][1]['events'][0]['origins'][0]['revision_id']==rows[0][1]['id']


def test_six_raw_attempts_do_not_stack_density_supply_beyond_selected_two_parents():
    snapshot=group_entries([entry(0,44100),entry(44100,88200)])[0][0]['_advanced']
    snapshot['section_plan']={'sample_rate':44100,'sections':[
        {'core':[0,44100],'active_seconds':1},{'core':[44100,88200],'active_seconds':1}]}
    raw=[]
    for index in range(6):
        raw.append({'id':str(index+10).zfill(32),'kind':'stem_raw','variant':'balanced--hard',
            'settings':{},'provenance':{},'events':[
                {'id':str(index)+'-a','start_ms':100+index*5,'lane':index%4,'end_ms':None},
                {'id':str(index)+'-b','start_ms':1100+index*5,'lane':index%4,'end_ms':None}]})
    fusion={'id':'f'*32,'kind':'fusion','variant':'balanced--hard','settings':{},
        'events':[{'id':'selected-a','start_ms':105,'lane':1,'end_ms':None,
                   'origins':[{'revision_id':raw[1]['id'],'note_id':raw[1]['events'][0]['id']}]},
                  {'id':'selected-b','start_ms':1105,'lane':1,'end_ms':None,
                   'origins':[{'revision_id':raw[1]['id'],'note_id':raw[1]['events'][1]['id']}]}],
        'provenance':{'parents':[raw[1]['id'],raw[4]['id']],
                      'density_validation':{'actual_heads':2,'attempts':2}}}
    result={'bounds':[0,88200],'advanced_result':raw+[fusion]}
    outputs=[row for part,row,bounds in owned_rows(snapshot,result) if row['kind']=='fusion']
    assert len(outputs)==2
    assert all(row['provenance']['density_validation']['model_supply']['heads']==2 for row in outputs)
    assert all(row['provenance']['density_validation']['candidate_supply']['heads']==2 for row in outputs)
    assert all(row['provenance']['density_validation']['cause']=='model_supply' for row in outputs)
    fusion['events'][0]['origins'].append({'revision_id':raw[0]['id'],'note_id':raw[0]['events'][0]['id']})
    declared_outputs=[row for part,row,bounds in owned_rows(snapshot,result) if row['kind']=='fusion']
    assert all(row['provenance']['density_validation']['model_supply']['heads']==2 for row in declared_outputs)
    # Even an additional quality wrapper must resolve through its selected
    # fusion parent, rather than opening the entire six-attempt pool.
    quality=copy.deepcopy(fusion);quality.update(id='e'*32,kind='quality')
    quality['provenance']['parents']=[fusion['id']]
    result['advanced_result'].append(quality)
    quality_outputs=[row for part,row,bounds in owned_rows(snapshot,result) if row['kind']=='quality']
    assert all(row['provenance']['density_validation']['model_supply']['heads']==2 for row in quality_outputs)


@pytest.mark.parametrize('explicit_policy',[False,True])
def test_full_batch_groups_queue_then_commits_and_reports_every_region(tmp_path,monkeypatch,explicit_policy):
    from malody_studio import advanced_plans,server
    source=tmp_path/'song.wav';sf.write(source,np.full((4*sp.SR,2),.1,np.float32),sp.SR,subtype='FLOAT')
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'uncertain':True}))
    store=advanced.ProjectStore(tmp_path/'projects');p=store.create(source,'test')
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':2*sp.SR})
    p=store.add_segment(p['id'],{'start_sample':2*sp.SR,'end_sample':4*sp.SR})
    p=store.update(p['id'],{'patterns':['balanced'],'difficulties':['hard']})
    adopted=store.add_revision(p['id'],p['segments'][0]['id'],'balanced--hard',
        [{'id':'old-note','start_ms':200,'lane':0,'end_ms':None}],p['settings'],'model_raw')
    p=store.load(p['id'])
    adopted_path=store.directory(p['id'])/'revisions'/(adopted['id']+'.json')
    adopted_bytes=adopted_path.read_bytes()
    before=copy.deepcopy(p);captured=[]
    monkeypatch.setattr(advanced_api,'store',store)
    monkeypatch.setattr(server,'jobs',{})
    monkeypatch.setattr(server,'ensure_engine',lambda _:None)
    monkeypatch.setattr(server,'enqueue_jobs',lambda entries:captured.extend(copy.deepcopy(entries)) or [{'id':'d'*32} for _ in entries])
    monkeypatch.setattr(advanced_plans,'get_or_build_plan',lambda *args:{'id':'test-plan','samples':4*sp.SR,
        'sections':[{'id':'whole-song','core':[0,4*sp.SR],'bpm_bucket_offset':0}]})
    result=advanced_api.generation_batch(p['id'],{'request_id':'e'*32,'segment_ids':[part['id'] for part in p['segments']],
        'expected_revision':p['revision'],**({'generation_context_policy':contract()} if explicit_policy else {})})
    assert len(captured)==len(result['jobs'])==1
    assert result['jobs'][0]['segment_ids']==[part['id'] for part in p['segments']]
    assert result['jobs'][0]['inference_count']==1
    assert store.load(p['id'])==before
    snapshot=captured[0][0]['_advanced']
    generated={'bounds':[0,4*sp.SR],'advanced_result':[{'id':'f'*32,'variant':'balanced--hard',
        'kind':'rules','settings':snapshot['settings'],'activate_initial':True,
        'provenance':{'global_budget_applied':True,'playability':{'budget_passes':1}},
        'events':[{'id':str(i),'start_ms':time,'lane':i%4,'end_ms':None} for i,time in enumerate((100,2000,3900))]}]}
    saved=advanced_api.commit_generated(captured[0][0],generated,job_id='d'*32)
    assert len(saved)==2
    job={'id':'d'*32,'options':captured[0][0],'status':'completed','created':'2026-10-05T00:00:00Z','advanced_revisions':saved}
    monkeypatch.setattr(server,'jobs',{'d'*32:job})
    tasks=advanced_api.project_tasks(p['id'])['tasks']
    assert {task['segment_id'] for task in tasks}=={part['id'] for part in p['segments']}
    detail=task_history.batch_detail(store,p['id'],snapshot['batch_id'],[job])
    assert detail['model_job_count']==1 and detail['counts']['completed']==2 and len(detail['results'])==2
    current=store.load(p['id'])
    assert current['segments'][0]['active']['balanced--hard']==adopted['id'] and not current['segments'][1]['active']
    assert adopted_path.read_bytes()==adopted_bytes
    assert [store.revision(p['id'],rid)['range'] for rid in saved]==[[0,2*sp.SR],[2*sp.SR,4*sp.SR]]


@pytest.mark.parametrize('policy',[contract()['version'],None,False,{'version':'unsupported'},{'version':contract()['version']}])
def test_invalid_generation_context_is_rejected_before_queueing(tmp_path,monkeypatch,policy):
    from fastapi import HTTPException
    from malody_studio import advanced_plans,server
    source=tmp_path/'song.wav';sf.write(source,np.full((4*sp.SR,2),.1,np.float32),sp.SR,subtype='FLOAT')
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'uncertain':True}))
    store=advanced.ProjectStore(tmp_path/'projects');p=store.create(source,'test')
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':2*sp.SR})
    p=store.add_segment(p['id'],{'start_sample':2*sp.SR,'end_sample':4*sp.SR})
    before=copy.deepcopy(p);captured=[]
    monkeypatch.setattr(advanced_api,'store',store)
    monkeypatch.setattr(server,'jobs',{})
    monkeypatch.setattr(server,'ensure_engine',lambda _:None)
    monkeypatch.setattr(server,'enqueue_jobs',lambda entries:captured.extend(entries) or [{'id':'d'*32} for _ in entries])
    monkeypatch.setattr(advanced_plans,'get_or_build_plan',lambda *args:{'sections':[]})
    with pytest.raises(HTTPException) as exc:
        advanced_api.generation_batch(p['id'],{'request_id':'e'*32,
            'segment_ids':[part['id'] for part in p['segments']],
            'expected_revision':p['revision'],'generation_context_policy':policy})
    assert exc.value.status_code==400 and '连续推理策略' in exc.value.detail
    assert not captured and store.load(p['id'])==before
    assert not list((store.directory(p['id'])/'batches').glob('*.json'))
