"""Delivery deletion never consumes task/project source archives."""
import hashlib
import json
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from malody_studio import library, server, advanced, advanced_api
from malody_studio.charts import Note, serialize, package


@pytest.fixture
def deliveries(tmp_path,monkeypatch):
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(advanced_api,'ROOT',tmp_path)
    store=advanced.ProjectStore(tmp_path/'outputs'/'advanced')
    monkeypatch.setattr(advanced_api,'store',store)
    jobs={};monkeypatch.setattr(server,'jobs',jobs)
    def make(record_id,title='删除测试',source=None):
        source=source or {'type':'song','job_id':record_id}
        directory=tmp_path/'outputs'/source.get('job_id',record_id)
        directory.mkdir(parents=True,exist_ok=True)
        sound=directory/'audio.ogg'
        sf.write(sound,np.ones((88200,2),np.float32)*.1,44100,format='OGG',subtype='VORBIS')
        report={'title':title,'artist':'艺人','duration':2.,'charts':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard','filename':'balanced--hard.mc'}]}
        chart=serialize([Note(500,0,800)],title,'艺人','Balanced Hard',120)
        archive=package(directory,{'balanced--hard':chart},sound,report,filenames={'balanced--hard':'balanced--hard.mc'})
        folder,_,_=library.publish(tmp_path,record_id,archive,report,'2026-10-05T00:00:00+00:00',source)
        if source['type']=='song':
            jobs[source['job_id']]={'id':source['job_id'],'title':title,'artist':'艺人','created':'2026-10-05T00:00:00+00:00','status':'completed','options':{'engine':'v32'},'report':report}
            (directory/'job.json').write_text(json.dumps(jobs[source['job_id']]),encoding='utf8')
        return directory,folder,report
    return tmp_path,store,jobs,make


def test_mixed_delete_preserves_sources_and_tombstones_old_routes(deliveries):
    root,store,jobs,make=deliveries
    job='a'*32;directory,folder,_=make(job)
    pid='b'*32;aid='c'*32;project=store.directory(pid);project.mkdir()
    record='advanced-'+aid
    old={'id':pid,'title':'项目','artist':'人','segments':[], 'assemblies':[{'id':aid}], 'samples':88200}
    advanced.atomic(project/'project.json',old)
    _,afolder,_=make(record,source={'type':'advanced','project_id':pid,'assembly_id':aid})
    frozen={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
    frozen[project/'project.json']=hashlib.sha256((project/'project.json').read_bytes()).hexdigest()
    with TestClient(server.app) as c:
        response=c.post('/api/library/delete',json={'record_ids':[job,record,'unknown']})
        rows=response.json()['results'];assert [r['deleted'] for r in rows]==[True,True,False]
        assert c.get(f'/api/jobs/{job}/download').status_code==410
        assert c.get(f'/api/advanced/projects/{pid}/assemblies/{aid}/download').status_code==410
        assert c.get('/api/history').json()['total']==0
        assert c.post('/api/library/delete',json={'record_ids':[job]}).json()['results'][0]['already_deleted']
    assert not folder.exists() and not afolder.exists()
    assert jobs[job]['status']=='completed'
    assert all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in frozen.items())
    with pytest.raises(ValueError,match='已删除'):library.publish(root,job,directory/'malody-4k.mcz',json.loads((directory/'report.json').read_text('utf8')))


def test_explicit_reexport_new_identity_and_history_page_clamp(deliveries):
    root,_,jobs,make=deliveries;job='d'*32;directory,_,_=make(job)
    with TestClient(server.app) as c:
        assert c.post('/api/library/delete',json={'record_ids':[job]}).json()['results'][0]['deleted']
        export=c.post(f'/api/jobs/{job}/export').json()
        assert export['record_id']!=job and job in library.tombstones(root)
        assert c.get(export['download']).status_code==200
        assert c.post(f'/api/jobs/{job}/export').json()['record_id']==export['record_id']
        history=c.get('/api/history?page=99').json()
        assert history['page']==1 and history['items'][0]['id']==export['record_id']
        assert history['items'][0]['job_id']==job
        assert c.get(f'/api/jobs/{job}/download').status_code==410
        assert c.post('/api/library/delete',json={'record_ids':[export['record_id']]}).json()['results'][0]['deleted']
        assert c.post(f'/api/jobs/{job}/export').json()['record_id']!=export['record_id']
    assert directory.is_dir() and jobs[job]['status']=='completed'


def test_denied_folder_rename_partial_failure_and_active_job_protected(deliveries,monkeypatch):
    root,_,jobs,make=deliveries;blocked='1'*32;good='2'*32;running='3'*32
    _,folder,_=make(blocked);make(good);make(running);jobs[running]['status']='running'
    from pathlib import Path
    original=Path.replace
    def replace(path,target):
        if path==folder:raise PermissionError('denied')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',replace)
    with TestClient(server.app) as c:
        rows=c.post('/api/library/delete',json={'record_ids':[blocked,good,running]}).json()['results']
        assert [r['deleted'] for r in rows]==[False,True,False]
        assert c.post('/api/library/delete',json={'record_ids':[blocked],'path':'C:/'}).status_code==400
        assert c.post('/api/library/delete',json={'record_ids':['../outside']}).json()['results'][0]['deleted'] is False
    assert folder.exists() and blocked not in library.tombstones(root)
    assert running in library.index(root) and jobs[running]['status']=='running'


def test_organizer_skips_deleted_delivery(deliveries):
    root,_,_,make=deliveries;job='4'*32;make(job)
    library.delete_delivery(root,job)
    from tools.organize_library import organize
    result=organize(root)
    assert result['records']==[{'record_id':job,'status':'deleted'}]
    assert job not in library.index(root)
