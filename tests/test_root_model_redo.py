"""Root redo actions retain model supply and refresh repaired defaults."""
import copy
import json
import pytest
from collections import Counter

from malody_studio import server
from malody_studio.charts import Note, chart_stats, serialize
from malody_studio.quality_workflow import candidate_contract


@pytest.mark.parametrize('has_models',[True,False])
def test_old_acoustic_cache_redo_preserves_only_real_model_heads(tmp_path,monkeypatch,has_models):
    job_id='7'*32;directory=tmp_path/'outputs'/job_id;song=directory/'0'
    song.mkdir(parents=True)
    raw=[Note(1000.,0,1600.),Note(1000.,1),Note(1010.,2)]
    chart=serialize(raw,'Song','Artist','Easy',120.)
    chart_bytes=json.dumps(chart).encode('utf-8')
    (song/'easy.mc').write_bytes(chart_bytes)
    archive=directory/'malody-4k.mcz';archive.write_bytes(b'original archive evidence')
    cache={'duration_ms':4000.,'bpm':120.,'engine':'mug','model_version':'fixture',
           'candidate_policy':{'selection_policy':'ranked_beam'},
           'candidates':[[1000.,1.,[[1000.,0,1600.],[1000.,1,None]]],
                         [1010.,1.,[[1010.,2,None]]],
                         *[[float(t),100.,[]] for t in range(100,3900,100) if t!=1000]]}
    if not has_models:cache['candidates']=cache['candidates'][2:]
    cache_path=directory/'generation-cache.json'
    cache_path.write_text(json.dumps(cache),encoding='utf-8');cache_bytes=cache_path.read_bytes()
    report={'duration':4.,'quality_alerts':[],'previews':{'easy':[[n.start,n.lane,n.end] for n in raw]},
            'difficulties':[{'key':'easy',**chart_stats(raw,4.),'validation':{'valid':True}}]}
    job={'id':job_id,'title':'Song','artist':'Artist','status':'completed',
         'options':{'seed':42,'engine':'mug','ln_ratio':.15,'difficulty_rules':{}},'report':report}
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(server,'jobs',{job_id:job})
    monkeypatch.setattr(server,'_save_report_and_package',lambda *args:None)
    from malody_studio import difficulty,quality
    monkeypatch.setattr(quality,'assess',lambda *args:[])
    def no_expansion(*args,**kwargs):raise AssertionError('root redo invoked legacy chord expansion')
    monkeypatch.setattr(difficulty,'calibrate',no_expansion)
    if not has_models:
        from fastapi import HTTPException
        with pytest.raises(HTTPException,match='可用模型音符') as error:
            server.regenerate_difficulty(job_id,'easy',{'rule':{'rate':20.,'chord':4,'hold_ms':2000}})
        assert error.value.status_code==409
        assert (song/'easy.mc').read_bytes()==chart_bytes
        assert archive.read_bytes()==b'original archive evidence'
        assert cache_path.read_bytes()==cache_bytes
        assert not (directory/'versions').exists()
        return
    result=server.regenerate_difficulty(job_id,'easy',{'rule':{'rate':20.,'chord':4,'hold_ms':2000}})
    preview=result['report']['previews']['easy']
    assert Counter(tuple(row) for row in preview)==Counter((n.start,n.lane,n.end) for n in raw)
    from malody_studio.advanced import chart_events
    written=chart_events(json.loads((directory/'versions'/'easy'/'v2.mc').read_text(encoding='utf-8')))
    assert len(written)==len(raw)
    assert sum(abs(event['start_ms']-1000.)<1 for event in written)==2
    assert any(abs(event['start_ms']-1000.)<1 and event['end_ms'] is not None
               and abs(event['end_ms']-1600.)<1 for event in written)
    adjusted=result['report']['difficulties'][0]['difficulty_adjustment']
    assert adjusted['selection_policy']=='model_only'
    assert adjusted['selected_acoustic_heads']==adjusted['candidate_acoustic_heads']==0
    assert adjusted['selected_model_heads']==3
    assert adjusted['budget_passes']==1
    assert (directory/'versions'/'easy'/'v1.mc').read_bytes()==chart_bytes
    assert (directory/'versions'/'easy'/'v1.mcz').read_bytes()==b'original archive evidence'
    assert cache_path.read_bytes()==cache_bytes


def test_full_root_redo_refreshes_candidate_policy_without_rewriting_old_snapshot(tmp_path,monkeypatch):
    job_id='8'*32;source=tmp_path/'outputs'/job_id/'0'/'audio.ogg'
    source.parent.mkdir(parents=True);source.write_bytes(b'existing audio')
    old={'seed':42,'engine':'mug','difficulties':['easy'],
         'candidate_policy':candidate_contract('evidenced-single-heads-v2')}
    before=copy.deepcopy(old)
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{job_id:{'id':job_id,'status':'completed','title':'Song',
                                            'artist':'Artist','options':old}})
    monkeypatch.setattr(server,'ensure_engine',lambda *args:None)
    captured=[]
    monkeypatch.setattr(server,'new_job',lambda path,options:captured.append((path,options)) or {'id':'new'})
    assert server.regenerate_job(job_id)=={'id':'new'}
    assert captured[0][0]==source
    assert captured[0][1]['candidate_policy']==candidate_contract()
    assert captured[0][1]['candidate_policy']['selection_policy']=='model_only'
    assert captured[0][1]['candidate_policy']['audio_vote_cap']==0.
    assert old==before
