import copy
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf

from malody_studio import library
from malody_studio.charts import Note, package, serialize


def fixture_package(tmp_path):
    audio = tmp_path/'audio.ogg'
    sf.write(audio, np.zeros((44100,2),np.float32), 44100, format='OGG', subtype='VORBIS')
    charts = {f'balanced--{key}':serialize([Note(100,0),Note(500,1)],'Song','Artist',key,120)
              for key in ['easy','hard']}
    report = dict(title='Song',artist='Artist',duration=1,charts=[
        dict(key=key,chart_id=key,pattern='balanced',difficulty=key.split('--')[-1],filename=key+'.mc')
        for key in charts])
    (tmp_path/'source').mkdir()
    archive = package(tmp_path/'source',charts,audio,report,filenames={k:k+'.mc' for k in charts})
    return archive, report


def read_charts(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {n:json.loads(z.read(n)) for n in z.namelist() if n.endswith('.mc')},z.read('0/audio.ogg')


def test_delivery_uses_creator_for_all_charts_and_preserves_source_and_old_package(tmp_path):
    archive, report = fixture_package(tmp_path)
    frozen = archive.read_bytes()
    _,_,old_path = library.publish(tmp_path,'old',archive,report)
    old_blob = old_path.read_bytes()
    old_charts,old_audio = read_charts(old_path)
    custom = {**report,'creator':'  Startrail  '}
    _,_,new_path = library.publish(tmp_path,'new',archive,custom)
    charts,audio = read_charts(new_path)
    assert all(c['meta']['creator']=='Startrail' for c in charts.values())
    for name,c in charts.items():
        assert c['note']==old_charts[name]['note']
        assert c['time']==old_charts[name]['time']
    assert audio==old_audio
    assert old_path.read_bytes()==old_blob and archive.read_bytes()==frozen
    assert custom['creator']=='  Startrail  '
    assert json.loads((new_path.parent/'report.json').read_text(encoding='utf-8'))['creator']=='Startrail'


def test_creator_changes_delivery_cache_identity(tmp_path):
    archive, report = fixture_package(tmp_path)
    _,first,path = library.publish(tmp_path,'temp-cache',archive,{**report,'creator':'A'})
    _,second,path = library.publish(tmp_path,'temp-cache',archive,{**report,'creator':'B'})
    assert first['identity']!=second['identity']
    charts,_ = read_charts(path)
    assert all(c['meta']['creator']=='B' for c in charts.values())
    _,reused,_ = library.publish(tmp_path,'temp-cache',archive,{**report,'creator':'B'})
    assert reused==second


def test_single_chart_creator_override_survives_delivery_without_changing_siblings(tmp_path):
    archive,report = fixture_package(tmp_path)
    report['creator']='Original'
    report['charts'][0]['creator']='Only Easy'
    _,first,path=library.publish(tmp_path,'mixed-creators',archive,report)
    charts,_=read_charts(path)
    assert charts['0/balanced--easy.mc']['meta']['creator']=='Only Easy'
    assert charts['0/balanced--hard.mc']['meta']['creator']=='Original'
    report['charts'][0]['creator']='New Easy'
    _,second,path=library.publish(tmp_path,'mixed-creators',archive,report)
    assert first['identity']!=second['identity']
    assert read_charts(path)[0]['0/balanced--easy.mc']['meta']['creator']=='New Easy'


@pytest.mark.parametrize('invalid',['','  ',None,'華'*121,'坏\n名字'])
def test_invalid_creator_does_not_publish(tmp_path,invalid):
    archive,report = fixture_package(tmp_path)
    baseline = copy.deepcopy(report)
    with pytest.raises(ValueError):
        library.publish(tmp_path,'invalid',archive,{**report,'creator':invalid})
    assert library.index(tmp_path)=={} and report==baseline
