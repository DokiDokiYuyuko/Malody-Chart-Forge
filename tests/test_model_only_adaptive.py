from malody_studio.adaptive_difficulty import Candidate, calibrate_adaptive, model_candidates
from malody_studio.charts import Note


def test_model_only_retains_detector_support_without_independent_heads():
    acoustic={'source':{'sample_rate':1000},'onset_samples':[100,400,800],
              'onset_strengths':[1.,3.,2.]}
    notes=[Note(100,0),Note(100,2),Note(800,1,1100)]
    candidates=model_candidates(notes,acoustic)
    assert [c[0] for c in candidates]==[100,800]
    assert [n for c in candidates for n in c[2]]==notes
    assert candidates[0].support['detections']
    legacy=model_candidates(notes,acoustic,selection_policy='ranked_beam')
    assert any(not c[2] and c[0]==400 for c in legacy)


def test_model_only_filters_external_audio_candidates_and_consumes_phrase_budget_once():
    candidates=[Candidate(100,1.,[Note(100,0),Note(100,2)]),
                Candidate(300,100.,[]),Candidate(900,2.,[Note(900,1)]),
                Candidate(1300,100.,[])]
    notes,report=calibrate_adaptive(candidates,2000,'hard',.15,42,
        selection_policy='model_only',audio_vote_cap=0)
    assert {n.start for n in notes}=={100,900}
    assert len(notes)==3
    assert report['candidate_acoustic_heads']==report['selected_acoustic_heads']==0
    assert report['budget_passes']==1
    phrases=report['section_stats'][0]['phrase_stats']
    assert len(phrases)==1 and not phrases[0]['redistributed']
    assert phrases[0]['unspent_heads']>0


def test_worker_execution_change_invalidates_raw_cache_without_mutating_revision(tmp_path,monkeypatch):
    from malody_studio import stem_generation
    monkeypatch.setattr(stem_generation,'ROOT',tmp_path)
    worker=tmp_path/'tools'/'mapperatorinator_worker.py'
    worker.parent.mkdir()
    worker.write_text('old lane conversion',encoding='utf-8')
    settings={'engine':'v32','seed':42}
    descriptor={'pcm_sha':'frozen-pcm','source_role':'vocals'}
    segment={'start_sample':0,'end_sample':44100}
    previous_recipe=stem_generation._raw_key(settings,descriptor,segment,'balanced--hard')
    revision={'provenance':{'raw_recipe_hash':previous_recipe},'events':[{'start_ms':100,'lane':0}]}
    worker.write_text('preserve model heads and lane holds',encoding='utf-8')
    assert stem_generation._raw_key(settings,descriptor,segment,'balanced--hard')!=previous_recipe
    assert revision=={'provenance':{'raw_recipe_hash':previous_recipe},'events':[{'start_ms':100,'lane':0}]}


def test_simple_classic_default_uses_only_model_heads_without_chord_inflation(tmp_path,monkeypatch):
    import numpy as np
    from malody_studio import pipeline, mapperatorinator, difficulty, quality, library
    audio=tmp_path/'audio.ogg';audio.write_bytes(b'test audio')
    (tmp_path/'tail-analysis.json').write_text('{}',encoding='utf-8')
    monkeypatch.setattr(pipeline,'convert',lambda *args:(np.ones(22050*8,dtype=np.float32),22050,8.,audio))
    monkeypatch.setattr(pipeline,'analyze',lambda *args:{'bpm':120.,'warnings':[]})
    raw=[Note(500+i*300,i%4) for i in range(20)]
    monkeypatch.setattr(mapperatorinator,'generate',lambda *args:({'lunatic':(raw,[[0,120]],0)},{}))
    monkeypatch.setattr(quality,'assess',lambda *args:[])
    monkeypatch.setattr(library,'publish',lambda *args,**kwargs:None)
    def forbidden(*args,**kwargs):
        raise AssertionError('default classic generation must not manufacture acoustic keys')
    monkeypatch.setattr(difficulty,'attacks',forbidden)
    options={'title':'test','artist':'test','engine':'v32','seed':42,'ln_ratio':.15,
             'difficulties':['lunatic'],'patterns':['balanced'],'dynamic_enabled':False}
    report,_=pipeline.run(audio,tmp_path,options,lambda *_:None)
    preview=report['previews']['balanced--lunatic']
    assert {(row[0],row[1]) for row in preview}=={(note.start,note.lane) for note in raw}
    adjustment=report['difficulties'][0]['difficulty_adjustment']
    assert adjustment['selection_policy']=='model_only'
    assert adjustment['selected_acoustic_heads']==0


def test_model_only_enforces_frozen_region_caps_across_the_boundary():
    plan={'sample_rate':1000,'sections':[
        {'id':'quiet','core':[0,1000],'active_seconds':1.,'profile':[{'start_sample':0,'end_sample':1000,'active_fraction':1.}], 'per_difficulty':{'lunatic':{
            'target_heads_soft':8,'hard_caps':{'peak_1s':1,'chord':1,'min_lane_gap_ms':100,'hold_max_ms':300}}}},
        {'id':'strong','core':[1000,2000],'active_seconds':1.,'profile':[{'start_sample':1000,'end_sample':2000,'active_fraction':1.}], 'per_difficulty':{'lunatic':{
            'target_heads_soft':10,'hard_caps':{'peak_1s':38,'chord':4,'min_lane_gap_ms':35,'hold_max_ms':1000}}}}]}
    candidates=[Candidate(900,1.,[Note(900,0,1500)]),Candidate(1100,1.,[Note(1100,1)]),
                Candidate(1900,1.,[Note(1900,2)])]
    notes,report=calibrate_adaptive(candidates,2000,'lunatic',.15,42,plan=plan,
        selection_policy='model_only',audio_vote_cap=0)
    assert [(note.start,note.end) for note in notes]==[(900,1200),(1900,None)]
    assert report['budget_passes']==1 and report['selected_acoustic_heads']==0


def test_new_rule_iteration_on_legacy_fast_cache_uses_model_only_and_preserves_parent(tmp_path,monkeypatch):
    import copy
    import json
    from types import SimpleNamespace
    from malody_studio import advanced, advanced_generation, paths
    from malody_studio.quality_workflow import candidate_contract
    settings=advanced.defaults();settings.update(strategy='fast',dynamic_enabled=False)
    revision={'id':'a'*32,'segment_id':'b'*32,'variant':'balanced--lunatic','range':[0,8*advanced.SR],
        'settings':settings,'events':[{'id':'old','start_ms':500,'lane':0,'end_ms':None}],
        'provenance':{'candidate_policy':candidate_contract('evidenced-single-heads-v1'),
                      'cache_job':'c'*32,'cache_file':'balanced-mother.json'}}
    original=copy.deepcopy(revision)
    cache=tmp_path/'outputs'/('c'*32);cache.mkdir(parents=True)
    (cache/'balanced-mother.json').write_text(json.dumps({'context_range':[0,8*advanced.SR],
        'candidates':[[500,1.,[[500,0,None]]],[1000,100.,[]],[2000,1.,[[2000,1,None]]]]}),encoding='utf-8')
    monkeypatch.setattr(paths,'ROOT',tmp_path)
    def save(pid,sid,variant,events,settings,kind,provenance,**kwargs):
        return {'events':events,'provenance':provenance}
    store=SimpleNamespace(revision=lambda *args:revision,load=lambda *args:{'duration':8},
        segment=lambda *args:{'id':revision['segment_id']},add_revision=save)
    result=advanced_generation.rule_candidate(store,'project',revision['id'],settings)
    assert {(event['start_ms'],event['lane']) for event in result['events']}=={(500,0),(2000,1)}
    assert result['provenance']['candidate_policy']==candidate_contract('model-heads-only-v3')
    assert result['provenance']['parent_candidate_policy']==original['provenance']['candidate_policy']
    assert revision==original
