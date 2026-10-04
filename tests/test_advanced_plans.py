import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from malody_studio.advanced import ProjectStore, atomic, defaults, read
from malody_studio import advanced_plans as cache, section_plan as sp
from malody_studio.separation import canonical_hash


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    project = {'id':'a'*32, 'source_pcm_sha256':'b'*64, 'samples':441000,
               'tempo':{'bpm':144, 'uncertain':True}}
    directory = store.directory(project['id'])
    directory.mkdir()
    calls = []

    def build(path, settings, tempo):
        calls.append((path, copy.deepcopy(settings), copy.deepcopy(tempo)))
        plan = {'version':sp.VERSION, 'source_pcm_sha':project['source_pcm_sha256'],
            'sample_rate':44100, 'samples':project['samples'], 'sections':[{'core':[0,project['samples']]}],
            'settings_hash':sp.plan_settings_hash(settings), 'reference_hash':canonical_hash(tempo)}
        digest = canonical_hash(plan)
        return {**plan, 'id':digest, 'content_hash':digest}

    monkeypatch.setattr(sp, 'build_plan', build)
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


@pytest.mark.parametrize('change', ['source','range','tempo','settings','old-version','fusion','tampered'])
def test_incompatible_or_tampered_plan_is_not_reused(case, change):
    store, p, settings, calls = case
    original=cache.get_or_build_plan(store,p,settings)
    path=store.directory(p['id'])/'section-plans'/(original['id']+'.json')
    if change=='source':p['source_pcm_sha256']='c'*64
    elif change=='range':p['samples']+=44100
    elif change=='tempo':p['tempo']['points']=[[0,144],[5000,180]]
    elif change=='settings':settings['difficulty_rules']['medium']['rate']=7
    else:
        saved=read(path)
        if change=='old-version':saved['version']='section-plan-v3'
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
    original=sp.build_plan
    def wrong(*args):
        plan=original(*args)
        plan['samples']+=1;plan['sections'][0]['core'][1]+=1
        body={k:v for k,v in plan.items() if k not in ('id','content_hash')}
        digest=canonical_hash(body);plan.update(id=digest,content_hash=digest)
        return plan
    monkeypatch.setattr(sp,'build_plan',wrong)
    with pytest.raises(ValueError,match='采样数量'):
        cache.get_or_build_plan(store,p,settings)
