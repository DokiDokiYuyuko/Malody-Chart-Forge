"""Bounded evidence tools, provider adapters and validated user-applied patches."""
import base64
import hashlib
import copy
import ctypes
from ctypes import wintypes
import io
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import httpx
import numpy as np
import soundfile as sf
from .advanced import ROOT, SR, uid, now, atomic, read, valid_events, stats

SKILL_VERSION='1.0.0'
PRIVATE=ROOT/'private'/'agent-config.dpapi'
_config_lock=threading.RLock()
_config={}

def crypt(data,decrypt=False):
    if os.name!='nt':raise ValueError('持久保存密钥仅支持 Windows 用户加密；其他系统请使用会话模式')
    class Blob(ctypes.Structure):_fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data);source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));out=Blob()
    lib=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    function=lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    function.restype=wintypes.BOOL
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(out)):raise ValueError('Windows 密钥加密操作失败')
    try:return ctypes.string_at(out.data,out.size)
    finally:
        kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree(ctypes.cast(out.data,ctypes.c_void_p))

def restore_config():
    global _config
    if PRIVATE.is_file():
        try:_config=json.loads(crypt(PRIVATE.read_bytes(),True))
        except (OSError,ValueError):_config={}

def config_public():
    with _config_lock:
        return {role:{k:v for k,v in value.items() if k!='key'}|{'has_key':bool(value.get('key'))} for role,value in _config.items()}

def save_config(payload):
    global _config
    with _config_lock:
        result={}
        for role in ('repair','audio'):
            value=payload.get(role)
            if not value:continue
            if not isinstance(value,dict):raise ValueError('API 配置无效')
            base=str(value.get('base_url','')).rstrip('/');parsed=urlparse(base)
            if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('API 地址须为有效 HTTP(S) 基础地址')
            protocol=value.get('protocol','responses')
            if protocol not in ('responses','chat'):raise ValueError('请选择 Responses 或 Chat Completions')
            model=str(value.get('model','')).strip();key=value.get('key') or _config.get(role,{}).get('key')
            if not model or len(model)>200 or not isinstance(key,str) or not key.strip():raise ValueError('请填写模型与 API 密钥')
            old=_config.get(role,{})
            config={'base_url':base,'protocol':protocol,'model':model,'key':key.strip()}
            if all(config.get(k)==old.get(k) for k in ('base_url','protocol','model','key')):config['capabilities']=old.get('capabilities',{})
            result[role]=config
        if 'repair' not in result:raise ValueError('请先配置修谱模型')
        if payload.get('remember'):
            data=crypt(json.dumps(result).encode());PRIVATE.parent.mkdir(exist_ok=True);temp=PRIVATE.with_suffix('.tmp');temp.write_bytes(data);temp.replace(PRIVATE)
        elif PRIVATE.exists():PRIVATE.unlink()
        _config=result;return config_public()

def configured(role):
    with _config_lock:
        if role not in _config:raise ValueError('请在 Agent 设置中配置'+('修谱模型' if role=='repair' else '听音模型'))
        return copy.deepcopy(_config[role])

class Provider:
    def __init__(self,config,transport=None):self.config=config;self.transport=transport
    def request(self,messages,tools=None,force=None):
        c=self.config
        if c['protocol']=='responses':
            body={'model':c['model'],'input':messages,'store':False,'max_output_tokens':6000}
            if tools:
                body['tools']=tools;body['parallel_tool_calls']=False
                if force:body['tool_choice']={'type':'function','name':force}
        else:
            body={'model':c['model'],'messages':messages,'max_tokens':6000}
            if tools:
                body['tools']=[{'type':'function','function':{k:v for k,v in t.items() if k!='type'}} for t in tools]
                if force:body['tool_choice']={'type':'function','function':{'name':force}}
        suffix='/responses' if c['protocol']=='responses' else '/chat/completions'
        # No implicit paid retries. Do not expose raw upstream responses containing private data.
        with httpx.Client(timeout=httpx.Timeout(120,connect=20),transport=self.transport,follow_redirects=False) as client:
            try:r=client.post(c['base_url']+suffix,headers={'Authorization':'Bearer '+c['key']},json=body)
            except httpx.RequestError:raise ValueError('API 连接超时或网络失败；请检查地址和网络后手动重试')
        if not r.is_success:raise ValueError(f'API 返回 HTTP {r.status_code}；请检查服务地址、模型、密钥和接口类型')
        try:data=r.json()
        except ValueError:raise ValueError('API 没有返回有效 JSON')
        calls=[];text='';out=[]
        if c['protocol']=='responses':
            if data.get('status') in ('incomplete','failed','cancelled'):raise ValueError('模型响应未完成；当前谱面保持原版本')
            out=data.get('output',[])
            for item in out:
                if item.get('type')=='function_call':calls.append({'id':item['call_id'],'name':item['name'],'arguments':item['arguments']})
                if item.get('type')=='message':text+=''.join(x.get('text','') for x in item.get('content',[]) if x.get('type')=='output_text')
        else:
            choices=data.get('choices') or []
            if not choices or choices[0].get('finish_reason') in ('length','content_filter'):raise ValueError('模型响应不完整')
            msg=choices[0].get('message',{});out=[msg];text=msg.get('content') or ''
            calls=[{'id':t['id'],'name':t['function']['name'],'arguments':t['function']['arguments']} for t in msg.get('tool_calls',[])]
        return {'calls':calls,'text':text,'output':out,'usage':data.get('usage',{})}

def tool(name,description,properties=None):
    properties=properties or {}
    return {'type':'function','name':name,'description':description,'strict':True,
            'parameters':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}}

def probe(role,transport=None):
    c=configured(role);p=Provider(c,transport);usage={};caps={'text':False,'tools':False,'audio':False,'checked':now()}
    try:
        result=p.request([{'role':'user','content':'Call capability_probe once, with echo=ok.'}],[tool('capability_probe','Return the echo.',{'echo':{'type':'string'}})],'capability_probe')
        caps['text']=True;caps['tools']=any(x['name']=='capability_probe' and json.loads(x['arguments']).get('echo')=='ok' for x in result['calls']);usage=result['usage']
    except ValueError:
        result=p.request([{'role':'user','content':'Reply with ok.'}]);caps['text']=bool(result['text']);usage=result['usage']
    if role=='audio':
        if c['protocol']!='chat':caps['audio_error']='当前听音适配器使用 Chat Completions 的 WAV 输入，请选择该接口'
        else:
            buf=io.BytesIO();tone=(np.sin(np.arange(8000)*2*np.pi*440/16000)*.05).astype('float32');sf.write(buf,tone,16000,format='WAV',subtype='PCM_16')
            try:
                r=p.request([{'role':'user','content':[{'type':'text','text':'Describe the short audio briefly.'},{'type':'input_audio','input_audio':{'format':'wav','data':base64.b64encode(buf.getvalue()).decode()}}]}]);caps['audio']=bool(r['text'])
            except ValueError as e:caps['audio_error']=str(e)
    with _config_lock:
        if all(_config.get(role,{}).get(k)==c.get(k) for k in ('model','key','base_url','protocol')):_config[role]['capabilities']=caps
    return {'capabilities':caps,'usage':usage,'note':'连接检查会产生少量 API 用量'}

def tempo_features(result,p):
    """Acoustic features are cached; manually edited beat anchors stay current."""
    result=copy.deepcopy(result);result['tempo']=p['tempo']
    if p['tempo'].get('points'):
        result['anchors']=[a for a in result['anchors'] if a['kind']!='estimated_beat']
        points=p['tempo']['points'];index=0
        for i,(start,bpm) in enumerate(points):
            end=points[i+1][0] if i+1<len(points) else p['duration']*1000
            for t in np.arange(start,end,60000/bpm):
                result['anchors'].append({'id':f'beat-{index}','time_ms':round(float(t),3),'kind':'estimated_beat','uncertain':not p['tempo'].get('manual',False)});index+=1
    return result

def features(store,pid):
    directory=store.directory(pid);cache=directory/'features.json'
    p=store.load(pid)
    if cache.is_file():return tempo_features(read(cache),p)
    import librosa
    from scipy.signal import find_peaks
    p=store.load(pid);data,_=sf.read(directory/'source.wav',dtype='float32',always_2d=True)
    y=librosa.resample(data.mean(axis=1),orig_sr=SR,target_sr=22050);hop=220
    spectrum=np.abs(librosa.stft(y,n_fft=1024,hop_length=hop));harmonic,percussive=librosa.decompose.hpss(spectrum)
    frequencies=librosa.fft_frequencies(sr=22050,n_fft=1024);bands=[]
    for lower,upper in ((50,250),(250,2000),(2000,11025)):
        v=np.log1p(spectrum[(frequencies>=lower)&(frequencies<upper)]*10)
        flux=np.maximum(np.diff(v,axis=1,prepend=v[:,:1]),0).mean(axis=0);flux/=max(float(np.percentile(flux,95)),1e-6);bands.append(flux)
    envelope=np.maximum.reduce(bands);peaks,_=find_peaks(envelope,distance=3,prominence=.15,height=.2)
    rms=librosa.feature.rms(S=spectrum,frame_length=1024,hop_length=hop)[0]
    anchors=[{'id':f'onset-{i}','time_ms':round(float(t*hop/22050*1000),3),'strength':round(float(envelope[t]),4),'kind':'onset'} for i,t in enumerate(peaks) if rms[t]>max(float(np.max(rms))*.008,1e-5)]
    anchors+=[{'id':f'beat-{i}','time_ms':t*1000,'kind':'estimated_beat','uncertain':p['tempo']['uncertain']} for i,t in enumerate(p['tempo']['beat_times'])]
    times=np.arange(0,len(rms),10);activity=[{'time_ms':round(float(i*hop/22050*1000),3),'rms':round(float(rms[i]),6),'bands':[round(float(v[i]),3) for v in bands],
        'harmonic':round(float(harmonic[:,i].mean()),5),'percussive':round(float(percussive[:,i].mean()),5)} for i in times]
    np.savez_compressed(directory/'spectrum.npz',spectrum=spectrum[:,::10],times=np.arange(0,spectrum.shape[1],10)*hop/22050*1000)
    result={'schema':1,'source_sha256':p['source_sha256'],'anchors':anchors,'activity':activity,'tempo':p['tempo'],'hpss_note':'HPSS 为谐波/打击成分参考，不是人声或乐器准确分离'}
    atomic(cache,result);return tempo_features(result,p)

def windows(events,start,end):
    cursor=start
    while cursor<end:
        finish=min(cursor+12000,end); heads=sorted(e['start_ms'] for e in events if cursor<=e['start_ms']<finish)
        if len(heads)>256:
            finish=heads[256]
            if finish<=cursor:raise ValueError('单个时刻超过 256 个音符，结构异常')
        yield cursor,finish;cursor=finish

def anchored_patch(base,groups,start,end,anchors,selected=None):
    if not isinstance(groups,list) or len(groups)>80:raise ValueError('修改分组格式无效或超过 80 组')
    events={e['id']:copy.deepcopy(e) for e in base['events']};known={a['id']:a for a in anchors};touched=set();seen_groups=set()
    diff=[]
    for group in groups:
        if not isinstance(group,dict) or not isinstance(group.get('id'),str) or group['id'] in seen_groups:raise ValueError('修改组 ID 重复或缺失')
        seen_groups.add(group['id'])
        if selected is not None and group['id'] not in selected:continue
        ops=group.get('operations');evidence=group.get('evidence');reason=group.get('reason')
        if not isinstance(ops,list) or not ops or len(ops)>64 or not isinstance(reason,str) or not reason.strip() or not isinstance(evidence,list) or not evidence or any(e not in known for e in evidence):raise ValueError('每组修改须含操作、理由与有效时间证据')
        for operation_index,op in enumerate(ops):
            if not isinstance(op,dict) or set(op)-{'type','note_id','lane','start_ms','end_ms','anchor_id','end_anchor_id'}:raise ValueError('修改操作字段无效')
            kind=op.get('type');eid=op.get('note_id');old=events.get(eid)
            if kind not in ('add','delete','update'):raise ValueError('仅支持增加、删除与更新音符')
            if kind!='add' and old is None:raise ValueError('修改引用了未知音符 ID')
            if kind!='add' and (eid in touched or not start<=old['start_ms']<end):raise ValueError('音符被重复修改或位于只读上下文')
            if kind!='add':touched.add(eid)
            if kind=='delete':del events[eid];diff.append({'type':'delete','before':old,'group_id':group['id']});continue
            stable_id='agent-'+hashlib.sha256((base['id']+group['id']+str(operation_index)).encode()).hexdigest()[:32]
            n=copy.deepcopy(old) if old else {'id':stable_id,'lane':None,'start_ms':None,'end_ms':None}
            for field in ('lane','start_ms','end_ms'):
                if field in op and (op[field] is not None or field=='end_ms'):n[field]=op[field]
            for field in ('start_ms','end_ms'):
                value=n.get(field)
                if value is None and field=='end_ms':continue
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('修改音符时间必须是有限数值')
            if kind=='add' or n['start_ms']!=old['start_ms']:
                ref=known.get(op.get('anchor_id'))
                if not ref or ref['id'] not in evidence or abs(ref['time_ms']-n['start_ms'])>1:raise ValueError('新增或移时必须引用对应音频/节拍时间锚点，误差不超过 1 ms')
            if n.get('end_ms') is not None and (old is None or n['end_ms']!=old.get('end_ms')):
                ref=known.get(op.get('end_anchor_id'))
                if not ref or ref['id'] not in evidence or abs(ref['time_ms']-n['end_ms'])>1:raise ValueError('修改长条尾部必须引用时间锚点')
            outside_tail=n.get('end_ms') is not None and n['end_ms']>end and (old is None or n['end_ms']!=old.get('end_ms'))
            if not start<=n['start_ms']<end or outside_tail:raise ValueError('修改越过核心范围；上下文只读')
            events[n['id']]=n;diff.append({'type':kind,'before':old,'after':n,'group_id':group['id']})
    if selected is not None and set(selected)-seen_groups:raise ValueError('选中了不存在的修改组')
    result=sorted(events.values(),key=lambda e:(e['start_ms'],e['lane']))
    a,b=(v*1000/SR for v in base['range']);valid_events(result,a,b,max(b,max((e.get('end_ms') or e['start_ms'] for e in base['events']),default=b)))
    return result,diff

def alignment_png(store,pid,base,start,end):
    from PIL import Image,ImageDraw,ImageFont
    data,_=sf.read(store.directory(pid)/'source.wav',start=round(start*SR/1000),stop=round(end*SR/1000),dtype='float32',always_2d=True)
    image=Image.new('RGB',(1440,720),'#0b1d2b');draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=18)
    draw.text((24,16),'Source audio and 4K events / original milliseconds',fill='#c4dfe8',font=font)
    def x(t):return 100+int((t-start)/(end-start)*1300)
    draw.rectangle((100,60,1400,220),fill='#102839');draw.rectangle((100,420,1400,660),fill='#102839')
    acoustic=features(store,pid)
    from .spectrum import SpectrumService
    service=SpectrumService(store.directory(pid))
    manifest=service.request('original',store.directory(pid)/'source.wav',start,end)
    deadline=time.monotonic()+20
    matrix=service.alignment_matrix(manifest['analysis_id'],start,end)
    while matrix is None and time.monotonic()<deadline:
        time.sleep(.05)
        matrix=service.alignment_matrix(manifest['analysis_id'],start,end)
    if matrix is None:raise ValueError('对齐频谱仍在分析，请稍后重试')
    if len(matrix['times_ms']):
        # Use exactly the viewport's fixed dB range, global frame centers and
        # logarithmic bands. Never stretch a cropped STFT to arbitrary endpoints.
        db=np.clip((matrix['db']+90)/90,0,1)
        sample_times=start+(np.arange(1300)+.5)/1300*(end-start)
        indices=np.clip(np.rint((sample_times-matrix['times_ms'][0])/(110/22050*1000)).astype(int),0,len(db)-1)
        columns=db[indices].T[::-1]
        pixels=np.stack((12+38*columns,28+175*columns,43+190*columns),axis=-1).astype('uint8')
        image.paste(Image.fromarray(pixels).resize((1300,140)),(100,245))
    draw.text((24,280),'STFT',fill='#c4dfe8',font=font)
    step=max(1,len(data)//2600);mono=data[::step].mean(axis=1)
    points=[(x(start+i*step/SR*1000),140-float(v)*72) for i,v in enumerate(mono)]
    if len(points)>1:draw.line(points,fill='#62cfe1',width=1)
    colors=['#62cfe1','#84aafa','#efc65c','#ed88bc']
    for e in base['events']:
        if e['start_ms']<end and (e.get('end_ms') or e['start_ms'])>=start:
            row=445+e['lane']*57
            draw.rectangle((x(max(start,e['start_ms'])),row,x(min(end,e.get('end_ms') or e['start_ms']+20)),row+8),fill=colors[e['lane']])
    for a in acoustic['anchors']:
        if start<=a['time_ms']<end:
            if a['kind']=='onset':draw.line((x(a['time_ms']),60,x(a['time_ms']),220),fill='#395955',width=1)
            elif a['kind']=='estimated_beat':
                for y in range(420,660,12):draw.line((x(a['time_ms']),y,x(a['time_ms']),y+4),fill='#7a7149',width=1)
    for i,key in enumerate(('D / 1','F / 2','J / 3','K / 4')):draw.text((25,440+i*57),key,fill=colors[i],font=font)
    for t in np.linspace(start,end,7):draw.text((x(t)-24,680),str(round(t)),fill='#c4dfe8',font=font)
    buf=io.BytesIO();image.save(buf,format='PNG');return buf.getvalue()

OP_SCHEMA={'type':'object','properties':{'type':{'type':'string','enum':['add','delete','update']},'note_id':{'type':['string','null']},'lane':{'type':['integer','null']},
    'start_ms':{'type':['number','null']},'end_ms':{'type':['number','null']},'anchor_id':{'type':['string','null']},'end_anchor_id':{'type':['string','null']}},
    'required':['type','note_id','lane','start_ms','end_ms','anchor_id','end_anchor_id'],'additionalProperties':False}
GROUP_SCHEMA={'type':'object','properties':{'id':{'type':'string'},'reason':{'type':'string'},'evidence':{'type':'array','items':{'type':'string'}},'operations':{'type':'array','items':OP_SCHEMA}},'required':['id','reason','evidence','operations'],'additionalProperties':False}
TOOLS=[tool('get_project_constraints','Read target playstyle, difficulty and readonly context.'),tool('get_chart_events','Read stable note IDs and source timestamps.'),
       tool('get_audio_features','Read onset/beat anchors, RMS, band flux and HPSS reference.'),tool('get_audio_snippet','Ask the configured audio model about exactly this window.'),
       tool('get_alignment_plot','Read the waveform and notes on the same source timeline.'),tool('compute_quality','Get structural and heuristic quality findings.'),
       tool('submit_patch_candidate','Validate groups; never apply to the active chart.',{'base_revision':{'type':'string'},'groups':{'type':'array','items':GROUP_SCHEMA}}),
       tool('compare_candidate','Read the current validated proposal and statistics.')]

def event_quality(events,start,end,alerts):
    core=[e for e in events if start<=e['start_ms']<end];chords={};intervals=[];last={}
    for e in sorted(core,key=lambda e:e['start_ms']):
        key=round(e['start_ms'],1);chords.setdefault(key,[]).append(e['id'])
        if e['lane'] in last:
            previous=last[e['lane']];intervals.append({'previous_id':previous['id'],'note_id':e['id'],'lane':e['lane'],'interval_ms':round(e['start_ms']-previous['start_ms'],3)})
        last[e['lane']]=e
    return {'source_range':[start,end],'stats':stats(core,start,end),'chords':[{'time_ms':t,'note_ids':ids} for t,ids in chords.items() if len(ids)>1],
            'same_lane_intervals':intervals,'hold_occupancy':[{'id':e['id'],'lane':e['lane'],'start_ms':e['start_ms'],'end_ms':e['end_ms']} for e in events if e.get('end_ms') is not None],
            'alerts':[x for x in alerts if start<=x.get('start_ms',start)<end],'note':'Metrics are evidence, not a musical-quality score or Malody level.'}

def run_review(store,pid,task_id,provider_factory=Provider):
    task_path=store.directory(pid)/'reviews'/(task_id+'.json');task=read(task_path)
    try:
        p=store.load(pid);base=store.revision(pid,task['base_revision']);config=configured('repair');provider=provider_factory(config)
        if not config.get('capabilities',{}).get('text'):raise ValueError('请先执行修谱模型连接检查')
        task.update(status='running',message='提取本地音频与音符证据',requests=0,tool_calls=0,tokens={},groups=[],diagnostics=[],tool_log=[]);atomic(task_path,task)
        all_features=features(store,pid);all_anchors=all_features['anchors'];task['anchors']=[x for x in all_anchors if task['start_ms']-2000<=x['time_ms']<=task['end_ms']+2000];examples=[]
        for rid in reversed(p['examples']):
            r=store.revision(pid,rid)
            if r['variant']==base['variant']:examples.append({'variant':r['variant'],'events':r['events'][:64],'source':'用户认可示例'})
            if len(examples)>=3:break
        skill=(ROOT/'skills'/'malody-chart-review'/'SKILL.md').read_text(encoding='utf-8')
        used_groups=[];tool_cap=config['capabilities'].get('tools',False)
        ranges=list(windows(base['events'],task['start_ms'],task['end_ms']));task['windows']=ranges
        for wi,(a,b) in enumerate(ranges):
            ca=max(0,a-2000);cb=min(p['duration']*1000,b+2000)
            anchors=[x for x in all_anchors if ca<=x['time_ms']<=cb]
            ev=[x for x in base['events'] if x['start_ms']<cb and (x.get('end_ms') or x['start_ms'])>=ca]
            evidence={'core_range':[a,b],'readonly_context':[ca,cb],'base_revision':base['id'],'events':ev,'anchors':anchors,
                'constraints':{'profile':p['profile'],'variant':base['variant'],'settings':base['settings']},'goal':task['goal'],'examples':examples,'tempo':p['tempo']}
            messages=[{'role':'system','content':skill},{'role':'user','content':json.dumps(evidence,ensure_ascii=False)}]
            window_calls=0;rounds=0;proposal=[];candidate=base['events'];audio_sent=False
            def observe():
                nonlocal audio_sent
                audio_config=configured('audio')
                if not audio_config.get('capabilities',{}).get('audio'):raise ValueError('听音模型未通过音频输入连接检查')
                data,_=sf.read(store.directory(pid)/'source.wav',start=round(ca*SR/1000),stop=round(cb*SR/1000),dtype='float32',always_2d=True)
                buf=io.BytesIO();sf.write(buf,data,SR,format='WAV',subtype='PCM_16');audio_sent=True
                response=provider_factory(audio_config).request([{'role':'user','content':[{'type':'text','text':f'Analyze musical attacks, sustained sounds and phrasing. Source range {ca:.3f}–{cb:.3f} ms. Do not invent precise note timestamps; local tools own alignment.'},{'type':'input_audio','input_audio':{'format':'wav','data':base64.b64encode(buf.getvalue()).decode()}}]}])
                task['requests']+=1;task['audio_ranges'].append([ca,cb]);add_usage(task,response['usage']);return response['text']
            # Audio snippets are sent only to the explicitly configured, tested audio role.
            if task.get('send_audio'):
                try:observation=observe();messages.append({'role':'user','content':'Supplemental listening observation (not precise timing evidence): '+observation})
                except ValueError as error:task['diagnostics'].append({'window':[a,b],'message':str(error)})
            for iteration in range(10 if tool_cap else 1):
                remaining=8-window_calls
                offered=TOOLS if remaining>0 and rounds<2 and tool_cap else None
                result=provider.request(messages,offered);task['requests']+=1;add_usage(task,result['usage'])
                messages.extend(result['output'] if config['protocol']=='responses' else [{'role':'assistant',**result['output'][0]}])
                if result['text']:task['diagnostics'].append({'window':[a,b],'message':result['text']})
                if not result['calls']:break
                for call in result['calls']:
                    is_image=False
                    if window_calls>=8:output={'error':'本窗口工具调用上限已到'}
                    else:
                        window_calls+=1;task['tool_calls']+=1
                        try:
                            args=json.loads(call['arguments']);name=call['name']
                            if name=='get_project_constraints':output=evidence['constraints']|{'core_range':[a,b],'readonly_context':[ca,cb]}
                            elif name=='get_chart_events':output=ev
                            elif name=='get_audio_features':output={'anchors':anchors,'activity':[x for x in all_features['activity'] if ca<=x['time_ms']<cb],'hpss_note':all_features['hpss_note']}
                            elif name=='get_audio_snippet':output={'observation':observe(),'source_range':[ca,cb]} if task.get('send_audio') and not audio_sent else {'note':'音频已在上下文提供，或用户未选择发送音频'}
                            elif name=='get_alignment_plot':output={'source_range':[ca,cb],'image':'下一条用户消息附对齐图'};is_image=True
                            elif name=='compute_quality':output=event_quality(ev,a,b,store.local_chart(pid,base['id'])['alerts'])
                            elif name=='submit_patch_candidate':
                                rounds+=1
                                if rounds>2:raise ValueError('本窗口修订最多两轮')
                                if args.get('base_revision')!=base['id']:raise ValueError('基础版本 ID 不匹配')
                                proposed=args.get('groups',[]);candidate,diff=anchored_patch(base,proposed,a,b,anchors)
                                proposal=[{**g,'id':f'w{wi}-'+g['id']} for g in proposed];output={'valid':True,'stats':stats(candidate,base['range'][0]*1000/SR,base['range'][1]*1000/SR),'changed_notes':len(diff)}
                            elif name=='compare_candidate':output={'groups':proposal,'before':base['stats'],'after':stats(candidate,base['range'][0]*1000/SR,base['range'][1]*1000/SR)}
                            else:raise ValueError('不支持该工具')
                        except (ValueError,TypeError,KeyError) as error:output={'error':str(error)}
                    task['tool_log'].append({'window':[a,b],'name':call['name'],'result':output});atomic(task_path,task)
                    if config['protocol']=='responses':messages.append({'type':'function_call_output','call_id':call['id'],'output':json.dumps(output,ensure_ascii=False)})
                    else:messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(output,ensure_ascii=False)})
                    if is_image:
                        url='data:image/png;base64,'+base64.b64encode(alignment_png(store,pid,base,ca,cb)).decode()
                        image={'type':'input_image','image_url':url} if config['protocol']=='responses' else {'type':'image_url','image_url':{'url':url}}
                        messages.append({'role':'user','content':[image]})
                if window_calls>=8 or rounds>=2:break
            used_groups.extend(proposal);task.update(message=f'已分析 {wi+1}/{len(ranges)} 个窗口',progress=round((wi+1)/len(ranges)*100));atomic(task_path,task)
        result,diff=anchored_patch(base,used_groups,task['start_ms'],task['end_ms'],all_anchors)
        task.update(status='completed',message='候选已验证，请比较后手动应用' if used_groups else '分析完成，未产生可应用修改',groups=used_groups,diff=diff,
                    candidate_events=result,stats_before=base['stats'],stats_after=stats(result,base['range'][0]*1000/SR,base['range'][1]*1000/SR),skill_version=SKILL_VERSION,tools_enabled=tool_cap)
    except Exception as error:task.update(status='failed',message=str(error),error=str(error))
    atomic(task_path,task);return task

def add_usage(task,usage):
    for key in ('input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens'):
        if isinstance(usage.get(key),int):task['tokens'][key]=task['tokens'].get(key,0)+usage[key]

def apply_review(store,pid,tid,selected,activate=True):
    with store.lock:
        p=store.load(pid);task=read(store.directory(pid)/'reviews'/(tid+'.json'));base=store.revision(pid,task['base_revision']);s=store.segment(p,base['segment_id'])
        if task['status']!='completed' or not isinstance(selected,list) or not selected or len(set(selected))!=len(selected):raise ValueError('请选择有效的修改组')
        if s['active'].get(base['variant'])!=base['id'] or [s['start_sample'],s['end_sample']]!=base['range']:raise ValueError('基础版本已变化，旧建议过期，请重新分析')
        ev,diff=anchored_patch(base,task['groups'],task['start_ms'],task['end_ms'],task.get('anchors') or features(store,pid)['anchors'],selected)
        revision=store.add_revision(pid,s['id'],base['variant'],ev,base['settings'],'agent',{'parent':base['id'],'review_task':tid,'selected_groups':selected,'diff':diff},bounds=base['range'],activate_initial=False)
        # The explicit Apply button is the sole permission to activate this candidate.
        if activate:store.select(pid,s['id'],base['variant'],revision['id'])
        task['decisions'].append({'created':now(),'accepted':selected,'rejected':[g['id'] for g in task['groups'] if g['id'] not in selected],'revision_id':revision['id']});atomic(store.directory(pid)/'reviews'/(tid+'.json'),task)
        return revision
