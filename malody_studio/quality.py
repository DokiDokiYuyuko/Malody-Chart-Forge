from collections import Counter
import numpy as np
from .charts import beat_value


def assess(notes, duration_seconds, target_nps, waveform=None, sample_rate=None, chart=None, difficulty=''):
    """Return actionable, timestamped heuristics without changing chart notes."""
    alerts=[]
    ordered=sorted(notes,key=lambda item:(item.start,item.lane))
    duration_ms=duration_seconds*1000
    window_ms=4000
    bins=Counter(int(max(0,n.start)//window_ms) for n in ordered)
    if waveform is not None and sample_rate and len(waveform):
        data=np.asarray(waveform,dtype=np.float32)
        peak=float(np.max(np.abs(data)))
        threshold=max(peak*.025,.0005)
        frame=max(1,int(sample_rate*.5))
        active=[]
        for start in range(0,len(data),frame):
            segment=data[start:start+frame]
            active.append(float(np.sqrt(np.mean(segment.astype(np.float64)**2)))>=threshold if len(segment) else False)
        handled_groups=set()
        for index,start in enumerate(range(0,len(data),frame)):
            window_start=(start/sample_rate)*1000
            window_end=min(duration_ms,window_start+frame/sample_rate*1000)
            if window_end-window_start<250 or not active[index]:continue
            group=int(window_start//window_ms)
            if group in handled_groups:continue
            handled_groups.add(group)
            begin=group*window_ms
            end=min(duration_ms,begin+window_ms)
            frame_lo=max(0,int(begin/1000*sample_rate/frame))
            frame_hi=min(len(active),int(np.ceil(end/1000*sample_rate/frame)))
            active_frames=sum(active[frame_lo:frame_hi])
            span=max(1.,(end-begin)/1000)
            if active_frames/max(1,frame_hi-frame_lo)<.5 or span<2 or float(target_nps)<2:continue
            count=bins[group]
            if count==0:
                alerts.append({'type':'active_audio_without_notes','severity':'warning','difficulty':difficulty,
                    'start_ms':begin,'end_ms':end,'metric':round(active_frames*frame/sample_rate,2),
                    'threshold':round(span*.5,2),
                    'message':f'{begin/1000:.0f}–{end/1000:.0f} 秒检测到持续声音，但没有谱面音符；请试听确认是否漏采。'})
            else:
                # A window with one token is not meaningfully covered just
                # because it is non-empty. Keep this a review hint rather than
                # a structural error: pads and intentional rests can be valid.
                minimum=max(2.,float(target_nps)*span*.2)
                if count<minimum:
                    alerts.append({'type':'active_audio_underfilled','severity':'warning','difficulty':difficulty,
                        'start_ms':begin,'end_ms':end,'metric':round(count/span,2),'threshold':round(minimum/span,2),
                        'message':f'{begin/1000:.0f}–{end/1000:.0f} 秒持续有声，但只有 {count} 个音符，明显低于目标密度参考；请试听确认是否漏采。'})
            if sum(a['type']=='active_audio_without_notes' for a in alerts)>=10:break
    if not notes:
        return alerts
    for index,count in sorted(bins.items()):
        start=index*window_ms; end=min(duration_ms,start+window_ms)
        span=max(1,(end-start)/1000)
        local=count/span
        threshold=max(float(target_nps)*1.75,float(target_nps)+4)
        if span>=2 and local>threshold:
            alerts.append({'type':'local_density','severity':'warning','difficulty':difficulty,
                'start_ms':start,'end_ms':end,'metric':round(local,2),'threshold':round(threshold,2),
                'message':f'{start/1000:.0f}–{end/1000:.0f} 秒局部密度 {local:.1f} NPS，超过目标参考值 {threshold:.1f}。'})
    if waveform is not None and sample_rate and len(waveform):
        data=np.asarray(waveform,dtype=np.float32)
        peak=float(np.max(np.abs(data)))
        threshold=max(peak*.012,.0002)
        frame=max(1,int(sample_rate))
        quiet=[]
        for start in range(0,len(data),frame):
            segment=data[start:start+frame]
            quiet.append(float(np.sqrt(np.mean(segment*segment)))<threshold if len(segment) else False)
        i=0
        while i<len(quiet):
            if not quiet[i]: i+=1; continue
            begin=i
            while i<len(quiet) and quiet[i]: i+=1
            start=begin*1000; end=min(duration_ms,i*1000)
            if end-start<1500: continue
            notes_in=[n for n in ordered if start<=n.start<end]
            if notes_in:
                alerts.append({'type':'notes_in_silence','severity':'warning','difficulty':difficulty,
                    'start_ms':start,'end_ms':end,'metric':len(notes_in),
                    'message':f'{start/1000:.0f}–{end/1000:.0f} 秒疑似静音段内有 {len(notes_in)} 个音符，请听音频确认。'})
    if chart:
        events=[]
        for event in chart.get('note',[]):
            if 'column' in event:
                try: events.append((float(beat_value(event['beat'])),event))
                except (KeyError,TypeError,ValueError): pass
        beat_sequence=[]
        for index,(beat,event) in enumerate(events):
            if not beat_sequence or beat>beat_sequence[-1][0]+1e-7:
                beat_sequence.append((beat,ordered[min(index,len(ordered)-1)].start))
        for (before,_),(after,timestamp) in zip(beat_sequence,beat_sequence[1:]):
            delta=after-before
            if 0<delta<1/24-1e-7:
                alerts.append({'type':'fine_subdivision','severity':'info','difficulty':difficulty,
                    'start_ms':round(timestamp),'end_ms':round(timestamp),'metric':round(delta,5),
                    'threshold':round(1/24,5),'message':'发现短于 1/24 拍的音符间隔，请检查节奏是否过密。'})
                if sum(a['type']=='fine_subdivision' for a in alerts)>=5: break
    return alerts[:30]
