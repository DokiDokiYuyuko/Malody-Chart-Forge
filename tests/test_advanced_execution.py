from malody_studio.advanced import atomic
from malody_studio.advanced_execution import execution_summary
from malody_studio.advanced_execution import spent_retry_rounds


def test_coalesced_passes_retries_and_cached_stems_are_distinct(tmp_path):
    atomic(tmp_path/'vocals'/'balanced-mother.json', {
        'format':2,'model':'v32','inference_groups':[{'key':'joined'}],
        'records':[{'key':'part1','inference_group':'joined','retry_heads':7},
                   {'key':'part2','inference_group':'joined','retry_heads':6}]})
    result={'reused_revisions':['old'], 'advanced_result':[{'kind':'fusion'},
        {'id':'old','kind':'stem_raw','provenance':{'cache_job':'previous'}}]}
    assert execution_summary(tmp_path,result)=={'model_passes':1,'retry_passes':1,'reused_versions':1,'fusion_candidates':1}


def test_mug_attempts_and_legacy_v32_results(tmp_path):
    atomic(tmp_path/'mug-mother.json',{'format':2,'model':'mug',
        'records':[{'attempts':[{},{}]},{'attempts':[{}]}]})
    atomic(tmp_path/'legacy-mother.json',{'format':1,'model':'v32','raw':{'medium':[], 'medium__retry':[]}})
    assert execution_summary(tmp_path,{})['model_passes']==3
    assert execution_summary(tmp_path,{})['retry_passes']==2


def test_resident_timings_preserve_individual_requests(tmp_path):
    cold={'pid':123,'model_reused':False,'load_seconds':1.2,'main_inference_seconds':3.4}
    warm={'pid':123,'model_reused':True,'load_seconds':0,'work_seconds':2.1}
    atomic(tmp_path/'balanced'/'v32-original'/'worker-result.json',{'resident':cold})
    atomic(tmp_path/'resident-mug-abc.json',{'requests':[warm]})
    summary=execution_summary(tmp_path,{'fusion_seconds':.5})
    assert summary['resident_requests']==[{'engine':'v32',**cold},{'engine':'mug',**warm}]
    assert summary['fusion_seconds']==.5


def test_density_rounds_count_rejected_primary_and_failed_retry(tmp_path):
    records=[{'key':'part1','inference_group':'joined','attempts':[{'retry':2}]},
             {'key':'part2','inference_group':'joined','attempts':[{'retry':0}]}]
    metadata={'local_retries':[{'key':'joined','bounded_attempts':1,'status':'completed'},
                               {'key':'joined','bounded_attempts':2,'status':'failed'}]}
    atomic(tmp_path/'v32-mother.json',{'format':2,'model':'v32','inference_groups':[{'key':'joined'}],
                                     'records':records,'metadata':metadata})
    assert spent_retry_rounds(records[0])==2
    assert spent_retry_rounds(records[1],metadata)==2
    assert execution_summary(tmp_path,{})['retry_passes']==2


def test_mug_round_indices_keep_consumed_budget_after_invalid_primary():
    assert spent_retry_rounds({'attempts':[{'retry':1}]})==1
    assert spent_retry_rounds({'attempts':[{'retry':2}],'spent_retry_rounds':2})==2


def test_dual_parent_density_round_directories_count_as_four_extra_passes(tmp_path):
    cache={'format':2,'model':'v32','inference_groups':[{'key':'whole'}],
           'records':[{'key':'core','inference_group':'whole','attempts':[{'retry':0}]}]}
    for role in ('vocals','accompaniment'):
        atomic(tmp_path/role/'balanced-mother.json',cache)
        for round_index in (1,2):
            atomic(tmp_path/role/('density-round-'+str(round_index))/'balanced--hard'/'balanced-mother.json',cache)
    result={'generation_context_policy':{'version':'continuous-model-pass-v1'},'member_segment_ids':list(range(21))}
    summary=execution_summary(tmp_path,result)
    assert summary['model_passes']==2 and summary['retry_passes']==4 and summary['owned_regions']==21
    # A worker retry within a parent retry is another real model pass, rather
    # than a second count of its parent request.
    nested={**cache,'metadata':{'local_retries':[{'key':'whole','bounded_attempts':1}]}}
    atomic(tmp_path/'vocals'/'density-round-2'/'balanced--hard'/'balanced-mother.json',nested)
    assert execution_summary(tmp_path,result)['retry_passes']==5


def _record_native_attempt(worker, key, index=1, status='failed'):
    output=worker/key
    atomic(output/'inference-attempts'/f'{index:04d}'/'resolved-parameters.json',{
        'args':{'output_path':str(output.resolve())},
        'inference_attempt':{'attempt':index,'original_output_path':str(output.resolve())}})
    atomic(output/'inference-attempts'/f'{index:04d}'/'attempt-result.json', {
        'schema':'v32-inference-attempt-v1','status':status,'attempt':index,
        'original_output_path':str(output.resolve())})


def test_failed_role_calls_without_mother_cache_are_counted(tmp_path):
    worker=tmp_path/'vocals'/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert__group-0')
    _record_native_attempt(worker,'master__group-1')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==2
    assert summary['retry_passes']==0


def test_native_attempts_replace_cache_counts_without_double_counting(tmp_path):
    cache={'format':2,'model':'v32','inference_groups':[{'key':'expert'},{'key':'master'}],
           'records':[{'key':'expert','inference_group':'expert','attempts':[{'retry':2}]},
                      {'key':'master','inference_group':'master'}]}
    atomic(tmp_path/'accompaniment'/'balanced-mother.json',cache)
    for role in ('vocals','accompaniment'):
        worker=tmp_path/role/'balanced'/'v32-original'
        _record_native_attempt(worker,'expert',status='completed' if role=='accompaniment' else 'failed')
        _record_native_attempt(worker,'master',status='completed' if role=='accompaniment' else 'failed')
    worker=tmp_path/'accompaniment'/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert__density_retry1',status='completed')
    _record_native_attempt(worker,'expert__density_retry2')
    # A timing/OOM fallback is another call in the same output folder.
    _record_native_attempt(worker,'expert__density_retry2',index=2,status='completed')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==4
    assert summary['retry_passes']==3


def test_copied_or_misbound_attempt_is_not_an_additional_call(tmp_path):
    worker=tmp_path/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert',status='completed')
    original=worker/'expert'/'inference-attempts'/'0001'/'attempt-result.json'
    from malody_studio.advanced import read
    atomic(tmp_path/'evidence'/'balanced'/'v32-original'/'expert'/'inference-attempts'/'0001'/'attempt-result.json',read(original))
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==1


def test_incomplete_diagnostics_preserve_legacy_cache_count(tmp_path):
    atomic(tmp_path/'balanced-mother.json',{'format':2,'model':'v32',
           'inference_groups':[{'key':'expert'},{'key':'master'}],'records':[]})
    worker=tmp_path/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert',status='completed')
    broken=worker/'master'/'inference-attempts'/'0001'/'attempt-result.json'
    broken.parent.mkdir(parents=True)
    broken.write_text('{',encoding='utf-8')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==2
    assert summary['native_attempt_diagnostics']['status']=='partial_capture'


def test_parent_density_round_native_failures_are_retries(tmp_path):
    worker=tmp_path/'vocals'/'density-round-1'/'balanced--expert'/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert')
    _record_native_attempt(worker,'expert',index=2,status='completed')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==0
    assert summary['retry_passes']==2


def test_partially_recorded_failed_group_is_added_to_other_cached_group(tmp_path):
    atomic(tmp_path/'balanced-mother.json',{'format':2,'model':'v32',
        'inference_groups':[{'key':'expert'}],'records':[{'key':'expert','inference_group':'expert',
            'attempts':[{'retry':1}]}]})
    worker=tmp_path/'balanced'/'v32-original'
    _record_native_attempt(worker,'master')
    _record_native_attempt(worker,'master__density_retry1')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==2
    assert summary['retry_passes']==2


def test_snapshot_without_terminal_result_is_reported_as_unresolved(tmp_path):
    folder=tmp_path/'balanced'/'v32-original'/'expert'/'inference-attempts'/'0001'
    atomic(folder/'resolved-parameters.json',{'inference_attempt':{'status':'started'}})
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==0
    assert summary['native_attempt_diagnostics']['issues'][0]['reason']=='missing_attempt_result'


def test_same_output_folder_copy_with_mismatched_snapshot_is_rejected(tmp_path):
    worker=tmp_path/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert',status='completed')
    _record_native_attempt(worker,'expert',index=2,status='completed')
    from malody_studio.advanced import read
    parent=worker/'expert'/'inference-attempts'
    atomic(parent/'0002'/'resolved-parameters.json',read(parent/'0001'/'resolved-parameters.json'))
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==1 and summary['retry_passes']==0
    assert summary['native_attempt_diagnostics']['issues'][0]['reason']=='snapshot_result_identity_mismatch'


def test_partial_primary_fallback_does_not_overlap_cached_density_retries(tmp_path):
    atomic(tmp_path/'balanced-mother.json',{'format':2,'model':'v32',
        'inference_groups':[{'key':'expert'}],'records':[{'key':'expert','inference_group':'expert',
            'attempts':[{'retry':2}]}]})
    worker=tmp_path/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert')
    _record_native_attempt(worker,'expert',index=2,status='completed')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==1
    assert summary['retry_passes']==3


def test_missing_first_manifest_does_not_relabel_fallback_as_primary(tmp_path):
    worker=tmp_path/'balanced'/'v32-original'
    _record_native_attempt(worker,'expert',index=2,status='completed')
    summary=execution_summary(tmp_path,{})
    assert summary['model_passes']==0
    assert summary['retry_passes']==1
    assert summary['native_attempt_diagnostics']['status']=='partial_capture'
