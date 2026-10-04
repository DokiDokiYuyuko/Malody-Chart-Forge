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
    return project.segments.filter(s=>s.included&&variants.some(key=>!s.active[key]&&!candidates(s,key).length)).map(s=>s.id);
  }
  function exportReadiness(project){const segments=project.segments.filter(s=>s.included);return project.variants.map(v=>({...v,
    missing:segments.filter(s=>!s.active[v.key]).map(s=>s.name)}));}
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
    return project.segments.filter(part=>scope==='all'||(scope==='current'?part.id===current:selected.has(part.id))).map(part=>part.id);
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
  const api={create,modes,candidates,missingSegments,exportReadiness,taskFor,selectionChoices,comparisonDiff,generationTargets,batchChoices,completionCanOpen,playbackChoices,playbackPart};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.AdvancedWorkbenchState=api;
})(typeof window==='object'?window:globalThis);
