const test = require('node:test');
const assert = require('node:assert/strict');
const { taskPresentation, taskErrorPresentation, taskFailures, rhythmSectionPresentation, rhythmPlanSummary, scrubPosition, requestErrorMessage, separationInputError } = require('../web/advanced-workflow.js');
const { npsRangeDefaults, initializeNpsRanges } = require('../web/advanced-workbench-state.js');

test('NPS ranges migrate once from legacy per-difficulty rates and saved ranges win',()=>{
  const difficulties=Object.keys(npsRangeDefaults);
  const legacyRates={easy:2.5,medium:5,hard:8.5,expert:13,master:18.5,lunatic:26};
  const migrated=initializeNpsRanges({difficulty_rules:Object.fromEntries(Object.entries(legacyRates).map(([key,rate])=>[key,{rate}]))},difficulties);
  assert.deepEqual(migrated,npsRangeDefaults);
  const changed=initializeNpsRanges({difficulty_rules:{hard:{rate:2}},nps_ranges:migrated},difficulties);
  assert.deepEqual(changed,npsRangeDefaults);
});

test('BPM buckets show actual density and generation conditions independently of scroll',()=>{
  const section={core:[0,441000],bpm_bucket_id:2,bpm_bucket_factor:1.2,
    tempo_confidence:{source:'beat_this_bpm_bucket',bpm:220},
    per_difficulty:{expert:{base_model_condition:5.9,model_condition:6.5,target_rate:15.6}}};
  const result=rhythmSectionPresentation(section,'balanced--expert');
  assert.equal(result.activityLabel,'BPM 桶 3');
  assert.match(result.candidateText,/220.00 BPM/);
  assert.match(result.conditionText,/5.90 → 生成条件 6.50/);
  assert.match(result.conditionText,/15.60 NPS/);
  assert.match(rhythmPlanSummary({bpm_buckets:{count:3,maximum_count:5}}),/中位桶为设定难度.*固定流速/);
});

test('separation rejects oversized windows with a useful Chinese limit before submitting', () => {
  assert.match(separationInputError('segment', '8'), /1–7\.8/);
  assert.match(separationInputError('segment', '8'), /完整歌曲/);
  assert.equal(separationInputError('segment', '7.8'), '');
  assert.equal(separationInputError('segment', '1'), '');
  assert.ok(separationInputError('segment', ''));
  assert.ok(separationInputError('segment', 'not a number'));
});

test('separation validates overlap and integer controls at the actual backend boundaries', () => {
  for (const [key, value] of [['overlap', '0'], ['overlap', '.95'], ['shifts', '20'], ['shifts', '1.5'], ['seed', '1.5'], ['seed', '2147483641']]) {
    assert.notEqual(separationInputError(key, value), '', `${key}: ${value}`);
  }
  for (const [key, value] of [['overlap', '.1'], ['overlap', '.75'], ['shifts', '0'], ['shifts', '4'], ['seed', '0'], ['seed', '2147483640']]) {
    assert.equal(separationInputError(key, value), '', `${key}: ${value}`);
  }
});

test('accepted queue and running tasks show their actual phase and progress', () => {
  const queued = taskPresentation({ status: 'queued', stage: '等待可用设备' });
  assert.equal(queued.status, '已进入队列');
  assert.equal(queued.phase, '等待可用设备');
  assert.equal(queued.progress, null);
  assert.equal(queued.active, true);
  const running = taskPresentation({ status: 'running', phase: '提取人声', progress: 37 });
  assert.equal(running.status, '正在执行');
  assert.equal(running.phase, '提取人声');
  assert.equal(running.progress, 37);
});

test('unknown progress stays indeterminate instead of claiming zero', () => {
  for (const progress of [undefined, null, '', 'unavailable']) {
    assert.equal(taskPresentation({ status: 'running', progress }).progress, null);
  }
  assert.equal(taskPresentation({ status: 'running', progress: 0 }).progress, 0);
});

test('completed and failed feedback keeps terminal status and stage', () => {
  assert.equal(taskPresentation({ status: 'completed', progress: 98 }).progress, 100);
  const failed = taskPresentation({ status: 'failed', stage: '加载模型失败', progress: 12 });
  assert.equal(failed.status, '失败');
  assert.equal(failed.phase, '加载模型失败');
  assert.equal(failed.active, false);
  assert.equal(taskPresentation({ status: 'running', progress: 150 }).progress, 100);
  assert.equal(taskPresentation({ status: 'running', progress: -5 }).progress, 0);
});

test('partial generation names failed stages and preserves completed candidates', () => {
  const task = {status: 'completed', progress: 98, advanced_errors: [
    {stage: 'accompaniment', error: 'CUDA out of memory'},
    {variant: 'balanced--expert', error: '缺少一个声部原谱，保留已完成原谱并阻止融合'},
    {pattern: 'speed', error: 'No timing points found in beatmap'}
  ]};
  assert.equal(taskPresentation(task).status, '部分完成');
  assert.equal(taskPresentation(task).active, false);
  assert.equal(taskPresentation(task).progress, 100);
  const failures = taskFailures(task);
  assert.equal(failures.length, 3);
  assert.equal(failures[0].scope, '伴奏');
  assert.match(failures[0].summary, /显存不足/);
  assert.equal(failures[1].scope, 'balanced--expert');
  assert.match(failures[1].summary, /保留已完成原谱/);
  assert.equal(failures[2].scope, 'speed');
  assert.match(failures[2].summary, /节拍点/);
  assert.equal(taskFailures({status: 'completed'}).length, 0);
  assert.equal(taskFailures({error: '生成失败'})[0].summary, '生成失败');
});

test('rhythm analysis distinguishes relative activity, used anchors and model conditions', () => {
  const tempo={points:[[0,144]],reference_source:'imported_chart',uncertain:true};
  const section={core:[0,16*44100],rhythm_activity:-.76,
    per_difficulty:{medium:{base_model_condition:3.3,model_condition:2.996,effective_boost:-.152}},
    tempo_confidence:{source:'audio_estimate',bpm:143.18,confidence:.8,margin:.25,
      alternatives:[{bpm:71.59},{bpm:143.18},{bpm:286.36}]}};
  const before=JSON.stringify({tempo,section});
  const result=rhythmSectionPresentation(section,'balanced--medium',tempo);
  assert.equal(result.startSeconds,0);assert.equal(result.endSeconds,16);
  assert.equal(result.activityText,'节奏活动：稀疏（相对量 -0.76）');
  assert.match(result.referenceText,/144 BPM \/ 待确认/);
  assert.match(result.conditionText,/3.30 → 3/);
  assert.match(result.conditionText,/密度预算调整 -15.2%/);
  assert.match(result.candidateText,/143.18 BPM.*待听音确认/);
  assert.equal(result.ambiguous,false);
  assert.equal(JSON.stringify({tempo,section}),before);
});

test('ambiguous doubled tail is not presented as a new BPM change', () => {
  const tempo={points:[[0,144]],uncertain:true};
  const tail={core:[208*44100,230.08*44100],rhythm_activity:.4,
    tempo_confidence:{source:'audio_estimate',bpm:286.36,confidence:.9,margin:.004,
      alternatives:[{bpm:71.59,support:.8},{bpm:143.18,support:.896},{bpm:286.36,support:.9}]}};
  const result=rhythmSectionPresentation(tail,'balanced--medium',tempo);
  assert.equal(result.activityLabel,'密集');
  assert.match(result.ambiguityText,/半\/双倍速歧义，待确认/);
  assert.match(result.ambiguityText,/不是新增 BPM 变化的证据/);
  const sections=[{tempo_confidence:{source:'audio_estimate',bpm:143.18,margin:.3}},
    {tempo_confidence:{source:'audio_estimate',bpm:144.91,margin:.3}},tail];
  const summary=rhythmPlanSummary({sections},tempo);
  assert.match(summary,/143.18–144.91 BPM/);
  assert.match(summary,/没有足以新增 BPM 变化的证据/);
  assert.equal(tempo.points.length,1);
  // This song's main sections also have half/double ambiguity. The nearby
  // one-times pulse remains a candidate, never a verified BPM change.
  sections[0].tempo_confidence.margin=.01948;
  sections[0].tempo_confidence.alternatives=[{bpm:71.59},{bpm:143.18},{bpm:286.36}];
  sections[1].tempo_confidence.margin=.00019;
  sections[1].tempo_confidence.alternatives=[{bpm:72.455},{bpm:144.91},{bpm:289.82}];
  assert.match(rhythmPlanSummary({sections},tempo),/143.18–144.91 BPM/);
});

test('rhythm analysis does not manufacture tempo from silence or manual references', () => {
  const silence={core:[0,44100],rhythm_activity:0,tempo_confidence:{source:'audio_estimate',bpm:null,confidence:0}};
  assert.match(rhythmSectionPresentation(silence,'balanced--easy',{}).candidateText,/证据不足/);
  assert.match(rhythmPlanSummary({sections:[silence]},{}),/没有新增可靠/);
  const manual={...silence,tempo_confidence:{source:'manual',bpm:165,variable_bpm:true}};
  const result=rhythmSectionPresentation(manual,'balanced--easy',
    {points:[[0,144],[500,180]],points_metadata:[{confirmed:true},{confirmed:true}]});
  assert.match(result.referenceText,/144 → 180 BPM/);
  assert.match(result.candidateText,/没有重新估计原曲拍速/);
  assert.match(result.candidateText,/跨过已确认 BPM 变化/);
  assert.equal(result.ambiguityText,'');
});

test('paused and interrupted work remains visible as active queue state', () => {
  assert.equal(taskPresentation({ status: 'paused' }).active, true);
  assert.equal(taskPresentation({ status: 'interrupted' }).active, true);
});

test('common failures are summarized and full diagnostics stay available', () => {
  const timing = taskErrorPresentation('No timing points found in beatmap');
  assert.match(timing.summary, /节拍点/);
  const ft = taskErrorPresentation('htdemucs_ft is not deployed');
  assert.match(ft.summary, /尚未部署/);
  const trace = taskErrorPresentation(`RuntimeError: failed\n${'x'.repeat(500)}`);
  assert.match(trace.summary, /failed/);
  assert.ok(trace.details.length > 0);
  assert.ok(trace.details.length <= 12000);
});

test('waveform dragging maps positions to the full shared audio clock', () => {
  assert.equal(scrubPosition(250, 100, 600, 180), 45);
  assert.equal(scrubPosition(400, 100, 600, 180), 90);
  assert.equal(scrubPosition(50, 100, 600, 180), 0);
  assert.equal(scrubPosition(850, 100, 600, 180), 180);
  assert.equal(scrubPosition(100, 100, 0, 180), 0);
});


test('missing backend route gives recovery instructions while project errors stay specific', () => {
  assert.match(requestErrorMessage('/projects/p/separations', 404, 'Not Found'), /重启制谱服务/);
  assert.match(requestErrorMessage('/projects/p/separations', 404, 'Not Found'), /不会丢失/);
  assert.equal(requestErrorMessage('/projects/p', 404, '项目不存在'), '项目不存在');
  assert.equal(requestErrorMessage('/projects/p/separations', 409, '模型尚未部署'), '模型尚未部署');
});

test('region cards pair tempo by time range, never by list index',()=>{
  const {regionTempo,regionTempoTitle}=require('../web/advanced-workflow.js');
  // Five user cards, ten internal regions (a historical v2 draft): index pairing would give card 3 the 341 of 81.7-85.5 s.
  const regions=[[0,100,115,1],[100,120,134,2],[120,140,115,1],[140,150,341,4],[150,200,116,1],[200,240,231,3],[240,300,114,1],[300,330,105,0]]
    .map(([a,b,bpm,id])=>({start_sample:a,end_sample:b,detected_bpm:bpm,bpm_bucket_id:id}));
  const draft={regions,segments:[]};
  assert.equal(regionTempo({start_sample:0,end_sample:100},draft).bpm,115);
  assert.equal(regionTempo({start_sample:0,end_sample:100},draft).multi,false);
  assert.equal(regionTempo({start_sample:300,end_sample:330},draft).bpm,105);
  const merged=regionTempo({start_sample:120,end_sample:200},draft);
  assert.equal(merged.multi,true);
  assert.ok(Math.abs(merged.bpm-(115*20+341*10+116*50)/80)<1e-9);
  assert.equal(regionTempo({start_sample:400,end_sample:500},draft),null);
  assert.equal(regionTempo({start_sample:0,end_sample:10},{regions:[{detected_bpm:200}]}),null);
});

test('server per-region summaries win and raw detector values stay secondary',()=>{
  const {regionTempo,regionTempoTitle}=require('../web/advanced-workflow.js');
  const draft={regions:[{start_sample:0,end_sample:100,detected_bpm:999,bpm_bucket_id:0}],
    segment_tempo:[{start_sample:0,end_sample:100,detected_bpm:114.5,observed_bpm:229,multi_tempo:false},
                   {start_sample:100,end_sample:200,detected_bpm:null,observed_bpm:null,multi_tempo:false}]};
  const tempo=regionTempo({start_sample:0,end_sample:100},draft);
  assert.equal(Math.round(tempo.bpm),115);assert.equal(tempo.multi,false);
  assert.match(regionTempoTitle(tempo),/检测原始值 229 BPM，已按整体速度折算/);
  assert.doesNotMatch(regionTempoTitle({bpm:114.5,observed:115,multi:false}),/检测原始值/);
  assert.match(regionTempoTitle({bpm:114.5,observed:null,multi:true}),/多个速度段/);
  assert.equal(regionTempo({start_sample:100,end_sample:200},draft),null);
});
