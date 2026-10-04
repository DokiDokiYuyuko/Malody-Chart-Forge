import copy
import io
import json
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from malody_studio import advanced as a
from malody_studio import advanced_agent as agent

@pytest.fixture
def project(tmp_path,monkeypatch):
    source=tmp_path/'input.wav';sf.write(source,np.tile(np.sin(np.arange(a.SR*8)*2*np.pi*440/a.SR)[:,None]*.1,(1,2)),a.SR,subtype='FLOAT')
    monkeypatch.setattr(a,'audio_metadata',lambda data:([.1]*2400,{'bpm':120.,'beat_times':[i*.5 for i in range(16)],'beat_variability':0,'uncertain':False,'warnings':[]}))
    store=a.ProjectStore(tmp_path/'projects');p=store.create(source,'测试歌曲','Test')
    p=store.update(p['id'],{'patterns':['balanced'],'difficulties':['easy']})
    return store,p

def segment(store,p,start,end):
    p=store.add_segment(p['id'],{'start_sample':round(start*a.SR),'end_sample':round(end*a.SR)})
    return p,next(s for s in p['segments'] if s['start_sample']==round(start*a.SR))

def event(t,lane=0,end=None,id=None):return {'id':id or a.uid(),'start_ms':t,'end_ms':end,'lane':lane}

def rev(store,p,s,events,kind='model_raw',activate=True):
    return store.add_revision(p['id'],s['id'],'balanced--easy',events,p['settings'],kind,activate_initial=activate)

def test_half_open_ranges_and_overlap(project):
    store,p=project;p,s=segment(store,p,0,1);p,s2=segment(store,p,1,1.25)
    with pytest.raises(ValueError,match='重叠'):segment(store,p,.5,1.5)
    with pytest.raises(ValueError,match='250'):segment(store,p,2,2.1)
    from malody_studio.advanced_generation import owned
    ev,_=owned([a.Note(1000,0)],0,1000,8000);assert ev==[]
    ev,_=owned([a.Note(1000,0)],1000,1250,8000);assert len(ev)==1

def test_revision_is_candidate_until_selected(project):
    store,p=project;p,s=segment(store,p,0,2);r=rev(store,p,s,[event(500)]);candidate=rev(store,p,s,[event(700)],'rules')
    current=store.load(p['id']);assert current['segments'][0]['active']['balanced--easy']==r['id']
    store.select(p['id'],s['id'],'balanced--easy',candidate['id'])
    assert store.revision(p['id'],r['id'])['events'][0]['start_ms']==500
    assert store.load(p['id'])['segments'][0]['active']['balanced--easy']==candidate['id']
    store.edit_segment(p['id'],s['id'],{'end_sample':3*a.SR})
    assert store.load(p['id'])['segments'][0]['active']=={}
    with pytest.raises(ValueError,match='范围'):store.select(p['id'],s['id'],'balanced--easy',r['id'])

def test_split_inherits_head_ownership_and_long_tail(project):
    store,p=project;p,s=segment(store,p,0,3);r=rev(store,p,s,[event(500,0,2500),event(1000,1),event(2000,2)])
    p=store.split(p['id'],s['id'],[a.SR,2*a.SR])
    assert len(p['segments'])==3
    rows=[store.revision(p['id'],part['active']['balanced--easy']) for part in p['segments']]
    assert [len(r['events']) for r in rows]==[1,1,1]
    assert rows[0]['events'][0]['end_ms']==2500
    assert len(set(e['id'] for r in rows for e in r['events']))==3

def test_pcm_cut_sample_count_and_nonoverlap_fades():
    data=np.ones((5*a.SR,2),dtype='float32');s=[{'id':'a','start_sample':0,'end_sample':a.SR},{'id':'b','start_sample':2*a.SR,'end_sample':3*a.SR}]
    pcm,m=a.assemble_pcm(data,s,1.5)
    assert len(pcm)==round(3.5*a.SR)
    assert m[1]['output_start']==round(2.5*a.SR)
    assert pcm[round(2.5*a.SR)-1,0]==0 and pcm[round(2.5*a.SR),0]==0
    s[1].update(start_sample=a.SR,end_sample=2*a.SR)
    pcm,m=a.assemble_pcm(data,s,0)
    assert pcm[a.SR-1,0]==pcm[a.SR,0]==1

def test_discontinuous_long_note_clip_and_short_to_tap():
    m=[{'segment_id':'a','source_start':0,'source_end':a.SR,'output_start':0,'output_end':a.SR},{'segment_id':'b','source_start':2*a.SR,'source_end':3*a.SR,'output_start':a.SR,'output_end':2*a.SR}]
    r=[{'events':[event(500,0,2300),event(950,1,1500)]},{'events':[event(2100,2)]}]
    ev,w=a.mapped_events(r,m)
    assert ev[0]['end_ms']==1000 and ev[1]['end_ms'] is None and ev[2]['start_ms']==1100
    assert any(x['type']=='short_hold_to_tap' for x in w)
    m[1].update(source_start=a.SR,source_end=2*a.SR);r[1]['events']=[event(1100,0)]
    ev,w=a.mapped_events(r,m);assert any(x['type']=='seam_conflict' for x in w)

def test_assembly_real_ogg_roundtrip_variable_bpm_and_missing(project):
    store,p=project;p,s=segment(store,p,.333,1.777);p,s2=segment(store,p,4.2,6.1)
    p=store.update(p['id'],{'tempo':{'points':[[0,120],[850,177],[4500,93]]},'difficulties':['easy','hard']})
    rev(store,p,s,[event(400,0,1300),event(900,1)]);rev(store,p,s2,[event(4500,2),event(5900,3)])
    result=a.assemble(store,p['id'],1.5)
    assert result['missing'][0]['variant']=='balanced--hard'
    directory=store.directory(p['id'])/'assemblies'/result['id'];report=a.read(directory/'report.json')
    assert sf.info(directory/'audio.ogg').frames==report['samples']
    import zipfile
    with zipfile.ZipFile(directory/'malody-4k.mcz') as z:
        assert '0/' in z.namelist() and '0/audio.ogg' in z.namelist()
        names=[n for n in z.namelist() if n.endswith('.mc')];assert len(names)==1
        chart=json.loads(z.read(names[0]));restored=a.chart_events(chart)
        for old,new in zip(report['preview']['balanced--easy'],restored):
            assert abs(old['start_ms']-new['start_ms'])<1
            if old['end_ms'] is not None:assert abs(old['end_ms']-new['end_ms'])<1
        assert all('cache' not in n and 'revisions' not in n for n in z.namelist())

def test_empty_fragment_allowed_but_whole_empty_not_exported(project):
    store,p=project;p,s=segment(store,p,0,.25);rev(store,p,s,[])
    p,s2=segment(store,p,2,3);rev(store,p,s2,[event(2500)])
    assert a.assemble(store,p['id'],0)['charts'][0]['notes']==1
    store.edit_segment(p['id'],s2['id'],{'included':False})
    with pytest.raises(ValueError,match='有效音符'):a.assemble(store,p['id'])


def test_identical_product_reused_but_chart_tempo_and_preroll_create_versions(project):
    store,p=project;p,s=segment(store,p,0,2);rev(store,p,s,[event(500)])
    first=a.assemble(store,p['id'],1.5)
    again=a.assemble(store,p['id'],1.5)
    assert again['id']==first['id'] and again['reused']
    assert len(store.load(p['id'])['assemblies'])==1
    store.update(p['id'],{'settings':{'seed':123}})
    assert a.assemble(store,p['id'],1.5)['id']==first['id']
    newer=rev(store,store.load(p['id']),s,[event(750)])
    store.select(p['id'],s['id'],'balanced--easy',newer['id'])
    second=a.assemble(store,p['id'],1.5)
    assert second['id']!=first['id']
    third=a.assemble(store,p['id'],0)
    assert third['id']!=second['id']
    store.update(p['id'],{'tempo':{'points':[[0,150]]}})
    assert a.assemble(store,p['id'],0)['id']!=third['id']
    assert (store.directory(p['id'])/'assemblies'/first['id']/'malody-4k.mcz').exists()


def test_concurrent_identical_exports_produce_one_archive(project):
    from concurrent.futures import ThreadPoolExecutor
    store,p=project;p,s=segment(store,p,0,2);rev(store,p,s,[event(500)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:a.assemble(store,p['id']),range(2)))
    assert results[0]['id']==results[1]['id']
    assert len(store.load(p['id'])['assemblies'])==1


def test_classic_export_ascii_paths_and_legacy_repair_preserve_notes_and_audio(project):
    import zipfile
    store,p=project;p,s=segment(store,p,0,2);rev(store,p,s,[event(500,0,1400)])
    result=a.assemble(store,p['id'])
    directory=store.directory(p['id'])/'assemblies'/result['id']
    original=directory/'malody-4k.mcz';original_bytes=original.read_bytes()
    with zipfile.ZipFile(original) as z:
        assert all(name.isascii() for name in z.namelist())
        old_chart=z.read('0/balanced--easy.mc');old_audio=z.read('0/audio.ogg')
        assert json.loads(old_chart)['meta']['song']['title'].startswith(p['title'])
    report=a.read(directory/'report.json');report.pop('export_format');a.atomic(directory/'report.json',report)
    repaired=a.assembly_archive(store,p['id'],result['id'])
    assert repaired!=original and original.read_bytes()==original_bytes
    with zipfile.ZipFile(repaired) as z:
        assert z.testzip() is None
        assert z.read('0/audio.ogg')==old_audio
        assert json.loads(z.read('0/balanced--easy.mc'))==json.loads(old_chart)
    assert a.assembly_archive(store,p['id'],result['id'])==repaired

def test_source_and_settings_immutability_and_optimistic_revision(project):
    store,p=project;p,s=segment(store,p,0,2);r=rev(store,p,s,[event(500)])
    d=copy.deepcopy(p['settings']);d['v32_temperature']=1.4;store.update(p['id'],{'settings':d})
    assert store.revision(p['id'],r['id'])['settings']['v32_temperature']==.9
    with pytest.raises(ValueError,match='已更新'):store.update(p['id'],{},0)
    assert (store.directory(p['id'])/'source-original.wav').exists()

def patch(base,type='update',**values):
    return [{'id':'g1','reason':'Test correction','evidence':['a1'],'operations':[{'type':type,'note_id':base['events'][0]['id'],**values}]}]

def test_agent_bounds_ids_anchors_conflicts_and_duplicate(project):
    store,p=project;p,s=segment(store,p,0,2);base=rev(store,p,s,[event(500,0,id='n1'),event(1000,0,id='n2')]);anchors=[{'id':'a1','time_ms':700}]
    good=patch(base,start_ms=700,anchor_id='a1');out,diff=agent.anchored_patch(base,good,0,2000,anchors);assert out[0]['start_ms']==700 and base['events'][0]['start_ms']==500
    for bad in [patch(base,note_id='bad',lane=1),patch(base,start_ms=701.1,anchor_id='a1'),patch(base,lane=4),patch(base,start_ms=2100,anchor_id='a1'),patch(base,end_ms=400),patch(base,start_ms=1000,anchor_id='a1')]:
        with pytest.raises((ValueError,TypeError)):agent.anchored_patch(base,bad,0,2000,anchors)
    duplicated=good+[{**good[0],'id':'g2'}]
    with pytest.raises(ValueError,match='重复'):agent.anchored_patch(base,duplicated,0,2000,anchors)
    with pytest.raises(ValueError,match='只读'):agent.anchored_patch(base,good,600,2000,anchors)

def test_patch_groups_are_atomic_and_added_ids_stable(project):
    store,p=project;p,s=segment(store,p,0,2);base=rev(store,p,s,[event(500)]);anchors=[{'id':'a1','time_ms':700}]
    groups=patch(base,'add',note_id=None,lane=2,start_ms=700,end_ms=None,anchor_id='a1')
    first,_=agent.anchored_patch(base,groups,0,2000,anchors);second,_=agent.anchored_patch(base,groups,0,2000,anchors)
    assert first==second
    without,_=agent.anchored_patch(base,groups,0,2000,anchors,[]);assert without==base['events']

def test_window_head_limit_and_readonly_hold_context():
    events=[event(i*10,0) for i in range(500)]
    windows=list(agent.windows(events,0,12000));assert len(windows)==2
    assert all(sum(a<=e['start_ms']<b for e in events)<=256 for a,b in windows)

@pytest.mark.parametrize('protocol',['chat','responses'])
def test_provider_tool_roundtrip_without_real_credentials(protocol):
    import httpx
    sent=[]
    def handler(request):
        data=json.loads(request.content);sent.append(data)
        if protocol=='chat':return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{'id':'call1','type':'function','function':{'name':'get_chart_events','arguments':'{}'}}]}}],'usage':{'total_tokens':10}})
        return httpx.Response(200,json={'status':'completed','output':[{'type':'function_call','call_id':'call1','name':'get_chart_events','arguments':'{}'}],'usage':{'total_tokens':10}})
    provider=agent.Provider({'protocol':protocol,'base_url':'https://test.invalid/v1','model':'test','key':'fake'},httpx.MockTransport(handler))
    r=provider.request([{'role':'user','content':'Test'}],[agent.tool('get_chart_events','Read')])
    assert r['calls'][0]['name']=='get_chart_events' and r['usage']['total_tokens']==10
    assert ('input' if protocol=='responses' else 'messages') in sent[0]

def test_provider_incomplete_and_network_error():
    import httpx
    config={'protocol':'responses','base_url':'https://test.invalid/v1','model':'test','key':'private-test-key'}
    p=agent.Provider(config,httpx.MockTransport(lambda r:httpx.Response(200,json={'status':'incomplete'})))
    with pytest.raises(ValueError,match='未完成'):p.request([])
    p=agent.Provider(config,httpx.MockTransport(lambda r:httpx.Response(401,json={'error':'private-test-key'})))
    with pytest.raises(ValueError) as e:p.request([])
    assert 'private-test-key' not in str(e.value)

def test_agent_review_candidate_and_manual_apply_staleness(project,monkeypatch):
    store,p=project;p,s=segment(store,p,0,2);base=rev(store,p,s,[event(500,id='n1')]);anchors=[{'id':'a1','time_ms':700,'kind':'onset'}]
    monkeypatch.setattr(agent,'features',lambda *_:{'anchors':anchors,'activity':[],'hpss_note':'Reference'})
    monkeypatch.setattr(agent,'configured',lambda role:{'protocol':'responses','capabilities':{'text':True,'tools':True}})
    groups=patch(base,start_ms=700,anchor_id='a1');task={'id':a.uid(),'base_revision':base['id'],'start_ms':0,'end_ms':2000,'goal':'Test','send_audio':False,'status':'queued','decisions':[],'audio_ranges':[]}
    path=store.directory(p['id'])/'reviews'/(task['id']+'.json');a.atomic(path,task)
    class Fake:
        def __init__(self,c):self.turn=0
        def request(self,messages,tools=None):
            self.turn+=1
            calls=[{'id':'call','name':'submit_patch_candidate','arguments':json.dumps({'base_revision':base['id'],'groups':groups})}] if self.turn==1 else []
            return {'calls':calls,'output':[],'text':'Evidence-based correction','usage':{'total_tokens':20}}
    t=agent.run_review(store,p['id'],task['id'],Fake)
    assert t['status']=='completed' and t['stats_after']['notes']==1
    assert store.load(p['id'])['segments'][0]['active']['balanced--easy']==base['id']
    result=agent.apply_review(store,p['id'],task['id'],['w0-g1'])
    assert result['events'][0]['start_ms']==700 and store.load(p['id'])['segments'][0]['active']['balanced--easy']==result['id']
    with pytest.raises(ValueError,match='过期'):agent.apply_review(store,p['id'],task['id'],['w0-g1'])

def test_actual_audio_features_and_alignment(project):
    store,p=project;p,s=segment(store,p,0,2);r=rev(store,p,s,[event(500,0,1100)])
    features=agent.features(store,p['id']);assert features['activity'] and features['source_sha256']==p['source_sha256']
    assert (store.directory(p['id'])/'spectrum.npz').exists()
    png=agent.alignment_png(store,p['id'],r,0,2000);assert png[:8]==b'\x89PNG\r\n\x1a\n'

def test_dpapi_config_never_returns_key(tmp_path,monkeypatch):
    monkeypatch.setattr(agent,'PRIVATE',tmp_path/'private'/'key.dpapi');monkeypatch.setattr(agent,'_config',{})
    config={'repair':{'base_url':'https://test.invalid/v1','model':'test','protocol':'responses','key':'private-fake-key'},'remember':True}
    public=agent.save_config(config);assert 'private-fake-key' not in json.dumps(public)
    assert b'private-fake-key' not in agent.PRIVATE.read_bytes()
    monkeypatch.setattr(agent,'_config',{});agent.restore_config();assert agent.configured('repair')['key']=='private-fake-key'

def test_router_creation_selection_export_and_mime(project,monkeypatch):
    from malody_studio import advanced_api
    from malody_studio.server import app
    store,p=project;monkeypatch.setattr(advanced_api,'store',store);p,s=segment(store,p,0,2);r=rev(store,p,s,[event(500)])
    client=TestClient(app);root='/api/advanced/projects/'+p['id']
    assert client.get(root+'/audio',headers={'range':'bytes=0-99'}).status_code==206
    assert client.get(root+'/audio').headers['content-type']=='audio/wav'
    assert client.get(root+'/revisions/'+r['id']).json()['chart']
    result=client.post(root+'/assemblies',json={'preroll':0}).json()
    chart=client.get(root+'/assemblies/'+result['id']+'/charts/balanced--easy').json();assert chart['chart']
    assert client.get(root+'/assemblies/'+result['id']+'/download').content[:2]==b'PK'

def test_short_fast_stratification_no_minimum_eight():
    from malody_studio.difficulty import calibrate
    notes,_=calibrate([],250,'easy',0,1,pattern='balanced',min_notes=0)
    assert notes==[]

def test_independent_short_segment_preserves_notes_and_conditions(project,tmp_path,monkeypatch):
    from malody_studio import advanced_generation as generation,engine
    store,p=project;p,s=segment(store,p,1,1.25)
    calls=[]
    class FakeEngine:
        def prepare(self,y,rate,progress):return 'prepared'
        def generate(self,wave,unused,options,progress):
            calls.append((options['mug_difficulty'],options['seed']))
            return [a.Note(1050,2),a.Note(1150,3),a.Note(2000,0)]
        def unload(self):pass
    monkeypatch.setattr(engine,'Engine',FakeEngine)
    settings={**p['settings'],'engine':'mug','fixed_seed':True,'dynamic_enabled':False}
    selection=a.variants(['balanced'],['easy','medium'])
    options={'_advanced':{'project':p,'segment':s,'settings':settings,'variants':selection}}
    one=generation.run(store.directory(p['id'])/'source.wav',tmp_path/'one',options,lambda *_:None)
    two=generation.run(store.directory(p['id'])/'source.wav',tmp_path/'two',options,lambda *_:None)
    assert [x[0] for x in calls[:2]]==[2.,3.3]
    assert calls[:2]==calls[2:]
    for row,row2 in zip(one['advanced_result'],two['advanced_result']):
        assert row['kind']=='model_raw'
        assert [(e['start_ms'],e['lane']) for e in row['events']]==[(1050,2),(1150,3)]
        assert [{k:v for k,v in e.items() if k!='id'} for e in row['events']]==[{k:v for k,v in e.items() if k!='id'} for e in row2['events']]

def test_v32_receives_native_segment_and_shared_timing(project,tmp_path,monkeypatch):
    from malody_studio import advanced_generation as generation,mapperatorinator
    store,p=project;p,s=segment(store,p,2,3)
    p=store.update(p['id'],{'tempo':{'points':[[0,120],[2000,180]]}});calls=[]
    def generate(source,directory,options,progress):
        calls.append(options)
        return {'easy':([a.Note(2100,1)],[[0,120],[2000,180]])},{}
    monkeypatch.setattr(mapperatorinator,'generate',generate)
    result=generation.run(store.directory(p['id'])/'source.wav',tmp_path/'v32',{'_advanced':{'project':p,'segment':s,'settings':{**p['settings'],'dynamic_enabled':False},'variants':p['variants']}},lambda *_:None)
    assert calls[0]['start_time']==2000 and calls[0]['end_time']==3000
    assert Path(calls[0]['timing_reference']).is_file()
    assert result['advanced_result'][0]['events'][0]['start_ms']==2100

def test_rules_retain_engine_and_do_not_select_candidate(project):
    from malody_studio.advanced_generation import rule_candidate
    store,p=project;p,s=segment(store,p,0,3);r=rev(store,p,s,[event(500,0,2800)])
    changed={**p['settings'],'engine':'mug'};changed['difficulty_rules']=copy.deepcopy(changed['difficulty_rules']);changed['difficulty_rules']['easy']['hold_ms']=400
    candidate=rule_candidate(store,p['id'],r['id'],changed)
    assert candidate['settings']['engine']=='v32'
    assert candidate['events'][0]['end_ms']==900
    assert store.load(p['id'])['segments'][0]['active']['balanced--easy']==r['id']

def test_atomic_write_retries_windows_reader_lock(tmp_path,monkeypatch):
    target=tmp_path/'manifest.json';replace=Path.replace;calls=[]
    def locked(path,destination):
        calls.append(destination)
        if len(calls)<3:raise PermissionError('Windows reader lock')
        return replace(path,destination)
    monkeypatch.setattr(Path,'replace',locked);monkeypatch.setattr(a.time,'sleep',lambda _:None)
    a.atomic(target,{'revision':2})
    assert a.read(target)['revision']==2 and len(calls)==3
    assert list(tmp_path.glob('*.tmp'))==[]

def test_manual_bpm_updates_cached_anchors_without_reanalysis(project):
    store,p=project
    a.atomic(store.directory(p['id'])/'features.json',{'anchors':[{'id':'onset-1','kind':'onset','time_ms':700},{'id':'beat-1','kind':'estimated_beat','time_ms':500}],'activity':[]})
    store.update(p['id'],{'tempo':{'points':[[0,60],[2000,120]]}})
    result=agent.features(store,p['id'])
    beats=[x['time_ms'] for x in result['anchors'] if x['kind']=='estimated_beat']
    assert beats[:4]==[0,1000,2000,2500] and result['anchors'][0]['id']=='onset-1'

@pytest.mark.parametrize('value',[None,True,float('nan'),'700'])
def test_patch_rejects_invalid_times(project,value):
    store,p=project;p,s=segment(store,p,0,2);base=rev(store,p,s,[event(500,id='n1')]);groups=patch(base,start_ms=value,anchor_id='a1')
    if value is None:groups[0]['operations'][0]['type']='add'
    with pytest.raises(ValueError):agent.anchored_patch(base,groups,0,2000,[{'id':'a1','time_ms':700}])

def test_advanced_retry_retains_snapshot_and_source(project,monkeypatch):
    from malody_studio import server,advanced_api
    store,p=project;p,s=segment(store,p,0,2)
    snapshot={'project':p,'segment':s,'settings':p['settings'],'variants':p['variants']}
    job={'id':a.uid(),'status':'failed','options':{'_advanced':snapshot,'engine':'v32'}};received=[]
    monkeypatch.setattr(advanced_api,'store',store);monkeypatch.setattr(server,'get_job',lambda _:job)
    monkeypatch.setattr(server,'ensure_engine',lambda _:None)
    monkeypatch.setattr(server,'enqueue_job',lambda options,ref:received.append((options,ref)) or {'id':'retry'})
    assert server.regenerate_job(job['id'])['id']=='retry'
    assert received[0][0]['_advanced']==snapshot and received[0][1]['path'].endswith('/source.wav')

def test_cross_window_hold_keeps_unchanged_tail_and_rejects_tail_edits(project):
    store,p=project;p,s=segment(store,p,0,5);base=rev(store,p,s,[event(500,0,4000,id='n1')])
    groups=patch(base,lane=1,end_ms=4000);anchors=[{'id':'a1','time_ms':500}]
    result,_=agent.anchored_patch(base,groups,0,2000,anchors)
    assert result[0]['lane']==1 and result[0]['end_ms']==4000
    groups[0]['operations'][0].update(end_ms=4500,end_anchor_id='a2');groups[0]['evidence'].append('a2')
    with pytest.raises(ValueError,match='越过'):agent.anchored_patch(base,groups,0,2000,anchors+[{'id':'a2','time_ms':4500}])


def test_export_uses_complete_original_and_peak_safe_gain_for_a_stem_chart(project):
    store,_=project
    phase=np.arange(2*a.SR)/a.SR
    source=store.root.parent/'peak-source.wav'
    music=np.column_stack((1.35*np.sin(2*np.pi*440*phase),1.2*np.sin(2*np.pi*660*phase))).astype(np.float32)
    sf.write(source,music,a.SR,subtype='FLOAT')
    p=store.create(source,'Peak export','');p,s=segment(store,p,0,2)
    r=store.add_revision(p['id'],s['id'],'balanced--easy',[event(400,0)],a.defaults(),'stem_raw',{'source_role':'vocals','source_id':'frozen-vocals'})
    result=a.assemble(store,p['id'],0)
    directory=store.directory(p['id'])/'assemblies'/result['id']
    report=a.read(directory/'report.json');processing=report['audio_processing']
    assert processing['source']=='original' and processing['quality']==7
    assert processing['gain']<1 and not processing['eq'] and not processing['denoise']
    preserved,_=sf.read(directory/'assembled.wav');np.testing.assert_allclose(preserved,music,atol=1e-7)
    encoded,rate=sf.read(directory/'audio.ogg')
    assert rate==a.SR and encoded.shape==music.shape
    # Encoding uses the original stereo music, not the chart's vocal source.
    assert np.corrcoef(encoded.ravel(),(music*processing['gain']).ravel())[0,1]>.999
    assert report['charts'][0]['sources'][0]['revision_id']==r['id']
    assert report['charts'][0]['sources'][0]['source_role']=='vocals'
