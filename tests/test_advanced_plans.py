import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from malody_studio.advanced import ProjectStore, atomic, defaults, read
from malody_studio import advanced_plans as cache, section_plan as sp, music_timing, arrangement, bpm_buckets
from malody_studio.separation import canonical_hash


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    project = {'id':'a'*32, 'source_pcm_sha256':'b'*64, 'samples':441000,
               'tempo':{'bpm':144, 'uncertain':True}}
    directory = store.directory(project['id'])
    directory.mkdir()
    calls = []

    policy={'adapter':'beat_this_final1','strong_reference_approved':False}
    timing={'id':'f'*64,'provenance':{'adapter':'beat_this_final1'}}
    def analyze(directory, actual, frozen_policy=None):
        assert frozen_policy==policy
        return {'id':'e'*64,'source':{'pcm_sha256':actual['source_pcm_sha256']},'acoustic':{'id':'cached-full-source'}}
    def build(evidence, timing, acoustic, settings, variants, profile, **options):
        assert set(options)=={'bucket_version'}
        assert timing['provenance']['adapter']=='beat_this_final1'
        calls.append((directory, copy.deepcopy(settings), copy.deepcopy(project['tempo']), options['bucket_version']))
        plan = {'version':sp.VERSION, 'source_pcm_sha':project['source_pcm_sha256'],
            'bpm_buckets':{'version':options['bucket_version']},
            'sample_rate':44100, 'samples':project['samples'], 'sections':[{'core':[0,project['samples']]}],
            'settings_hash':sp.plan_settings_hash(settings), 'reference_hash':canonical_hash(project['tempo'])}
        digest = canonical_hash(plan)
        return {'section_plan':{**plan, 'id':digest, 'content_hash':digest}}

    monkeypatch.setattr(arrangement, 'build', build)
    monkeypatch.setattr(music_timing,'selected_policy',lambda:copy.deepcopy(policy))
    monkeypatch.setattr(music_timing,'load_or_analyze',analyze)
    monkeypatch.setattr(music_timing,'select_timing',lambda _:copy.deepcopy(timing))
    return store, project, defaults(), calls


def test_repeated_and_concurrent_analysis_build_once_and_return_independent_data(case):
    store, p, settings, calls = case
    with ThreadPoolExecutor(max_workers=4) as pool:
        plans = list(pool.map(lambda _:cache.get_or_build_plan(store,p,settings), range(4)))
    assert len(calls)==1
    assert len({plan['id'] for plan in plans})==1
    plans[0]['sections'][0]['core'][1]=100
    assert cache.get_or_build_plan(store,p,settings)['sections'][0]['core'][1]==p['samples']
    assert p['tempo']=={'bpm':144,'uncertain':True}


@pytest.mark.parametrize('change', ['source','range','tempo','settings','regions','old-version','legacy-analysis','fusion','tampered'])
def test_incompatible_or_tampered_plan_is_not_reused(case, change):
    store, p, settings, calls = case
    original=cache.get_or_build_plan(store,p,settings)
    path=store.directory(p['id'])/'section-plans'/(original['id']+'.json')
    if change=='source':p['source_pcm_sha256']='c'*64
    elif change=='range':p['samples']+=44100
    elif change=='tempo':p['tempo']['points']=[[0,144],[5000,180]]
    elif change=='settings':settings['difficulty_rules']['medium']['rate']=7
    elif change=='regions':settings['region_max_count']=12
    else:
        saved=read(path)
        if change=='old-version':saved['version']='section-plan-v3'
        elif change=='legacy-analysis':saved.pop('planner')
        elif change=='fusion':saved['fusion_only']=True
        else:saved['sections'][0]['core'][1]=9999
        # Even an honestly rehashed old-version/fusion plan is not the requested plan.
        if change!='tampered':
            body={k:v for k,v in saved.items() if k not in ('id','content_hash')}
            saved.update(id=canonical_hash(body),content_hash=canonical_hash(body))
        atomic(path,saved)
    result=cache.get_or_build_plan(store,p,settings)
    assert len(calls)==2
    assert sp.validate_plan(result,p['source_pcm_sha256'])==result


def test_sampling_only_changes_reuse_rhythm_analysis(case):
    store,p,settings,calls=case
    first=cache.get_or_build_plan(store,p,settings)
    settings.update(seed=42, v32_temperature=1.2)
    second=cache.get_or_build_plan(store,p,settings)
    assert first['id']==second['id'] and len(calls)==1


def test_wrong_project_audio_length_is_rejected(case,monkeypatch):
    store,p,settings,_=case
    original=arrangement.build
    def wrong(*args,**kwargs):
        result=original(*args,**kwargs);plan=result['section_plan']
        plan['samples']+=1;plan['sections'][0]['core'][1]+=1
        body={k:v for k,v in plan.items() if k not in ('id','content_hash')}
        digest=canonical_hash(body);plan.update(id=digest,content_hash=digest)
        return result
    monkeypatch.setattr(arrangement,'build',wrong)
    with pytest.raises(ValueError,match='采样数量'):
        cache.get_or_build_plan(store,p,settings)


def test_default_analysis_requires_beat_this_and_does_not_call_legacy(case,monkeypatch):
    store,p,settings,calls=case
    monkeypatch.setattr(music_timing,'selected_policy',lambda:{'adapter':'librosa'})
    with pytest.raises(RuntimeError,match='Beat This'):
        cache.get_or_build_plan(store,p,settings)
    assert calls==[]


def test_wrong_detector_result_does_not_publish_a_plan(case,monkeypatch):
    store,p,settings,calls=case
    monkeypatch.setattr(music_timing,'select_timing',lambda _:{'provenance':{'adapter':'librosa'}})
    with pytest.raises(RuntimeError,match='Beat This'):
        cache.get_or_build_plan(store,p,settings)
    assert calls==[] and not list((store.directory(p['id'])/'section-plans').glob('*.json'))


def test_new_analysis_uses_current_bucket_version_and_historical_version_is_a_separate_plan(case):
    store, p, settings, calls = case
    assert bpm_buckets.VERSION=='bpm-buckets-v3'
    current=cache.get_or_build_plan(store,p,settings)
    assert current['bpm_buckets']['version']=='bpm-buckets-v3' and [row[3] for row in calls]==['bpm-buckets-v3']
    old=cache.get_or_build_plan(store,p,settings,'bpm-buckets-v2')
    assert old['bpm_buckets']['version']=='bpm-buckets-v2' and old['id']!=current['id']
    # Each version finds its own saved plan; neither request rebuilds or replaces the other.
    assert cache.get_or_build_plan(store,p,settings)['id']==current['id']
    assert cache.get_or_build_plan(store,p,settings,'bpm-buckets-v2')['id']==old['id']
    assert [row[3] for row in calls]==['bpm-buckets-v3','bpm-buckets-v2']
