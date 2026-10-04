import copy
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api, server, separation, separation_tasks, stem_generation


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'beat_times':[],'uncertain':True}))
    store = advanced.ProjectStore(tmp_path/'outputs'/'advanced')
    source = tmp_path/'input.wav'; sf.write(source,np.full((44100,2),.2,np.float32),44100,subtype='FLOAT')
    p = store.create(source,'独立测试','')
    monkeypatch.setattr(advanced_api,'store',store)
    monkeypatch.setattr(server,'ensure_engine',lambda *_:None)
    calls=[]
    monkeypatch.setattr(server,'enqueue_job',lambda options,source_ref: calls.append((copy.deepcopy(options),source_ref)) or {'id':'a'*32})
    app=FastAPI();app.include_router(advanced_api.router)
    return store,p,calls,TestClient(app)


def stems(store,p):
    folder=store.directory(p['id'])/'stems'/('b'*64);folder.mkdir(parents=True)
    rows=[]
    for role in ('vocals','accompaniment'):
        data=np.full((p['samples'],2),.1,np.float32);path=folder/(role+'.wav');sf.write(path,data,44100,subtype='FLOAT')
        rows.append({'role':role,'source_id':'b'*64+':'+role,'file':path.name,'path':str(path),'pcm_sha':separation.pcm_hash(data),'file_sha256':separation.file_hash(path),'frames':len(data)})
    m={'id':'b'*64,'recipe_hash':'b'*64,'adapter_version':separation.VERSION,'source_pcm_sha':p['source_pcm_sha256'],
       'frame_count':p['samples'],'origin_sample':0,'sample_rate':44100,'stems':rows,'settings':separation.validated_settings()}
    advanced.atomic(folder/'manifest.json',m)
    return m


def test_separation_empty_project_is_independent_and_named(setup,tmp_path,monkeypatch):
    store,p,calls,client=setup;prefix='/api/advanced/projects/'+p['id']
    assert not p['segments']
    monkeypatch.setattr(separation,'deployment',lambda *_:{})
    response=client.post(prefix+'/separations',json={})
    assert response.status_code==200
    options,ref=calls[0]
    assert options['_advanced']['task_type']=='separation' and 'segment' not in options['_advanced']
    m=stems(store,p)
    monkeypatch.setattr(separation_tasks,'ROOT',tmp_path)
    monkeypatch.setattr(separation_tasks,'ensure_stems',lambda *_:copy.deepcopy(m))
    # No chart inference or fusion function may be called by this independent stage.
    monkeypatch.setattr(stem_generation,'fuse_revisions',lambda *_:pytest.fail('separation fused charts'))
    result=separation_tasks.run(store.directory(p['id'])/'source.wav',tmp_path/'job',options,lambda *_:None)
    assert result['stem_set_id']==m['id'] and 'advanced_result' not in result
    assert 'htdemucs' in result['stem_set']['name'] and '全曲' in result['stem_set']['summary']
    assert store.load(p['id'])['segments']==[]
    assert client.get(prefix+'/separations').json()['stem_sets'][0]['name']


def test_models_catalog_reports_defaults_parameters_and_availability(setup,monkeypatch):
    _,_,calls,client=setup
    catalog={'htdemucs':{'id':'htdemucs','defaults':{'model':'htdemucs'},'parameters':[{'key':'overlap','min':.1,'max':.75}],
                         'deployment_status':{'ready':True,'assets_installed':True,'inference_verified':True}},
             'melband_roformer_kim':{'id':'melband_roformer_kim','defaults':{'model':'melband_roformer_kim'},'parameters':[{'key':'overlap_count','min':2,'max':8}],
                                     'experimental':True,'deployment_status':{'ready':False,'assets_installed':True,'inference_verified':False}},
             'htdemucs_ft':{'id':'htdemucs_ft','defaults':{'model':'htdemucs_ft'},'parameters':[]}}
    monkeypatch.setattr(separation,'get_separation_models',lambda include_status=False:copy.deepcopy(catalog),raising=False)
    response=client.get('/api/advanced/separation/models')
    assert response.status_code==200,response.text
    assert response.json()['defaults']=={'fast':'htdemucs','quality':'melband_roformer_kim'}
    assert response.json()['models']==list(catalog.values()) and not calls


def test_unavailable_separation_model_is_rejected_before_enqueue(setup,monkeypatch):
    store,p,calls,client=setup
    def missing_model(model):
        assert model=='htdemucs_ft'
        raise RuntimeError('分离模型尚未部署：htdemucs_ft')
    monkeypatch.setattr(separation,'deployment',missing_model)
    result=client.post(f"/api/advanced/projects/{p['id']}/separations",json={'settings':{'model':'htdemucs_ft'}})
    assert result.status_code==409 and 'htdemucs_ft' in result.json()['detail']
    assert not calls


@pytest.mark.parametrize('model', ['htdemucs', 'htdemucs_ft'])
@pytest.mark.parametrize('settings,message', [
    ({'segment': 8}, '1–7.8 秒'),
    ({'overlap': 0}, '10%–75%'),
    ({'overlap': .95}, '10%–75%'),
    ({'shifts': 20}, '0–4 的整数'),
    ({'shifts': 1.5}, '0–4 的整数'),
    ({'seed': 1.5}, '分离种子'),
])
def test_separation_bad_parameters_explain_limits_before_enqueue(setup,monkeypatch,model,settings,message):
    store,p,calls,client=setup
    monkeypatch.setattr(separation,'deployment',lambda *_:pytest.fail('invalid settings checked model deployment'))
    response=client.post(f"/api/advanced/projects/{p['id']}/separations",json={'settings':{'model':model,**settings}})
    assert response.status_code==400,response.text
    assert message in response.json()['detail']
    assert not calls


@pytest.mark.parametrize('model', ['htdemucs', 'htdemucs_ft'])
def test_separation_accepts_actual_upper_limits(setup,monkeypatch,model):
    store,p,calls,client=setup
    monkeypatch.setattr(separation,'deployment',lambda *_:{})
    settings={'model':model,'segment':7.8,'overlap':.75,'shifts':4,'seed':2147483640}
    response=client.post(f"/api/advanced/projects/{p['id']}/separations",json={'settings':settings})
    assert response.status_code==200,response.text
    assert calls[0][0]['_advanced']['settings']==settings


@pytest.mark.parametrize('role',['vocals','accompaniment','vocals_accompaniment'])
def test_explicit_generation_freezes_available_source_without_separation(setup,role):
    store,p,calls,client=setup;m=stems(store,p)
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100});sid=p['segments'][0]['id']
    source_id=role if role=='vocals_accompaniment' else m['id']+':'+role
    response=client.post(f"/api/advanced/projects/{p['id']}/segments/{sid}/generate",json={'source_id':source_id,'stem_set_id':m['id'],'auto_fuse':False})
    assert response.status_code==200,response.text
    assert response.json()['separation_count']==0 and response.json()['fusion_count']==0
    snap=calls[-1][0]['_advanced']
    assert len(snap['input_sources'])==(2 if role=='vocals_accompaniment' else 1)
    assert snap['auto_fuse'] is False and snap['stem_set_id']==m['id']
    assert all(d['frame_count']==p['samples'] and d['parent_source_id']==p['source_pcm_sha256'] for d in snap['input_sources'])


def test_invalid_source_clock_and_segment_scope_rejected(setup):
    store,p,calls,client=setup;m=stems(store,p)
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100});sid=p['segments'][0]['id']
    before=copy.deepcopy(p)
    with pytest.raises(ValueError,match='项目设置'):store.edit_segment(p['id'],sid,{'profile':'keyboard'})
    assert store.load(p['id'])==before
    m['source_pcm_sha']='other';advanced.atomic(store.directory(p['id'])/'stems'/m['id']/'manifest.json',m)
    result=client.post(f"/api/advanced/projects/{p['id']}/segments/{sid}/generate",json={'source_id':m['stems'][0]['source_id'],'stem_set_id':m['id']})
    assert result.status_code==400 and not calls


def test_waveform_detail_and_anchor_patch_preserve_existing_notes(setup):
    store,p,calls,client=setup;p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100});sid=p['segments'][0]['id']
    r=store.add_revision(p['id'],sid,'balanced--easy',[{'id':'note','start_ms':100,'end_ms':None,'lane':0}],advanced.defaults(),'model_raw')
    prefix='/api/advanced/projects/'+p['id']
    result=client.get(prefix+'/waveform',params={'start_ms':200,'end_ms':800,'points':32})
    assert result.status_code==200 and len(result.json()['peaks'])==32
    assert client.get(prefix+'/waveform',params={'end_ms':2000}).status_code==400
    result=client.patch(prefix,json={'tempo':{'points':[[0,120],[400,150]]}})
    assert result.status_code==200
    assert result.json()['tempo']['reference_source']=='user_confirmed'
    assert store.revision(p['id'],r['id'])==r


def test_single_stem_runner_never_separates_or_fuses_and_reuses_raw(setup,tmp_path,monkeypatch):
    from malody_studio import advanced_generation
    store,p,calls,client=setup;m=stems(store,p)
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100});sid=p['segments'][0]['id']
    response=client.post(f"/api/advanced/projects/{p['id']}/segments/{sid}/generate",json={
        'source_id':m['id']+':vocals','stem_set_id':m['id'],'auto_fuse':False,'variants':['balanced--easy']})
    assert response.status_code==200
    options=calls[-1][0];generated=[]
    monkeypatch.setattr(stem_generation,'ROOT',tmp_path)
    monkeypatch.setattr(stem_generation,'ensure_stems',lambda *_:pytest.fail('explicit generation separated'))
    monkeypatch.setattr(stem_generation,'fuse_revisions',lambda *_:pytest.fail('explicit generation fused'))
    def infer(source,directory,options,progress):
        snapshot=options['_advanced'];generated.append(snapshot['source']['source_role'])
        return {'advanced_result':[{'variant':'balanced--easy','events':[{'id':'note','start_ms':100,'end_ms':None,'lane':0}],
                                  'settings':snapshot['settings'],'provenance':{}}]}
    monkeypatch.setattr(advanced_generation,'run',infer)
    directory=tmp_path/'job';directory.mkdir()
    result=stem_generation.run(store.directory(p['id'])/'source.wav',directory,options,lambda *_:None)
    assert generated==['vocals'] and len(result['advanced_result'])==1
    row=result['advanced_result'][0]
    assert row['kind']=='stem_raw' and row['provenance']['source_role']=='vocals'
    saved=advanced_api.commit_generated(options,result)
    assert saved==[row['id']]
    options['_advanced']['segment']=store.load(p['id'])['segments'][0]
    retry=tmp_path/'retry';retry.mkdir()
    second=stem_generation.run(store.directory(p['id'])/'source.wav',retry,options,lambda *_:None)
    assert generated==['vocals'] and second['reused_revisions']==saved
    assert second['advanced_result']==[]


def test_separation_queue_is_exclusive_fifo_and_paused_tasks_do_not_run(tmp_path,monkeypatch):
    ids=['1'*32,'2'*32,'3'*32]
    class Pool:
        def __init__(self):self.calls=[]
        def submit(self,*args):self.calls.append(args)
    pool=Pool();monkeypatch.setattr(server,'pool',pool);monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'configured_concurrency',lambda:2)
    monkeypatch.setattr(server,'scheduler_active',set())
    data={}
    for index,job_id in enumerate(ids):
        (tmp_path/'outputs'/job_id).mkdir(parents=True)
        data[job_id]={'id':job_id,'title':'queue','artist':'','status':'queued' if index!=2 else 'paused',
                     'queue_order':index,'options':{'_advanced':{'task_type':'separation'}} if index==0 else {}}
    monkeypatch.setattr(server,'jobs',data)
    server.dispatch_next()
    assert [c[1] for c in pool.calls]==[ids[0]]
    assert data[ids[1]]['status']=='queued' and data[ids[2]]['status']=='paused'
    server.dispatch_next()
    assert len(pool.calls)==1
    data[ids[0]]['status']='completed';server.scheduler_active.clear();server.dispatch_next()
    assert [c[1] for c in pool.calls]==ids[:2]
    assert data[ids[2]]['status']=='paused'
    assert server.delete_job(ids[2])=={'cancelled':ids[2]}
    assert data[ids[2]]['status']=='cancelled'


@pytest.mark.parametrize('source_id',['vocals_accompaniment','c'*64+':vocals','../../other'])
def test_missing_explicit_source_is_readable_and_never_queued(setup,source_id):
    store,p,calls,client=setup;p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100})
    result=client.post(f"/api/advanced/projects/{p['id']}/segments/{p['segments'][0]['id']}/generate",json={
        'source_id':source_id,'stem_set_id':'c'*64,'auto_fuse':False})
    assert result.status_code in (400,409),result.text
    assert result.json()['detail'] and not calls


def test_original_corruption_and_empty_stem_set_never_queue(setup):
    store,p,calls,client=setup;m=stems(store,p);p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100})
    endpoint=f"/api/advanced/projects/{p['id']}/segments/{p['segments'][0]['id']}/generate"
    m['stems']=[];advanced.atomic(store.directory(p['id'])/'stems'/m['id']/'manifest.json',m)
    assert client.post(endpoint,json={'source_id':'vocals_accompaniment','stem_set_id':m['id']}).status_code==400
    sf.write(store.directory(p['id'])/'source.wav',np.zeros((44100,2),np.float32),44100,subtype='FLOAT')
    assert client.post(endpoint,json={'source_id':'original'}).status_code==400
    assert not calls


def test_assembly_waveform_uses_actual_source_duration_and_rejects_nonfinite(setup):
    store,p,calls,client=setup;folder=store.directory(p['id'])/'assemblies'/('d'*32);folder.mkdir(parents=True)
    sf.write(folder/'assembled.wav',np.full((22050,2),.3,np.float32),44100,subtype='FLOAT')
    endpoint=f"/api/advanced/projects/{p['id']}/waveform"
    result=client.get(endpoint,params={'source_id':'assembly:'+'d'*32,'points':32})
    assert result.status_code==200 and result.json()['end_ms']==500
    assert len(result.json()['peaks'])==32
    assert client.get(endpoint,params={'source_id':'assembly:'+'d'*32,'end_ms':800}).status_code==400
    for params in ({'start_ms':'nan'},{'end_ms':'inf'},{'start_ms':499.999,'end_ms':500}):
        assert client.get(endpoint,params={'source_id':'assembly:'+'d'*32,**params}).status_code==400


def test_valid_separation_cache_never_starts_gpu(setup,tmp_path,monkeypatch):
    store,p,calls,client=setup;m=stems(store,p)
    model={'adapter_version':separation.VERSION,'code_version':'test','models':{'htdemucs':['weight']},
           'files':{'weight':{'bytes':1,'sha256':'test'}},'environment':{},'dependency_lock_sha256':'test'}
    options=separation.validated_settings()
    recipe={'adapter_version':separation.VERSION,'source_pcm_sha':p['source_pcm_sha256'],'sample_rate':44100,
            'frame_count':p['samples'],'origin_sample':0,'settings':options,'deployment_hash':separation.canonical_hash(model)}
    key=separation.canonical_hash(recipe);cache=tmp_path/'cache'/'separation'/key;cache.mkdir(parents=True)
    import shutil
    for row in m['stems']:shutil.copyfile(row['path'],cache/row['file'])
    m.update(recipe);m.update(id=key,recipe_hash=key);advanced.atomic(cache/'manifest.json',m)
    monkeypatch.setattr(separation,'ROOT',tmp_path)
    monkeypatch.setattr(separation,'deployment',lambda *_:model)
    monkeypatch.setattr(separation.subprocess,'Popen',lambda *_args,**_kwargs:pytest.fail('valid cache started GPU'))
    for _ in range(2):
        cached=separation.ensure_stems(store.directory(p['id'])/'source.wav',settings=options)
        assert cached['id']==key


def test_standalone_task_status_cancel_and_retry_without_model_or_segment(setup,tmp_path,monkeypatch):
    store,p,calls,client=setup
    prefix=f"/api/advanced/projects/{p['id']}"
    monkeypatch.setattr(separation,'deployment',lambda *_:{})
    client.post(prefix+'/separations',json={});options,ref=calls[0];job_id='a'*32
    directory=tmp_path/'outputs'/job_id;directory.mkdir(parents=True)
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{job_id:{'id':job_id,'status':'paused','options':options,'source_ref':ref}})
    result=client.get(prefix+'/separations/'+job_id)
    assert result.status_code==200 and result.json()['status']=='paused' and result.json()['task_type']=='separation'
    assert server.delete_job(job_id)=={'cancelled':job_id}
    server.jobs[job_id]['status']='failed'
    monkeypatch.setattr(server,'ensure_engine',lambda *_:pytest.fail('separation retry checked chart model'))
    retry=server.regenerate_job(job_id)
    assert retry['id']==job_id and len(calls)==2
    assert calls[-1][0]['_advanced']['task_type']=='separation'
    assert client.get(prefix+'/separations/'+'e'*32).status_code==404


def test_unavailable_separation_model_is_rejected_on_retry(setup,tmp_path,monkeypatch):
    store,p,calls,client=setup
    prefix=f"/api/advanced/projects/{p['id']}"
    monkeypatch.setattr(separation,'deployment',lambda *_:{})
    client.post(prefix+'/separations',json={'settings':{'model':'htdemucs_ft'}})
    options,ref=calls[-1];job_id='a'*32
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{job_id:{'id':job_id,'status':'failed','options':options,'source_ref':ref}})
    monkeypatch.setattr(separation,'deployment',lambda *_:(_ for _ in ()).throw(RuntimeError('分离模型尚未部署：htdemucs_ft')))
    with pytest.raises(HTTPException) as exc:
        server.regenerate_job(job_id)
    assert exc.value.status_code==409 and 'htdemucs_ft' in exc.value.detail
    assert len(calls)==1


@pytest.mark.parametrize('dynamic',[False,True])
def test_v32_stem_uses_uncertain_original_timing_when_no_points(setup,tmp_path,monkeypatch,dynamic):
    from malody_studio import advanced_generation,mapperatorinator,section_plan
    from malody_studio.charts import Note
    store,p,calls,client=setup;m=stems(store,p)
    settings={**advanced.defaults(),'engine':'v32','strategy':'independent','dynamic_enabled':dynamic}
    descriptor=separation.resolve_source(store,p['id'],m['id']+':vocals')
    before=copy.deepcopy(p['tempo']);captured=[]
    def infer(source,folder,options,progress):
        reference=Path(options['timing_reference']);text=reference.read_text(encoding='utf-8')
        assert '[TimingPoints]' in text and '0.0,500.0,4,1,0,100,1,0' in text
        captured.append(reference)
        return {r['key']:([Note(100,0)],[[0,120]]) for r in options['_advanced_presets']},{}
    from pathlib import Path
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':p,'segment':{'start_sample':0,'end_sample':p['samples']},'settings':settings,
              'variants':[{'key':'speed--medium','pattern':'speed','difficulty':'medium'}],
              'source':descriptor,'_stem_raw_only':True}
    if dynamic:snapshot['section_plan']=section_plan.build_plan(store.directory(p['id'])/'source.wav',settings,p['tempo'])
    result=advanced_generation.run(descriptor['path'],tmp_path/('dynamic' if dynamic else 'static'),{'_advanced':snapshot},lambda *_:None)
    assert captured
    reference=result['advanced_result'][0]['provenance']['timing_reference']
    assert reference['source']=='original_audio_analysis' and reference['uncertain'] is True
    assert reference['points']==[[0.,120.]] and p['tempo']==before


def test_timing_reference_never_invents_bpm_and_keeps_confirmed_points():
    from malody_studio.advanced_generation import timing_reference_info
    for bpm in (None,0,1000,float('nan')):
        with pytest.raises(ValueError):timing_reference_info({'tempo':{'bpm':bpm}})
    confirmed={'tempo':{'points':[[0,150],[27318,240]],'reference_source':'imported_chart','uncertain':True}}
    result=timing_reference_info(confirmed)
    assert result['points']==confirmed['tempo']['points'] and result['source']=='imported_chart' and result['uncertain'] is True


def test_version_summary_batch_sources_backfill_without_mutating_revisions(setup):
    store,p,calls,client=setup;p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100});sid=p['segments'][0]['id']
    provenance={'cache_job':'f'*32,'source_role':'vocals','source_id':'stem:vocals','stem_set_id':'b'*64,'section_plan_id':'e'*64}
    r=store.add_revision(p['id'],sid,'balanced--easy',[],advanced.defaults(),'stem_raw',provenance)
    p=store.load(p['id']);summary=p['segments'][0]['versions']['balanced--easy'][0]
    assert summary['batch_id']==provenance['cache_job'] and summary['source_id']=='stem:vocals'
    for key in ('batch_id','cache_job','source_id','source_role','stem_set_id','section_plan_id'):summary.pop(key)
    advanced.atomic(store.directory(p['id'])/'project.json',p)
    updated=store.load(p['id']);restored=updated['segments'][0]['versions']['balanced--easy'][0]
    assert restored['batch_id']==provenance['cache_job'] and restored['stem_set_id']==provenance['stem_set_id']
    assert updated['revision']==p['revision'] and store.revision(p['id'],r['id'])==r



def test_mixed_tempo_save_preserves_point_trust_and_existing_import_reference(setup,tmp_path):
    store,_,calls,client=setup
    path=tmp_path/'long.wav';sf.write(path,np.zeros((44100*48,2),np.float32),44100,subtype='FLOAT')
    p=store.create(path,'混合节拍','');p['tempo']['references']=[{'source':'imported_chart','points':[[0,150]]}];p=store.save(p)
    metadata=[{'source':'audio_analysis','confirmed':False},{'source':'user_confirmed','confirmed':True}]
    points=[[0,150],[27318.0000243365,240]]
    p=store.update(p['id'],{'tempo':{'points':points,'points_metadata':metadata}})
    assert p['tempo']['uncertain'] is True and p['tempo']['manual'] is True
    assert p['tempo']['reference_source']=='mixed' and p['tempo']['points_metadata']==metadata
    assert p['tempo']['points']==points and p['tempo']['references']==[{'source':'imported_chart','points':[[0,150]]}]
    # Removing the user point restores an entirely automatic reference.
    p=store.update(p['id'],{'tempo':{'points':[points[0]],'points_metadata':[metadata[0]]}})
    assert p['tempo']['manual'] is False and p['tempo']['uncertain'] is True
    assert p['tempo']['reference_source']=='audio_analysis'
    with pytest.raises(ValueError):store.update(p['id'],{'tempo':{'points':[points[0]],'points_metadata':[{'source':'audio_analysis','confirmed':'false'}]}})


def test_revision_audio_defaults_to_own_stem_and_explicit_audition_is_isolated(setup):
    import io
    store,p,_,client=setup;m=stems(store,p)
    for row,level in zip(m['stems'],(.04,.16)):
        sf.write(row['path'],np.full((p['samples'],2),level,np.float32),advanced.SR,subtype='FLOAT')
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':44100});sid=p['segments'][0]['id']
    root='/api/advanced/projects/'+p['id'];saved=[]
    for role,lane in [('vocals',0),('accompaniment',1),('fusion',2)]:
        provenance={'source_role':role,'source_id':m['id']+':'+role}
        r=store.add_revision(p['id'],sid,'balanced--easy',[{'id':role,'start_ms':100+lane*100,'end_ms':None,'lane':lane}],advanced.defaults(),'fusion' if role=='fusion' else 'stem_raw',provenance)
        saved.append(r)
        preview=client.get(root+'/revisions/'+r['id']).json()
        expected='original' if role=='fusion' else m['id']+':'+role
        assert preview['audio_source_id']==expected
        response=client.get(preview['audio_url']);assert response.status_code==200
        data,rate=sf.read(io.BytesIO(response.content))
        assert rate==advanced.SR and len(data)==44100
        assert float(data.mean())==pytest.approx({'vocals':.04,'accompaniment':.16,'fusion':.2}[role],abs=1e-6)
        assert preview['events'][0]['id']==role and preview['chart'] is not None
    before=store.load(p['id']);voice=saved[0]
    response=client.get(root+'/revisions/'+voice['id']+'/audio',params={'source_id':m['id']+':accompaniment'})
    data,_=sf.read(io.BytesIO(response.content));assert float(data.mean())==pytest.approx(.16,abs=1e-6)
    own=client.get(root+'/revisions/'+voice['id']+'/audio');data,_=sf.read(io.BytesIO(own.content))
    assert float(data.mean())==pytest.approx(.04,abs=1e-6)
    assert store.load(p['id'])==before
    assert store.revision(p['id'],voice['id'])==voice
