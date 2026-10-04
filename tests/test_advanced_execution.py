from malody_studio.advanced import atomic
from malody_studio.advanced_execution import execution_summary


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
