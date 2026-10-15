import copy
import json
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from malody_studio import artifact_retention as retention, advanced, advanced_api, server
from malody_studio.separation import VERSION, pcm_hash, file_hash, validate_manifest

def write(path, data=b'fixture'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path

def test_compact_task_preserves_deliverables_parameters_and_chart_versions(tmp_path):
    preserved=['job.json','queue-worker.json','report.json','generation-cache.json',
               'chart-cache/balanced/generation-cache.json','versions/easy/manifest.json',
               'models/inference-attempts/1/resolved-parameters.json','0/easy.mc','malody-4k.mcz']
    removed=['source.wav','retained.wav','models/input.wav','worker-result.json',
             'v32-generation-diagnostics.json','models/model-events.json','event-evidence/data.json']
    for name in preserved+removed:write(tmp_path/name)
    result=retention.cleanup(tmp_path)
    assert all((tmp_path/name).exists() for name in preserved)
    assert not any((tmp_path/name).exists() for name in removed)
    assert result['removed_files']==len(removed)

def test_debug_retains_full_failure_scene(tmp_path):
    paths=[write(tmp_path/'retained.wav'),write(tmp_path/'event-evidence/full.json')]
    assert retention.cleanup(tmp_path,debug=True)['removed_files']==0
    assert all(path.exists() for path in paths)
    assert retention.retain_debug({'_advanced':{'settings':{'retain_debug_artifacts':True}}})
    with pytest.raises(ValueError):retention.retain_debug({'retain_debug_artifacts':'false'})
    assert advanced.validate_settings({})['retain_debug_artifacts'] is False
    with pytest.raises(ValueError):advanced.validate_settings({'retain_debug_artifacts':1})

def test_project_keeps_exact_originals_and_immutable_history(tmp_path):
    root=tmp_path;project=root/'outputs/advanced'/('a'*32)
    original=write(project/'source.wav');original_bytes=original.read_bytes()
    exact=write(project/'source-original.wav',b'exact input')
    revision=write(project/'revisions'/('b'*32+'.json'),b'{"events":[1,2]}')
    package=write(project/'assemblies'/('c'*32)/'malody-4k.mcz')
    scratch=[write(project/'event-evidence/full.json'),write(project/'assemblies'/('c'*32)/'assembled.wav')]
    retention.prune_project(project,root,prune_stems=True)
    assert original.read_bytes()==original_bytes and exact.read_bytes()==b'exact input'
    assert revision.read_bytes()==b'{"events":[1,2]}' and package.exists()
    assert not any(path.exists() for path in scratch)

def test_shared_project_cleanup_waits_for_explicit_legacy_approval(tmp_path):
    project=tmp_path/'outputs/advanced'/('a'*32)
    scratch=write(project/'preview'/'working.wav')
    write(tmp_path/'logs/artifact-cleanup-approval.pending.json',b'{"approval_required":true}')
    result=retention.prune_project(project,tmp_path,prune_stems=True)
    assert result['mode']=='awaiting_legacy_cleanup_approval' and scratch.exists()

@pytest.mark.parametrize('status',['completed','failed'])
def test_terminal_queue_job_cleans_scratch_and_keeps_original_upload(tmp_path,monkeypatch,status):
    job_id='a'*32;directory=tmp_path/'outputs'/job_id
    upload=write(tmp_path/'uploads/original.wav')
    scratch=write(directory/'source.wav');write(directory/'retained.wav')
    write(directory/'queue-worker.json',b'{}');write(directory/'malody-4k.mcz')
    jobs={job_id:{'id':job_id,'status':status,'options':{'retain_debug_artifacts':False}}}
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(server,'jobs',jobs)
    server.finalize_storage(job_id,upload)
    assert upload.exists() and not scratch.exists() and not (directory/'retained.wav').exists()
    assert jobs[job_id]['status']==status and jobs[job_id]['artifact_retention']['removed_files']==2

def test_project_cleanup_waits_for_all_batch_siblings(tmp_path,monkeypatch):
    pid='b'*32;store=advanced.ProjectStore(tmp_path/'outputs/advanced');project=store.directory(pid)
    write(project/'source.wav');scratch=write(project/'event-evidence/full.json')
    first='a'*32;second='c'*32;write(tmp_path/'outputs'/first/'source.wav')
    options={'_advanced':{'project':{'id':pid},'settings':{}}}
    jobs={first:{'id':first,'status':'completed','options':options},second:{'id':second,'status':'queued','options':options}}
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(server,'jobs',jobs);monkeypatch.setattr(advanced_api,'store',store)
    server.finalize_storage(first,project/'source.wav');assert scratch.exists()
    jobs[second]['status']='completed'
    server.finalize_storage(first,project/'source.wav');assert not scratch.exists()

def stem_fixture(root):
    pid='a'*32;key='b'*64;project=root/'outputs/advanced'/pid;folder=project/'stems'/key
    write(project/'source.wav')
    audio=np.full((256,2),.01,dtype=np.float32);rows=[]
    folder.mkdir(parents=True)
    for role in ('vocals','accompaniment'):
        path=folder/(role+'.wav');sf.write(path,audio,44100,subtype='FLOAT')
        rows.append({'role':role,'file':path.name,'path':str(path),'source_id':key+':'+role,
                     'pcm_sha':pcm_hash(audio),'file_sha256':file_hash(path)})
    manifest={'id':key,'recipe_hash':key,'adapter_version':VERSION,'sample_rate':44100,'origin_sample':0,
              'frame_count':256,'source_pcm_sha':'source-identity','settings':{'model':'htdemucs'},'stems':rows}
    write(folder/'manifest.json',json.dumps(manifest).encode())
    return project,folder,manifest

def test_pruned_stem_manifest_can_be_frozen_but_not_played(tmp_path):
    project,folder,manifest=stem_fixture(tmp_path)
    retention.prune_project(project,tmp_path,prune_stems=True)
    pruned=json.loads((folder/'manifest.json').read_text())
    assert pruned['storage']['audio_pruned'] and not (folder/'vocals.wav').exists()
    validate_manifest(folder,pruned,allow_pruned=True)
    with pytest.raises(ValueError):validate_manifest(folder,pruned)

def test_pruned_inputs_rebuild_from_original_and_keep_frozen_identity(tmp_path,monkeypatch):
    from malody_studio import separation
    project,folder,manifest=stem_fixture(tmp_path)
    raw=tmp_path/'rebuild';raw.mkdir()
    restored=copy.deepcopy(manifest)
    for row in restored['stems']:
        dest=raw/row['file'];dest.write_bytes(Path(row['path']).read_bytes());row['path']=str(dest)
    snapshot={'project':{'id':project.name},'stem_set_id':manifest['id'],
              'input_sources':[{**r,'stem_set_id':manifest['id']} for r in manifest['stems']]}
    frozen=copy.deepcopy(snapshot)
    retention.prune_project(project,tmp_path,prune_stems=True)
    calls=[]
    def rebuild(source,directory,settings,progress):
        calls.append((source,settings));return restored
    monkeypatch.setattr(separation,'ensure_stems',rebuild)
    retention.restore_inputs(tmp_path,{'_advanced':snapshot},tmp_path/'worker',lambda *_:None)
    assert calls[0][0]==project/'source.wav' and snapshot==frozen
    assert (folder/'vocals.wav').is_file()
    assert not json.loads((folder/'manifest.json').read_text()).get('storage',{}).get('audio_pruned')

def test_ephemeral_playback_is_deleted_after_range_response(tmp_path):
    path=write(tmp_path/'preview'/'preview.transient-123.wav',b'0123456789')
    response=server.audio_response(path,'bytes=2-5')
    import asyncio
    async def consume():
        content=b''.join([chunk async for chunk in response.body_iterator])
        await response.background()
        return content
    assert asyncio.run(consume())==b'2345' and not path.exists()

def test_peak_safe_production_audio_is_transient_and_original_is_unchanged(tmp_path):
    from malody_studio.playback_audio import audition_file
    source=tmp_path/'source.wav';sf.write(source,np.full((256,2),1.3,np.float32),44100,subtype='FLOAT')
    before=source.read_bytes();temporary=audition_file(source,tmp_path)
    assert retention.ephemeral_audio(temporary)
    retention.remove_ephemeral(temporary)
    assert not temporary.exists() and source.read_bytes()==before

def test_debug_retry_preserves_old_job_options_and_uses_original(tmp_path,monkeypatch):
    job_id='a'*32;source=write(tmp_path/'uploads'/('b'*32+'.wav'))
    original={'id':job_id,'status':'completed','title':'Song','artist':'Artist',
              'source_ref':{'type':'upload','name':source.name},'options':{'retain_debug_artifacts':False}}
    before=copy.deepcopy(original);captured=[]
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(server,'jobs',{job_id:original})
    monkeypatch.setattr(server,'ensure_engine',lambda _:None)
    monkeypatch.setattr(server,'new_job',lambda path,options:captured.append((path,options)) or {'id':'new'})
    with TestClient(server.app) as client:
        response=client.post('/api/jobs/'+job_id+'/regenerate',json={'retain_debug_artifacts':True})
    assert response.status_code==200
    assert captured[0][0]==source and captured[0][1]['retain_debug_artifacts'] is True
    assert original==before
