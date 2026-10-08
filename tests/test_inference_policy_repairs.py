import copy
import pytest

from malody_studio import advanced_generation as generation, paired_timing
from malody_studio.density_calibration import freeze, initial_condition


@pytest.mark.parametrize('role',['mix','vocals','accompaniment'])
def test_initial_calibration_requires_opt_in_qualification_and_independent_recordings(role):
    rows=[{'engine':'v32','source_role':role,'pattern':'balanced','condition':6,
           'achieved_rate':8.5,'recording_id':str(i)} for i in range(3)]
    qualification={'qualified':True,'validation_id':'independent-holdout-v1','min_recordings':3}
    for settings in ({},{'calibration_opt_in':True}, {'calibration_qualification':qualification}):
        assert initial_condition(freeze(settings,rows),'v32','hard',4.6,source_role=role,pattern='balanced')==4.6
    settings={'calibration_opt_in':True,'calibration_qualification':qualification}
    assert initial_condition(freeze(settings,rows),'v32','hard',4.6,source_role=role,pattern='balanced')==6
    assert initial_condition(freeze(settings,rows[:1]*5),'v32','hard',4.6,source_role=role,pattern='balanced')==4.6
    assert initial_condition(freeze(settings,rows),'v32','hard',4.6,source_role=role,pattern='stream')==4.6
    unidentified=[{k:v for k,v in row.items() if k not in ('source_role','pattern')} for row in rows]
    assert initial_condition(freeze(settings,unidentified),'v32','hard',4.6,source_role=role,pattern='balanced')==4.6


def density_requests():
    policy=freeze({})
    return [{'key':str(i),'difficulty_key':'hard','sr':6,'seed':i+10,
             'core':[i*44100,(i+1)*44100],'context':[0,4*44100],
             'start_time':i*1000,'end_time':(i+1)*1000,'section_id':str(i),
             'density_policy':policy,'density_min_heads':8,'disable_density_retry':False,
             'density_rounds':[{'condition':10,'seed':i+20},{'condition':1,'seed':i+30}]}
            for i in range(2)]


def test_density_groups_identical_conditions_with_core_checks_and_frozen_seeds():
    requests=density_requests();before=copy.deepcopy(requests)
    groups,owners=generation._coalesce_v32_requests(requests)
    assert len(groups)==1 and owners['0'] is owners['1']
    group=groups[0]
    assert group['start_time']==0 and group['end_time']==4000
    assert group['core_start_time']==0 and group['core_end_time']==2000
    assert group['density_min_heads']==16
    assert [(r['start_time'],r['end_time'],r['density_min_heads']) for r in group['density_sections']]==[(0,1000,8),(1000,2000,8)]
    assert len(group['density_rounds'])==2
    assert generation._coalesce_v32_requests(requests)[0]==groups
    assert requests==before


@pytest.mark.parametrize('change',['condition','retry_condition','retry_disabled','policy'])
def test_density_group_rejects_different_condition_or_retry_contract(change):
    requests=density_requests()
    if change=='condition':requests[1]['sr']=6.01
    elif change=='retry_condition':requests[1]['density_rounds'][0]['condition']=9
    elif change=='retry_disabled':requests[1]['disable_density_retry']=True
    else:requests[1]['density_policy']=freeze({'seed':22})
    assert len(generation._coalesce_v32_requests(requests)[0])==2


@pytest.mark.parametrize('tempo',[{'bpm':120,'uncertain':True},
    {'points':[[0,120]],'uncertain':True}, {'bpm':120,'manual':False}])
def test_unconfirmed_timing_is_never_injected_for_any_role(tmp_path,tempo):
    for role in ('mix','vocals','accompaniment'):
        local={}
        generation.attach_timing_reference(local,tmp_path,{'tempo':tempo},{'source_role':role})
        assert 'timing_reference' not in local and 'timing_fallback_reference' not in local


def test_timing_rescue_requires_actual_failure_authorization_and_preserves_manual_clock():
    with pytest.raises(ValueError):paired_timing._reference_project({'tempo':{}},[[0,120]],'paired_stem_model_output')
    for project in ({'tempo':{'manual':True,'uncertain':False,'bpm':120}},
                    {'timing_map':{'eligibility':{'v32':True}}}):
        before=copy.deepcopy(project)
        with pytest.raises(ValueError):paired_timing._reference_project(project,[[0,60]],'paired_stem_model_output',True)
        assert project==before


@pytest.mark.parametrize('points',[[[0,60]],[[0,240]],[[100,120]]])
def test_rescue_rejects_half_speed_double_speed_or_shift_against_reliable_beat_fit(points):
    project={'timing_map':{'eligibility':{'v32':False,'beat':True},
                          'fit':{'eligible':True,'bpm':120,'phase_sample':0}}}
    with pytest.raises(ValueError):paired_timing._reference_project(project,points,'paired_stem_model_output',True)


def test_rescue_keeps_uncertain_map_and_is_not_confirmed():
    project={'tempo':{'bpm':100,'uncertain':True},'timing_map':{'eligibility':{'v32':False}}}
    rescued=paired_timing._reference_project(project,[[0,120]],'paired_stem_model_output',True)
    assert rescued['tempo']==project['tempo'] and rescued['timing_map']==project['timing_map']
    reference=generation.timing_reference_info(rescued)
    assert reference['uncertain'] and reference['confirmed'] is False
    assert reference['authorized_missing_timing'] and reference['attempts']==1


def test_worker_supply_checks_exclude_padding_overlap_and_dense_siblings():
    from tools.mapperatorinator_worker import density_supply_deficits
    sections=[{'start_time':1000,'end_time':2000,'density_min_heads':1,
               'density_target_heads':3,'acoustic_times':[1100,1500,1800]},
              {'start_time':2000,'end_time':3000,'density_min_heads':3}]
    # First core has one model head plus two independent audio heads; audio
    # beside that head annotates it. Dense padding cannot fill the second core.
    deficits=density_supply_deficits([100,200,500,1105,3100,3200],sections)
    assert len(deficits)==1 and deficits[0]['start_time']==2000
    assert deficits[0]['deficit']==3
    sections[0]['density_target_heads']=4
    deficits=density_supply_deficits([1105,2100,2200,2300],sections)
    assert len(deficits)==1 and deficits[0]['model_heads']==1
    assert deficits[0]['independent_acoustic_heads']==2


def test_new_execution_identity_excludes_stale_v32_rows_but_retains_the_audit():
    from malody_studio.density_calibration import condition_next
    identity={'hash':'repaired-serializer-clock-only-continuous'}
    rows=[{'engine':'v32','condition':4,'achieved_rate':8.5,'recording_id':'old-missing'},
          {'engine':'v32','condition':4.5,'achieved_rate':8.5,'recording_id':'old-dropped-holds',
           'execution_identity':{'hash':'single-pending-hold'}},
          {'engine':'mug','condition':3,'achieved_rate':8.5,'recording_id':'unaffected-mug'}]
    policy=freeze({'conditions':{'v32':{'hard':5.9}}},rows,execution_identity=identity)
    assert policy['observations']==rows and policy['excluded_count']==2
    assert policy['excluded_observation_reasons']=={'missing_execution_identity':1,'execution_identity_mismatch':1}
    assert all(curve['engine']!='v32' for curve in policy['response_curves'])
    assert condition_next(policy,'v32','hard',8.5,used_conditions=[5.9])==10
    rows.append({'engine':'v32','condition':6,'achieved_rate':8.5,'recording_id':'new',
                 'execution_identity':identity})
    current=freeze({},rows,execution_identity=identity)
    assert condition_next(current,'v32','hard',8.5)==6


def test_old_frozen_retry_does_not_read_latest_execution_or_curve(monkeypatch):
    from malody_studio import density_calibration
    rows=[{'engine':'v32','condition':4,'achieved_rate':8.5,'recording_id':'old'}]
    policy=freeze({},rows);before=copy.deepcopy(policy)
    def forbidden():raise AssertionError('retry must use only its immutable frozen snapshot')
    monkeypatch.setattr(density_calibration,'v32_execution_identity',forbidden)
    rows[0]['condition']=9
    assert density_calibration.condition_next(policy,'v32','hard',8.5)==4
    assert policy==before


def test_app_freeze_stamps_current_execution_and_rejects_old_library(tmp_path,monkeypatch):
    from malody_studio import quality_workflow, density_calibration, paths
    from malody_studio.advanced import atomic
    from malody_studio.section_plan import canonical_hash
    identity={'hash':'current-v32-execution'}
    monkeypatch.setattr(density_calibration,'v32_execution_identity',lambda:copy.deepcopy(identity))
    monkeypatch.setattr(paths,'ROOT',tmp_path)
    row={'engine':'v32','condition':4,'achieved_rate':8.5,'recording_id':'old'}
    record={'observations':[row]};record['hash']=canonical_hash(record)
    path=tmp_path/'data'/'density-calibration'/'current.json';atomic(path,record)
    before=path.read_bytes()
    policy=quality_workflow.freeze({'engine':'v32'})['density_policy']
    assert policy['execution_identity']==identity and policy['excluded_count']==1
    assert policy['observations']==[row] and not policy['response_curves']
    assert path.read_bytes()==before
