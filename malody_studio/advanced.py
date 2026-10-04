"""Sample-accurate authoring. Immutable revisions, explicit selection, no cloud I/O."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import numpy as np
import soundfile as sf
from .paths import ROOT
from .audio import ffmpeg, analyze
from .charts import Note, beat_value, chart_stats, package, validate_chart
from .difficulty import PRESETS, PATTERN_CHOICES
from .mapperatorinator import serialize_with_timing
from .quality import assess

SR = 44100
CONDITIONS = dict(zip(PRESETS, (2., 3.3, 4.6, 5.9, 7.2, 8.)))

def revision_audio_source(revision):
    provenance = revision.get('provenance', {})
    if provenance.get('source_role') in ('vocals', 'accompaniment'):
        return provenance.get('source_id') or 'original'
    return 'original'

def uid():
    return uuid.uuid4().hex

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uid() + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    try:
        for attempt in range(10):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt==9:raise
                time.sleep(.025*(attempt+1))
    finally:temporary.unlink(missing_ok=True)

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('项目或版本 ID 无效')
    return value

def defaults():
    return dict(engine='v32', strategy='independent', steps=50, seed=20261003,
                fixed_seed=False, ln_ratio=.15, pattern_strength=20, mug_style='ranked',
                dynamic_enabled=True, dynamic_strength=1., section_granularity='balanced',
                source_mode='mix', separation_preset='htdemucs',
                mug_guidance=1.5, mug_eta=0., v32_temperature=.9, v32_top_p=.9,
                v32_column_temperature=.8, v32_cfg_scale=1., v32_year=2024,
                v32_descriptors=[], v32_negative_descriptors=[],
                conditions={'mug': dict(CONDITIONS), 'v32': dict(CONDITIONS)},
                difficulty_rules={k: {f: v[f] for f in ('rate', 'chord', 'gap', 'peak', 'hold_ms')} for k,v in PRESETS.items()})

def merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result

def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(defaults()):
        raise ValueError('生成设置包含未知字段')
    d = merge(defaults(), value)
    # Missing settings in historical snapshots retain the original generation policy.
    if 'dynamic_enabled' not in value:d['dynamic_enabled']=False
    if not isinstance(d['dynamic_enabled'],bool) or d['source_mode'] not in ('mix','vocals_accompaniment') or d['separation_preset'] not in ('htdemucs','htdemucs_ft'):
        raise ValueError('段落适配或分轨设置无效')
    if d['section_granularity'] not in ('fine','balanced','coarse'):
        raise ValueError('制谱段落粒度无效')
    if d['engine'] not in ('mug','v32') or d['strategy'] not in ('independent','fast'):
        raise ValueError('模型或生成策略无效')
    ranges = {'dynamic_strength':(0,1), 'seed': (0,2147483640), 'ln_ratio': (0,.8), 'pattern_strength': (5,35),
              'mug_guidance': (1,30), 'mug_eta': (0,1), 'v32_temperature': (.1,2),
              'v32_top_p': (.1,1), 'v32_column_temperature': (.1,2), 'v32_cfg_scale': (.5,5), 'v32_year': (2007,2024)}
    for key,(lo,hi) in ranges.items():
        v=d[key]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not lo<=v<=hi:
            raise ValueError(f'{key} 超出范围')
    if d['steps'] not in (20,50,100) or d['mug_style'] not in ('ranked','loved','graveyard'):
        raise ValueError('采样步数或模型风格无效')
    if not isinstance(d['fixed_seed'],bool) or int(d['seed'])!=d['seed'] or int(d['v32_year'])!=d['v32_year']:
        raise ValueError('种子、年份或固定种子设置无效')
    for engine, max_value in [('mug',8),('v32',10)]:
        if set(d['conditions'].get(engine,{}))!=set(PRESETS): raise ValueError('难度条件须包含六档')
        for v in d['conditions'][engine].values():
            if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not 1<=v<=max_value: raise ValueError('模型难度条件超出范围')
    rr={'rate':(.5,50),'chord':(1,4),'gap':(20,500),'peak':(1,56),'hold_ms':(100,5000)}
    if set(d['difficulty_rules'])!=set(PRESETS): raise ValueError('谱面规则须包含六档')
    for values in d['difficulty_rules'].values():
        if set(values)!=set(rr): raise ValueError('谱面规则字段无效')
        for key,(lo,hi) in rr.items():
            v=values[key]
            if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not lo<=v<=hi or (key!='rate' and int(v)!=v): raise ValueError('谱面规则超出范围')
    for key in ('v32_descriptors','v32_negative_descriptors'):
        v=d[key]
        if not isinstance(v,list) or len(v)>4 or len(set(v))!=len(v) or any(not isinstance(t,str) or len(t)>64 for t in v): raise ValueError('风格标签最多四个不重复值')
    return d

def variants(patterns, difficulties):
    if not isinstance(patterns,list) or not patterns or len(set(patterns))!=len(patterns) or any(p not in PATTERN_CHOICES for p in patterns): raise ValueError('请选择有效排键')
    if not isinstance(difficulties,list) or not difficulties or len(set(difficulties))!=len(difficulties) or any(d not in PRESETS for d in difficulties): raise ValueError('请选择有效难度')
    return [{'key':p+'--'+d,'pattern':p,'difficulty':d} for p in patterns for d in difficulties]

def decode(source, path):
    r=subprocess.run([ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',str(source),'-vn','-ac','2','-ar',str(SR),'-c:a','pcm_f32le',str(path)],capture_output=True,timeout=120)
    if r.returncode: raise ValueError('音频解码失败，请检查音乐文件')
    data, rate=sf.read(path,dtype='float32',always_2d=True)
    if rate!=SR or not .25<=len(data)/SR<=600 or not np.isfinite(data).all(): raise ValueError('高级台支持 250 ms 至 10 分钟的有效音频')
    return data

def audio_metadata(data):
    import librosa
    mono=librosa.resample(data.mean(axis=1),orig_sr=SR,target_sr=22050)
    try: tempo=analyze(mono,22050)
    except ValueError: tempo={'bpm':120.,'beat_times':[],'beat_variability':1.,'warnings':['节拍参考不可靠，暂用 120 BPM；可以手动修改，不改变音符时间。']}
    tempo['uncertain']=not tempo['beat_times'] or tempo.get('beat_variability',1)>.04
    waveform=[round(float(np.max(np.abs(c))),4) if len(c) else 0 for c in np.array_split(data,2400)]
    return waveform,tempo

def as_notes(events, origin=0, end=None):
    return [Note(e['start_ms']-origin,e['lane'],None if e.get('end_ms') is None else min(e['end_ms'],end if end is not None else e['end_ms'])-origin) for e in events]

def valid_events(events, start, end, source_end):
    for e in events:
        if not isinstance(e,dict):raise ValueError('音符事件格式无效')
        lane=e.get('lane');t=e.get('start_ms')
        if isinstance(lane,bool) or not isinstance(lane,int) or lane not in range(4):raise ValueError('轨道必须为 0–3')
        if isinstance(t,bool) or not isinstance(t,(float,int)) or not math.isfinite(t):raise ValueError('音符时间必须是有限数值')
    seen=set(); occupied=[-1e9]*4; previous=[-1e9]*4
    for e in sorted(events,key=lambda e:(e['start_ms'],e['lane'])):
        if not isinstance(e.get('id'),str) or e['id'] in seen: raise ValueError('音符 ID 重复或缺失')
        seen.add(e['id']); lane=e.get('lane'); t=e.get('start_ms'); tail=e.get('end_ms')
        if isinstance(lane,bool) or not isinstance(lane,int) or lane not in range(4): raise ValueError('轨道必须为 0–3')
        if isinstance(t,bool) or not isinstance(t,(float,int)) or not math.isfinite(t) or not start<=t<end: raise ValueError('音符头越过片段范围')
        if tail is not None and (isinstance(tail,bool) or not isinstance(tail,(float,int)) or not math.isfinite(tail) or not t<tail<=source_end+.01): raise ValueError('长条尾部无效或越过音频范围')
        if t<=previous[lane]+.1 or t<occupied[lane]-.1: raise ValueError(f'第 {lane+1} 轨存在重复音符或长条占轨冲突')
        occupied[lane]=min(tail,end) if tail is not None else t; previous[lane]=t
    return True

def stats(events, start, end, bpm=120, waveform=None):
    notes=as_notes(events,start,end)
    s=chart_stats(notes,(end-start)/1000) if notes else {'notes':0,'holds':0,'ln_ratio':0,'average_nps':0,'peak_nps':0,'lanes':[0]*4,'density':[]}
    s['segment_nps']=round(len(notes)/max(.001,(end-start)/1000),2)
    return s

def chart_events(chart):
    points=sorted(chart['time'],key=lambda p:float(beat_value(p['beat'])))
    def ms(value):
        target=float(beat_value(value)); total=0.
        for i,p in enumerate(points):
            start=float(beat_value(p['beat'])); nxt=float(beat_value(points[i+1]['beat'])) if i+1<len(points) else target
            if target<=start:break
            total+=(min(target,nxt)-start)*60000/p['bpm']
            if target<=nxt:break
        return total
    bgm=next((e for e in chart['note'] if e.get('type')==1 and e.get('sound')),{}); origin=-bgm.get('offset',0)
    return [{'id':uid(),'start_ms':origin+ms(e['beat']),'end_ms':origin+ms(e['endbeat']) if 'endbeat' in e else None,'lane':e['column']} for e in chart['note'] if 'column' in e]

def version_source_metadata(revision):
    provenance=revision.get('provenance',{})
    role=provenance.get('source_role') or ('fusion' if revision.get('kind')=='fusion' else 'mix')
    return {'batch_id':provenance.get('generation_batch_id') or provenance.get('batch_id') or provenance.get('cache_job') or 'revision:'+revision['id'],
            'generation_batch_id':provenance.get('generation_batch_id'), 'generation_job_id':provenance.get('generation_job_id'),
            'cache_job':provenance.get('cache_job'), 'source_role':role,
            'source_id':provenance.get('source_id') or ('original' if role=='mix' else None),
            'stem_set_id':provenance.get('stem_set_id'), 'section_plan_id':provenance.get('section_plan_id')}


class ProjectStore:
    def __init__(self, root=None):
        self.root=Path(root or ROOT/'outputs'/'advanced'); self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.assembly_locks={}

    def directory(self, pid): return self.root/identifier(pid)
    def load(self,pid):
        try:
            p=read(self.directory(pid)/'project.json')
            for segment in p['segments']:
                for versions in segment['versions'].values():
                    for version in versions:
                        if 'engine' not in version or any(key not in version for key in ('batch_id','cache_job','source_role','source_id','stem_set_id','section_plan_id','generation_batch_id','generation_job_id')):
                            r=self.revision(pid,version['id'])
                            if 'engine' not in version:version.update(engine=r['settings']['engine'],seed=r.get('provenance',{}).get('seed'))
                            for key,value in version_source_metadata(r).items():version.setdefault(key,value)
            return p
        except FileNotFoundError:raise ValueError('高级项目不存在')
    def save(self,p):
        p['revision']+=1; p['updated']=now(); atomic(self.directory(p['id'])/'project.json',p)
        return copy.deepcopy(p)
    def check(self,p,expected):
        if expected is not None and expected!=p['revision']: raise ValueError('项目已更新，请刷新后重试')
    def list(self):
        rows=[]
        for path in self.root.glob('*/project.json'):
            p=read(path);rows.append({k:p[k] for k in ('id','title','artist','updated','revision','duration')})
        return sorted(rows,key=lambda p:p['updated'],reverse=True)

    def create(self,source,title,artist='',background=None):
        if not isinstance(title,str) or not title.strip() or len(title)>120 or len(artist)>120:raise ValueError('曲名或音乐人无效')
        pid=uid(); directory=self.directory(pid);directory.mkdir()
        # Preserve the exact source bytes, without trusting the original filename.
        shutil.copyfile(source,directory/('source-original'+Path(source).suffix.lower()))
        data=decode(source,directory/'source.wav'); waveform,tempo=audio_metadata(data)
        if background and Path(background).is_file():shutil.copyfile(background,directory/'background.jpg')
        p={'schema':1,'id':pid,'revision':0,'title':title.strip(),'artist':artist or 'Unknown','created':now(),'updated':now(),
           'duration':len(data)/SR,'sample_rate':SR,'samples':len(data),'source_sha256':hashlib.sha256((directory/'source.wav').read_bytes()).hexdigest(),
           'source_pcm_sha256':hashlib.sha256(data.astype('<f4').tobytes()).hexdigest(),
           'profile':'phone','settings':defaults(),'variants':variants(['balanced'],['easy','medium','hard']),
           'tempo':tempo,'waveform':waveform,'segments':[],'assemblies':[],'reviews':[],'examples':[],'feedback':[],'background':bool(background)}
        atomic(directory/'project.json',p); return p

    def update(self,pid,payload,expected=None):
        with self.lock:
            p=self.load(pid);self.check(p,expected)
            if 'settings' in payload:p['settings']=validate_settings(payload['settings'])
            if 'profile' in payload:
                if payload['profile'] not in ('phone','keyboard'):raise ValueError('请选择手机四指或键盘 4K')
                p['profile']=payload['profile']
            if 'patterns' in payload or 'difficulties' in payload:p['variants']=variants(payload.get('patterns',list(dict.fromkeys(v['pattern'] for v in p['variants']))),payload.get('difficulties',list(dict.fromkeys(v['difficulty'] for v in p['variants']))))
            if 'tempo' in payload:
                points=payload['tempo'].get('points',[])
                if not points or len(points)>1000:raise ValueError('请填写至少一个 BPM 锚点')
                last=-1
                for a in points:
                    if not isinstance(a,list) or len(a)!=2 or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in a) or not 0<=a[0]<=p['duration']*1000 or a[0]<=last or not 20<=a[1]<=600:raise ValueError('BPM 锚点须按时间递增，范围为 20–600')
                    last=a[0]
                if points[0][0]!=0:raise ValueError('首个 BPM 锚点必须在原曲 0 ms')
                p['tempo'].update(points=copy.deepcopy(points),bpm=points[0][1])
                if 'points_metadata' in payload['tempo']:
                    metadata=payload['tempo']['points_metadata']
                    if not isinstance(metadata,list) or len(metadata)!=len(points) or any(not isinstance(row,dict) or ('confirmed' in row and not isinstance(row['confirmed'],bool)) or ('source' in row and not isinstance(row['source'],str)) for row in metadata):raise ValueError('BPM 锚点来源须逐点对应，并使用布尔确认状态')
                    p['tempo']['points_metadata']=copy.deepcopy(metadata)
                else:p['tempo']['points_metadata']=[{'source':'user_confirmed','confirmed':True} for _ in points]
                metadata=p['tempo']['points_metadata']
                sources={row.get('source') or 'unknown' for row in metadata}
                p['tempo'].update(uncertain=any(row.get('confirmed') is not True for row in metadata),
                                  manual=any(row.get('source') in ('user_confirmed','manual') for row in metadata),
                                  reference_source=next(iter(sources)) if len(sources)==1 else 'mixed')
            return self.save(p)

    def range_check(self,p,start,end,exclude=None):
        if any(isinstance(v,bool) or not isinstance(v,int) for v in (start,end)) or not 0<=start<end<=p['samples'] or end-start<math.ceil(SR*.25):raise ValueError('片段至少 250 ms，边界须为音频内的整数采样点')
        for s in p['segments']:
            if s['id']!=exclude and start<s['end_sample'] and end>s['start_sample']:raise ValueError('片段不能重叠；请先拆分已有片段')

    def add_segment(self,pid,payload,expected=None):
        with self.lock:
            p=self.load(pid);self.check(p,expected);start=payload['start_sample'];end=payload['end_sample'];self.range_check(p,start,end)
            s={'id':uid(),'name':str(payload.get('name') or f'片段 {len(p["segments"])+1}')[:80],'start_sample':start,'end_sample':end,'included':True,'overrides':{},'versions':{},'active':{}}
            p['segments'].append(s);p['segments'].sort(key=lambda s:s['start_sample']);return self.save(p)

    def segment(self,p,sid):
        return next((s for s in p['segments'] if s['id']==sid),None) or (_ for _ in ()).throw(ValueError('片段不存在'))

    def edit_segment(self,pid,sid,payload,expected=None):
        with self.lock:
            p=self.load(pid);self.check(p,expected);s=self.segment(p,sid)
            if any(k in payload for k in ('profile','patterns','difficulties','settings')):raise ValueError('片段接口只保存 overrides；玩法与组合须在项目设置中保存')
            start=payload.get('start_sample',s['start_sample']);end=payload.get('end_sample',s['end_sample']);self.range_check(p,start,end,sid)
            if [start,end]!=[s['start_sample'],s['end_sample']]:s['active']={}
            s.update(start_sample=start,end_sample=end)
            if 'name' in payload:s['name']=str(payload['name'])[:80]
            if 'included' in payload:
                if not isinstance(payload['included'],bool):raise ValueError('拼接选择无效')
                s['included']=payload['included']
            if 'overrides' in payload:validate_settings(merge(p['settings'],payload['overrides']));s['overrides']=copy.deepcopy(payload['overrides'])
            p['segments'].sort(key=lambda s:s['start_sample']);return self.save(p)

    def revision(self,pid,rid):return read(self.directory(pid)/'revisions'/(identifier(rid)+'.json'))

    def add_revision(self,pid,sid,variant,events,settings,kind,provenance=None,bounds=None,activate_initial=True,revision_id=None):
        with self.lock:
            p=self.load(pid);s=self.segment(p,sid);bounds=bounds or [s['start_sample'],s['end_sample']]
            start,end=(v*1000/SR for v in bounds); valid_events(events,start,end,p['duration']*1000)
            rid=identifier(revision_id) if revision_id else uid()
            if (self.directory(pid)/'revisions'/(rid+'.json')).exists():
                existing=self.revision(pid,rid)
                if existing['events']!=events or existing['variant']!=variant or existing['range']!=bounds:raise ValueError('版本提交冲突')
                return existing
            r={'id':rid,'created':now(),'segment_id':sid,'variant':variant,'range':bounds,'settings':copy.deepcopy(settings),'kind':kind,
                'events':copy.deepcopy(events),'stats':stats(events,start,end),'provenance':provenance or {},'review':{'status':'unreviewed','reason':''}}
            atomic(self.directory(pid)/'revisions'/(rid+'.json'),r)
            s['versions'].setdefault(variant,[]).append({'id':rid,'kind':kind,'created':r['created'],'stats':r['stats'],'range':bounds,'review':r['review'],'engine':settings['engine'],'seed':r['provenance'].get('seed'),'source_role':r['provenance'].get('source_role'),'parent_revisions':r['provenance'].get('parents',[]),**version_source_metadata(r)})
            if activate_initial and variant not in s['active'] and bounds==[s['start_sample'],s['end_sample']]:s['active'][variant]=rid
            self.save(p);return r

    def select(self,pid,sid,variant,rid,expected=None):
        with self.lock:
            p=self.load(pid);self.check(p,expected);s=self.segment(p,sid);r=self.revision(pid,rid)
            if r['segment_id']!=sid or r['variant']!=variant or r['range']!=[s['start_sample'],s['end_sample']]:raise ValueError('版本属于其他组合或片段范围已变化')
            s['active'][variant]=rid;return self.save(p)

    def feedback(self,pid,rid,status,reason):
        if status not in ('unreviewed','usable','needs_changes'):raise ValueError('评审状态无效')
        with self.lock:
            p=self.load(pid);r=self.revision(pid,rid);s=self.segment(p,r['segment_id']);review={'status':status,'reason':str(reason)[:2000]}
            # Notes/settings are immutable; review decisions live in the project manifest.
            for v in s['versions'].get(r['variant'],[]):
                if v['id']==rid:v['review']=review
            p['feedback'].append({'revision_id':rid,'created':now(),**review});return self.save(p)

    def split(self,pid,sid,cuts,expected=None):
        with self.lock:
            p=self.load(pid);self.check(p,expected);s=self.segment(p,sid)
            if not isinstance(cuts,list) or not cuts or any(isinstance(c,bool) or not isinstance(c,int) for c in cuts) or len(set(cuts))!=len(cuts):raise ValueError('拆分点无效')
            boundaries=[s['start_sample'],*sorted(cuts),s['end_sample']]
            if any(b-a<math.ceil(SR*.25) for a,b in zip(boundaries,boundaries[1:])):raise ValueError('拆分后每段至少 250 ms')
            new=[]; inherited=[]
            for i,(a,b) in enumerate(zip(boundaries,boundaries[1:])):
                child=copy.deepcopy(s);child.update(id=uid(),name=s['name']+f' · {i+1}',start_sample=a,end_sample=b,versions={},active={});new.append(child)
                for variant,rid in s['active'].items():
                    r=self.revision(pid,rid); ev=[e for e in r['events'] if a*1000/SR<=e['start_ms']<b*1000/SR]
                    inherited.append((child['id'],variant,ev,r))
            p['segments']=[x for x in p['segments'] if x['id']!=sid]+new;p['segments'].sort(key=lambda x:x['start_sample']);self.save(p)
            for child,variant,ev,r in inherited:self.add_revision(pid,child,variant,ev,r['settings'],'split',{**r.get('provenance',{}),'parent':r['id']})
            return self.load(pid)

    def local_chart(self,pid,rid):
        p=self.load(pid);r=self.revision(pid,rid);a,b=(v*1000/SR for v in r['range'])
        ev=copy.deepcopy(r['events']); changes=[]
        for e in ev:
            if e.get('end_ms') is not None and e['end_ms']>b:
                e['end_ms']=b;changes.append({'type':'clipped_hold','start_ms':e['start_ms'],'note_id':e['id']})
            if e.get('end_ms') is not None and e['end_ms']-e['start_ms']<100:e['end_ms']=None;changes.append({'type':'short_hold_to_tap','note_id':e['id']})
        notes=as_notes(ev,a,b);timing=project_timing(p,a,b)
        chart=serialize_with_timing(notes,p['title'],p['artist'],r['variant'],timing) if notes else None
        y,_=sf.read(self.directory(pid)/'source.wav',start=r['range'][0],stop=r['range'][1],dtype='float32',always_2d=True)
        target=r['settings']['difficulty_rules'][r['variant'].split('--')[1]]['rate']
        alerts=assess(notes,(b-a)/1000,target,y.mean(axis=1),SR,chart,r['variant'])
        for alert in alerts:alert['start_ms']+=a;alert['end_ms']+=a
        for gap in r.get('provenance',{}).get('active_audio_gaps',[]):
            alerts.append({'type':'model_underfilled_active_core','severity':'warning','difficulty':gap.get('difficulty',r['variant']),
                'start_ms':gap['start_ms'],'end_ms':gap['end_ms'],'metric':gap['notes'],'threshold':gap['expected_minimum'],
                'message':f"{gap['start_ms']/1000:.1f}–{gap['end_ms']/1000:.1f} 秒原曲有明显起音，但此档仅生成 {gap['notes']} 个音符（最低参考 {gap['expected_minimum']}）；建议试听并重生成此段。"})
        for excluded in r.get('provenance',{}).get('excluded_leading_holds',[]):
            alerts.append({'type':'head_outside_segment','severity':'info','start_ms':a,'end_ms':min(b,excluded['end_ms']),
                'lane':excluded['lane'],'message':'长条头位于片段之前，本段排除该长条；可调整范围或保留前一连续片段。'})
        return {'title':p['title'],'artist':p['artist'],'pattern':r['variant'].split('--')[0],'difficulty':r['variant'].split('--')[1],
                'events':ev,'chart':chart,'range':r['range'],'duration':(b-a)/1000,'stats':r['stats'],'alerts':alerts,'boundary_changes':changes,
                'audio_source_id':revision_audio_source(r),
                'audio_url':f'/api/advanced/projects/{pid}/revisions/{rid}/audio?source_id={quote(revision_audio_source(r),safe="")}', 'revision':r}

def project_timing(p,start,end):
    points=p['tempo'].get('points') or [[0,p['tempo']['bpm']]]
    active=next((bpm for t,bpm in reversed(points) if t<=start),points[0][1])
    return [(0.,active)]+[(t-start,bpm) for t,bpm in points if start<t<end]

def assemble_pcm(data,segments,preroll):
    chunks=[np.zeros((round(preroll*SR),2),dtype=np.float32)]; mapping=[];cursor=len(chunks[0])
    for i,s in enumerate(segments):
        a,b=s['start_sample'],s['end_sample'];part=data[a:b].copy();fade=min(round(.005*SR),len(part))
        if (i and segments[i-1]['end_sample']!=a) or (i==0 and a>0):part[:fade]*=np.linspace(0,1,fade,dtype=np.float32)[:,None]
        if (i+1<len(segments) and b!=segments[i+1]['start_sample']) or (i==len(segments)-1 and b<len(data)):part[-fade:]*=np.linspace(1,0,fade,dtype=np.float32)[:,None]
        mapping.append({'segment_id':s['id'],'source_start':a,'source_end':b,'output_start':cursor,'output_end':cursor+b-a})
        chunks.append(part);cursor+=b-a
    return np.concatenate(chunks),mapping

def mapped_events(revisions,mapping):
    out=[];seams=[]
    for i,(r,m) in enumerate(zip(revisions,mapping)):
        start=m['source_start']*1000/SR;end=m['source_end']*1000/SR;delta=(m['output_start']-m['source_start'])*1000/SR
        # A hold can continue only through an unbroken chain of selected source ranges.
        limit=end; j=i+1
        while j<len(mapping) and mapping[j-1]['source_end']==mapping[j]['source_start']:limit=mapping[j]['source_end']*1000/SR;j+=1
        for e in r['events']:
            if not start<=e['start_ms']<end:continue
            n=copy.deepcopy(e);n['source_id']=e['id'];n['start_ms']+=delta
            if e.get('end_ms') is not None:
                tail=min(e['end_ms'],limit)
                if tail<e['end_ms']:seams.append({'type':'clipped_hold','segment_id':m['segment_id'],'note_id':e['id'],'source_ms':tail,'output_ms':tail+delta})
                n['end_ms']=tail+delta
                if tail-e['start_ms']<100:n['end_ms']=None;seams.append({'type':'short_hold_to_tap','note_id':e['id'],'output_ms':n['start_ms']})
            out.append(n)
    out.sort(key=lambda e:(e['start_ms'],e['lane']))
    # Conflicts at a continuous seam are never silently removed.
    occupied=[-1e9]*4
    for e in out:
        if e['start_ms']<occupied[e['lane']]-.1:seams.append({'type':'seam_conflict','severity':'error','note_id':e['id'],'output_ms':e['start_ms'],'lane':e['lane']})
        occupied[e['lane']]=max(occupied[e['lane']],e.get('end_ms') or e['start_ms'])
    return out,seams

def assemble(store,pid,preroll=1.5,expected=None):
    # Serialise exports per project, without blocking edits or other projects.
    with store.lock:
        lock=store.assembly_locks.setdefault(identifier(pid),threading.RLock())
    with lock:
        return _assemble(store,pid,preroll,expected)


def _assemble(store,pid,preroll=1.5,expected=None):
    if isinstance(preroll,bool) or not isinstance(preroll,(float,int)) or not math.isfinite(preroll) or not 0<=preroll<=3:raise ValueError('准备时间须为 0–3 秒')
    preroll=float(preroll)
    with store.lock:
        p=store.load(pid);store.check(p,expected); snapshot=p['revision'];segments=sorted([s for s in p['segments'] if s['included']],key=lambda s:s['start_sample'])
        if not segments:raise ValueError('请先选择要拼接的片段')
        missing=[];complete=[]
        for v in p['variants']:
            absent=[s['name'] for s in segments if not s['active'].get(v['key'])]
            if absent:missing.append({'variant':v['key'],'segments':absent})
            else:complete.append(v)
        if not complete:raise ValueError('所选片段没有完整组合，请补齐生成或使用候选版本')
        refs={v['key']:[store.revision(pid,s['active'][v['key']]) for s in segments] for v in complete}
        identity={'format':2,'source':p['source_sha256'],'title':p['title'],'artist':p['artist'],
                  'background':p['background'],'tempo':p['tempo'],'preroll':preroll,'variants':p['variants'],
                  'segments':[[s['id'],s['start_sample'],s['end_sample'],s['active']] for s in segments]}
        fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        for old in reversed(p['assemblies']):
            folder=store.directory(pid)/'assemblies'/old['id']
            same=old.get('fingerprint')==fingerprint
            # Legacy exports can be reused only when their append was the last project mutation.
            if not old.get('fingerprint') and not old.get('stale') and old.get('project_revision')==snapshot-1:
                legacy=read(folder/'report.json') if (folder/'report.json').exists() else {}
                same=legacy.get('export_format')=='classic-ascii-v2' and legacy.get('preroll')==preroll
            if same and all((folder/name).is_file() for name in ('report.json','audio.ogg','malody-4k.mcz')):
                return {**old,'reused':True}
    data,_=sf.read(store.directory(pid)/'source.wav',dtype='float32',always_2d=True); pcm,mapping=assemble_pcm(data,segments,preroll)
    aid=uid();directory=store.directory(pid)/'assemblies'/aid;directory.mkdir(parents=True)
    sf.write(directory/'assembled.wav',pcm,SR,subtype='FLOAT')
    from .playback_audio import gain_for_peak
    gain=gain_for_peak(float(np.max(np.abs(pcm),initial=0.)))
    r=subprocess.run([ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',str(directory/'assembled.wav'),'-af',f'volume={gain:.12f}','-c:a','libvorbis','-q:a','7',str(directory/'audio.ogg')],capture_output=True,timeout=120)
    if r.returncode:raise ValueError('成品音频编码失败')
    duration=len(pcm)/SR; info=sf.info(directory/'audio.ogg')
    if info.subtype!='VORBIS' or abs(info.frames-len(pcm))>1:raise ValueError('成品音频采样数量校验失败')
    timing=[]
    for m in mapping:
        start,end=m['source_start']*1000/SR,m['source_end']*1000/SR
        for t,bpm in project_timing(p,start,end):timing.append((t+m['output_start']*1000/SR,bpm))
    timing=[(0.,timing[0][1])]+timing
    charts={};filenames={};rows=[];preview={};changes=[]
    for v in complete:
        revisions=refs[v['key']]
        if any(r['range']!=[s['start_sample'],s['end_sample']] for r,s in zip(revisions,segments)):raise ValueError('片段范围变化，当前版本已过期')
        ev,seams=mapped_events(revisions,mapping);changes.extend([{**x,'variant':v['key']} for x in seams])
        if any(x.get('severity')=='error' for x in seams):raise ValueError('连续片段长条发生占轨冲突，请在接缝附近修订后导出：'+v['key'])
        if not ev:missing.append({'variant':v['key'],'reason':'整张成品没有音符'});continue
        valid_events(ev,0,duration*1000,duration*1000)
        engines=list(dict.fromkeys(r['settings']['engine'] for r in revisions)); engine=engines[0] if len(engines)==1 else 'Mixed-Models'
        title=p['title']+'（剪辑版）';n=as_notes(ev); chart=serialize_with_timing(n,title,p['artist'],v['key'],timing)
        if p['background']:chart['meta']['background']='background.jpg'
        validate_chart(chart,duration*1000)
        # Round-trip through the serialized variable-BPM coordinates.
        restored=chart_events(chart)
        if any(abs(a['start_ms']-b['start_ms'])>=1 or (a['end_ms'] is not None and abs(a['end_ms']-b['end_ms'])>=1) for a,b in zip(ev,restored)):raise ValueError('谱面时间回读误差超过 1 ms')
        # Classic importers may ignore ZIP's Unicode filename flag. Keep paths portable;
        # the full song title and chart identity remain in UTF-8 metadata.
        charts[v['key']]=chart;filenames[v['key']]=v['key']+'.mc';preview[v['key']]=ev
        rows.append({**v,**chart_stats(n,duration),'engine':engine,'sources':[{'segment_id':s['id'],'revision_id':r['id'],'engine':r['settings']['engine'],'source_role':r.get('provenance',{}).get('source_role','mix')} for s,r in zip(segments,revisions)]})
    if not charts:raise ValueError('成品至少需要一个有效音符')
    report={'schema':1,'export_format':'classic-ascii-v2','fingerprint':fingerprint,'advanced':True,'title':p['title']+'（剪辑版）','duration':duration,'sample_rate':SR,'samples':len(pcm),'preroll':preroll,
            'mapping':mapping,'charts':rows,'missing':missing,'seam_changes':changes,'preview':preview,'project_revision':snapshot,'created':now(),
            'audio_processing':{'source':'original','gain':gain,'codec':'vorbis','quality':7,'eq':False,'denoise':False}}
    archive=package(directory,charts,directory/'audio.ogg',report,store.directory(pid)/'background.jpg' if p['background'] else None,filenames)
    result={'id':aid,'fingerprint':fingerprint,'created':report['created'],'duration':duration,'mapping':mapping,'charts':rows,'missing':missing,'seam_changes':changes,'project_revision':snapshot,'download':f'/api/advanced/projects/{pid}/assemblies/{aid}/download'}
    with store.lock:
        latest=store.load(pid); result['stale']=latest['revision']!=snapshot;latest['assemblies'].append(result);store.save(latest)
    return result


def assembly_archive(store,pid,aid):
    """Return a portable derivative of legacy exports, preserving original archives."""
    directory=store.directory(pid)/'assemblies'/identifier(aid)
    report=read(directory/'report.json')
    if report.get('export_format')=='classic-ascii-v2':return directory/'malody-4k.mcz'
    with store.lock:
        lock=store.assembly_locks.setdefault(identifier(pid),threading.RLock())
    with lock:
        destination=directory/'compatibility-v2';archive=destination/'malody-4k.mcz'
        if archive.is_file():return archive
        charts={read(path)['meta']['version']:read(path) for path in (directory/'0').glob('*.mc')}
        if set(charts)!={row['key'] for row in report['charts']}:raise ValueError('成品谱面与报告不一致，无法生成兼容曲包')
        destination.mkdir(exist_ok=True)
        fixed={**report,'export_format':'classic-ascii-v2','original_archive':directory.name}
        return package(destination,charts,directory/'audio.ogg',fixed,
                       directory/'0'/'background.jpg' if (directory/'0'/'background.jpg').is_file() else None,
                       {key:key+'.mc' for key in charts})
