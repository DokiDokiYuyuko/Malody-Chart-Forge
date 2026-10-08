import threading
import time
from pathlib import Path
import numpy as np
import pytest
from malody_studio import resident


@pytest.mark.parametrize('changed',['v32_event_serialization.py','v32_recovery.py','v32_cfg.py',
                                    'v32_generation_diagnostics.py','v32_attempt_diagnostics.py','v32_grammar_mask.py',
                                    'v32_fast_decode.py','v32_batch_decode.py','v32_batch_streams.py'])
def test_serializer_change_invalidates_cached_gpu_owner(tmp_path,monkeypatch,changed):
    """Hot owner identity must include the CPU converter loaded by V32."""
    original_root=resident.ROOT
    files=['tools/resident_worker.py','tools/mapperatorinator_worker.py',
           'malody_studio/engine.py','malody_studio/resident.py','malody_studio/resident_mug.py',
           'malody_studio/v32_event_serialization.py','malody_studio/v32_recovery.py','malody_studio/v32_cfg.py',
           'malody_studio/v32_generation_diagnostics.py','malody_studio/v32_attempt_diagnostics.py',
           'malody_studio/v32_grammar_mask.py','malody_studio/v32_fast_decode.py',
           'malody_studio/v32_batch_decode.py','malody_studio/v32_batch_streams.py']
    for name in files:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((original_root/name).read_bytes() if (original_root/name).exists() else b'converter v1')
    monkeypatch.setattr(resident,'ROOT',tmp_path)
    before=resident.code_version()
    converter=tmp_path/'malody_studio'/changed
    converter.write_bytes(converter.read_bytes()+b'\n# converter changed\n')
    assert resident.code_version()!=before


@pytest.fixture
def rpc(tmp_path,monkeypatch):
    from functools import partial
    # A failed fake-worker thread must fail this test promptly, not wait for the
    # production one-hour GPU request timeout.
    monkeypatch.setattr(resident,'call',partial(resident.call,timeout=5))
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
            request=resident.read_message(path,('id','payload'));path.unlink()
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
            request=resident.read_message(path,('id','payload'));path.unlink();resident.atomic(rpc/(request['id']+'.result'),response)
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


def test_transient_response_read_is_retried_without_losing_success(rpc,monkeypatch):
    original=Path.read_text;attempts=[]
    def unreliable(path,*args,**kwargs):
        if path.suffix=='.result':
            attempts.append(path)
            if len(attempts)==1:raise PermissionError('temporarily shared')
            if len(attempts)==2:return '{'
            if len(attempts)==3:return '{}'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',unreliable)
    def worker():
        path=rpc/'session.request'
        while not path.exists():time.sleep(.005)
        request=resident.read(path);path.unlink()
        resident.atomic(rpc/(request['id']+'.result'),{'result':{'charts':{'hard':'saved.osu'}},'request_id':request['id'],'session':'session'})
    thread=threading.Thread(target=worker);thread.start()
    assert resident.call('v32',{})=={'charts':{'hard':'saved.osu'}}
    thread.join(timeout=2)
    assert len(attempts)==4 and not list(rpc.glob('*.result'))


def test_invalid_message_is_preserved_with_actionable_error(rpc):
    path=rpc/'invalid.result';resident.atomic(path,{})
    with pytest.raises(RuntimeError,match='原始文件已保留'):
        resident.read_message(path,('result','error'),timeout=0)
    assert path.exists()


@pytest.mark.parametrize('response',[{'result':None},{'result':{},'request_id':'wrong'},{'error':''}])
def test_malformed_or_wrong_request_response_cannot_be_consumed(rpc,response):
    def worker():
        path=rpc/'session.request'
        while not path.exists():time.sleep(.005)
        request=resident.read_message(path,('id',),timeout=2);path.unlink();resident.atomic(rpc/(request['id']+'.result'),response)
    thread=threading.Thread(target=worker);thread.start()
    with pytest.raises(RuntimeError,match='原始文件已保留'):resident.call('v32',{})
    thread.join(timeout=2)
    assert len(list(rpc.glob('*.result')))==1


@pytest.mark.parametrize('error,reused',[(ValueError('No timing points found'),True),
                                        (RuntimeError('CUDA failure'),False)])
def test_owner_keeps_model_for_timing_errors_and_recovers_cuda(tmp_path,monkeypatch,error,reused):
    import json
    import sys
    from types import SimpleNamespace
    from tools import resident_worker as worker
    monkeypatch.setattr(worker,'ROOT',tmp_path)
    monkeypatch.setattr(worker,'HOME',tmp_path)
    cleared=[];observed=[]
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda:False,empty_cache=lambda:cleared.append(True))))
    request_file=tmp_path/'input.json'
    request_file.write_text(json.dumps({'audio':str(tmp_path/'audio.wav')}),encoding='utf-8')
    def main(path,cache,progress):
        observed.append('model' in cache)
        if len(observed)==1:
            cache['model']=object()
            raise error
        (tmp_path/'test.stop').touch()
        return {'reused':observed[-1]}
    monkeypatch.setitem(sys.modules,'mapperatorinator_worker',SimpleNamespace(main=main))
    worker.atomic(tmp_path/'test.request',{'id':'first','payload':{'request_path':str(request_file)}})
    thread=threading.Thread(target=worker.serve,args=('v32','test','test-version'))
    thread.start()
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'first.result').exists() and time.monotonic()<deadline:time.sleep(.01)
        assert (tmp_path/'first.result').exists()
        assert worker.read(tmp_path/'first.result')['error']==str(error)
        worker.atomic(tmp_path/'test.request',{'id':'second','payload':{'request_path':str(request_file)}})
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert worker.read(tmp_path/'second.result')['result']=={'reused':reused}
        assert observed==[False,reused]
        # Final shutdown releases once; CUDA recovery releases an additional time.
        assert len(cleared)==(1 if reused else 2)
    finally:
        if thread.is_alive():
            (tmp_path/'test.stop').touch();thread.join(timeout=2)


def test_power_throttling_opt_out_is_process_local_and_reports_acceptance():
    import os
    accepted=resident.disable_power_throttling()
    assert accepted is (os.name=='nt')
