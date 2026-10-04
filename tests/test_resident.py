import threading
import time
from pathlib import Path
import numpy as np
import pytest
from malody_studio import resident


@pytest.fixture
def rpc(tmp_path,monkeypatch):
    monkeypatch.setattr(resident,'HOME',tmp_path)
    monkeypatch.setattr(resident,'code_version',lambda:'tested-code')
    monkeypatch.setattr(resident,'alive',lambda pid:pid==123)
    resident.atomic(tmp_path/'state.json',{'pid':123,'session':'session','engine':'v32','version':'tested-code','phase':'idle'})
    return tmp_path


def test_two_calls_reuse_owner_and_keep_payload_results_isolated(rpc):
    def worker():
        for _ in range(2):
            path=rpc/'session.request'
            while not path.exists():time.sleep(.005)
            request=resident.read(path);path.unlink()
            resident.atomic(rpc/(request['id']+'.result'),{'result':{'seed':request['payload']['seed']}})
    thread=threading.Thread(target=worker);thread.start()
    first=resident.call('v32',{'seed':11});second=resident.call('v32',{'seed':29});thread.join(timeout=2)
    assert first=={'seed':11} and second=={'seed':29}
    assert not list(rpc.glob('*.result'))


def test_failed_request_releases_lease_for_next_request(rpc):
    def worker():
        for response in ({'error':'invalid model output'},{'result':{'ok':True}}):
            path=rpc/'session.request'
            while not path.exists():time.sleep(.005)
            request=resident.read(path);path.unlink();resident.atomic(rpc/(request['id']+'.result'),response)
    thread=threading.Thread(target=worker);thread.start()
    with pytest.raises(RuntimeError,match='invalid model output'):resident.call('v32',{})
    assert resident.call('v32',{})=={'ok':True};thread.join(timeout=2)


def test_manual_release_refuses_busy_owner_without_stopping_request(rpc):
    with resident.lease():
        with pytest.raises(RuntimeError,match='正在处理'):resident.release()
    assert not (rpc/'session.stop').exists()


def test_crashed_worker_fails_current_request_without_silently_resubmitting(rpc,monkeypatch):
    calls=[]
    def alive(pid):
        calls.append(pid)
        return not (rpc/'session.request').exists()
    monkeypatch.setattr(resident,'alive',alive)
    with pytest.raises(RuntimeError,match='已中断'):resident.call('v32',{})
    assert (rpc/'session.request').exists()


def test_remote_mug_keeps_wave_and_seed_owned_by_each_instance(tmp_path,monkeypatch):
    from malody_studio import resident_mug as proxy
    monkeypatch.setattr(proxy,'ROOT',tmp_path);records=[]
    def fake(engine,payload,progress):
        assert engine=='mug'
        records.append(payload.copy())
        if payload['action']=='prepare':
            np.save(payload['output'],np.load(payload['input']))
            return {'z_length':32,'resident':{'model_reused':True}}
        return {'notes':[[100,payload['options']['seed']%4,None]],'resident':{'model_reused':True}}
    monkeypatch.setattr(proxy,'call',fake)
    monkeypatch.delenv('STARTRAIL_JOB_DIRECTORY',raising=False)
    a=proxy.RemoteEngine();b=proxy.RemoteEngine()
    wave_a=a.prepare(np.ones(20),22050,lambda *_:None);wave_b=b.prepare(np.zeros(20),22050,lambda *_:None)
    assert np.load(wave_a['path']).sum()==20 and np.load(wave_b['path']).sum()==0
    assert a.generate(wave_a,None,{'seed':1},lambda *_:None)[0].lane==1
    assert b.generate(wave_b,None,{'seed':2},lambda *_:None)[0].lane==2
    a.unload();assert Path(wave_b['path']).exists();b.unload()
    assert not Path(wave_a['path']).exists()
