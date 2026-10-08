/* Task windows keep transport, selection and immutable revisions in the studio. */
(() => {
  const separationFields=[
    ['segment','处理窗口（秒）',7.8,1,7.8,.1,'1–7.8 秒；标准与 FT 的模型窗口上限均为 7.8 秒。完整歌曲会逐窗全部处理。'],
    ['overlap','重叠比例',.25,.1,.75,.05,'0.1–0.75（10%–75%）；重叠越多，处理越慢。'],
    ['shifts','随机平移次数',1,0,4,1,'0–4 的整数；次数越多，处理越慢。'],
    ['seed','分离种子',20261003,0,2147483640,1,'0–2147483640 的整数；相同种子可复现分离设置。']
  ];
  function separationInputError(key,raw){const field=separationFields.find(row=>row[0]===key);if(!field)return '未知的分离参数。';const [,title,,min,max]=field,value=typeof raw==='number'?raw:typeof raw==='string'&&raw.trim()!==''?Number(raw):NaN;const integer=key==='shifts'||key==='seed';if(!Number.isFinite(value)||value<min||value>max||(integer&&!Number.isInteger(value)))return `${title}须为 ${min}–${max}${integer?' 的整数':''}。${key==='segment'?'模型上限为 7.8 秒，完整歌曲仍会全部处理。':''}`;return '';}
    function taskPresentation(task){const labels={queued:'已进入队列',running:'正在执行',paused:'等待恢复',completed:'已完成',failed:'失败',interrupted:'已中断',cancelled:'已取消',submitting:'正在提交'};const value=Number(task.progress);return {status:task.status==='completed'&&task.advanced_errors?.length?'部分完成':labels[task.status]||'状态待更新',phase:task.stage||task.phase||task.message||'等待任务阶段更新',progress:task.status==='completed'?100:task.progress!=null&&task.progress!==''&&Number.isFinite(value)?Math.max(0,Math.min(100,value)):null,active:['queued','running','paused','interrupted','submitting'].includes(task.status)};}
    function taskErrorPresentation(error){const raw=String(error||'').trim();if(!raw)return null;const flat=raw.replace(/\s+/g,' ');let summary;if(/No timing points found in beatmap/i.test(raw))summary='谱面输入没有可用的节拍点。请重试；高级任务现在会自动带入项目节拍参考。';else if(/htdemucs_ft.*not deployed|尚未部署.*htdemucs_ft/i.test(raw))summary='任务提交时 Demucs FT 权重尚未部署。部署后可重试；重试时会重新检查模型。';else if(/CUDA out of memory/i.test(raw))summary='显存不足，当前设置未能完成。请降低并发或选择较粗的片段粒度后重试。';else{const match=[...raw.matchAll(/(?:AssertionError|RuntimeError|ValueError|FileNotFoundError|ImportError):\s*([^\r\n]+)/g)].pop();summary=match?.[1]?.trim()||flat;if(summary.length>220)summary=summary.slice(0,217)+'…';}return {summary,details:raw.length>280?raw.slice(-12000):''};}
  function scrubPosition(clientX,left,width,duration){return width>0?Math.max(0,Math.min(duration,(clientX-left)/width*duration)):0;}
  function taskFailures(task){const rows=[];if(task.error)rows.push({scope:'任务',...taskErrorPresentation(task.error)});for(const failure of task.advanced_errors||[]){const parts=[({vocals:'人声',accompaniment:'伴奏',fusion:'融合'})[failure.stage]||failure.stage,failure.pattern,failure.variant].filter(Boolean);rows.push({scope:parts.join(' / ')||'未完成的组合',...(taskErrorPresentation(failure.error)||{summary:'没有返回可用结果',details:''})});}return rows;}
  function rhythmSectionPresentation(section,variant,tempo={}){
    if(section.tempo_confidence?.source==='beat_this_bpm_bucket'){
      const key=String(variant||'').split('--').at(-1),detail=section.per_difficulty?.[key],bpm=section.tempo_confidence.bpm,id=section.bpm_bucket_id;
      return {startSeconds:section.core[0]/44100,endSeconds:section.core[1]/44100,
        activityLabel:id==null?'拍点不足':'BPM 桶 '+(id+1),activityText:'密度倍率 '+Number(section.bpm_bucket_factor??1).toFixed(2),
        candidateText:Number.isFinite(bpm)?'Beat This · '+bpm.toFixed(2)+' BPM':'本段拍点不足，沿用基础难度',
        referenceText:'固定流速',conditionText:detail?'基础条件 '+detail.base_model_condition.toFixed(2)+' → 生成条件 '+detail.model_condition.toFixed(2)+' · 目标 '+detail.target_rate.toFixed(2)+' NPS':'请先选择组合',ambiguityText:''};
    }
    const core=section.core||[0,0],start=core[0]*1000/44100,end=core[1]*1000/44100,activity=Number(section.rhythm_activity)||0,key=String(variant||'').split('--').at(-1),detail=section.per_difficulty?.[key],evidence=section.tempo_confidence||{},points=tempo.points?.length?tempo.points:[[0,tempo.bpm]],covered=points.map((point,index)=>({point,index})).filter(({point,index})=>point[0]<end&&(points[index+1]?.[0]??Infinity)>start),bpms=[...new Set(covered.map(({point})=>point[1]).filter(Number.isFinite))],unconfirmed=covered.some(({index})=>tempo.points_metadata?.[index]?tempo.points_metadata[index].confirmed!==true:!!tempo.uncertain);
    const label=activity<-.25?'稀疏':activity>.25?'密集':'普通',number=value=>Number(value).toFixed(2).replace(/\.00$/,''),boost=Number(detail?.effective_boost)||0,alternatives=(evidence.alternatives||[]).filter(item=>Number.isFinite(item.bpm)),ambiguous=evidence.source==='audio_estimate'&&alternatives.length>1&&Number(evidence.margin)<.2;
    let candidate=evidence.source==='manual'?'本段沿用已确认节拍参考；没有重新估计原曲拍速':Number.isFinite(evidence.bpm)?'原曲拍速候选 '+number(evidence.bpm)+' BPM / 音频估计，待听音确认':'原曲拍速候选：证据不足，待确认';
    if(evidence.variable_bpm)candidate+='（本段跨过已确认 BPM 变化）';
    const ambiguity=ambiguous?'半/双倍速歧义，待确认：'+alternatives.map(item=>number(item.bpm)).join(' / ')+' BPM。这不是新增 BPM 变化的证据。':evidence.source==='audio_estimate'?'候选置信度 '+Math.round((Number(evidence.confidence)||0)*100)+'%；仍需结合听音确认。':'';
    return {startSeconds:start/1000,endSeconds:end/1000,activityLabel:label,activityText:'节奏活动：'+label+'（相对量 '+(activity>0?'+':'')+activity.toFixed(2)+'）',referenceText:'当前使用节拍参考：'+(bpms.length?bpms.map(number).join(' → ')+' BPM':'尚无有效 BPM')+(unconfirmed?' / 待确认':''),conditionText:detail?'当前组合 '+key+' · 模型条件 '+number(detail.base_model_condition)+' → '+number(detail.model_condition)+' · 密度预算调整 '+(boost>0?'+':'')+(boost*100).toFixed(1)+'%':'当前组合没有模型条件记录',candidateText:candidate,ambiguityText:ambiguity,ambiguous};
  }
  function rhythmPlanSummary(plan,tempo={}){
    if(plan.bpm_buckets){const p=plan.bpm_buckets;return p.count?'Beat This · '+p.count+' 个 BPM 桶（最多 '+p.maximum_count+' 个）· '+(p.version==='bpm-buckets-v3'?'占时最长的桶':'中位桶')+'为设定难度 · 固定流速':'Beat This 拍点不足，难度暂不浮动 · 固定流速';}
    const estimates=(plan.sections||[]).map(section=>section.tempo_confidence).filter(e=>e?.source==='audio_estimate'&&Number.isFinite(e.bpm)),points=tempo.points?.length?tempo.points:[[0,tempo.bpm]],base=points[0]?.[1],single=points.every(point=>point[1]===base),close=value=>Number.isFinite(base)&&Math.abs(value/base-1)<=.05,consistent=single&&estimates.length>0&&estimates.every(e=>close(e.bpm)||((e.alternatives||[]).length>1&&Number(e.margin)<.2&&(e.alternatives||[]).some(item=>close(item.bpm)))),nearCandidates=estimates.map(e=>close(e.bpm)?e.bpm:(e.alternatives||[]).map(item=>item.bpm).filter(close).sort((a,c)=>Math.abs(a-base)-Math.abs(c-base))[0]).filter(Number.isFinite);
    const range=nearCandidates.length?Math.min(...nearCandidates).toFixed(2)+'–'+Math.max(...nearCandidates).toFixed(2)+' BPM':'';
    return consistent?'逐段候选与当前 '+base+' BPM 参考接近'+(range?'（'+range+'）':'')+'；半/双倍速歧义不能证明歌曲变速。当前分析没有足以新增 BPM 变化的证据。':!estimates.length?'本次没有新增可靠的原曲拍速候选；请结合节拍参考和听音确认。':'原曲逐段拍速是待确认候选；候选之间的差异和半/双倍速歧义不能直接当作 BPM 变化。';
  }
  function requestErrorMessage(path,status,detail){if(status===404&&detail==='Not Found')return '后台尚未加载这个功能，请重启制谱服务后刷新页面；原曲和项目不会丢失。';return typeof detail==='string'?detail:'操作失败，请检查输入';}
  // Region cards are paired with tempo facts by time range, never by list index.
  function regionTempo(row,draft){
    const a=row.start_sample,b=row.end_sample,positive=value=>Number.isFinite(value)&&value>0;
    const exact=(draft.segment_tempo||[]).find(item=>item.start_sample===a&&item.end_sample===b);
    if(exact&&positive(exact.detected_bpm))return {bpm:exact.detected_bpm,observed:positive(exact.observed_bpm)?exact.observed_bpm:null,multi:!!exact.multi_tempo};
    let total=0,sum=0;const kinds=new Set();
    for(const region of draft.regions||[]){
      if(!Number.isFinite(region.start_sample)||!Number.isFinite(region.end_sample)||!positive(region.detected_bpm))continue;
      const overlap=Math.min(b,region.end_sample)-Math.max(a,region.start_sample);if(overlap<=0)continue;
      total+=overlap;sum+=region.detected_bpm*overlap;kinds.add(region.bpm_bucket_id??Math.round(region.detected_bpm));
    }
    return total?{bpm:sum/total,observed:null,multi:kinds.size>1}:null;
  }
  function regionTempoTitle(tempo){
    let text=tempo.multi?'此区域含多个速度段，显示按时长加权的折算拍速':'此区域的估算拍速';
    if(tempo.observed&&Math.abs(Math.log(tempo.observed/tempo.bpm))>.06)text+='；检测原始值 '+Math.round(tempo.observed)+' BPM，已按整体速度折算';
    return text;
  }
  const helpers={regionTempo,regionTempoTitle,taskPresentation,taskErrorPresentation,taskFailures,rhythmSectionPresentation,rhythmPlanSummary,scrubPosition,requestErrorMessage,separationFields,separationInputError};if(typeof module!=='undefined')module.exports=helpers;if(typeof window==='undefined')return;
  window.AdvancedWorkflow=helpers;
})();
