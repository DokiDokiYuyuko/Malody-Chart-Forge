import copy
import hashlib
import json
import zipfile
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from malody_studio import audio_bounds as bounds, advanced, advanced_api, advanced_generation, library, server
from malody_studio.charts import Note, serialize, package


@pytest.mark.parametrize('tail,weak,expected',[(44100,False,100),(44099,False,44199),(88200,True,88300)])
def test_tail_requires_one_second_of_exact_zero_in_all_channels(tmp_path,tail,weak,expected):
    data=np.zeros((100+tail,2),np.float32);data[:100]=[.2,-.2]
    if weak:data[-1,1]=1e-9
    path=tmp_path/'pcm.wav';sf.write(path,data,44100,subtype='FLOAT')
    assert bounds.detect_tail(path)['cutoff_sample']==expected


def fixture_project(tmp_path,monkeypatch):
    sr=44100;data=np.zeros((sr*5,2),np.float32);data[:sr]=.1;data[sr*2:sr*3]=[.2,-.2]
    source=tmp_path/'pcm.wav';sf.write(source,data,sr,subtype='FLOAT')
    monkeypatch.setattr(advanced,'audio_metadata',lambda _:([0,.2],{'bpm':120,'points':[[0,120]],'manual':True}))
    store=advanced.ProjectStore(tmp_path/'outputs'/'advanced');p=store.create(source,'测试曲','音乐人')
    for a,b in [(0,sr),(sr,sr*2),(sr*2,sr*4),(sr*4,sr*5)]:p=store.add_segment(p['id'],{'start_sample':a,'end_sample':b})
    monkeypatch.setattr(advanced_api,'store',store)
    return store,p


def test_middle_silence_skips_model_and_empty_stem_is_not_silence(tmp_path,monkeypatch):
    store,p=fixture_project(tmp_path,monkeypatch);original=store.directory(p['id'])/'source.wav'
    opts={'_advanced':{'project':p,'segment':p['segments'][1],'settings':p['settings'],'variants':p['variants'],'original_source':{'path':str(original)}}}
    monkeypatch.setattr(advanced_generation,'_run_untrimmed',lambda *args:pytest.fail('silence invoked model'))
    result=advanced_generation.run(original,tmp_path/'run',opts,lambda *args:None)
    assert result['advanced_result'] and all(not r['events'] and r['provenance']['silence_verified'] for r in result['advanced_result'])
    zero=tmp_path/'empty-stem.wav';sf.write(zero,np.zeros((p['samples'],2),np.float32),44100,subtype='FLOAT')
    opts['_advanced']['segment']=p['segments'][0];opts['_advanced']['source']={'source_role':'vocals'}
    calls=[]
    monkeypatch.setattr(advanced_generation,'_run_untrimmed',lambda *args:calls.append(1) or {'advanced_result':[]})
    advanced_generation.run(zero,tmp_path/'stem',opts,lambda *args:None)
    assert calls==[1]


def test_keep_tail_generates_adoptable_empty_version_on_original_range(tmp_path,monkeypatch):
    store,p=fixture_project(tmp_path,monkeypatch)
    p=store.update(p['id'],{'tail_trim_enabled':False})
    original=store.directory(p['id'])/'source.wav';part=p['segments'][-1]
    options={'_advanced':{'project':p,'segment':part,'settings':p['settings'],'variants':p['variants'],
        'timing_map':{'source':{'effective_end_sample':3*advanced.SR}},'original_source':{'path':str(original)}}}
    monkeypatch.setattr(advanced_generation,'_run_untrimmed',lambda *_:pytest.fail('Zero tail invoked inference'))
    result=advanced_generation.run(original,tmp_path/'kept-tail',options,lambda *_:None)
    assert not result.get('skipped') and result['bounds']==[part['start_sample'],part['end_sample']]
    assert all(row['kind']=='silence' and row['activate_initial'] and not row['events'] for row in result['advanced_result'])
    advanced_api.commit_generated(options,result)
    assert store.segment(store.load(p['id']),part['id'])['active']


def test_trim_keeps_versions_and_clips_hold_in_sample_exact_assembly(tmp_path,monkeypatch):
    store,p=fixture_project(tmp_path,monkeypatch)
    settings=p['settings'];key=p['variants'][0]['key'];s=p['segments'][2]
    store.add_revision(p['id'],s['id'],key,[{'id':'hold','start_ms':2900.,'end_ms':3500.,'lane':0}],settings,'rules',activate_initial=True)
    for part in p['segments'][:2]:store.add_revision(p['id'],part['id'],key,[],settings,'silence',activate_initial=True)
    before=store.load(p['id']);assert bounds.content_end(before)==44100*3
    assert advanced.export_readiness(before)[0]['ready']
    result=advanced.assemble(store,p['id']);report=advanced.read(store.directory(p['id'])/'assemblies'/result['id']/'report.json')
    assert report['samples']==44100*3+66150
    assert report['mapping'][-1]['source_end']==44100*3
    assert any(row['type']=='clipped_hold' for row in report['seam_changes'])
    assert store.load(p['id'])['segments']==before['segments']
    restored=store.update(p['id'],{'tail_trim_enabled':False})
    assert not advanced.export_readiness(restored)[0]['ready']


def test_excluded_segment_never_enqueues_and_retry_preserves_failure(tmp_path,monkeypatch):
    store,p=fixture_project(tmp_path,monkeypatch);sid=p['segments'][-1]['id']
    monkeypatch.setattr(server,'enqueue_job',lambda *args:pytest.fail('excluded tail queued'))
    monkeypatch.setattr(server,'enqueue_jobs',lambda *args:pytest.fail('excluded batch queued'))
    monkeypatch.setattr(server,'jobs',{})
    with TestClient(server.app) as c:
        r=c.post(f'/api/advanced/projects/{p["id"]}/segments/{sid}/generate',json={})
        assert r.json()['status']=='skipped'
        batch=c.post(f'/api/advanced/projects/{p["id"]}/generation-batches',json={'request_id':'e'*32,'segment_ids':[sid]}).json()
        assert batch['jobs']==[] and len(batch['skipped'])==1
        assert c.get(f'/api/advanced/projects/{p["id"]}/generation-batches/{batch["id"]}').json()['status']=='completed'
        busy={'id':'f'*32,'status':'running','options':{'_advanced':{'project':p}}}
        server.jobs[busy['id']]=busy
        assert c.patch(f'/api/advanced/projects/{p["id"]}',json={'tail_trim_enabled':False}).status_code==409
    failed={'id':'a'*32,'status':'failed','error':'No timing points found in beatmap.','options':{'_advanced':{'project':p,'segment':p['segments'][-1]}}}
    saved=copy.deepcopy(failed)
    assert server.retry_advanced_job(failed)['status']=='skipped'
    assert failed==saved


def test_library_preserves_source_and_idempotent_snapshot(tmp_path):
    out=tmp_path/'original';out.mkdir()
    audio=out/'audio.ogg';sf.write(audio,np.ones((44100*2,2),np.float32)*.1,44100,format='OGG',subtype='VORBIS')
    chart=serialize([Note(500,0,700)],'旧标题','旧音乐人','old model name',120)
    report={'title':'帝国少女','artist':'音乐人','duration':2.,'charts':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard','filename':'legacy.mc'}]}
    archive=package(out,{'balanced--hard':chart},audio,report,filenames={'balanced--hard':'legacy.mc'})
    original_hash=hashlib.sha256(archive.read_bytes()).hexdigest()
    one=library.publish(tmp_path,'a'*32,archive,report,'2026-10-04T11:34:00+00:00')
    two=library.publish(tmp_path,'a'*32,archive,report,'2026-10-04T11:34:00+00:00')
    assert one[0]==two[0] and one[0].name.startswith('20261004-193400-')
    assert one[2].name=='帝国少女_Balanced_Hard.mcz'
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==original_hash
    with zipfile.ZipFile(one[2]) as z:
        fixed=json.loads(z.read('0/balanced--hard.mc'))
        assert fixed['meta']['song']['title']=='帝国少女' and fixed['meta']['version']=='Balanced Hard'
        assert fixed['note']==chart['note'] and fixed['time']==chart['time']
        assert z.read('0/audio.ogg')==audio.read_bytes()
    third=library.publish(tmp_path,'b'*32,archive,report,'2026-10-04T11:34:00+00:00')
    assert third[0]!=one[0]
    with pytest.raises(ValueError):library.locate(tmp_path,'../../outside')


def test_library_open_folder_rejects_client_paths(tmp_path,monkeypatch):
    monkeypatch.setattr(server,'ROOT',tmp_path)
    with TestClient(server.app) as c:
        assert c.post('/api/library/open-folder',json={'record_id':'missing','path':'C:/'}).status_code==400
        assert c.post('/api/library/open-folder',json={'record_id':'missing'}).status_code==400


def test_all_zero_and_non_44100_sources(tmp_path):
    path=tmp_path/'zero.wav';sf.write(path,np.zeros((88200,3),np.float32),44100,subtype='FLOAT')
    assert bounds.detect_tail(path)['cutoff_sample']==0
    sf.write(path,np.zeros(48000),48000,subtype='FLOAT')
    with pytest.raises(ValueError):bounds.detect_tail(path)


def test_invalid_timing_never_invents_reference_and_recovery_is_bounded(tmp_path):
    from malody_studio.v32_recovery import recovery_reference
    path=tmp_path/'reference.osu'
    frozen={'tempo':{'points':[[0,150],[1000,180]],'reference_available':True}}
    with pytest.raises(ValueError):advanced_generation.write_reference(path,frozen)
    assert not path.exists()
    options={};advanced_generation.attach_timing_reference(options,tmp_path,frozen,{'source_role':'vocals'})
    assert not options and not (tmp_path/'timing-fallback.osu').exists()
    frozen['tempo'].update(manual=True,uncertain=False,reference_source='user_confirmed')
    advanced_generation.write_reference(path,frozen)
    attempted=set();missing=AssertionError('No timing points found in beatmap.')
    assert recovery_reference(missing,'balanced--hard',path,attempted)==str(path)
    with pytest.raises(ValueError,match='BPM'):recovery_reference(missing,'balanced--hard',path,attempted)
    assert recovery_reference(missing,'balanced--expert',path,attempted)==str(path)
    with pytest.raises(ValueError,match='BPM'):recovery_reference(missing,'other',None,attempted)
    with pytest.raises(AssertionError):recovery_reference(AssertionError('different failure'),'x',path,attempted)
    invalid={'tempo':{'bpm':120,'reference_available':False}}
    options={};advanced_generation.attach_timing_reference(options,tmp_path,invalid,{'source_role':'vocals'})
    assert not options and not (tmp_path/'timing-fallback.osu').exists()
    path.write_text('[TimingPoints]\n0,500,4,1,0,100,1,0\n-1,500,4,1,0,100,1,0\n',encoding='utf-8')
    with pytest.raises(ValueError):recovery_reference(missing,'invalid',path,attempted)


def test_metadata_updates_keep_adoptions_and_export_snapshot(tmp_path,monkeypatch):
    store,p=fixture_project(tmp_path,monkeypatch);key=p['variants'][0]['key']
    for part in p['segments'][:3]:store.add_revision(p['id'],part['id'],key,[{'id':advanced.uid(),'start_ms':part['start_sample']/44.1+100,'end_ms':None,'lane':0}],p['settings'],'rules',activate_initial=True)
    a=advanced.assemble(store,p['id']);before=store.load(p['id'])
    changed=store.update(p['id'],{'title':' 新曲名 ','artist':' 新音乐人 '},before['revision'])
    assert changed['segments']==before['segments'] and changed['title']=='新曲名'
    report=advanced.read(store.directory(p['id'])/'assemblies'/a['id']/'report.json')
    assert report['title']=='测试曲' and report['artist']=='音乐人'
    with pytest.raises(ValueError):store.update(p['id'],{'title':' '})
    with pytest.raises(ValueError):store.update(p['id'],{'artist':'x'*121})
