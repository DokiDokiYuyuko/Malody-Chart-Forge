import copy
import numpy as np
from malody_studio import adaptive_difficulty as ad
from malody_studio.charts import Note


def acoustic(times, strengths=None):
    return {'id':'frozen','source':{'sample_rate':1000},'onset_samples':times,
            'onset_strengths':strengths or [1.]*len(times)}


def plan(target, phrases, end=2000):
    return {'sample_rate':1000,'arrangement_enabled':True,'dynamic_enabled':True,
            'sections':[{'id':'s','core':[0,end],
              'profile':[{'start_sample':0,'end_sample':end,'active_fraction':1.}],
              'per_difficulty':{'expert':{'target_heads_soft':target}},
              'phrases':[{'range':p} for p in phrases]}]}


def test_real_model_chord_hold_and_stagger_preserved_audio_same_sound_suppressed():
    raw=[Note(100.,0,500.),Note(100.,1),Note(131.,2),Note(149.,3)]
    before=copy.deepcopy(raw)
    candidates=ad.evidence_candidates(raw,acoustic([100,112,131,149,500,500]),
                                      extra_acoustic=[{**acoustic([500,502]),"id":"stem-copy"}])
    assert raw==before
    assert [c[0] for c in candidates if c[2]]==[100,131,149]
    assert [c[0] for c in candidates if not c[2]]==[500]
    assert len(candidates[0][2])==2 and candidates[0][2][0].end==500
    notes,report=ad.calibrate_adaptive(candidates,2000,'expert',0,42,plan=plan(12,[[0,2000]]))
    assert {n.start for n in notes}=={100,131,149,500}
    assert sum(n.start==100 for n in notes)==2
    assert sum(n.start==500 for n in notes)==1
    assert report['candidate_model_heads']==4 and report['candidate_acoustic_heads']==1
    assert any(d.get('candidate_provenance',{}).get('kind')=='acoustic' for d in report['decisions'])


def test_cached_multiscale_onsets_require_attack_proof_not_energy():
    proof={'sources':{'original':{'channels':[{'frame_ms':[100,200], 'energy':[1.,1.],
      'onsets':[{'time_ms':100.,'strength':1.,'multiscale_agreement':True,'scale_support':2},
                {'time_ms':200.,'strength':4.,'multiscale_agreement':False,'scale_support':1}]}]}}}
    candidates=ad.evidence_candidates([],proof)
    assert [c[0] for c in candidates]==[100]
    assert candidates[0].support['detections'][0]['channel']==0
    assert ad.evidence_candidates([],{'sources':{'original':{'channels':[{'energy':[5.]}]}}})==[]
    assert ad.evidence_candidates([],{'profile':[{'start_sample':0,'end_sample':1000,'exact_silence':True}],
        **acoustic([100])})==[]


def test_stereo_opposite_phase_attacks_do_not_cancel():
    sr=22050;t=np.arange(sr)/sr
    mono=(np.sin(2*np.pi*440*t)*np.where((t>.2)&(t<.6),.2,0)).astype(np.float32)
    stereo=np.column_stack([mono,-mono])
    assert ad.attacks_adaptive(stereo,sr,[])
    assert ad.attacks_adaptive(np.zeros_like(stereo),sr,[])==[]
    assert [c[0] for c in ad.attacks_adaptive(stereo,sr,[])]==[c[0] for c in ad.attacks_adaptive(mono,sr,[])]


def test_hard_conflict_refills_unused_real_candidates_inside_same_phrase_only():
    candidates=[(100.,4.,[Note(100.,i) for i in range(4)]),
                (101.,3.9,[Note(101.,0)]),
                (600.,2.,[]),(900.,1.,[]), (1800.,1.,[])]
    notes,report=ad.calibrate_adaptive(candidates,2000,'expert',0,42,
        overrides={'chord':4,'gap':100,'peak':20},plan=plan(12,[[0,1000],[1000,2000]]))
    first=[n for n in notes if n.start<1000]
    assert len(first)==6 and {n.start for n in first}=={100,600,900}
    assert len([n for n in notes if n.start>=1000])==1
    stats=report['section_stats'][0]['phrase_stats']
    assert [s['target_heads'] for s in stats]==[6,6]
    assert stats[0]['refilled_after_hard_conflict']>0
    assert stats[1]['unspent_heads']==5


def test_empty_phrase_budget_never_repeated_or_borrowed():
    candidates=[(1100+i*100.,1.,[]) for i in range(8)]
    notes,report=ad.calibrate_adaptive(candidates,2000,'expert',0,42,plan=plan(8,[[0,1000],[1000,2000]]))
    assert len(notes)==4
    stats=report['section_stats'][0]['phrase_stats']
    assert stats[0]['selected_heads']==0 and stats[0]['unspent_heads']==4
    assert sum(s['target_heads'] for s in stats)==8


def test_fusion_cached_original_audio_is_singleton_beside_real_chord_and_stagger():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'speed--expert','range':[0,88200],
          'settings':{},'events':events,'provenance':{'source_role':role,'source_id':role,
          'stem_set_id':'s','parent_source_id':'pcm'}}
    def event(t,lane,identity,end=None):
        return {'id':identity,'start_ms':t,'lane':lane,'end_ms':end}
    v=rev('vocals',[event(100.,0,'v',500.),event(100.,1,'chord')])
    a=rev('accompaniment',[event(131.,2,'a')])
    frozen={'id':'p','source_pcm_sha':'pcm','sections':[{'id':'s','core':[0,88200],
      'perDifficulty':{'expert':{'target_heads':10,'hard_caps':{'peak_1s':19,'chord':3,'min_lane_gap_ms':40}}}}]}
    before=copy.deepcopy((v,a,frozen))
    result=fuse_revisions(v,a,frozen,acoustic=acoustic([100,112,131,600,600]),selection_policy='ranked_beam')
    assert (v,a,frozen)==before
    assert sum(e['start_ms']==100 for e in result['events'])==2
    assert sum(e['start_ms']==600 for e in result['events'])==1
    assert {e['start_ms'] for e in result['events']}=={100,131,600}
    audio=next(e for e in result['events'] if e['start_ms']==600)
    assert audio['end_ms'] is None and audio['candidate_provenance']['kind']=='acoustic'
    assert audio['origins'][0]['candidate_kind']=='acoustic'
    assert result['stats'][0]['candidate_acoustic_heads']==1


def test_distinct_strong_peaks_within_detector_survive_nearby_cross_channel_copies():
    onsets=[{'time_ms':t,'strength':1.,'multiscale_agreement':True,'uncertainty_ms':3.,'scale_support':2}
            for t in (100.,108.)]
    copies=[{**o,'time_ms':o['time_ms']+1} for o in onsets]
    frozen={'id':'s','sources':{'original':{'channels':[{'onsets':onsets},{'onsets':copies}]}}}
    candidates=ad.evidence_candidates([],frozen)
    assert [c[0] for c in candidates]==[100.,108.]
    assert all(len(c.support['detections'])==2 for c in candidates)


def test_fusion_preceding_region_rolling_peak_and_hold_occupancy_continue():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'balanced--expert','range':[44100,88200],
            'settings':{},'events':events,'provenance':{'source_role':role,'source_id':role,
            'stem_set_id':'s','parent_source_id':'pcm'}}
    previous=[{'id':'previous','start_ms':900.,'lane':0,'end_ms':1800.}]
    measured={'id':'p','sections':[{'core':[44100,88200],
            'perDifficulty':{'expert':{'target_heads':5,'hard_caps':{'peak_1s':2}}}}]}
    v=rev('vocals',[{'id':'a','start_ms':1100.,'lane':0,'end_ms':None}])
    a=rev('accompaniment',[{'id':'b','start_ms':1200.,'lane':1,'end_ms':None}])
    before=copy.deepcopy(previous)
    result=fuse_revisions(v,a,measured,preceding_events=previous)
    assert previous==before
    assert len(result['events'])==1
    assert result['events'][0]['lane']!=0
    assert result['provenance']['preceding_constraints_hash']


def test_fusion_soft_budget_keeps_real_model_chord_whole_or_omits_whole():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'balanced--expert','range':[0,44100],
            'settings':{},'events':events,'provenance':{'source_role':role,'source_id':role,
            'stem_set_id':'s','parent_source_id':'pcm'}}
    voices=[{'id':str(i),'start_ms':100.,'lane':i,'end_ms':None} for i in range(4)]
    p={'id':'p','sections':[{'core':[0,44100],'perDifficulty':{'expert':{
       'target_heads':1,'hard_caps':{'chord':4,'peak_1s':19,'min_lane_gap_ms':55}}}}]}
    result=fuse_revisions(rev('vocals',voices),rev('accompaniment',[]),p,acoustic=acoustic([600]))
    assert sum(e['start_ms']==100 for e in result['events'])==0
    assert result['stats'][0]['selected_partial_model_chords']==0
    p['sections'][0]['perDifficulty']['expert']['target_heads']=8
    result=fuse_revisions(rev('vocals',voices),rev('accompaniment',[]),p,acoustic=acoustic([600]))
    assert sum(e['start_ms']==100 for e in result['events'])==4
    assert result['stats'][0]['selected_complete_model_chords']==1


def test_adaptive_soft_quota_never_cuts_a_real_model_chord_to_make_room():
    group=(100.,3.,[Note(100.,i) for i in range(4)])
    candidates=[(50.,4.,[]),group,(800.,1.,[])]
    notes,report=ad.calibrate_adaptive(candidates,2000,'expert',0,42,
        overrides={'chord':4,'peak':19},plan=plan(3,[[0,2000]]))
    assert not any(n.start==100 for n in notes)
    assert {n.start for n in notes}=={50,800}
    assert any(d.get('reason')=='frozen_phrase_budget_whole_model_chord' for d in report['decisions'])


import pytest

@pytest.mark.parametrize('channels',[(0,0),(0,1)])
def test_quality_keeps_independent_acoustic_singletons_at_original_times(channels):
    from malody_studio.chart_quality import apply
    events=[]
    for i,(timestamp,channel) in enumerate(zip((100.,108.),channels)):
        events.append({'id':str(i),'start_ms':timestamp,'end_ms':None,'lane':i,
            'candidate_provenance':{'kind':'acoustic'},
            'candidate_support':{'detections':[{'cache_id':'frozen','source_role':'original',
                'channel':channel,'time_ms':timestamp,'strength':2.,'kind':'multiscale_positive_spectral_flux'}]}})
    common={'time_ms':104.,'strength':2.,'uncertainty_ms':2.,'multiscale_agreement':True}
    evidence={'heads':{e['id']:{'onsets':[common]} for e in events}}
    before=copy.deepcopy(events)
    result=apply(events,{},evidence,'expert','balanced')
    assert events==before and result['events']==events
    assert not any(d['type']=='chord_aligned' for d in result['decisions'])
    assert any('acoustic' in d.get('reason','') for d in result['decisions'])


def test_quality_audio_singleton_veto_still_allows_actual_model_chord_alignment():
    from malody_studio.chart_quality import apply
    common={'time_ms':104.,'strength':2.,'uncertainty_ms':2.,'multiscale_agreement':True}
    model=[{'id':str(i),'start_ms':t,'end_ms':None,'lane':i,
            'candidate_provenance':{'kind':'model'},'model_group_id':'actual-model-group'}
            for i,t in enumerate((100.,108.))]
    result=apply(model,{}, {'heads':{e['id']:{'onsets':[common]} for e in model}},'expert','balanced')
    assert [e['start_ms'] for e in result['events']]==[104.,104.]
    assert result['summary']['heads_after']==2


@pytest.mark.parametrize('previous_channel,current_channel,separation,expected_audio',[
    (0,0,.5,0), (0,1,5.,0), (0,0,5.,1), (0,1,14.,1)])
def test_fusion_seam_audio_dedup_requires_detector_or_overlapping_onset_proof(previous_channel,current_channel,separation,expected_audio):
    from malody_studio.fusion import fuse_revisions
    def rev(role):
        return {'id':role,'variant':'balanced--expert','range':[44100,88200],
          'settings':{},'events':[], 'provenance':{'source_role':role,'source_id':role,
          'stem_set_id':'s','parent_source_id':'pcm'}}
    previous_time=1000.-separation
    proof={'cache_id':'frozen','source_role':'original','channel':previous_channel,
           'kind':'multiscale_positive_spectral_flux','uncertainty_ms':3.}
    previous=[{'id':'previous','start_ms':previous_time,'lane':0,'end_ms':None,
       'candidate_provenance':{'kind':'acoustic'},'candidate_support':{'detections':[proof]},
       'origins':[{'candidate_kind':'acoustic','note_id':'old-peak'}]}]
    sound={'id':'frozen','sources':{'original':{'channels':[{'onsets':[]} for _ in range(current_channel+1)]}}}
    sound['sources']['original']['channels'][current_channel]['onsets']=[{
       'time_ms':1000.,'strength':2.,'multiscale_agreement':True,'scale_support':2,'uncertainty_ms':3.}]
    plan={'id':'p','sections':[{'core':[44100,88200],'perDifficulty':{'expert':{'target_heads':4}}}]}
    before=copy.deepcopy(previous)
    result=fuse_revisions(rev('vocals'),rev('accompaniment'),plan,acoustic=sound,preceding_events=previous,selection_policy='ranked_beam')
    assert previous==before and len(result['events'])==expected_audio
    if not expected_audio:
        omission=next(d for d in result['decisions'] if d.get('reason')=='shared_acoustic_onset_already_selected_in_prefix')
        assert omission['reason_category']=='duplicate_acoustic_evidence'
        assert omission['prefix_event_id']=='previous' and omission['prefix_origins']


def test_fusion_omission_categories_only_proven_insertion_conflicts_are_hard():
    from malody_studio.fusion import _omission_category
    caps={'peak':2,'chord':4,'gap':55.,'release':35.,'hold':400.}
    item={'start_ms':500.,'end_ms':None}
    old=[{'start_ms':100.,'lane':0,'end_ms':None},{'start_ms':700.,'lane':1,'end_ms':None}]
    assert _omission_category(item,old,caps)==('hard_constraint',['rolling_peak_cap'])
    caps['peak']=19
    assert _omission_category(item,old,caps)==('soft_selection',[])
    occupied=[{'start_ms':100.,'lane':i,'end_ms':800.} for i in range(4)]
    kind,conflicts=_omission_category(item,occupied,caps)
    assert kind=='hard_constraint' and 'previous_hold_occupancy' in conflicts
    following=[{'start_ms':520.,'lane':i,'end_ms':None} for i in range(4)]
    assert _omission_category(item,following,caps)[0]=='hard_constraint'


def test_fusion_model_stagger_is_not_deleted_by_prefix_audio_dedup():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'balanced--expert','range':[44100,88200],
           'settings':{},'events':events,'provenance':{'source_role':role,'source_id':role,
           'stem_set_id':'s','parent_source_id':'pcm'}}
    previous=[{'id':'old','start_ms':999.5,'lane':0,'end_ms':None,'candidate_provenance':{'kind':'acoustic'},
       'candidate_support':{'detections':[{'cache_id':'frozen','source_role':'original','channel':0,
         'kind':'multiscale_positive_spectral_flux','uncertainty_ms':3.}]}}]
    event={'id':'model','start_ms':1000.,'lane':1,'end_ms':None}
    plan={'id':'p','sections':[{'core':[44100,88200],'perDifficulty':{'expert':{'target_heads':4}}}]}
    result=fuse_revisions(rev('vocals',[event]),rev('accompaniment',[]),plan,preceding_events=previous)
    assert [e['start_ms'] for e in result['events']]==[1000.]
    assert result['events'][0]['origins'][0]['note_id']=='model'


def test_acoustic_placeholder_lane_carries_no_model_lane_preference():
    from malody_studio.fusion import _lane_preference
    acoustic={'source_role':'original_acoustic','lane':0}
    assert [_lane_preference(acoustic,lane) for lane in range(4)]==[0.,0.,0.,0.]
    model={'source_role':'vocals','lane':2}
    assert [_lane_preference(model,lane) for lane in range(4)]==[0.,0.,.15,0.]


@pytest.mark.parametrize('tail',[None,1400.])
def test_fusion_seam_onset_supports_previous_real_model_tap_or_ln_without_extra_head(tail):
    from malody_studio.fusion import fuse_revisions
    def rev(role):
        return {'id':role,'variant':'balanced--expert','range':[44100,88200],'settings':{},'events':[],
            'provenance':{'source_role':role,'source_id':role,'stem_set_id':'s','parent_source_id':'pcm'}}
    previous=[{'id':'previous-model','start_ms':997.,'lane':0,'end_ms':tail,
       'candidate_provenance':{'kind':'model'},'origins':[{'note_id':'raw-model'}]}]
    plan={'id':'p','sections':[{'core':[44100,88200],'perDifficulty':{'expert':{'target_heads':4}}}]}
    result=fuse_revisions(rev('vocals'),rev('accompaniment'),plan,acoustic=acoustic([1000.]),preceding_events=previous)
    assert result['events']==[] and previous[0]['end_ms']==tail
    support=next(d for d in result['decisions'] if d['type']=='audio_support')
    assert support['prefix_event_ids']==['previous-model']
    assert len(support['candidate_support']['detections'])==1


def test_fusion_model_audio_proof_and_new_energy_layer_identity_survive_selection_and_omission():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'balanced--expert','range':[0,44100],'settings':{},'events':events,
            'provenance':{'source_role':role,'source_id':role,'stem_set_id':'s','parent_source_id':'pcm'}}
    model={'id':'raw','start_ms':100.,'lane':0,'end_ms':None,
           'audio_evidence':{'salience':.8,'audible':True,'rms':.1,'mixture_rms':.2}}
    plan={'id':'p','sections':[{'core':[0,44100],'perDifficulty':{'expert':{'target_heads':4}}}]}
    result=fuse_revisions(rev('vocals',[model]),rev('accompaniment',[]),plan,
        acoustic=acoustic([100.]),model_energy_evidence_id='new-versioned-energy')
    selected=result['events'][0]
    assert selected['candidate_provenance']['kind']=='model'
    assert selected['candidate_support']['detections']
    assert selected['candidate_support']['energy_evidence_id']=='new-versioned-energy'
    assert result['provenance']['model_energy_evidence_id']=='new-versioned-energy'
    plan['sections'][0]['perDifficulty']['expert']['target_heads']=0
    omitted=fuse_revisions(rev('vocals',[model]),rev('accompaniment',[]),plan,
        acoustic=acoustic([100.]),model_energy_evidence_id='new-versioned-energy')['decisions'][0]
    assert omitted['candidate_support']['detections']
    assert omitted['salience']==.8 and omitted['reason_category']=='soft_selection'


def test_model_skeleton_stage_reserves_late_real_chord_and_hold_before_audio_fill():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'balanced--expert','range':[0,88200],'settings':{},'events':events,
          'provenance':{'source_role':role,'source_id':role,'stem_set_id':'s','parent_source_id':'pcm'}}
    model=[{'id':'late-ln','start_ms':1700.,'lane':0,'end_ms':1980.},
           {'id':'late-tap','start_ms':1700.,'lane':1,'end_ms':None}]
    onset_times=[100.,200.,300.,400.,500.,600.,700.,800.,900.,1000.,1100.,1200.,1300.,1400.,1500.,1600.,1700.,1800.,1900.]
    sound={'id':'frozen','sources':{'original':{'channels':[{'onsets':[
       {'time_ms':t,'strength':1.,'multiscale_agreement':True,'scale_support':2,'uncertainty_ms':1.} for t in onset_times]}]}}}
    plan={'sections':[{'core':[0,88200],'phrases':[{'range':[0,88200]}],
       'perDifficulty':{'expert':{'target_heads':6,'hard_caps':{'peak_1s':19,'chord':3,'min_lane_gap_ms':55}}}}]}
    result=fuse_revisions(rev('vocals',model),rev('accompaniment',[]),plan,acoustic=sound,
       audio_vote_cap=1.,selection_policy='model_skeleton_then_audio')
    selected=result['events']
    raw=[e for e in selected if e['candidate_provenance']['kind']=='model']
    assert len(raw)==2 and {e['lane'] for e in raw}=={0,1}
    assert next(e for e in raw if e['lane']==0)['end_ms']==1980.
    assert len(selected)<=8 and result['stats'][0]['fixed_qualified_skeleton_heads']==2
    for e in selected:
        if e['candidate_provenance']['kind']=='acoustic':
            assert e['end_ms'] is None
            assert not(e['lane']==0 and 1700.-55<e['start_ms']<1980.+35)
    assert result['stats'][0]['selected_complete_model_chords']==1


def test_model_skeleton_eligibility_does_not_treat_energy_as_onset_or_sustain():
    from malody_studio.fusion import fuse_revisions
    def rev(role,events):
        return {'id':role,'variant':'balanced--expert','range':[0,44100],'settings':{},'events':events,
          'provenance':{'source_role':role,'source_id':role,'stem_set_id':'s','parent_source_id':'pcm'}}
    raw={'id':'raw-energy','start_ms':500.,'lane':0,'end_ms':None,'audio_evidence':{'salience':2.,'audible':True}}
    sound={'id':'energy-only','sources':{'original':{'channels':[{'onsets':[]}]}}}
    plan={'sections':[{'core':[0,44100],'perDifficulty':{'expert':{'target_heads':3}}}]}
    result=fuse_revisions(rev('vocals',[raw]),rev('accompaniment',[]),plan,acoustic=sound,
      selection_policy='model_skeleton_then_audio',audio_vote_cap=1.)
    assert result['stats'][0]['fixed_qualified_skeleton_heads']==0
    assert result['events'][0]['candidate_support']['selection_stage']=='same_phrase_unused_quota_fill'
    proof=result['events'][0]['candidate_support']['skeleton_eligibility']
    assert not proof['multiscale_onset'] and not proof['spectral_sustain']


def test_quality_alignment_rolls_back_instead_of_exceeding_capped_model_hold_duration():
    from malody_studio.chart_quality import apply
    common={'time_ms':104.,'strength':2.,'uncertainty_ms':2.,'multiscale_agreement':True}
    model=[{'id':'tap','start_ms':100.,'end_ms':None,'lane':0,'model_group_id':'raw-group'},
           {'id':'ln','start_ms':108.,'end_ms':308.,'lane':1,'model_group_id':'raw-group'}]
    evidence={'heads':{e['id']:{'onsets':[common],'sustain_support':1.,'important_sustain':True} for e in model}}
    settings={'ln_ratio':1.,'difficulty_rules':{'expert':{'hold_ms':200.}}}
    result=apply(model,settings,evidence,'expert','balanced')
    assert result['events']==model
    assert any(d.get('reason')=='hold_duration_cap' for d in result['decisions'])


def test_insertion_checks_only_affected_sections_rolling_caps_and_pair_gaps():
    from malody_studio.fusion import _insertion_lanes
    caps={'peak':4,'chord':4,'gap':50.,'release':35.,'hold':400.,'section_caps':[
       {'range_ms':[0.,1000.],'peak':1,'gap':200.,'release':35.},
       {'range_ms':[1000.,2000.],'peak':4,'gap':50.,'release':35.}]}
    old=[{'start_ms':900.,'lane':0,'end_ms':None}]
    assert _insertion_lanes({'start_ms':1100.,'end_ms':None},old,caps)==([],['rolling_peak_cap'])
    caps['section_caps'][0]['peak']=4
    lanes,_=_insertion_lanes({'start_ms':1050.,'end_ms':None},old,caps)
    assert 0 not in lanes and set(lanes)=={1,2,3}
    assert 0 in _insertion_lanes({'start_ms':1100.,'end_ms':None},old,caps)[0]
    assert _insertion_lanes({'start_ms':2500.,'end_ms':None},old,caps)[0]==[0,1,2,3]


@pytest.mark.parametrize('model_heads',[1,4])
def test_whole_chart_skeleton_pass_protects_next_region_model_chord_from_prefix_audio(model_heads):
    from malody_studio.fusion import fuse_revisions
    def rev(role,bounds,events):
        return {'id':role+str(bounds[0]),'variant':'balanced--expert','range':bounds,'settings':{},'events':events,
          'provenance':{'source_role':role,'source_id':role,'stem_set_id':'s','parent_source_id':'pcm'}}
    sections=[{'id':str(i),'core':[i*44100,(i+1)*44100],
       'perDifficulty':{'expert':{'target_heads':6,'hard_caps':{'chord':4,'peak_1s':19,'min_lane_gap_ms':55}}}} for i in range(2)]
    sound={'id':'frozen','sources':{'original':{'channels':[{'onsets':[
      {'time_ms':t,'strength':1.,'multiscale_agreement':True,'scale_support':2} for t in (900.,998.,1001.)]}]}}}
    plan={'sections':sections}
    early=(rev('vocals',[0,44100],[]),rev('accompaniment',[0,44100],[]))
    late=(rev('vocals',[44100,88200],[{'id':str(i),'start_ms':1001.,'lane':i,'end_ms':None} for i in range(model_heads)]),rev('accompaniment',[44100,88200],[]))
    kwargs={'evidence':sound,'audio_vote_cap':1.,'selection_policy':'model_skeleton_then_audio'}
    bones0=fuse_revisions(*early,plan,fill_unused_quota=False,**kwargs)['events']
    bones1=fuse_revisions(*late,plan,preceding_events=bones0,fill_unused_quota=False,**kwargs)['events']
    first=fuse_revisions(*early,plan,fixed_skeleton_events=bones0,following_events=bones1,**kwargs)
    final=fuse_revisions(*late,plan,fixed_skeleton_events=bones1,preceding_events=first['events'],**kwargs)
    assert len(final['events'])==model_heads and all(e['start_ms']==1001. for e in final['events'])
    assert any(d.get('reason')=='nearby_onset_supports_following_model_head' for d in first['decisions'])
    assert not any(e['start_ms']==998. for e in first['events'])
    assert any(e['start_ms']==900. for e in first['events'])


@pytest.mark.parametrize('energies,expected', [([0.,0.],0),([0.,1.],1)])
def test_v2_stem_singleton_uses_frozen_original_floor_independently_per_channel(energies,expected):
    sound={'id':'frozen','sources':{'original':{'channels':[
       {'onsets':[],'frame_ms':[100.],'energy':[value],'audible_floor':.1} for value in energies]},
       'vocals':{'channels':[{'onsets':[{'time_ms':100.,'strength':2.,'scale_support':2,'multiscale_agreement':True}]}]}}}
    pool=ad.evidence_candidates([],sound,require_original_audibility=True)
    assert len(pool)==expected
    if expected:assert pool[0].support['detections'][0]['original_audibility']['verified']
    raw=ad.evidence_candidates([Note(100.,0)],sound,require_original_audibility=True)
    assert raw[0][2] and raw[0].support['detections']


def test_adaptive_all_phrase_skeleton_is_fixed_before_earlier_phrase_hold_fill():
    notes=[Note(900.,0,1400.)]+[Note(1100.,i) for i in range(4)]
    sound={'id':'frozen','sources':{'original':{'channels':[{'onsets':[
       {'time_ms':1100.,'strength':1.,'scale_support':2,'multiscale_agreement':True}]}]}}}
    pool=ad.evidence_candidates(notes,sound)
    result,report=ad.calibrate_adaptive(pool,2000.,'expert',1.,42,overrides={'chord':4},
       plan=plan(12,[[0,1000],[1000,2000]]),evidence=sound,selection_policy='model_skeleton_then_audio',audio_vote_cap=1.)
    assert sum(n.start==1100. for n in result)==4
    assert not any(n.start==900. for n in result)
    assert sum(r['fixed_qualified_skeleton_heads'] for r in report['section_stats'][0]['phrase_stats'])==4


def test_custom_sub_35ms_tap_gap_does_not_apply_long_note_release_rule():
    from malody_studio.fusion import _insertion_lanes
    caps={'peak':19,'chord':4,'gap':10.,'release':35.,'hold':400.}
    old=[{'start_ms':100.,'lane':0,'end_ms':None}]
    assert 0 in _insertion_lanes({'start_ms':120.,'end_ms':None},old,caps)[0]
    following=[{'start_ms':140.,'lane':0,'end_ms':None}]
    assert 0 in _insertion_lanes({'start_ms':120.,'end_ms':None},following,caps)[0]
    assert 0 not in _insertion_lanes({'start_ms':120.,'end_ms':250.},following,caps)[0]


def test_v2_stem_singleton_without_original_frames_is_unknown_and_not_added():
    sound={'id':'stem-only','sources':{'vocals':{'channels':[{'onsets':[
       {'time_ms':100.,'strength':2.,'multiscale_agreement':True,'scale_support':2}]}]}}}
    assert ad.evidence_candidates([],sound,require_original_audibility=True)==[]
    assert len(ad.evidence_candidates([],sound))==1
    supplied=ad.evidence_candidates([Note(100.,0)],sound,require_original_audibility=True)
    assert len(supplied)==1 and supplied[0][2] and supplied[0].support['detections']


def test_adaptive_selected_candidate_proof_keeps_its_own_detection_identity():
    sound={'id':'frozen','sources':{'original':{'channels':[{'onsets':[
       {'time_ms':t,'strength':1.,'scale_support':2,'multiscale_agreement':True} for t in (100.,400.,1100.)]}]}}}
    pool=ad.evidence_candidates([Note(1100.,2)],sound)
    _,report=ad.calibrate_adaptive(pool,2000.,'expert',0.,42,plan=plan(6,[[0,2000]]),
      evidence=sound,selection_policy='model_skeleton_then_audio',audio_vote_cap=1.)
    selected=[d for d in report['decisions'] if d['type']=='selected']
    assert len(selected)==3
    for d in selected:
        assert {p['time_ms'] for p in d['candidate_support']['detections']}=={d['start_ms']}
