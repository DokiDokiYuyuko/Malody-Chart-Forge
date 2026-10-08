import json
from pathlib import Path
import pytest

from malody_studio import folder_open, library, task_history
from tests.test_task_history import history_context


def denied(_):
    error=PermissionError('directory ShellExecute denied');error.winerror=5
    raise error


def test_win5_fallback_sends_absolute_unicode_directory_as_one_argument(tmp_path,monkeypatch):
    target=tmp_path/'【Original MV】機械の声 _ V.W.P × V.I.P';target.mkdir()
    monkeypatch.setattr(folder_open.os,'startfile',denied,raising=False)
    monkeypatch.setenv('SystemRoot',r'C:\Windows')
    launches=[]
    monkeypatch.setattr(folder_open.subprocess,'Popen',lambda args,**kwargs:launches.append((args,kwargs)))
    folder_open.open_registered_directory(target)
    assert launches[0][0]==[r'C:\Windows\explorer.exe',str(target.resolve())]
    assert launches[0][1]['shell'] is False
    assert target.is_dir()


def test_standard_open_success_does_not_launch_fallback(tmp_path,monkeypatch):
    opened=[];monkeypatch.setattr(folder_open.os,'startfile',opened.append,raising=False)
    monkeypatch.setattr(folder_open.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('unnecessary fallback'))
    folder_open.open_registered_directory(tmp_path)
    assert opened==[str(tmp_path.resolve())]


def test_other_open_errors_and_missing_directories_do_not_launch_fallback(tmp_path,monkeypatch):
    def missing(_):raise FileNotFoundError('association missing')
    monkeypatch.setattr(folder_open.os,'startfile',missing,raising=False)
    monkeypatch.setattr(folder_open.subprocess,'Popen',lambda *args,**kwargs:pytest.fail('unexpected fallback'))
    with pytest.raises(FileNotFoundError):folder_open.open_registered_directory(tmp_path)
    with pytest.raises(ValueError):folder_open.open_registered_directory(tmp_path/'missing')


def test_library_fallback_still_requires_registered_contained_directory(tmp_path,monkeypatch):
    home=library.home(tmp_path);target=home/'song'/'delivery';target.mkdir(parents=True)
    (target/'archive.mcz').write_bytes(b'fixture')
    (target/'manifest.json').write_text(json.dumps({'record_id':'registered','archive':'archive.mcz'}),encoding='utf-8')
    (home/'index.json').write_text(json.dumps({'registered':{'directory':'song/delivery'},
                                            'escape':{'directory':'../outside'}}),encoding='utf-8')
    monkeypatch.setattr(folder_open.os,'startfile',denied,raising=False)
    launches=[];monkeypatch.setattr(folder_open.subprocess,'Popen',lambda args,**kwargs:launches.append(args))
    assert library.open_folder(tmp_path,'registered')['opened'] is True
    assert launches[0][1]==str(target.resolve())
    with pytest.raises(ValueError):library.open_folder(tmp_path,'escape')
    with pytest.raises(ValueError):library.open_folder(tmp_path,'../../outside')
    assert len(launches)==1


def test_task_history_win5_fallback_keeps_record_ownership_checks(history_context,monkeypatch,tmp_path):
    store,pid,bid,jobs,client=history_context
    monkeypatch.setattr(task_history.os,'startfile',denied,raising=False)
    launches=[];monkeypatch.setattr(folder_open.subprocess,'Popen',lambda args,**kwargs:launches.append(args))
    record=f'batch:{pid}:{bid}'
    assert client.post('/api/task-history/open-folder',json={'record_id':record}).status_code==200
    assert launches[0][1]==str(store.directory(pid).resolve())
    assert client.post('/api/task-history/open-folder',json={'record_id':record,'path':str(tmp_path.parent)}).status_code==400
    assert client.post('/api/task-history/open-folder',json={'record_id':record,'job_id':'f'*32}).status_code==400
    assert len(launches)==1
