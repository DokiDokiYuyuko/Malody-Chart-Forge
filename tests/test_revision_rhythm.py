import copy
import json

from malody_studio.revision_rhythm import hydrate_events
from tests.test_quality_workflow import project, add_part, request


class Store:
    def __init__(self, revision):self.rows={revision['id']:revision}
    def revision(self,pid,rid):return copy.deepcopy(self.rows[rid])


def fixture(tmp_path):
    rid='a'*32;job='b'*32;directory=tmp_path/'outputs'/job
    directory.mkdir(parents=True)
    charts={}
    for suffix,snap in (('',4),('__density_retry2',8)):
        folder=directory/('round'+(suffix or 'initial'));folder.mkdir()
        path=folder/'model-events.json'
        path.write_text(json.dumps({'types_first':True,'timing':['0,500,4,2,1,60,1,0'],
            'events':[['circle',0],['t',1128],['snap',snap],['column',1]]}),encoding='utf-8')
        charts['one'+suffix]=str(folder/'model.osu')
    cache={'raw':{'expert':[[1128.,1,2000.],[1129.,1,None],[1128.,2,None]]},
        'source':{'source_role':'accompaniment'},'metadata':{'charts':charts},
        'records':[{'difficulty_key':'expert','inference_group':'one','selected_round':2,
                    'density_policy':{'version':'fixture'},'core':[0,88200]}]}
    cache_path=directory/'accompaniment'/'balanced-mother.json';cache_path.parent.mkdir()
    cache_path.write_text(json.dumps(cache),encoding='utf-8')
    revision={'id':rid,'variant':'balanced--expert','kind':'stem_raw','events':[
        {'id':'native','start_ms':1128.,'lane':1,'end_ms':2000.},
        {'id':'near','start_ms':1129.,'lane':1,'end_ms':None},
        {'id':'other-lane','start_ms':1128.,'lane':2,'end_ms':None}],
        'provenance':{'cache_job':job,'cache_file':'accompaniment/balanced-mother.json',
                      'source_role':'accompaniment'}}
    revision_path=tmp_path/'revision.json';revision_path.write_text(json.dumps(revision),encoding='utf-8')
    events=[]
    for i,row in enumerate(revision['events']):
        events.append({'id':str(i),'start_ms':row['start_ms'],'lane':3,
            'end_ms':1528. if i==0 else None,'origins':[{'revision_id':rid,'note_id':row['id'],
            'original_start_ms':row['start_ms'],'original_lane':row['lane'],'original_end_ms':row['end_ms']}]})
    owner={'id':'c'*32,'kind':'fusion','variant':'balanced--expert',
           'settings':{'difficulty_rules':{'expert':{'hold_ms':400.}}},
           'provenance':{'fusion_version':'stem-fusion-v5'}}
    return Store(revision),events,owner,[cache_path,revision_path,*directory.glob('round*/model-events.json')]


def test_selected_retry_native_proof_has_exact_head_and_lane_ownership(tmp_path):
    store,events,owner,files=fixture(tmp_path);before=copy.deepcopy((store.rows,events))
    fingerprints={path:path.read_bytes() for path in files}
    result=hydrate_events(store,'pid',events,owners={row['id']:owner for row in events},root=tmp_path)
    assert result[0]['model_rhythm']['available'] is True
    assert result[0]['model_rhythm']['snap_divisor']==8
    assert result[0]['model_rhythm']['raw_note_id']=='native'
    assert result[0]['model_rhythm']['source_role']=='accompaniment'
    assert result[1]['model_rhythm']['available'] is False
    assert result[2]['model_rhythm']['available'] is False
    assert (store.rows,events)==before
    assert all(path.read_bytes()==data for path,data in fingerprints.items())
    assert result[0]['tail_policy']=={'kind':'rule_cap','model_end_ms':2000.,'cap_ms':400.,
        'uncapped_start_ms':1128.,'proof':'legacy_fusion_exact_recorded_cap'}


def test_manual_or_inexact_tail_cannot_claim_a_recorded_rule_cap(tmp_path):
    store,events,owner,_=fixture(tmp_path)
    manual={**owner,'kind':'manual'}
    assert 'tail_policy' not in hydrate_events(store,'pid',events,owners={'0':manual},root=tmp_path)[0]
    wrong=copy.deepcopy(events);wrong[0]['end_ms']=1500.
    assert 'tail_policy' not in hydrate_events(store,'pid',wrong,owners={'0':owner},root=tmp_path)[0]
    wrong=copy.deepcopy(events);wrong[0]['start_ms']+=1.
    assert 'tail_policy' not in hydrate_events(store,'pid',wrong,owners={'0':owner},root=tmp_path)[0]


def test_missing_or_falsely_claimed_origin_is_explicitly_unavailable(tmp_path):
    store,events,owner,_=fixture(tmp_path)
    changed=copy.deepcopy(events);changed[0]['origins'][0]['note_id']='another-head'
    result=hydrate_events(store,'pid',changed,owners={'0':owner},root=tmp_path)
    assert result[0]['model_rhythm']['available'] is False
    assert 'tail_policy' not in result[0]
    store.rows['a'*32]['provenance']['cache_file']='../../not-a-cache.json'
    result=hydrate_events(store,'pid',events,root=tmp_path)
    assert result[0]['model_rhythm']['available'] is False


def test_frozen_section_cap_takes_precedence_over_global_rule(tmp_path):
    store,events,owner,_=fixture(tmp_path)
    owner['settings']['difficulty_rules']['expert']['hold_ms']=900.
    plan={'sample_rate':44100,'sections':[{'core':[0,88200],
        'per_difficulty':{'expert':{'hard_caps':{'hold_max_ms':400.}}}}]}
    result=hydrate_events(store,'pid',events,owners={'0':owner},plan=plan,root=tmp_path)
    assert result[0]['tail_policy']['cap_ms']==400.


def test_quality_derivation_saves_owned_region_summary_and_full_span_audit(project):
    from malody_studio import quality_workflow
    store,p=project
    left=add_part(store,p,0,1,[{'id':'left','start_ms':100.,'lane':0,'end_ms':None}])
    right=add_part(store,p,1,2,[{'id':'right-a','start_ms':1100.,'lane':1,'end_ms':None},
                            {'id':'right-b','start_ms':1500.,'lane':2,'end_ms':None}])
    originals={row['id']:store.revision(p['id'],row['id']) for row in (left,right)}
    result=quality_workflow.derive_many(store,p['id'],request(store,p,[left,right]))
    children=[store.revision(p['id'],row['id']) for row in result['revisions']]
    assert [row['provenance']['quality_summary']['heads_after'] for row in children]==[1,2]
    assert all(row['provenance']['span_quality_summary']['heads_after']==3 for row in children)
    assert all(row['provenance']['quality_summary']['scope']=='owned_region' for row in children)
    assert all(event['model_rhythm']['available'] is False for row in children for event in row['events'])
    assert all(store.revision(p['id'],rid)==value for rid,value in originals.items())
