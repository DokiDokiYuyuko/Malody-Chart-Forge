const test=require('node:test');const assert=require('node:assert/strict');const T=require('../web/advanced-timeline.js');
test('millisecond label rounds carry without changing original precision',()=>{assert.equal(T.time(149.3150000243365),'02:29.315');assert.equal(T.time(59.9998),'01:00.000');});
test('new analysis displays its own nonzero clock while manual confirmation remains authoritative',()=>{
  const legacy={bpm:91,points:[[0,91]],uncertain:true},timing={source:{sample_rate:44100},tempo_points:[{sample:6048,bpm:120}],uncertain:true};
  assert.deepEqual(T.displayTempo(legacy,timing).points,[[6048*1000/44100,120]]);
  assert.equal(T.displayTempo(legacy,timing).uncertain,true);
  assert.deepEqual(T.displayTempo({...legacy,manual:true,uncertain:false},timing).points,legacy.points);
});
test('unreliable analysis does not display a legacy BPM or invent zero-time beats',()=>{
  const timing={source:{sample_rate:44100},tempo_points:[],beat_samples:[6048,28098],eligibility:{meter:false,phase:false},uncertain:true};
  assert.equal(T.displayTempo({bpm:91},timing).label,'节拍待确认');
  assert.deepEqual(T.displayTempo({bpm:91},timing).points,[]);
  assert.deepEqual(T.timingAnchors({bpm:91},timing,2).map(b=>b.time_ms),[6048*1000/44100,28098*1000/44100]);
  assert(T.timingAnchors({bpm:91},timing,2).every(b=>b.uncertain&&b.kind==='beat'));
});
test('time editor accepts minutes and millisecond precision',()=>{assert.equal(T.parseTime('00:27.318'),27.318);assert.equal(T.parseTime('2:29.315'),149.315);assert.throws(()=>T.parseTime('2:70.1'));});
test('export removes precisely the excluded source intervals',()=>{assert.deepEqual(T.removedRanges(60,[{included:true,start_sample:28*44100,end_sample:44*44100}]),[[0,28],[44,60]]);});
test('stale candidates cannot mark a changed segment as adopted',()=>{assert.equal(T.segmentStatus({included:true,start_sample:10,end_sample:30,active:{},versions:{a:[{range:[0,30]}]}},'a'),'stale');});
test('neighbor tempo changes cluster only at overview resolution',()=>{const points=[[0,150],[148817,241],[149315.0000243365,240]];assert.equal(T.clusters(points,0,180,600).length,2);assert.equal(T.clusters(points,148,150,600).length,2);});
test('fresh audio analysis has a visible zero-time BPM reference without a points array',()=>{assert.deepEqual(T.tempoPoints({bpm:120.185,beat_times:[1.1],uncertain:true}),[[0,120.185]]);assert.deepEqual(T.tempoPoints(null),[[0,120]]);});
test('adding a manual tempo change preserves uncertain automatic zero reference',()=>{const edited=T.tempoEdit({bpm:120.185,uncertain:true},[[0,120.185],[27318,240]],null);assert.deepEqual(edited.points_metadata,[{source:'audio_analysis',confirmed:false},{source:'user_confirmed',confirmed:true}]);assert.equal(edited.uncertain,true);assert.equal(edited.reference_source,'mixed');});
test('deleting a tempo marker keeps the provenance and precision of retained markers',()=>{const source={points:[[0,120.185],[27318,240],[149315.0000243365,241]],uncertain:true,points_metadata:[{source:'audio_analysis',confirmed:false},{source:'user_confirmed',confirmed:true},{source:'imported_chart',confirmed:false}]};const edited=T.tempoEdit(source,[source.points[0],source.points[2]],1);assert.equal(edited.points[1][0],149315.0000243365);assert.deepEqual(edited.points_metadata,[source.points_metadata[0],source.points_metadata[2]]);assert.equal(edited.uncertain,true);});
test('visible range values are parsed on submission even before blur',()=>{assert.deepEqual(T.parseRange('00:28.000','00:44.000',[0,0],60),[28,44]);assert.throws(()=>T.parseRange('00:28.000','00:28.100',[0,0],60),/250/);assert.throws(()=>T.parseRange('invalid','00:44.000',[0,0],60),/分:秒/);});
test('unchanged millisecond display does not quantize stored sample boundaries',()=>{const original=[1234567/44100,1940417/44100];assert.deepEqual(T.parseRange(T.time(original[0]),T.time(original[1]),original,60),original);});
test('a queued snapshot for an older range does not claim the newly edited range',()=>{const segment={id:'s',included:true,start_sample:20,end_sample:60,active:{},versions:{a:[{range:[10,50]}]}};assert.equal(T.segmentStatus(segment,'a',[{segment_id:'s',status:'queued',start_sample:10,end_sample:50}]),'stale');assert.equal(T.segmentStatus(segment,'a',[{segment_id:'s',status:'queued',start_sample:20,end_sample:60}]),'queued');});
