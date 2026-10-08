import json
import subprocess
from types import SimpleNamespace

import pytest

from malody_studio import music


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    executable=tmp_path/'runtime/bin/yt-dlp.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'test fixture: never executed')
    monkeypatch.setattr(music,'ROOT',tmp_path)
    monkeypatch.setattr(music.shutil,'which',lambda name:None)
    return tmp_path


@pytest.mark.parametrize('stderr,category',[
    ('ERROR: Requested format is not available. Use --list-formats', 'format'),
    ('ERROR: Private video. Sign in if you have been granted access', 'source_restricted'),
    ('ERROR: Sign in to confirm your age', 'authentication_required'),
    ('ERROR: Video unavailable. This video has been removed', 'source_restricted'),
    ("WARNING: No supported JavaScript runtime could be found\nERROR: Sign in to confirm you're not a bot", 'bot_verification'),
    ('ERROR: [youtube] Sign in to confirm you’re not a bot. Use --cookies-from-browser', 'bot_verification'),
    ('ERROR Sign in to confirm you‘re not a bot', 'bot_verification'),
    ('ERROR: Could not copy Chrome cookie database. Permission denied', 'cookie_locked'),
    ('ERROR: cookies database is locked', 'cookie_locked'),
    ('ERROR: Failed to decrypt with DPAPI', 'local_cookie_decryption'),
    ('ERROR: Failed to decrypt cookie', 'local_cookie_decryption'),
    ('ERROR: Unable to extract yt initial data', 'extractor'),
    ('ERROR: No supported JavaScript runtime could be found', 'extractor'),
    ('ERROR: Failed to establish a new connection: [WinError 10013]', 'local_permission'),
    ('ERROR: getaddrinfo failed', 'network'),
    ('ERROR: connection timed out', 'timeout'),
    ('WARNING: Video unavailable for one optional result\nERROR: connection reset', 'network'),
    ('ERROR: The requested resource is not available in this extractor version', 'unknown'),
    ('ERROR: unexpected internal failure: diagnostic-private-text', 'unknown'),
])
def test_search_failure_keeps_original_error_and_does_not_guess_network(downloader,monkeypatch,stderr,category):
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:
                        SimpleNamespace(returncode=1,stderr=stderr,stdout='partial extraction output'))
    with pytest.raises(music.MusicSourceError) as caught:
        music.search_page('暗黑火锅')
    error=caught.value
    assert error.category==category
    record=json.loads((downloader/'logs/music-source-errors'/(error.diagnostic_id+'.json')).read_text(encoding='utf-8'))
    assert record['stage']=='search' and record['exit_code']==1
    assert record['stderr']==stderr and record['stdout']=='partial extraction output'
    assert record['arguments'][-1]=='ytsearch20:暗黑火锅'
    assert stderr not in str(error)
    assert '检查网络连接' not in str(error)
    if category=='format': assert '需要登录' not in str(error)
    if category in ('bot_verification','local_cookie','local_cookie_decryption','cookie_locked'):assert '下架' not in str(error)
    if category=='bot_verification':assert '机器人' in str(error)
    if category=='local_cookie':assert '本机' in str(error) and 'Cookie' in str(error)
    if category=='local_cookie_decryption':assert 'Windows 无法解密' in str(error) and 'cookies_file' in str(error)
    if category=='cookie_locked':assert '保存工作' in str(error) and '完全退出' in str(error)


@pytest.mark.parametrize('exception,category',[
    (PermissionError('local launch denied'),'local_permission'),
    (FileNotFoundError('binary removed during launch'),'component_missing'),
    (OSError('invalid executable format'),'local_startup'),
    (subprocess.TimeoutExpired('yt-dlp',75,output=b'partial stdout',stderr=b'original timeout stderr'),'timeout'),
])
def test_launch_failures_preserve_exception_and_metadata_stage(downloader,monkeypatch,exception,category):
    def fail(*args,**kwargs): raise exception
    monkeypatch.setattr(music.subprocess,'run',fail)
    with pytest.raises(music.MusicSourceError) as caught:
        music.search_page('https://youtu.be/LtrB_8CejUA')
    record=json.loads((downloader/'logs/music-source-errors'/(caught.value.diagnostic_id+'.json')).read_text(encoding='utf-8'))
    assert record['stage']=='metadata' and record['exit_code'] is None
    assert record['category']==category and record['exception_type']==type(exception).__name__
    if category=='timeout':assert record['stderr']=='original timeout stderr'


def test_missing_program_has_separate_diagnostic(downloader,monkeypatch):
    (downloader/'runtime/bin/yt-dlp.exe').unlink()
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:pytest.fail('missing binary must not launch'))
    with pytest.raises(music.MusicSourceError) as caught:music.command('-f','bestaudio',music.url_for('LtrB_8CejUA'))
    record=json.loads((downloader/'logs/music-source-errors'/(caught.value.diagnostic_id+'.json')).read_text(encoding='utf-8'))
    assert record['category']=='component_missing' and record['stage']=='download'


def test_success_search_pagination_and_direct_metadata_keep_existing_protocol(downloader,monkeypatch):
    calls=[]
    entry={'id':'LtrB_8CejUA','title':'Yunomi - Jellyfish','channel':'Waifu Wednesdays','duration':235}
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return SimpleNamespace(returncode=0,stderr='nonfatal warning',stdout=json.dumps(
            {'entries':[entry]*20} if '--flat-playlist' in argv else entry))
    monkeypatch.setattr(music.subprocess,'run',run)
    monkeypatch.setattr(music,'candidates',{})
    page=music.search_page('暗黑火锅',cursor=20)
    assert page['results'][0]['id']=='LtrB_8CejUA' and page['results'][0]['duration']==235
    assert page['next_cursor']==40
    argv=calls[0][0]
    assert argv[argv.index('--playlist-start')+1]=='21'
    assert argv[argv.index('--playlist-end')+1]=='40' and argv[-1]=='ytsearch40:暗黑火锅'
    direct=music.search_page('https://youtu.be/LtrB_8CejUA?list=ignored')
    assert direct['results'][0]['title']==entry['title']
    assert '--flat-playlist' not in calls[1][0] and calls[1][0][-1]==music.url_for('LtrB_8CejUA')
    assert not (downloader/'logs/music-source-errors').exists()


def test_failed_search_does_not_touch_ready_track_or_audio(downloader,monkeypatch):
    library=downloader/'uploads/library';directory=library/'LtrB_8CejUA';directory.mkdir(parents=True)
    track={'id':'LtrB_8CejUA','status':'ready','title':'Cached track'}
    state=directory/'track.json';state.write_text(json.dumps(track),encoding='utf-8')
    audio=directory/'audio.ogg';audio.write_bytes(b'original successful audio')
    original=(state.read_bytes(),audio.read_bytes())
    monkeypatch.setattr(music,'LIBRARY',library)
    monkeypatch.setattr(music,'tracks',{track['id']:dict(track)})
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:
                        SimpleNamespace(returncode=1,stderr='ERROR: unexpected extraction failure',stdout=''))
    with pytest.raises(music.MusicSourceError):music.search_page('暗黑火锅')
    assert music.get(track['id'])==track
    assert (state.read_bytes(),audio.read_bytes())==original


def test_diagnostic_permission_failure_does_not_replace_source_failure(downloader,monkeypatch):
    from pathlib import Path
    original=Path.write_text
    def deny(path,*args,**kwargs):
        if 'music-source-errors' in path.parts:raise PermissionError('diagnostic directory denied')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',deny)
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:
                        SimpleNamespace(returncode=1,stderr='ERROR: Requested format is not available',stdout=''))
    with pytest.raises(music.MusicSourceError) as caught:music.search_page('暗黑火锅')
    assert caught.value.category=='format'
    assert 'diagnostic directory denied' not in str(caught.value)


def test_search_api_exposes_safe_message_and_keeps_raw_stderr_local(downloader,monkeypatch):
    from fastapi.testclient import TestClient
    from malody_studio import server
    raw='ERROR: internal downloader detail should-stay-local'
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:
                        SimpleNamespace(returncode=2,stderr=raw,stdout=''))
    with TestClient(server.app) as client:
        response=client.get('/api/music/search',params={'q':'暗黑火锅'})
    assert response.status_code==400
    assert 'should-stay-local' not in response.text
    assert '错误详情已记录' in response.json()['detail']
    diagnostics=list((downloader/'logs/music-source-errors').glob('*.json'))
    assert len(diagnostics)==1
    assert json.loads(diagnostics[0].read_text(encoding='utf-8'))['stderr']==raw


@pytest.mark.parametrize('node_source',['path','bundled','missing'])
def test_command_uses_existing_node_absolute_path_and_utf8_env(downloader,monkeypatch,node_source):
    calls=[]
    bundled=downloader/'runtime/bin/node.exe'
    if node_source!='missing':bundled.write_bytes(b'fixture only')
    path_node=downloader/'Node with spaces/node.exe'
    def which(name):
        assert name=='node'
        return str(path_node) if node_source=='path' else None
    monkeypatch.setattr(music.shutil,'which',which)
    monkeypatch.setenv('PYTHONIOENCODING','ascii')
    monkeypatch.setenv('MALODY_TEST_INHERITED','preserved')
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return SimpleNamespace(returncode=0,stderr='',stdout=' 中文成功 ’ \n')
    monkeypatch.setattr(music.subprocess,'run',run)
    assert music.command('--dump-single-json','fixture',timeout=12)=='中文成功 ’'
    argv,kwargs=calls[0]
    if node_source=='missing':assert '--js-runtimes' not in argv
    else:
        expected=path_node if node_source=='path' else bundled
        assert argv[argv.index('--js-runtimes')+1]=='node:'+str(expected.resolve())
    assert '--cookies-from-browser' not in argv
    assert '--ignore-config' in argv
    assert kwargs['env']['PYTHONIOENCODING']=='utf-8'
    assert kwargs['env']['MALODY_TEST_INHERITED']=='preserved'
    assert music.os.environ['PYTHONIOENCODING']=='ascii'
    assert kwargs['encoding']=='utf-8' and kwargs['errors']=='replace'
    assert kwargs['capture_output'] is True and kwargs['timeout']==12


def test_browser_config_is_explicit_and_reloaded_each_command(downloader,monkeypatch):
    settings=downloader/'runtime/music-source-settings.json'
    assert '--cookies-from-browser' not in music._source_options(('fixture',),credentials=True)
    for browser in ('edge','chrome','firefox'):
        settings.write_text(json.dumps({'cookies_from_browser':browser}),encoding='utf-8')
        options=music._source_options(('fixture',),credentials=True)
        assert options[options.index('--cookies-from-browser')+1]==browser
    settings.write_text('{}',encoding='utf-8')
    assert '--cookies-from-browser' not in music._source_options(('fixture',),credentials=True)
    settings.unlink()
    assert '--cookies-from-browser' not in music._source_options(('fixture',),credentials=True)


@pytest.mark.parametrize('contents',[
    b'{broken', b'\xff', b'[]', b'null', b'"edge"',
    b'{"cookies_from_browser":null}', b'{"cookies_from_browser":[]}',
    b'{"cookies_from_browser":true}', b'{"cookies_from_browser":"safari"}',
    b'{"cookies_from_browser":"edge:secret-profile"}',
    b'{"cookies_from_browser":"edge","profile":"secret-profile"}',
    b'{"cookies":"secret-cookie-value"}',
])
def test_bad_source_config_fails_clearly_without_launch_or_value_leak(downloader,monkeypatch,contents):
    (downloader/'runtime/music-source-settings.json').write_bytes(contents)
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:pytest.fail('invalid config must not launch'))
    with pytest.raises(music.MusicSourceError) as caught:music._source_options(('fixture',),credentials=True)
    assert caught.value.category=='local_config' and '配置' in str(caught.value)
    assert 'secret-' not in str(caught.value)
    diagnostic=(downloader/'logs/music-source-errors'/(caught.value.diagnostic_id+'.json')).read_text(encoding='utf-8')
    assert 'secret-' not in diagnostic


def test_unreadable_source_config_is_local_config(downloader,monkeypatch):
    from pathlib import Path
    original=Path.read_text
    def deny(path,*args,**kwargs):
        if path.name=='music-source-settings.json':raise PermissionError('private settings detail')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',deny)
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:pytest.fail('unreadable config must not launch'))
    with pytest.raises(music.MusicSourceError) as caught:music._source_options(('fixture',),credentials=True)
    assert caught.value.category=='local_config'
    assert '无法读取' in str(caught.value) and 'private settings detail' not in str(caught.value)


@pytest.mark.parametrize('absolute',[False,True])
def test_cookie_file_uses_valid_project_file_without_reading_cookie_values(downloader,monkeypatch,absolute):
    from pathlib import Path
    cookie_file=downloader/'runtime/youtube cookies.txt'
    cookie_file.write_text('# Netscape HTTP Cookie File\nsecret-cookie-value',encoding='utf-8')
    configured=str(cookie_file) if absolute else 'runtime/youtube cookies.txt'
    (downloader/'runtime/music-source-settings.json').write_text(
        json.dumps({'cookies_file':configured}),encoding='utf-8')
    original=Path.open
    class HeaderOnly:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def readline(self,size):
            assert size==4096
            return '# Netscape HTTP Cookie File\n'
    def open_file(path,*args,**kwargs):
        if path==cookie_file:return HeaderOnly()
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_file)
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if len(calls)==1:
            return SimpleNamespace(returncode=1,stderr="ERROR: Sign in to confirm you're not a bot",stdout='')
        return SimpleNamespace(returncode=0,stderr='',stdout='ok')
    monkeypatch.setattr(music.subprocess,'run',run)
    assert music.command('fixture')=='ok'
    assert len(calls)==2
    assert '--cookies' not in calls[0] and '--cookies-from-browser' not in calls[0]
    assert calls[1][calls[1].index('--cookies')+1]==str(cookie_file.resolve())
    assert '--cookies-from-browser' not in calls[1]
    assert 'secret-cookie-value' not in str(calls)


@pytest.mark.parametrize('case',[
    'missing','outside_relative','outside_absolute','bad_header','bad_extension',
    'directory','empty','non_string','invalid_utf8','both',
])
def test_cookie_file_rejects_invalid_settings_without_launch_or_contents_leak(downloader,monkeypatch,case):
    cookie_file=downloader/'runtime/youtube.txt'
    cookie_file.write_text('# Netscape HTTP Cookie File\nsecret-cookie-value',encoding='utf-8')
    value='runtime/youtube.txt'
    if case=='missing':cookie_file.unlink()
    elif case in ('outside_relative','outside_absolute'):
        outside=downloader.parent/(downloader.name+'-outside.txt')
        outside.write_text('# Netscape HTTP Cookie File\nsecret-cookie-value',encoding='utf-8')
        value=str(outside) if case=='outside_absolute' else '../'+outside.name
    elif case=='bad_header':cookie_file.write_text('secret-cookie-value\n',encoding='utf-8')
    elif case=='bad_extension':
        cookie_file.rename(cookie_file.with_suffix('.json'))
        value='runtime/youtube.json'
    elif case=='directory':value='runtime'
    elif case=='empty':value=''
    elif case=='non_string':value=[]
    elif case=='invalid_utf8':cookie_file.write_bytes(b'\xffsecret-cookie-value')
    settings={'cookies_file':value}
    if case=='both':settings['cookies_from_browser']='edge'
    (downloader/'runtime/music-source-settings.json').write_text(json.dumps(settings),encoding='utf-8')
    monkeypatch.setattr(music.subprocess,'run',lambda *a,**kw:pytest.fail('invalid cookie file must not launch'))
    with pytest.raises(music.MusicSourceError) as caught:music._source_options(('fixture',),credentials=True)
    assert caught.value.category=='local_config' and 'cookies_file' in str(caught.value)
    if case=='both':assert '互斥' in str(caught.value)
    diagnostic=(downloader/'logs/music-source-errors'/(caught.value.diagnostic_id+'.json')).read_text(encoding='utf-8')
    assert 'secret-cookie-value' not in diagnostic and 'secret-cookie-value' not in str(caught.value)


@pytest.mark.parametrize('contents',[
    '{broken', '{"cookies_file":"private/missing.txt"}', '{"cookies_from_browser":"edge"}',
])
def test_anonymous_success_never_reads_credentials_config(downloader,monkeypatch,contents):
    from pathlib import Path
    (downloader/'runtime/music-source-settings.json').write_text(contents,encoding='utf-8')
    original=Path.read_text
    def read(path,*args,**kwargs):
        if path.name=='music-source-settings.json':pytest.fail('anonymous request must not read config')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    bundled=downloader/'runtime/bin/node.exe';bundled.write_bytes(b'fixture')
    assert music._source_options(('fixture',))==['--js-runtimes','node:'+str(bundled.resolve())]
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0,stderr='',stdout='anonymous success')
    monkeypatch.setattr(music.subprocess,'run',run)
    assert music.command('fixture')=='anonymous success'
    assert len(calls)==1 and '--cookies' not in calls[0] and '--cookies-from-browser' not in calls[0]


@pytest.mark.parametrize('initial',[
    "ERROR: Sign in to confirm you're not a bot", 'ERROR: Sign in to confirm your age',
])
@pytest.mark.parametrize('second,category',[
    ('',None), ("ERROR: Sign in to confirm you're not a bot",'bot_verification'),
    ('ERROR: Failed to decrypt with DPAPI','local_cookie_decryption'),
])
def test_authentication_retries_with_edge_exactly_once(downloader,monkeypatch,initial,second,category):
    (downloader/'runtime/music-source-settings.json').write_text(
        '{"cookies_from_browser":"edge"}',encoding='utf-8')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        assert len(calls)<=2
        stderr=initial if len(calls)==1 else second
        return SimpleNamespace(returncode=1 if stderr else 0,stderr=stderr,stdout='authenticated success' if not stderr else '')
    monkeypatch.setattr(music.subprocess,'run',run)
    if category:
        with pytest.raises(music.MusicSourceError) as caught:music.command('fixture')
        assert caught.value.category==category
    else:assert music.command('fixture')=='authenticated success'
    assert len(calls)==2
    assert '--cookies-from-browser' not in calls[0] and '--cookies' not in calls[0]
    assert calls[1][calls[1].index('--cookies-from-browser')+1]=='edge'


@pytest.mark.parametrize('stderr,category',[
    ('ERROR: connection reset','network'),
    ('ERROR: Private video. Sign in if you have been granted access','source_restricted'),
    ('ERROR: Video has been removed. Sign in','source_restricted'),
])
def test_non_authentication_failure_never_reads_config_or_retries(downloader,monkeypatch,stderr,category):
    from pathlib import Path
    (downloader/'runtime/music-source-settings.json').write_text('{broken',encoding='utf-8')
    original=Path.read_text
    def read(path,*args,**kwargs):
        if path.name=='music-source-settings.json':pytest.fail('non-authentication failure must not read config')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1,stderr=stderr,stdout='')
    monkeypatch.setattr(music.subprocess,'run',run)
    with pytest.raises(music.MusicSourceError) as caught:music.command('fixture')
    assert caught.value.category==category and len(calls)==1


@pytest.mark.parametrize('settings',[None,'{}'])
def test_authentication_without_configured_credentials_does_not_retry(downloader,monkeypatch,settings):
    if settings is not None:
        (downloader/'runtime/music-source-settings.json').write_text(settings,encoding='utf-8')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1,stderr="ERROR: Sign in to confirm you're not a bot",stdout='')
    monkeypatch.setattr(music.subprocess,'run',run)
    with pytest.raises(music.MusicSourceError) as caught:music.command('fixture')
    assert caught.value.category=='bot_verification' and len(calls)==1


@pytest.mark.parametrize('elapsed',[4,10,12])
def test_authenticated_retry_shares_timeout_deadline(downloader,monkeypatch,elapsed):
    (downloader/'runtime/music-source-settings.json').write_text(
        '{"cookies_from_browser":"edge"}',encoding='utf-8')
    now=[100.0]
    monkeypatch.setattr(music,'time',SimpleNamespace(monotonic=lambda:now[0]))
    calls=[]
    def run(argv,**kwargs):
        calls.append(kwargs['timeout'])
        if len(calls)==1:
            now[0]+=elapsed
            return SimpleNamespace(returncode=1,stderr="ERROR: Sign in to confirm you're not a bot",stdout='')
        raise subprocess.TimeoutExpired('fixture',kwargs['timeout'])
    monkeypatch.setattr(music.subprocess,'run',run)
    with pytest.raises(music.MusicSourceError) as caught:music.command('fixture',timeout=10)
    assert caught.value.category=='timeout'
    assert calls==([10,6] if elapsed==4 else [10])


def test_invalid_credentials_config_is_reported_only_after_bot_failure(downloader,monkeypatch):
    (downloader/'runtime/music-source-settings.json').write_text(
        '{"cookies_file":"private/missing.txt"}',encoding='utf-8')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1,stderr="ERROR: Sign in to confirm you're not a bot",stdout='')
    monkeypatch.setattr(music.subprocess,'run',run)
    with pytest.raises(music.MusicSourceError) as caught:music.command('fixture')
    assert caught.value.category=='local_config' and len(calls)==1
