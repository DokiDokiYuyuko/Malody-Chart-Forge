import copy
import tempfile
import unittest
from pathlib import Path
import numpy as np
import soundfile as sf
from malody_studio.chart_quality import apply, contract, summarize_region
from malody_studio.event_evidence import analyze_audio, cached_evidence, head_evidence


def events(times, tails=None, lanes=None):
    return [dict(id=str(i), start_ms=t, end_ms=(tails or {}).get(i), lane=(lanes or list(range(len(times))))[i],
                 origins=[dict(note_id=str(i), original_start_ms=t)]) for i, t in enumerate(times)]


def proof(rows, anchor=1000., **extra):
    return {'heads': {row['id']: {'onsets': [dict(time_ms=anchor, strength=2., uncertainty_ms=2.,
                                                multiscale_agreement=True, scale_support=2)], **extra} for row in rows}}


class QualityPolicyTests(unittest.TestCase):
    def test_region_summary_counts_existing_proof_without_reapplying_quality(self):
        raw=events([997.,1006.,2000.],{0:1400.,2:2300.})
        evidence=proof(raw)
        evidence['heads']['0'].update(sustain_support=.99,release_supported=True,rearticulations=0)
        evidence['heads']['2'].update(sustain_support=.1)
        result=apply(raw,{},evidence,'hard','balanced');original=copy.deepcopy(result)
        left=summarize_region(result['events'][:2],result['decisions'],before_events=raw[:2])
        right=summarize_region(result['events'][2:],result['decisions'])
        self.assertEqual(left['summary']['heads_before'],2)
        self.assertEqual(left['summary']['chord_corrected'],1)
        self.assertEqual(left['summary']['ln_before'],1)
        self.assertEqual(left['summary']['ln_after'],1)
        self.assertEqual(left['summary']['ln_protected'],1)
        self.assertEqual(right['summary']['heads_before'],1)
        self.assertEqual(right['summary']['chord_candidates'],0)
        self.assertEqual(right['summary']['ln_before'],1)
        self.assertEqual(right['summary']['ln_after'],0)
        self.assertEqual(right['summary']['ln_protected'],0)
        self.assertEqual(result,original)
        self.assertEqual({r['event_id'] for r in right['decisions']},{'2'})

    def test_region_summary_clips_group_proof_to_local_heads(self):
        raw=events([997.,1006.]);result=apply(raw,{},proof(raw),'hard','balanced')
        original=copy.deepcopy(result['decisions'])
        local=summarize_region(result['events'][:1],result['decisions'])
        moves=next(row['moves'] for row in local['decisions'] if row['type']=='chord_aligned')
        self.assertEqual([row['event_id'] for row in moves],['0'])
        self.assertEqual(result['decisions'],original)
        with self.assertRaises(ValueError):
            summarize_region(result['events'],result['decisions'],before_events=raw[:1])

    def test_tap_only_and_ln_head_only_accents_preserve_selected_supply(self):
        for tails in ({}, {0:1400.,1:1450.}):
            raw=events([997.,1006.],tails);original=copy.deepcopy(raw)
            result=apply(raw,{},proof(raw),'hard','balanced')
            self.assertEqual([e['start_ms'] for e in result['events']],[1000.,1000.])
            self.assertEqual([e['end_ms'] for e in result['events']],[tails.get(0),tails.get(1)])
            self.assertEqual({e['id'] for e in result['events']},{e['id'] for e in raw})
            self.assertEqual(raw,original)
            self.assertEqual(result['events'],apply(result['events'],{},proof(raw),'hard','balanced')['events'])

    def test_normal_chart_distinct_explicit_model_attacks_cannot_be_aligned(self):
        raw=events([997.,1006.],{0:1400.})
        for index,row in enumerate(raw):row['model_group_id']='attack-'+str(index)
        self.assertEqual(apply(raw,{},proof(raw),'hard','balanced')['events'],raw)

    def test_mixed_triple_true_accent_preserves_heads_origins_and_tail(self):
        raw=events([997., 1000., 1006.], {0:1400., 2:1450.}); original=copy.deepcopy(raw)
        result=apply(raw, {}, proof(raw), 'hard', 'balanced')
        self.assertEqual([e['start_ms'] for e in result['events']], [1000.]*3)
        self.assertEqual([e['end_ms'] for e in result['events']], [1400., None, 1450.])
        self.assertEqual(raw, original); self.assertEqual(len(result['events']), 3)
        self.assertEqual([e['origins'] for e in result['events']], [e['origins'] for e in raw])
        self.assertEqual(result['summary']['chord_corrected'],1)
        again=apply(result['events'], {}, proof(raw), 'hard', 'balanced')
        self.assertEqual(result['events'], again['events'])

    def test_proximity_and_leakage_identity_are_not_proof(self):
        raw=events([1000.,1008.]); raw[0]['audio_evidence']={'shared_event_id':'leak'}
        self.assertEqual(apply(raw, {}, {}, 'hard', 'balanced')['events'],raw)

    def test_independent_attack_and_subdivision_veto(self):
        raw=events([997.,1006.]); evidence=proof(raw, independent_onset=True)
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['events'],raw)
        evidence=proof(raw); evidence['heads']['1']['onsets'].append(dict(time_ms=1007.,strength=3.,uncertainty_ms=1.,multiscale_agreement=True))
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['events'],raw)

    def test_rolling_group_full_span_not_chain(self):
        raw=events([990.,1000.,1010.]); output=apply(raw,{},proof(raw),'hard','balanced')['events']
        self.assertEqual(output[-1]['start_ms'],1010.)

    def test_fine_subdivision_and_strict_model_required(self):
        raw=events([997.,1006.])
        self.assertEqual(apply(raw,{},proof(raw),'hard','balanced',timing={'finest_reliable_subdivision_ms':20})['events'],raw)
        self.assertEqual(apply(raw,{},proof(raw),'master','balanced')['events'],raw)
        raw=events([998.,1003.]); [r.update(model_group_id='group') for r in raw]
        result=apply(raw,{},proof(raw,subdivision_agreement=True),'master','balanced')
        self.assertEqual(result['summary']['chord_corrected'],1)

    def test_partial_origins_cannot_fake_strict_model_group(self):
        raw=events([998.,1003.])
        for row in raw:row['origins']=[{'stem_role':'vocals'}]
        self.assertEqual(apply(raw,{},proof(raw,subdivision_agreement=True),'master','balanced')['events'],raw)

    def test_high_difficulty_real_split_survives_even_with_group_labels(self):
        raw=events([999.,1004.],{0:1400.})
        for row in raw:row['model_group_id']='same'
        evidence=proof(raw,subdivision_agreement=True)
        evidence['heads']['1']['onsets'].append(dict(time_ms=1005.,strength=3.,uncertainty_ms=.5,multiscale_agreement=True))
        self.assertEqual(apply(raw,{},evidence,'lunatic','technical')['events'],raw)

    def test_uncertain_timing(self):
        raw=events([997.,1006.]); evidence=proof(raw)
        evidence['heads']['0']['onsets'][0]['uncertainty_ms']=12.
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['events'],raw)

    def test_weak_peak_cannot_veto_shared_accent(self):
        raw=events([997.,1006.]); evidence=proof(raw)
        evidence['heads']['1']['onsets'].append(dict(time_ms=1007.,strength=.7,uncertainty_ms=1.,multiscale_agreement=True))
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['summary']['chord_corrected'],1)
        evidence['heads']['1']['onsets'][-1].update(strength=3.,multiscale_agreement=False)
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['summary']['chord_corrected'],1)

    def test_unresolved_windows_are_not_independent_attack_proof(self):
        raw=events([997.,1006.]); evidence=proof(raw)
        evidence['heads']['1']['onsets'].append(dict(time_ms=1004.,strength=2.,uncertainty_ms=3.,multiscale_agreement=True))
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['summary']['chord_corrected'],1)
        evidence['heads']['1']['onsets'][-1].update(time_ms=1007.,uncertainty_ms=1.)
        self.assertEqual(apply(raw,{},evidence,'hard','balanced')['events'],raw)

    def test_lane_conflict_rolls_whole_group_back(self):
        raw=events([925.,997.,1006.],lanes=[0,0,1]); result=apply(raw,{},proof(raw),'hard','balanced')
        self.assertEqual(result['events'],raw)
        self.assertEqual(result['summary']['chord_retained'],1)

    def test_region_seam_and_short_hold_roll_back(self):
        raw=events([997.,1006.]); plan={'region_boundaries':[1000*44.1]}
        self.assertEqual(apply(raw,{},proof(raw),'hard','balanced',plan=plan)['events'],raw)
        raw=events([997.,1006.], {0:1098.})
        self.assertEqual(apply(raw,{},proof(raw),'hard','balanced')['events'],raw)

    def test_exact_mixed_chord_untouched(self):
        raw=events([1000.,1000.,1000.],{0:1500.,1:1400.})
        self.assertEqual(apply(raw,{},proof(raw),'hard','balanced')['events'],raw)

    def test_alignment_cannot_extend_hold_past_frozen_cap(self):
        raw=events([1003.,1006.],{0:1403.})
        evidence=proof(raw,sustain_support=.99,release_supported=True,rearticulations=0)
        result=apply(raw,{},evidence,'expert','balanced')
        self.assertEqual(result['events'],raw)
        self.assertEqual(result['decisions'][-1]['reason'],'hold_duration_cap')
        plan={'sections':[{'core':[0,88200],'per_difficulty':{'expert':{'hard_caps':{'hold_max_ms':403.}}}}]}
        result=apply(raw,{},evidence,'expert','balanced',plan=plan)
        self.assertEqual([r['start_ms'] for r in result['events']],[1000.,1000.])
        self.assertEqual(result['events'][0]['end_ms'],1403.)

    def test_hold_soft_goal_no_fabrication_and_exception(self):
        raw=events([1000.,2000.,3000.],{0:1300.,1:2300.,2:3300.})
        evidence=proof(raw,sustain_support=.85,rearticulations=0,release_supported=False)
        evidence['heads']['0'].update(sustain_support=.99,release_supported=True)
        result=apply(raw,{},evidence,'hard','balanced')
        self.assertEqual(result['summary']['ln_before'],3)
        self.assertEqual(result['summary']['ln_after'],1)
        self.assertEqual(result['summary']['ln_protected'],1)
        self.assertEqual([e['start_ms'] for e in result['events']],[1000.,2000.,3000.])

    def test_contract_is_immutable(self):
        value=contract();value['default_ln_ratio']=9
        self.assertEqual(contract()['default_ln_ratio'],.15)


class EvidenceTests(unittest.TestCase):
    def test_known_stem_sustain_cannot_borrow_mixture_instrument(self):
        frames=list(range(0,2001,10))
        original={'onsets':[],'frame_ms':frames,'energy':[1. if t<=1300 else 0. for t in frames],
                  'spectral_continuity':[1.]*len(frames),'audible_floor':.01}
        vocal={**copy.deepcopy(original),'energy':[0.]*len(frames)}
        evidence={'sources':{'original':{'channels':[original]},'vocals':{'channels':[vocal]}}}
        event=dict(id='v',start_ms=1000.,end_ms=1300.,lane=0,origins=[{'stem_role':'vocals'}])
        measured=head_evidence(event,evidence)
        self.assertEqual(measured['matched_sustain_role'],'vocals')
        self.assertEqual(measured['sustain_support'],0.)
        self.assertEqual(apply([event],{},evidence,'hard','balanced')['events'][0]['end_ms'],None)
        del evidence['sources']['vocals']
        fallback=head_evidence(event,evidence)
        self.assertEqual(fallback['matched_sustain_role'],'original')
        self.assertEqual(fallback['sustain_support'],1.)
        self.assertTrue(fallback['release_supported'])

    def test_antiphase_channels_do_not_cancel_and_path_cache(self):
        sr=44100; wave=np.zeros(sr,dtype=np.float32)
        wave[10000:12000]=np.sin(np.arange(2000)*2*np.pi*440/sr)
        pcm=np.stack((wave,-wave),axis=1)
        result=analyze_audio(pcm)
        channels=result['sources']['original']['channels']
        self.assertEqual(len(channels),2)
        self.assertTrue(channels[0]['onsets']);self.assertTrue(channels[1]['onsets'])
        self.assertLessEqual(result['hop_ms'],2.1)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'data') as directory:
            path=Path(directory)/'source.wav';sf.write(path,pcm,sr,subtype='FLOAT')
            first=cached_evidence(path,stems={'vocals':path},cache_dir=Path(directory)/'cache')
            second=cached_evidence(path,stems={'vocals':path},cache_dir=Path(directory)/'cache')
            self.assertIs(first,second);self.assertIn('id',first)


if __name__ == '__main__': unittest.main()
