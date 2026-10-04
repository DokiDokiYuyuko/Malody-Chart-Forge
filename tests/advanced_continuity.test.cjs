const test=require('node:test'),assert=require('node:assert/strict');
const W=require('../web/advanced-workbench-state.js');
const parts=[0,1,2].map(i=>({id:'s'+i,start_sample:i*44100,end_sample:(i+1)*44100,active:{easy:'adopt'+i},versions:{easy:[{id:'adopt'+i},{id:'batch'+i}]}}));
const project={segments:parts};
test('continuous adopted preview uses original half-open sample boundaries',()=>{
 const rows=W.playbackChoices(project,'easy',{revision:{id:'adopt0'}},null);
 assert.deepEqual(rows.map(r=>r.revisionId),['adopt0','adopt1','adopt2']);
 assert.equal(W.playbackPart(rows,44099).segment.id,'s0');
 assert.equal(W.playbackPart(rows,44100).segment.id,'s1');
 assert.equal(W.playbackPart(rows,132300),null);
});
test('batch preview uses exact batch candidates, never older adopted fallback',()=>{
 const batch={results:[0,2].map(i=>({revision_id:'batch'+i,segment_id:'s'+i,variant:'easy',primary:true,adoptable:true,range:[i*44100,(i+1)*44100]}))};
 const rows=W.playbackChoices(project,'easy',{revision:{id:'batch0'}},batch);
 assert.deepEqual(rows.map(r=>r.revisionId),['batch0',null,'batch2']);
 batch.results[1].range=[0,1];assert.equal(W.playbackChoices(project,'easy',{revision:{id:'batch0'}},batch)[2].revisionId,null);
});
test('following raw stems keeps the chosen role and separation version',()=>{
 const batch={results:parts.flatMap((p,i)=>['vocals','accompaniment'].map(role=>({revision_id:'batch'+i+role,segment_id:p.id,variant:'easy',primary:false,adoptable:true,range:[p.start_sample,p.end_sample],source_role:role,stem_set_id:'set'})))};
 assert.deepEqual(W.playbackChoices(project,'easy',{revision:{id:'batch0vocals'}},batch).map(r=>r.revisionId),['batch0vocals','batch1vocals','batch2vocals']);
});
test('gaps do not change the source clock or fabricate a selected segment',()=>{
 const rows=W.playbackChoices({segments:[parts[0],parts[2]]},'easy',{revision:{id:'adopt0'}},null);
 assert.equal(W.playbackPart(rows,50000),null);
 assert.equal(W.playbackPart(rows,88200).segment.id,'s2');
});
