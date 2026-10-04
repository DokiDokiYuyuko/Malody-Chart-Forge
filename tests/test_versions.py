import json
import zipfile
from malody_studio.charts import Note, serialize, chart_stats, package
from fastapi.testclient import TestClient
from malody_studio import server


def test_single_tier_regeneration_versions_and_restores_without_changing_other_difficulty(monkeypatch,tmp_path):
    job_id='9'*32;directory=tmp_path/'outputs'/job_id;song=directory/'0';song.mkdir(parents=True)
    audio=tmp_path/'audio.ogg';audio.write_bytes(b'ogg-test-data')
    notes_easy=[Note(500+i*700,i%4) for i in range(20)]
    notes_medium=[Note(400+i*600,(i+1)%4) for i in range(25)]
    charts={'easy':serialize(notes_easy,'Song','Artist','4K Easy',120),
            'medium':serialize(notes_medium,'Song','Artist','4K Medium',120)}
    easy_stats=chart_stats(notes_easy,20);medium_stats=chart_stats(notes_medium,20)
    previews={key:[[round(n.start,2),n.lane,None] for n in notes] for key,notes in [('easy',notes_easy),('medium',notes_medium)]}
    report={'title':'Song','artist':'Artist','duration':20,'bpm':120,'engine':'MuG Diffusion v1.0.0',
        'warnings':[],'quality_alerts':[],'previews':previews,'artwork':{},'difficulties':[
            {'key':'easy','label':'Easy',**easy_stats,'difficulty_adjustment':{'target_active_nps':2.5},'validation':{'valid':True}},
            {'key':'medium','label':'Medium',**medium_stats,'difficulty_adjustment':{'target_active_nps':5},'validation':{'valid':True}}]}
    package(directory,charts,audio,report)
    cache={'format':1,'duration_ms':20000,'bpm':120,'engine':'mug','model_version':'MuG Diffusion v1.0.0',
        'candidates':[[float(i*400),1.0,[]] for i in range(1,48)],'timings':{},'raw_counts':{'easy':48},'options':{}}
    (directory/'generation-cache.json').write_text(json.dumps(cache),encoding='utf-8')
    job={'id':job_id,'title':'Song','artist':'Artist','status':'completed','created':'now','queue_order':1,
        'options':{'title':'Song','artist':'Artist','engine':'mug','difficulties':['easy','medium'],'seed':7,
                   'ln_ratio':.15,'difficulty_rules':{}},'report':report,'download':f'/api/jobs/{job_id}/download'}
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(server,'jobs',{job_id:job})
    original_medium=(song/'medium.mc').read_bytes()
    with TestClient(server.app) as client:
        result=client.post(f'/api/jobs/{job_id}/difficulties/easy/regenerate',json={'seed':21,'rule':{'rate':3.5}})
        assert result.status_code==200,result.text
        assert result.json()['active']==2
        with zipfile.ZipFile(directory/'malody-4k.mcz') as archive:
            assert archive.testzip() is None
            assert archive.read('0/medium.mc')==original_medium
        versions=client.get(f'/api/jobs/{job_id}/difficulties/easy/versions').json()
        assert [item['version'] for item in versions['versions']]==[1,2]
        assert (directory/'versions/easy/v1.mcz').is_file()
        assert (directory/'versions/easy/v2.mcz').is_file()
        restored=client.post(f'/api/jobs/{job_id}/difficulties/easy/restore',json={'version':1})
        assert restored.status_code==200,restored.text
        assert restored.json()['active']==1
        assert (song/'medium.mc').read_bytes()==original_medium
