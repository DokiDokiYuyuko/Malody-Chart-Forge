/* User tasks and source clocks are separate from candidate provenance. */
(function(root){
  const modes=['prepare','segment','generate','finish'];
  function create(){
    const state={project:null,mode:'prepare',segmentId:null,variant:null,selected:new Set(),draft:null,plan:null,
      preview:null,compare:null,slot:'a',parallel:false,assembly:null,source:'original',position:0,tasks:[],stems:[],sequence:0,
      generationScope:'all',resultBatch:null,adoption:new Set(),submittedBatch:null};
    const listeners=new Set();
    return {state,change(patch){Object.assign(state,patch);for(const listener of listeners)listener(state);return state;},
      subscribe(listener){listeners.add(listener);return()=>listeners.delete(listener);},
      context(){return {project:state.project?.id,segment:state.segmentId,variant:state.variant,sequence:++state.sequence};},
      matches(context){return context.project===state.project?.id&&context.segment===state.segmentId&&context.variant===state.variant&&context.sequence===state.sequence;}};
  }
  function candidates(segment,variant,{raw=false,source='original'}={}){
    return (segment?.versions?.[variant]||[]).filter(v=>v.range?.[0]===segment.start_sample&&v.range?.[1]===segment.end_sample)
      .filter(v=>raw||!['vocals','accompaniment'].includes(v.source_role)||v.source_id===source||v.id===segment.active?.[variant]).slice().reverse();
  }
  function missingSegments(project,variants=project.variants.map(v=>v.key)){
    return effectiveSegments(project).filter(s=>s.included&&variants.some(key=>!s.active[key]&&!candidates(s,key).length)).map(s=>s.id);
  }
  function contentEnd(project){return project.tail_trim?.enabled!==false?Math.min(project.samples??Infinity,project.tail_trim?.cutoff_sample??Infinity):(project.samples??Infinity);}
  function effectiveSegments(project){return project.segments.filter(part=>part.start_sample<Math.min(part.end_sample,contentEnd(project)));}
  function adoptedFor(segment,key){const id=segment.active?.[key];return (segment.versions?.[key]||[]).find(v=>v.id===id&&v.range?.[0]===segment.start_sample&&v.range?.[1]===segment.end_sample);}
  function exportReadiness(project){const segments=effectiveSegments(project).filter(s=>s.included);return project.variants.map(v=>({...v,
    missing:segments.filter(s=>!adoptedFor(s,v.key)).map(s=>s.name),
    missingIds:segments.filter(s=>!adoptedFor(s,v.key)).map(s=>s.id),ready:!!segments.length&&segments.every(s=>adoptedFor(s,v.key))}));}
  function recoveryFor(part,variant,tasks){const relevant=tasks.filter(t=>t.task_type==='generation'&&t.segment_id===part.id&&
    t.start_sample===part.start_sample&&t.end_sample===part.end_sample&&(!t.variants||t.variants.some(v=>(typeof v==='string'?v:v.key)===variant)));
    const active=relevant.find(t=>['running','queued','paused'].includes(t.status));
    if(active)return {kind:'waiting',task:active};
    const latest=relevant.sort((a,b)=>String(b.created).localeCompare(String(a.created)))[0];
    if(latest&&['failed','interrupted'].includes(latest.status))return {kind:'retry',task:latest};
    const candidate=candidates(part,variant).find(v=>v.id!==part.active?.[variant]);
    return candidate?{kind:'choose',task:latest,version:candidate}:{kind:'generate',task:latest};}

  function taskFor(segment,tasks){return tasks.filter(t=>t.task_type==='generation'&&t.segment_id===segment.id&&
    t.start_sample===segment.start_sample&&t.end_sample===segment.end_sample).sort((a,b)=>String(b.created).localeCompare(String(a.created)))[0];}
  function selectionChoices(project,ids){return project.segments.filter(s=>ids.has(s.id)).flatMap(s=>project.variants.flatMap(v=>{
    const best=candidates(s,v.key).find(r=>r.id!==s.active[v.key]);return best?[{segment_id:s.id,variant:v.key,revision_id:best.id}]:[];}));}
  function comparisonDiff(a,b){
    const signature=e=>[e.start_ms,e.lane,e.end_ms??null].join(':'),same=new Set(b.map(signature)),original=new Set(a.map(signature)),old=new Map(a.map(e=>[e.id,e])),remaining=new Set(a.filter(e=>!same.has(signature(e))));
    const changes=[];for(const e of b){if(original.has(signature(e)))continue;const before=old.get(e.id);changes.push({type:before?(before.start_ms!==e.start_ms?'move':before.lane!==e.lane?'lane':'hold'):'add',before:before||null,after:e});if(before)remaining.delete(before);}
    for(const e of remaining)changes.push({type:'delete',before:e,after:null});return changes;
  }
  function generationTargets(project,scope,current,selected){
    if(!project)return [];
    return effectiveSegments(project).filter(part=>scope==='all'||(scope==='current'?part.id===current:selected.has(part.id))).map(part=>part.id);
  }
  function batchChoices(project,batch,selected){
    return (batch?.results||[]).filter(row=>row.primary&&row.adoptable&&selected.has(row.revision_id)).filter(row=>{
      const part=project.segments.find(s=>s.id===row.segment_id);
      return part&&project.variants.some(v=>v.key===row.variant)&&row.range?.[0]===part.start_sample&&row.range?.[1]===part.end_sample;
    }).map(row=>({segment_id:row.segment_id,variant:row.variant,revision_id:row.revision_id}));
  }
  function completionCanOpen(submission,state,{visible,playing,game}={}){
    return !!submission&&!submission.delivered&&submission.autoAllowed&&visible&&!playing&&!game&&state.mode==='generate'&&
      state.project?.id===submission.projectId&&state.segmentId===submission.segmentId&&state.variant===submission.variant;
  }
  function playbackChoices(project,variant,preview,batch){
    const revision=preview?.revision,anchor=project.segments.find(p=>(p.versions?.[variant]||[]).some(r=>r.id===revision?.id));
    const batchRow=batch?.results.find(r=>r.revision_id===revision?.id);
    return project.segments.slice().sort((a,b)=>a.start_sample-b.start_sample).map(part=>{
      let id=part.active?.[variant]||null;
      if(batchRow){
        const match=batch.results.find(r=>r.segment_id===part.id&&r.variant===variant&&r.adoptable&&
          r.range?.[0]===part.start_sample&&r.range?.[1]===part.end_sample&&
          (batchRow.primary?r.primary:r.source_role===batchRow.source_role&&r.stem_set_id===batchRow.stem_set_id));
        id=match?.revision_id||null; // Never fill missing batch results with an older chart.
      }
      if(part.id===anchor?.id)id=revision.id;
      return {segment:part,revisionId:id};
    });
  }
  function playbackPart(rows,sample){return rows.find(r=>r.segment.start_sample<=sample&&sample<r.segment.end_sample)||null;}
  function allExportReady(project){const rows=exportReadiness(project);return rows.length>0&&rows.every(row=>row.ready);}
  function schemeIntent(project,settings,source,patterns,difficulties,profile){return JSON.stringify([project?.id,settings,source,patterns.slice().sort(),difficulties.slice().sort(),profile]);}
  const npsRangeDefaults={easy:{min:2,max:3},medium:{min:4,max:6},hard:{min:6.8,max:10.2},expert:{min:10.4,max:15.6},master:{min:14.8,max:22.2},lunatic:{min:20.8,max:31.2}};
  function initializeNpsRanges(settings,difficulties=Object.keys(npsRangeDefaults)){
    const round=value=>Math.round((value+Number.EPSILON)*10)/10;
    return Object.fromEntries(difficulties.map(key=>{
      const saved=settings?.nps_ranges?.[key],min=Number(saved?.min),max=Number(saved?.max);
      if(Number.isFinite(min)&&Number.isFinite(max)&&min>=.5&&max<=50&&min<=max)return[key,{min:round(min),max:round(max)}];
      const rate=Number(settings?.difficulty_rules?.[key]?.rate),fallback=npsRangeDefaults[key];
      const center=Number.isFinite(rate)&&rate>0?rate:(fallback.min+fallback.max)/2;
      return[key,{min:round(center*.8),max:round(center*1.2)}];
    }));
  }
  function intensityLabel(value){return ({calm:'舒缓',regular:'常规',dense:'密集',sparse:'舒缓',normal:'常规'})[value]|| (typeof value==='number'?(value<-.25?'舒缓':value>.25?'密集':'常规'):'常规');}
  const fusionModes=[
    {value:'vocals_priority',label:'人声为主',hint:'人声音符原样保留；伴奏里与之冲突的音符会舍弃，冲突的长条会缩短。'},
    {value:'accompaniment_priority',label:'伴奏为主',hint:'伴奏音符原样保留；人声里与之冲突的音符会舍弃，冲突的长条会缩短。'},
    {value:'relane',label:'融合（自动避让）',hint:'冲突的音符会挪到同一时刻的空轨，挪不开时按所选主次取舍。'}];
  const fusionPrimaries=[{value:'vocals',label:'人声'},{value:'accompaniment',label:'伴奏'}];
  /* New submissions default to relane with vocals primary; the server applies the same defaults. */
  function normalizeFusion(settings){
    if(!fusionModes.some(m=>m.value===settings.fusion_mode))settings.fusion_mode='relane';
    if(!fusionPrimaries.some(m=>m.value===settings.fusion_primary))settings.fusion_primary='vocals';
    return settings;
  }
  function fusionPrimaryVisible(mode){return mode==='relane';}
  function fusionModeHint(mode){return fusionModes.find(m=>m.value===mode)?.hint||'';}
  function fusionUsesStems(source){return typeof source==='string'&&(source.startsWith('dual:')||source==='vocals_accompaniment');}
  function fusionSummaryText(summary){
    if(!summary||!fusionModes.some(m=>m.value===summary.mode))return '';
    const total=key=>Object.values(summary.counts||{}).reduce((sum,row)=>sum+(Number(row?.[key])||0),0);
    const mode=fusionModes.find(m=>m.value===summary.mode).label,primary=fusionPrimaries.find(m=>m.value===summary.primary)?.label;
    const side=summary.mode==='relane'?(primary?'挪不开时保留'+primary:''):'';
    const silent=total('stem_silent'),roleName={vocals:'人声',accompaniment:'伴奏'};
    const laneGap=total('lane_gap_dropped');
    const dropNote=key=>key==='dropped'?[silent?'静音段内 '+silent:'',laneGap?'同轨间距过近 '+laneGap:''].filter(Boolean):[];
    const parts=[['relaned','挪轨'],['tail_spacing_relane','为保护长条尾挪轨'],['lane_gap_relane','为拉开同轨间距挪轨'],['dropped','舍弃'],['shortened','缩短长条'],['to_tap','长条转单点'],['aligned','对齐同一击点'],['tail_spacing','拉开长条尾间距']].map(([key,name])=>{
      if(!total(key))return '';
      const note=dropNote(key);
      return name+' '+total(key)+(note.length?'（含'+note.join('、')+'）':'');
    }).filter(Boolean);
    const residual=Number(summary.residual_same_stem_lane_gap)||0;
    if(residual)parts.push('模型自身同轨过近未改动 '+residual);
    const skipped=(summary.stem_silence_skipped||[]).map(role=>roleName[role]||role);
    if(skipped.length)parts.push('静音段规则未应用：'+skipped.join('、'));
    return '声部合并：'+[mode,side,parts.join(' · ')||'没有冲突'].filter(Boolean).join(' · ');
  }
  const api={fusionModes,fusionPrimaries,normalizeFusion,fusionPrimaryVisible,fusionModeHint,fusionUsesStems,fusionSummaryText,create,modes,contentEnd,effectiveSegments,adoptedFor,recoveryFor,candidates,missingSegments,exportReadiness,taskFor,selectionChoices,comparisonDiff,generationTargets,batchChoices,completionCanOpen,playbackChoices,playbackPart,allExportReady,schemeIntent,npsRangeDefaults,initializeNpsRanges,intensityLabel};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.AdvancedWorkbenchState=api;
})(typeof window==='object'?window:globalThis);
