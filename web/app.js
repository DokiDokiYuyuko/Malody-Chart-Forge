const $ = id => document.getElementById(id);
let selectedFile = null, useReference = false, activeJob = null, report = null, difficulty = null, activePattern = null, timer = null, ready = false;
let importedTrack = null, pendingImport = null, musicTimer = null, searchNumber = 0;
let historyPage = 1, historyQuery = '', historyRequest = 0, musicItems = [], musicCursor = 0, musicHasMore = false, musicSearchQuery = '', musicSelection = new Set(), musicSearchRange = [5,600], queueTimer = null, queueRequest = 0;
let healthState = null, previewSpeed = 1, previewNoteCache = null;
const emptyPreviewNotes = [];
function showView(view) {
  if (view !== 'queue') { clearTimeout(queueTimer); ++queueRequest; window.TaskHistory?.leave(); }
  if ($('advanced-view')) $('advanced-view').hidden = view !== 'advanced';
  document.body.classList.toggle('advanced-open', view === 'advanced');
  if (view !== 'advanced') window.AdvancedStudio?.leave();
  $('studio-view').hidden = view !== 'studio'; $('library-view').hidden = view !== 'library'; $('queue-view').hidden = view !== 'queue';
  for (const name of ['studio','advanced','library','queue']) { if (!$('nav-'+name)) continue; $('nav-'+name).classList.toggle('active', name === view); $('nav-'+name).setAttribute('aria-pressed', String(name === view)); }
  if (view === 'advanced') { audio.pause(); window.AdvancedStudio?.enter(); return; }
  if (view === 'library') { history(); loadStorage(); } else if (view === 'queue') { window.TaskHistory?.enter(); loadQueue(); } else { drawChart(); }
}
function showStep(step) {
  document.body.classList.remove('has-job');
  $('music-panel').hidden = step !== 'music'; $('settings-panel').hidden = step !== 'settings';
  for (const name of ['music','settings']) { $('step-'+name).classList.toggle('active', name === step); $('step-'+name).setAttribute('aria-pressed', String(name === step)); }
  document.querySelector('.control-scroll').scrollTop = 0;
}
function setCover(url) {
  $('song-cover').hidden = !url; $('cover-placeholder').hidden = !!url;
  if (url) $('song-cover').src = url; else $('song-cover').removeAttribute('src');
}
$('song-cover').onerror = () => setCover(null);
$('nav-studio').onclick = () => showView('studio'); $('nav-library').onclick = () => showView('library');
$('nav-queue').onclick = () => showView('queue');
$('step-music').onclick = () => showStep('music'); $('step-settings').onclick = () => showStep('settings');
$('next-settings').onclick = () => showStep('settings');
const mobileEdit = document.createElement('button'); mobileEdit.className = 'mobile-edit'; mobileEdit.textContent = '调整音乐与难度';
mobileEdit.onclick = () => { showStep('music'); window.scrollTo({top:0}); };
document.querySelector('.workspace').prepend(mobileEdit);
$('history-prev').onclick = () => { historyPage--; history(); };
$('history-next').onclick = () => { historyPage++; history(); };
$('history-search-form').onsubmit = e => { e.preventDefault(); historyQuery = $('history-query').value.trim(); historyPage = 1; history(); };
for (const id of ['history-engine','history-status','history-difficulty','history-sort']) $(id).onchange = () => { historyPage = 1; history(); };
$('history-reset').onclick = () => { $('history-query').value = ''; historyQuery = ''; for (const id of ['history-engine','history-status','history-difficulty']) $(id).value = ''; $('history-sort').value = 'newest'; historyPage = 1; history(); };
for (const id of ['engine-state','engine-state-mobile']) $(id).onclick = () => { const panel = $('engine-diagnostics'); panel.hidden = !panel.hidden; for (const button of [$('engine-state'),$('engine-state-mobile')]) button.setAttribute('aria-expanded', String(!panel.hidden)); };
$('mode-recommended').onclick = () => setSettingsMode(false);
$('mode-custom').onclick = () => setSettingsMode(true);
$('reset-settings').onclick = () => { const currentEngine=$('engine').value; applyPreferences(preferenceDefaults); if ($('engine').selectedOptions[0]?.disabled) $('engine').value=currentEngine; syncSettingsDisplays(); previewSpeed=Number($('preview-speed').value); syncPreviewSpeedControl(); setSettingsMode(false,false); updateDifficultyRuleVisibility(); updateEngine(); syncPatternSelection(false); persistPreferences(); drawChart(); };
const audio = $('audio');
const failurePanel=document.createElement('section');failurePanel.id='failure-quality';failurePanel.className='failure-quality';failurePanel.hidden=true;$('job-error').after(failurePanel);
let mascotFrame = -1;
const mascotSprite = new Image();
// Local bounds of the eight poses, measured in their 4 × 2 sprite cells.
const mascotBounds = [[10,148,309,599],[8,150,307,592],[6,149,299,599],[1,149,301,602],[10,51,314,503],[0,59,310,477],[7,62,304,510],[3,55,304,509]];
mascotSprite.onload = () => { const frame = Math.max(0, mascotFrame); mascotFrame = -1; setMascotFrame(frame); };
mascotSprite.src = '/static/assets/miku-run-sprites.png';
function setMascotFrame(frame) {
  const mascot = $('seek-mascot');
  if (!mascot || !mascot.clientWidth || mascotFrame === frame) return;
  mascotFrame = frame;
  if (!mascotSprite.complete || !mascotSprite.naturalWidth) return;
  const column = frame % 4, row = Math.floor(frame / 4);
  const cellWidth = mascotSprite.naturalWidth / 4, cellHeight = mascotSprite.naturalHeight / 2;
  const [left,top,boundRight,bottom] = mascotBounds[frame], right = Math.min(boundRight,cellWidth);
  // Copy only this pose, so baseline alignment can never reveal a neighbouring sprite row.
  const [ctx,width,height] = canvasSize(mascot);
  const sx = width / cellWidth, sy = height / cellHeight;
  ctx.clearRect(0,0,width,height);
  ctx.drawImage(mascotSprite,column*cellWidth+left,row*cellHeight+top,right-left,bottom-top,
    left*sx,(top+602-bottom)*sy,(right-left)*sx,(bottom-top)*sy);
}
function syncPreviewToggle() {
  const playing = !audio.paused && !audio.ended;
  const button = $('preview-toggle');
  button.classList.toggle('is-playing', playing);
  button.setAttribute('aria-pressed', String(playing));
  button.setAttribute('aria-label', playing ? '暂停音乐' : '播放音乐');
  button.title = playing ? '暂停音乐' : '播放音乐';
  $('play-icon').toggleAttribute('hidden', playing);
  $('pause-icon').toggleAttribute('hidden', !playing);
  $('playfield').classList.toggle('is-playing', playing);
  if (!playing) setMascotFrame(0);
}
async function togglePreviewPlayback() {
  if (!report || !audio.src) return;
  if (audio.paused || audio.ended) {
    if (audio.ended) audio.currentTime = 0;
    try { const lease=window.MusicPlayback?.claim(audio,'studio-preview');if(lease)await window.MusicPlayback.play(lease);else await audio.play(); } catch { $('preview-toggle').title = '音乐暂时无法播放'; }
  } else audio.pause();
}
const statusLabels = {queued:'等待生成', running:'生成中', paused:'已暂停', interrupted:'生成中断', needs_source:'需重新导入', cancelled:'已取消', completed:'已完成', failed:'失败'};
const patternCatalog = [
  ['balanced','Balanced','均衡'],['jackspeed','Jack','单轨连键'],['stream','Stream','跨轨连打'],['speed','Speed','快速交替'],
  ['jumpstream','Jumpstream','双押连打'],['handstream','Handstream','多押连打'],['chordjack','Chordjack','和弦连键'],['stamina','Stamina','耐力'],['technical','Technical','技巧']
];
function selectedPatterns() { return [...document.querySelectorAll('input[name="pattern-choice"]:checked')].map(input=>input.value); }
function renderPatternOptions() {
  const host=$('pattern-options');
  host.replaceChildren(...patternCatalog.map(([key,label,description])=>{
    const labelNode=document.createElement('label');labelNode.dataset.pattern=key;
    const input=document.createElement('input');input.type='checkbox';input.name='pattern-choice';input.value=key;input.checked=key==='balanced';
    const title=document.createElement('strong');title.textContent=label;
    const detail=document.createElement('small');detail.textContent=description;
    labelNode.append(input,title,detail);return labelNode;
  }));
}
renderPatternOptions();
const preferenceIds = ['ln-ratio','pattern','pattern-strength','engine','steps','mug-difficulty','mug-style','mug-guidance','mug-eta','v32-difficulty','v32-temperature','v32-top-p','v32-column-temperature','v32-cfg-scale','v32-year','v32-descriptors','v32-negative-descriptors','bpm','seed','preview-speed'];
function readPreferences() { return {version:1, mode:$('mode-custom')?.getAttribute('aria-pressed') === 'true', values:Object.fromEntries(preferenceIds.map(id => [id,$(id)?.value]).filter(([,value]) => value !== undefined)), patterns:selectedPatterns(), difficulties:[...document.querySelectorAll('input[name="difficulty"]:checked')].map(input => input.value), rules:Object.fromEntries([...document.querySelectorAll('[data-difficulty][data-rule]')].map(input => [`${input.dataset.difficulty}.${input.dataset.rule}`,input.value]))}; }
const preferenceDefaults = readPreferences();
function applyPreferences(values) { if (!values || values.version !== 1) return; for (const [id,value] of Object.entries(values.values || {})) if ($(id) && value !== undefined) $(id).value = id==='preview-speed'?window.PlayfieldRenderer.speedValue(value):value; const patterns=Array.isArray(values.patterns)?values.patterns:(values.values?.pattern?[values.values.pattern]:['balanced']); for (const input of document.querySelectorAll('input[name="pattern-choice"]')) input.checked=patterns.includes(input.value); if (Array.isArray(values.difficulties)) for (const input of document.querySelectorAll('input[name="difficulty"]')) input.checked = values.difficulties.includes(input.value); for (const input of document.querySelectorAll('[data-difficulty][data-rule]')) { const value=values.rules?.[`${input.dataset.difficulty}.${input.dataset.rule}`]; if (value !== undefined) input.value=value; } }
function persistPreferences() { try { localStorage.setItem('malody-chart-forge.preferences.v1', JSON.stringify(readPreferences())); } catch {} }
function setSettingsMode(custom, save=true) { $('settings-panel').classList.toggle('recommended-mode', !custom); $('mode-recommended').classList.toggle('selected', !custom); $('mode-custom').classList.toggle('selected', custom); $('mode-recommended').setAttribute('aria-pressed', String(!custom)); $('mode-custom').setAttribute('aria-pressed', String(custom)); for (const detail of document.querySelectorAll('#settings-panel details.advanced')) detail.open = custom; if (save) persistPreferences(); }
try { const saved=JSON.parse(localStorage.getItem('malody-chart-forge.preferences.v1') || 'null'); applyPreferences(saved); setSettingsMode(Boolean(saved?.mode), false); } catch { setSettingsMode(false, false); }
function syncPatternSelection(save=true) {
  const patterns=selectedPatterns();
  $('pattern').value=patterns[0]||'balanced';
  const difficulties=[...document.querySelectorAll('input[name="difficulty"]:checked')];
  const count=patterns.length*difficulties.length;
  $('combination-summary').textContent=patterns.length?`${$('engine').selectedOptions[0]?.textContent.split(' · ')[0]||'模型'} · ${patterns.length} 种排键 × ${difficulties.length} 档难度 = ${count} 张谱面`:'请至少选择一种排键。';
  $('generate').disabled=!ready||Boolean(pendingImport)||!patterns.length||!difficulties.length;
  if(save)persistPreferences();
}
document.querySelectorAll('input[name="pattern-choice"]').forEach(input=>input.addEventListener('change',()=>syncPatternSelection()));
document.querySelectorAll('input[name="difficulty"]').forEach(input=>input.addEventListener('change',()=>syncPatternSelection()));
syncPatternSelection(false);
function syncSettingsDisplays() {
  $('ln-value').textContent=Math.round(Number($('ln-ratio').value)*100)+'%';
  $('pattern-strength-value').textContent=$('pattern-strength').value;
  $('preview-speed-value').innerHTML=Number($('preview-speed').value).toFixed(1)+'<small>×</small>';
  for (const input of document.querySelectorAll('[data-rule="rate"]')) {
    const target=document.querySelector(`input[name="difficulty"][value="${input.dataset.difficulty}"]`)?.closest('label')?.querySelector('em');
    if (target) target.textContent=Number(input.value).toFixed(1).replace(/\.0$/,'')+' NPS';
  }
  for (const [id,digits] of [['mug-guidance',1],['mug-eta',2],['v32-temperature',2],['v32-top-p',2],['v32-column-temperature',2],['v32-cfg-scale',2]]) $(id+'-value').textContent=Number($(id).value).toFixed(digits);
}
syncSettingsDisplays(); previewSpeed=Number($('preview-speed').value);
$('preview-speed-value').innerHTML=previewSpeed.toFixed(1)+'<small>×</small>';
function syncPreviewSpeedControl() {
  const slider=$('preview-speed'), value=Number(slider.value), percent=(value-Number(slider.min))/(Number(slider.max)-Number(slider.min))*100;
  slider.style.setProperty('--speed-progress',`${percent}%`);
  $('preview-speed-value').innerHTML=value.toFixed(1)+'<small>×</small>';
  for(const button of document.querySelectorAll('.preview-speed-presets button')) button.setAttribute('aria-pressed',String(Math.abs(Number(button.dataset.speed)-value)<0.051));
}
syncPreviewSpeedControl();
function syncSeekControl() {
  const track=$('seek-track'), marker=$('seek-mascot'), input=$('seek');
  if(!track||!marker||!input||track.clientWidth<=0)return;
  const inset=marker.offsetWidth/2, usable=Math.max(0,track.clientWidth-inset*2), ratio=Math.min(1,Math.max(0,Number(input.value)/(Number(input.max)||1)));
  const offset=inset+usable*ratio;
  marker.style.left=`${offset}px`; $('seek-fill').style.width=`${usable*ratio}px`;
  input.style.left='0px'; input.style.width=`${track.clientWidth}px`;
}
for (const input of document.querySelectorAll('#settings-panel input:not([type="file"]), #settings-panel select')) { input.addEventListener('input', persistPreferences); input.addEventListener('change', persistPreferences); }
for (const input of document.querySelectorAll('input[name="difficulty"]')) {
  input.closest('label').dataset.key = input.value;
  input.addEventListener('change', () => { updateDifficultyRuleVisibility(); persistPreferences(); });
}
function updateDifficultyRuleVisibility() { for (const row of document.querySelectorAll('[data-rule-difficulty]')) row.hidden = !document.querySelector(`input[name="difficulty"][value="${row.dataset.ruleDifficulty}"]`)?.checked; }
updateDifficultyRuleVisibility();
function showError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
function fileSelected(file) {
  if (!file) return;
  clearMusicSelection();
  selectedFile = file; useReference = false;
  $('file-label').textContent = file.name;
  $('file-size').textContent = (file.size / 1048576).toFixed(1) + ' MB';
  $('title').value = file.name.replace(/\.[^.]+$/, '');
  $('song-title').textContent = $('title').value; $('song-artist').textContent = '已选择本地音乐'; setCover(null);
  showError('form-error', '');
}
$('file').addEventListener('change', e => fileSelected(e.target.files[0]));
['dragenter','dragover'].forEach(name => $('dropzone').addEventListener(name, e => { e.preventDefault(); $('dropzone').classList.add('dragging'); }));
['dragleave','drop'].forEach(name => $('dropzone').addEventListener(name, e => { e.preventDefault(); $('dropzone').classList.remove('dragging'); }));
$('dropzone').addEventListener('drop', e => fileSelected(e.dataTransfer.files[0]));
$('ln-ratio').addEventListener('input', () => $('ln-value').textContent = Math.round($('ln-ratio').value * 100) + '%');
$('pattern-strength').addEventListener('input', () => $('pattern-strength-value').textContent = $('pattern-strength').value);
for (const input of document.querySelectorAll('[data-rule="rate"]')) {
  input.addEventListener('input', () => {
    const target = document.querySelector(`input[name="difficulty"][value="${input.dataset.difficulty}"]`)
      ?.closest('label')?.querySelector('em');
    if (target) target.textContent = Number(input.value).toFixed(1).replace(/\.0$/, '') + ' NPS';
  });
}
for (const [id, digits] of [['mug-guidance',1],['mug-eta',2],['v32-temperature',2],['v32-top-p',2],['v32-column-temperature',2],['v32-cfg-scale',2]]) {
  $(id).addEventListener('input', () => $(id + '-value').textContent = Number($(id).value).toFixed(digits));
}
$('reference').onclick = () => {
  clearMusicSelection();
  useReference = true; selectedFile = null; $('file').value = '';
  $('file-label').textContent = '参考曲已选择'; $('file-size').textContent = '';
  $('title').value = 'シリウスの心臓（天狼星的心脏）'; $('artist').value = 'ヰ世界情緒';
  $('song-title').textContent = $('title').value; $('song-artist').textContent = $('artist').value;
  setCover('https://i.ytimg.com/vi/UKZt1vq8bKI/hqdefault.jpg'); showStep('settings');
};
async function request(url, options) {
  const response = await fetch(url, options);
  let data; try { data = await response.json(); } catch { throw new Error('服务返回了无效响应'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入后重试');
  return data;
}
function buildGenerationFormData(includeSource=false) {
  const data = new FormData();
  for (const id of ['title','artist','steps','seed','engine','pattern','pattern-strength',
    'mug-difficulty','mug-style','mug-guidance','mug-eta','v32-difficulty','v32-temperature',
    'v32-top-p','v32-column-temperature','v32-cfg-scale','v32-year','v32-descriptors',
    'v32-negative-descriptors']) data.append(id.replaceAll('-', '_'), $(id).value);
  const rules = {};
  for (const input of document.querySelectorAll('[data-difficulty][data-rule]')) {
    const key = input.dataset.difficulty;
    (rules[key] ||= {})[input.dataset.rule] = Number(input.value);
  }
  data.append('dynamic_enabled',String($('dynamic-enabled').checked));data.append('difficulty_rules', JSON.stringify(rules));
  data.append('patterns', JSON.stringify(selectedPatterns()));
  data.append('difficulties', JSON.stringify([...document.querySelectorAll('input[name=difficulty]:checked')].map(x => x.value)));
  data.append('ln_ratio', $('ln-ratio').value);
  if (!$('bpm').disabled && $('bpm').value) data.append('bpm', $('bpm').value);
  if (includeSource && $('artwork-url').value && !useReference && !importedTrack) data.append('artwork_url', $('artwork-url').value.trim());
  if (includeSource && !useReference && !importedTrack && selectedFile) data.append('file', selectedFile);
  return data;
}
function generationSettingsSnapshot() {
  const snapshot = Object.fromEntries(buildGenerationFormData(false).entries());
  snapshot.difficulties = JSON.parse(snapshot.difficulties);
  snapshot.dynamic_enabled=snapshot.dynamic_enabled==='true';snapshot.patterns = JSON.parse(snapshot.patterns);
  snapshot.difficulty_rules = JSON.parse(snapshot.difficulty_rules);
  delete snapshot.title; delete snapshot.artist;
  return snapshot;
}
$('generate-form').onsubmit = async e => {
  e.preventDefault(); showError('form-error', '');
  const chosen = [...document.querySelectorAll('input[name=difficulty]:checked')].map(x => x.value);
  if (!$('title').value.trim()) { showStep('music'); return showError('form-error', '先选择音乐并填写曲名。'); }
  if (!chosen.length) return showError('form-error', '请至少选择一个难度。');
  if (!selectedPatterns().length) return showError('form-error', '请至少选择一种排键方式。');
  if (pendingImport) return showError('form-error', '音乐仍在下载，请等待完成。');
  if (!useReference && !selectedFile && !importedTrack) return showError('form-error', '请先上传音乐，或搜索并导入一首音乐。');
  if (selectedFile?.size > 200 * 1048576) return showError('form-error', '音乐不能超过 200 MB。');
  const data = buildGenerationFormData(!useReference && !importedTrack);
  $('generate').disabled = true; $('generate').textContent = '提交音乐…';
  try { const endpoint = importedTrack ? `/api/music/${importedTrack.id}/generate` : (useReference ? '/api/reference' : '/api/jobs'); const job = await request(endpoint, {method:'POST',body:data}); await watchJob(job.id); }
  catch (error) { showError('form-error', error.message); }
  finally { $('generate').disabled = !ready; $('generate').textContent = '生成 4K 曲包'; }
};
async function watchJob(id) {
  $('music-preview').pause();
  activeJob = id; clearTimeout(timer); report = null; difficulty = null;
  audio.pause(); audio.removeAttribute('src'); audio.load();
  ['download','results','audio','seek-area','preview-toggle'].forEach(x => $(x).hidden = true);
  $('preview-empty').hidden = false; $('difficulty-tabs').replaceChildren(); $('pattern-tabs').replaceChildren(); $('arcade-open').hidden=true; $('arcade-autoplay').hidden=true; activePattern=null;
  const failurePanel=document.getElementById('failure-quality');if(failurePanel)failurePanel.hidden=true;
  $('result-empty').hidden = false; showView('studio');
  document.body.classList.add('has-job');
  if (window.matchMedia('(max-width: 720px)').matches) window.scrollTo({top:0});
  showError('job-error', ''); await poll(id);
}
async function poll(id) {
  if (activeJob !== id) return;
  try {
    const job = await request('/api/jobs/' + id);
    if (activeJob !== id) return;
    $('song-title').textContent = job.title; $('song-artist').textContent = job.artist;
    $('progress-area').hidden = false; $('progress-message').textContent = job.message;
    $('progress-number').textContent = job.progress + '%'; $('progress-bar').value = job.progress;
    $('preview-status').textContent = statusLabels[job.status];
    if (job.status === 'completed') { display(job); await history(); }
    else if (job.status === 'failed') { showError('job-error', job.error || '请重试或检查服务日志。'); displayQualityFailure(job); await history(); }
    else { timer = setTimeout(() => poll(id), 1800); await history(); }
  } catch (error) { showError('job-error', '无法连接服务：' + error.message); timer = setTimeout(() => poll(id), 5000); }
}
function displayQualityFailure(job){
  const alerts=job.quality_alerts||[],host=document.getElementById('failure-quality');
  if(!host||!alerts.length||!job.quality_preview)return;
  const key=job.quality_difficulty||alerts[0].difficulty||'unknown';
  const labels={easy:'Easy',medium:'Medium',hard:'Hard',expert:'Expert',master:'Master',lunatic:'Lunatic'};
  const label=labels[key]||key;
  const duration=Number(job.quality_duration)||0;
  report={duration,previews:{[key]:job.quality_preview}};difficulty=key;
  host.hidden=false;host.replaceChildren();
  const heading=document.createElement('strong');heading.textContent='结构校验未通过 · '+label+' 冲突定位';host.append(heading);
  for(const alert of alerts){
    const button=document.createElement('button');button.type='button';button.className='quality-alert';button.dataset.severity='error';
    button.textContent=alert.message;button.title='跳转到冲突轨道与时间';
    button.onclick=()=>{difficulty=key;audio.currentTime=Math.min(duration,Math.max(0,(alert.start_ms||0)/1000));$('time').textContent=formatTime(audio.currentTime);syncSeekControl();drawChart();$('playfield').scrollIntoView({behavior:'smooth',block:'center'});};
    host.append(button);
  }
  $('results').hidden=true;$('result-empty').hidden=true;$('preview-empty').hidden=true;
  $('difficulty-tabs').replaceChildren();
  const tab=document.createElement('button');tab.textContent=label;tab.dataset.key=key;tab.className='active';tab.setAttribute('aria-pressed','true');tab.onclick=()=>{difficulty=key;drawChart();};$('difficulty-tabs').append(tab);
  $('audio').hidden=false;$('seek-area').hidden=false;$('preview-toggle').hidden=false;
  $('seek').max=duration;$('seek').value=0;$('duration').textContent=formatTime(duration);$('time').textContent='0:00';
  audio.src=`/api/jobs/${job.id}/files/audio.ogg`;audio.currentTime=0;
  $('preview-status').textContent=`未通过校验 · ${label} / ${formatTime(duration)}`;
  syncPreviewToggle();drawChart();
}
function link(text, href) { const a = document.createElement('a'); a.textContent = text; a.href = href; a.download = ''; return a; }
function difficultyLabel(label) { return String(label||'').replace(/\s*[·•]\s*发狂$/,'').trim(); }
function display(job) {
  report = job.report;
  $('progress-area').hidden = true;
  $('result-empty').hidden = true;
  activeJob=job.id;
  window.activeArcadeJob=job.id;
  setCover(report.artwork?.status === 'ready' ? `/api/jobs/${job.id}/files/background.jpg` : null);
  $('download').href = job.download; $('download').download = ''; $('download').hidden = false;
  $('results').hidden = false; $('audio').hidden = false; $('seek-area').hidden = false; $('preview-toggle').hidden = false;
  $('arcade-open').hidden=false; $('arcade-autoplay').hidden=false;
  $('partial-badge').hidden=!report.partial;
  $('preview-empty').hidden = true;
  audio.src = `/api/jobs/${job.id}/files/audio.ogg`;
  $('seek').max = report.duration; $('seek').value = 0; $('duration').textContent=formatTime(report.duration); $('time').textContent='0:00'; audio.currentTime = 0; syncSeekControl();
  $('report-link').href = `/api/jobs/${job.id}/files/report.json`; $('report-link').download = 'report.json';
  const rows=report.difficulties||report.charts||[];
  const patterns=report.patterns?.length?report.patterns:[...new Set(rows.map(row=>row.pattern||'balanced'))];
  if(!patterns.includes(activePattern))activePattern=patterns[0]||'balanced';
  $('pattern-tabs').replaceChildren(...patterns.map(pattern=>{
    const button=document.createElement('button');button.type='button';button.dataset.pattern=pattern;button.textContent=patternCatalog.find(item=>item[0]===pattern)?.[1]||pattern;
    button.setAttribute('aria-pressed',String(pattern===activePattern));button.onclick=()=>renderPatternCharts(pattern);return button;
  }));
  const chartRows=rows.filter(row=>(row.pattern||patterns[0]||'balanced')===activePattern);
  $('difficulty-tabs').replaceChildren(...chartRows.map(row=>{
    const id=row.chart_id||row.key,button=document.createElement('button');button.type='button';button.textContent=difficultyLabel(row.label);button.dataset.key=id;button.dataset.difficulty=row.difficulty||row.key;
    button.onclick=()=>selectDifficulty(id);return button;
  }));
  const table = document.createElement('table'); table.className = 'difficulty-summary';
  table.setAttribute('aria-label', '当前排键各难度实测物量对比');
  const head = document.createElement('tr');
  for (const text of ['难度', '音符数', '平均/秒', '峰值/秒']) {const cell=document.createElement('th');cell.scope='col';cell.textContent=text;head.append(cell);}
  const thead=document.createElement('thead');thead.append(head);table.append(thead);const tbody=document.createElement('tbody');
  for(const rowData of chartRows){
    const id=rowData.chart_id||rowData.key,row=document.createElement('tr');row.dataset.key=id;
    const labelCell=document.createElement('td'),badge=document.createElement('span');badge.className='difficulty-badge';badge.dataset.key=rowData.difficulty||rowData.key;badge.textContent=difficultyLabel(rowData.label);labelCell.append(badge);row.append(labelCell);
    for(const value of [rowData.notes,Number(rowData.average_nps).toFixed(2),rowData.peak_nps]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}tbody.append(row);
  }
  table.append(tbody);let summary=$('difficulty-summary');if(!summary){summary=document.createElement('div');summary.id='difficulty-summary';$('stats').after(summary);}summary.replaceChildren(table);
  $('iteration-difficulty').replaceChildren(...rows.map(row=>{const option=document.createElement('option');option.value=row.chart_id||row.key;option.textContent=`${patternCatalog.find(item=>item[0]===(row.pattern||'balanced'))?.[1]||row.pattern} · ${difficultyLabel(row.label)}`;return option;}));
  $('iteration-difficulty').onchange=()=>selectDifficulty($('iteration-difficulty').value);
  const alerts=report.quality_alerts||report.difficulties.flatMap(d=>d.quality_alerts||[]);
  const alertHost=$('quality-alerts'),visibleAlerts=alerts.filter(alert=>!alert.pattern||alert.pattern===activePattern);alertHost.hidden=!visibleAlerts.length;
  alertHost.replaceChildren(...visibleAlerts.map(alert=>{const button=document.createElement('button');button.type='button';button.className='quality-alert';button.dataset.severity=alert.severity;button.textContent=alert.message;button.title='跳转到谱面对应时间';button.onclick=()=>{selectDifficulty(alert.chart_id||alert.difficulty);audio.currentTime=Math.min(report.duration,Math.max(0,(alert.start_ms||0)/1000));$('time').textContent=formatTime(audio.currentTime);syncSeekControl();drawChart();$('playfield').scrollIntoView({behavior:'smooth',block:'center'});};return button;}));
  $('warnings').replaceChildren(...report.warnings.map(w => { const li = document.createElement('li'); li.textContent = w; return li; }));
  $('preview-status').textContent = `${report.engine.startsWith('Mapperatorinator') ? 'V32 / 分段 BPM' : report.bpm + ' BPM'} / ${formatTime(report.duration)}`;
  const preferred=report.difficulties.find(row=>(row.chart_id||row.key)===difficulty&&row.pattern===activePattern);
  renderPatternCharts(activePattern,preferred?.chart_id||preferred?.key||chartRows[0]?.chart_id||chartRows[0]?.key);syncPreviewToggle();
}
function renderPatternCharts(pattern, preferredKey=null) {
  activePattern=pattern;
  for(const button of $('pattern-tabs').children){const selected=button.dataset.pattern===pattern;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));}
  const rows=(report?.difficulties||report?.charts||[]).filter(row=>(row.pattern||'balanced')===pattern);
  $('difficulty-tabs').replaceChildren(...rows.map(row=>{const key=row.chart_id||row.key,button=document.createElement('button');button.type='button';button.textContent=difficultyLabel(row.label);button.dataset.key=key;button.dataset.difficulty=row.difficulty||row.key;button.onclick=()=>selectDifficulty(key);return button;}));
  const active=rows.find(row=>(row.chart_id||row.key)===preferredKey)||rows[0];
  const links=[];
  for(const row of rows){const key=row.chart_id||row.key,stem=String(row.filename||`${key}.mc`).replace(/\.mc$/i,'');
    const pack=link(`${difficultyLabel(row.label)} · 单谱 MCZ`, `/api/jobs/${activeJob}/charts/${encodeURIComponent(key)}/download`);pack.dataset.key=key;pack.className='chart-pack-link';pack.setAttribute('aria-label',`下载 ${row.pattern_label||pattern} ${difficultyLabel(row.label)} 单谱曲包`);links.push(pack);
    const chart=link(`${difficultyLabel(row.label)} · .mc`, `/api/jobs/${activeJob}/files/${encodeURIComponent(row.filename||`${key}.mc`)}`);chart.dataset.key=key;chart.setAttribute('aria-label',`下载 ${row.pattern_label||pattern} ${difficultyLabel(row.label)} 谱面 .mc`);links.push(chart);
  }
  $('chart-links').replaceChildren(...links);
  for(const row of document.querySelectorAll('.difficulty-summary tbody tr'))row.hidden=!rows.some(item=>(item.chart_id||item.key)===row.dataset.key);
  const alerts=(report?.quality_alerts||[]).filter(alert=>!alert.pattern||alert.pattern===pattern),host=$('quality-alerts');host.hidden=!alerts.length;
  host.replaceChildren(...alerts.map(alert=>{const button=document.createElement('button');button.type='button';button.className='quality-alert';button.dataset.severity=alert.severity;button.textContent=alert.message;button.title='跳转到谱面对应时间';button.onclick=()=>{selectDifficulty(alert.chart_id||alert.difficulty);audio.currentTime=Math.min(report.duration,Math.max(0,(alert.start_ms||0)/1000));$('time').textContent=formatTime(audio.currentTime);syncSeekControl();drawChart();$('playfield').scrollIntoView({behavior:'smooth',block:'center'});};return button;}));
  if(active)selectDifficulty(active.chart_id||active.key);
}
function selectDifficulty(key) {
  const d=(report?.difficulties||report?.charts||[]).find(row=>(row.chart_id||row.key)===key)||
    (report?.difficulties||[]).find(row=>row.difficulty===key&&row.pattern===activePattern);
  if(!d)return;
  const changedPattern=(d.pattern||'balanced')!==activePattern;
  if(changedPattern){renderPatternCharts(d.pattern||'balanced',d.chart_id||d.key);return;}
  key=d.chart_id||d.key;difficulty=key;
  difficulty = key;
  if($('iteration-difficulty')&&[...$('iteration-difficulty').options].some(option=>option.value===key))$('iteration-difficulty').value=key;
  for (const b of $('difficulty-tabs').children) { b.classList.toggle('active', b.dataset.key === key); b.setAttribute('aria-pressed', String(b.dataset.key === key)); }
  for (const row of document.querySelectorAll('.difficulty-summary tbody tr')) {
    const selected = row.dataset.key === key; row.classList.toggle('is-current', selected);
    if (selected) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
  }
  const items = [['音符',d.notes,'个'],['长条',d.holds,'个'],['平均密度',Number(d.average_nps).toFixed(2),'音符/秒'],['一秒峰值',d.peak_nps,'音符/秒']];
  $('stats').replaceChildren(...items.map(([label,value,unit]) => {
    const div = document.createElement('div'); div.className = 'stat';
    const small = document.createElement('small'); small.textContent = label;
    const strong = document.createElement('strong'); strong.textContent = value + ' ';
    const span = document.createElement('span'); span.textContent = unit; strong.append(span); div.append(small,strong); return div;
  }));
  if(window.ChartArcade?.isActive()) window.ChartArcade.close();
  $('arcade-open').dataset.chartId=key;
  drawChart();
}
async function runTierIteration(){
  if(!activeJob||!report)return;
  const key=$('iteration-difficulty').value,button=$('iteration-run');
  const selected=(report.difficulties||[]).find(row=>(row.chart_id||row.key)===key);
  button.disabled=true;button.textContent='正在重生成…';
  try{
    const snapshot=generationSettingsSnapshot();
    await request(`/api/jobs/${activeJob}/charts/${encodeURIComponent(key)}/regenerate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seed:Number(snapshot.seed),rule:snapshot.difficulty_rules[selected?.difficulty||key]||{}})});
    const job=await request(`/api/jobs/${activeJob}`);display(job);await loadQueue();
  }catch(error){showError('job-error',error.message);}
  finally{button.disabled=false;button.textContent='重生成此档';}
}
$('iteration-run').onclick=runTierIteration;
async function showTierVersions(){
  if(!activeJob||!report)return;
  const key=$('iteration-difficulty').value;
  try{
    const data=await request(`/api/jobs/${activeJob}/charts/${encodeURIComponent(key)}/versions`);
    const label=report.difficulties.find(d=>d.key===key)?.label||key;
    $('version-heading').textContent=`${difficultyLabel(label)} · 版本比较`;
    $('version-active-note').textContent=`当前使用 v${data.active}。每次单档重生成都会保留谱面、统计和对应的 MCZ。`;
    $('version-list').replaceChildren(...data.versions.map(item=>{
      const card=document.createElement('article');card.className='version-card';
      const title=document.createElement('strong');title.textContent=`v${item.version}${item.version===data.active?' · 当前':''}`;
      const stats=document.createElement('p');stats.textContent=`${item.notes??'—'} 音符 · 平均 ${Number(item.average_nps||0).toFixed(2)} NPS · 峰值 ${item.peak_nps??'—'} NPS`;
      const actions=document.createElement('div');actions.className='version-actions';
      for(const [suffix,text] of [['mc','下载 .mc'],['mcz','下载单谱 MCZ']]){const a=document.createElement('a');a.textContent=text;a.href=`/api/jobs/${activeJob}/charts/${encodeURIComponent(key)}/versions/v${item.version}.${suffix}`;a.download='';actions.append(a);}
      if(item.version!==data.active){const restore=document.createElement('button');restore.type='button';restore.textContent='恢复此版';restore.onclick=async()=>{restore.disabled=true;try{await request(`/api/jobs/${activeJob}/charts/${encodeURIComponent(key)}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:item.version})});$('version-dialog').close();display(await request(`/api/jobs/${activeJob}`));}catch(error){showError('version-error',error.message);restore.disabled=false;}};actions.append(restore);}
      card.append(title,stats,actions);return card;
    }));
    showError('version-error','');$('version-dialog').showModal();
  }catch(error){showError('job-error',error.message);}
}
$('iteration-versions').onclick=showTierVersions;
$('version-close').onclick=()=>$('version-dialog').close();
function formatTime(seconds) { const s = Math.max(0, Math.floor(seconds)); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); }
  $('seek').oninput = () => { if (audio.src) { audio.currentTime = Number($('seek').value); $('time').textContent=formatTime(audio.currentTime); syncSeekControl(); drawChart(); } };
  audio.addEventListener('timeupdate', () => { $('seek').value = audio.currentTime; $('time').textContent = formatTime(audio.currentTime); syncSeekControl(); if (audio.paused) drawChart(); });
  audio.addEventListener('seeked', () => { syncSeekControl(); if (audio.paused) drawChart(); });
function canvasSize(canvas) {
  const rect = canvas.getBoundingClientRect(), scale = window.devicePixelRatio || 1;
  const width = Math.round(rect.width * scale), height = Math.round(rect.height * scale);
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext('2d'); ctx.setTransform(scale,0,0,scale,0,0); return [ctx,rect.width,rect.height];
}
function drawChart() {
  const [ctx,w,h]=canvasSize($('chart-canvas')); if(!w||!h)return;
  const source=report&&difficulty?(report.previews[difficulty]||emptyPreviewNotes):emptyPreviewNotes;
  if(previewNoteCache?.source!==source)previewNoteCache={source,notes:window.PlayfieldRenderer.prepareNotes(source.map(([start,lane,end])=>({start:Number(start),lane:Number(lane),end:end?Number(end):null})))};
  window.PlayfieldRenderer.render(ctx,w,h,{...window.GameAppearance.get(),speed:previewSpeed,now:(audio.currentTime||0)*1000,notes:previewNoteCache.notes,night:document.documentElement.dataset.theme==='dark'});
}
function animateChart(){
  syncSeekControl();
  setMascotFrame(window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : Math.floor(performance.now()/110)%8);
  drawChart();
  if(!audio.paused) requestAnimationFrame(animateChart);
}
window.addEventListener('resize',()=>{drawChart();syncSeekControl();const frame=Math.max(0,mascotFrame);mascotFrame=-1;setMascotFrame(frame);});
window.addEventListener('appearancechange',drawChart);
window.addEventListener('gameappearancechange',drawChart);
function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(0, bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
async function loadStorage() {
  try { const info=await request('/api/storage'); $('storage-status').textContent=`曲包与下载音乐占用 ${formatBytes(info.total_bytes)}（曲包 ${formatBytes(info.outputs_bytes)} / 音乐 ${formatBytes(info.uploads_bytes)}）`; }
  catch { $('storage-status').textContent='暂时无法读取本地占用空间'; }
}
async function history() {
  const number = ++historyRequest;
  const params = new URLSearchParams({page:String(historyPage),page_size:'8',q:historyQuery,
    engine:$('history-engine').value,status:$('history-status').value,
    difficulty:$('history-difficulty').value,sort:$('history-sort').value});
  try {
    const result = await request('/api/history?' + params);
    if (number !== historyRequest) return;
    historyPage = result.page; $('history-count').textContent = result.total_all;
    $('history-page').textContent = `${result.page} / ${result.pages} 页 · ${result.total} 个曲包`;
    $('history-prev').disabled = result.page <= 1; $('history-next').disabled = result.page >= result.pages;
    showError('history-error','');
    $('history-list').replaceChildren(...result.items.map(item => {
      const card=document.createElement('article'); card.className='history-card'; card.dataset.status=item.status;
      const coverUrl = item.cover || item.thumbnail;
      const cover=document.createElement(coverUrl?'img':'div'); cover.className='history-cover';
      if (coverUrl) { cover.src=coverUrl; cover.alt=item.title+' 封面'; cover.loading='lazy'; }
      else { cover.classList.add('history-cover-placeholder'); cover.textContent='4K'; }
      const body=document.createElement('div'); body.className='history-body';
      const name=document.createElement('h2'); name.textContent=item.title;
      const artist=document.createElement('p'); artist.textContent=item.artist || '音乐人未填写';
      const art=document.createElement('div'); art.className='history-art';
      const engine=document.createElement('span'); engine.className='history-engine'; engine.textContent=item.engine==='advanced'?'高级制谱':item.engine==='v32'?'V32':'MuG';
      const meta=document.createElement('p'); meta.className='history-meta';
      const created=document.createElement('time'); created.dateTime=item.created; created.textContent=new Date(item.created).toLocaleDateString('zh-CN'); meta.append(created);
      const levels=document.createElement('div'); levels.className='history-difficulties'; levels.setAttribute('aria-label','曲包包含的难度');
      const keys=item.difficulties || [], labels=item.difficulty_labels?.length ? item.difficulty_labels : keys;
      labels.forEach((label,index)=>{ const badge=document.createElement('span'); badge.className='difficulty-badge'; const key=keys[index] || ''; badge.dataset.key=key==='normal'?'medium':key; badge.textContent=difficultyLabel(label); levels.append(badge); });
      const actions=document.createElement('div'); actions.className='history-actions';
      const open=document.createElement('button'); open.className='preview-action'; open.textContent='预览'; open.title=item.status==='completed'?'打开预览':'查看进度'; open.setAttribute('aria-label',open.title); open.onclick=()=>item.type==='advanced' ? window.AdvancedStudio?.openResult({projectId:item.project_id,assemblyId:item.assembly_id}) : watchJob(item.id); actions.append(open);
      if (item.status==='completed' && item.download) { const download=document.createElement('a'); download.href=item.download; download.textContent='下载'; download.title='下载 MCZ 曲包'; download.className='history-download'; actions.append(download); }
      if (item.type!=='advanced' && ['completed','failed','interrupted'].includes(item.status)) { const rerun=document.createElement('button'); rerun.textContent=item.status==='completed'?'重生成':'重试'; rerun.title=item.status==='completed'?'按原设置重新生成':'按原设置重试'; rerun.setAttribute('aria-label',rerun.title); rerun.onclick=async()=>{ rerun.disabled=true; try { const job=await request(`/api/jobs/${item.id}/regenerate`,{method:'POST'}); await watchJob(job.id); } catch(error) { showError('history-error',error.message); } finally { rerun.disabled=false; } }; actions.append(rerun); }
      if (item.type!=='advanced' && !['queued','running'].includes(item.status)) { const remove=document.createElement('button'); remove.textContent='删除'; remove.title='删除曲包记录'; remove.className='danger-button'; remove.onclick=async()=>{ if (!window.confirm(`删除“${item.title}”的曲包和报告，并清理本条记录独占的原始上传音乐？共享的在线下载音乐库不会受影响。此操作无法撤销。`)) return; remove.disabled=true; try { await request(`/api/jobs/${item.id}`,{method:'DELETE'}); await history(); await loadStorage(); } catch(error) { showError('history-error',error.message); remove.disabled=false; } }; actions.append(remove); }
      const state=document.createElement('small'); state.textContent=statusLabels[item.status]; state.className='history-state';
      art.append(cover,engine,state); body.append(name,artist,levels,meta,actions); card.append(art,body); return card;
    }));
    if (!result.items.length) { const empty=document.createElement('p'); empty.className='history-empty'; empty.textContent=(historyQuery||$('history-engine').value||$('history-status').value||$('history-difficulty').value)?'没有匹配的曲包，试试清除筛选。':'还没有曲包，去工作台生成第一首。'; $('history-list').append(empty); }
  } catch (error) { showError('history-error', error.message); }
}
function renderDiagnostics() {
  const list=$('engine-diagnostics-list');
  if (!healthState) { list.textContent='生成服务未连接，请检查项目环境后重试。'; return; }
  list.replaceChildren(...Object.entries(healthState.engines || {}).map(([key,item])=>{
    const row=document.createElement('article'); row.className='engine-diagnostic-row';
    const heading=document.createElement('strong'); heading.textContent=item.label;
    const state=document.createElement('span'); state.textContent=item.ready?'可生成':(item.reason || '环境未就绪'); state.className=item.ready?'ready':'not-ready';
    const detail=document.createElement('small'); detail.textContent=`Python：${item.python_ready?'已安装':'未安装'} · 权重文件：${item.weights_ready?'大小正常':'缺失或未完成'}${key==='v32'?' · CUDA：'+(item.cuda_available?'可用':'不可用'):''}`;
    row.append(heading,state,detail); return row;
  }));
}
async function connect() {
  try {
    healthState=await request('/api/health'); $('reference').hidden=!healthState.reference;
    for (const option of $('engine').options) {
      const item=healthState.engines?.[option.value];
      if (item) { option.disabled=!item.ready; option.textContent=item.label+(item.ready?'':' · 未就绪'); }
    }
    if ($('engine').selectedOptions[0]?.disabled) {
      const available=[...$('engine').options].find(option=>!option.disabled);
      if (available) $('engine').value=available.value;
    }
    renderDiagnostics(); updateEngine();
    if (!ready) setTimeout(connect,5000);
  } catch {
    ready=false; $('generate').disabled=true; for (const button of [$('engine-state'),$('engine-state-mobile')]) { button.textContent='生成服务未连接'; button.title='点击查看排查说明'; } renderDiagnostics(); setTimeout(connect,5000);
  }
}
connect(); history(); loadStorage(); drawChart();
function updateEngine() {
  const v32=$('engine').value==='v32';
  $('steps-field').hidden=v32; $('bpm').disabled=v32;
  $('mug-settings').hidden=v32; $('v32-settings').hidden=!v32;
  $('pattern-strength-field').hidden=v32;
  $('engine-hint').textContent=v32?'V32 生成共享母谱；这里的参考难度是模型输入，上方六档决定最终谱面规则。':'MuG 生成共享母谱；这里的参考星级是模型输入，上方六档决定最终谱面规则。可继续调整风格、键型和采样方式。';
  const state=healthState?.engines?.[$('engine').value]; ready=Boolean(state?.ready);
  $('generate').disabled=!ready || Boolean(pendingImport) || !selectedPatterns().length || !document.querySelector('input[name="difficulty"]:checked');
  const statusText=ready?`本机已就绪 · ${state.label}`:(state?.reason || '点击检查本机生成环境');
  const statusTitle=ready?`当前设备：${healthState.gpu}`:(state?.reason || '查看本机模型状态');
  for (const button of [$('engine-state'),$('engine-state-mobile')]) { button.textContent=statusText; button.title=statusTitle; }
  syncPatternSelection(false);
  renderDiagnostics();
}
$('engine').onchange=()=>{ updateEngine(); persistPreferences(); };
$('preview-speed').oninput=()=>{ previewSpeed=Number($('preview-speed').value); syncPreviewSpeedControl(); persistPreferences(); drawChart(); };
for(const button of document.querySelectorAll('.preview-speed-presets button')) button.onclick=()=>{ $('preview-speed').value=button.dataset.speed; previewSpeed=Number(button.dataset.speed); syncPreviewSpeedControl(); persistPreferences(); drawChart(); };
$('preview-focus').onclick=()=>{ const on=document.body.classList.toggle('focus-preview'); $('preview-focus').setAttribute('aria-pressed',String(on)); $('preview-focus').textContent=on?'退出专注预览':'专注预览'; drawChart(); };
$('playfield').addEventListener('click', event=>{if (!window.ChartArcade?.isActive() && !event.target.closest('#arcade-panel')) togglePreviewPlayback();});
$('preview-toggle').addEventListener('click', event=>{ event.stopPropagation(); togglePreviewPlayback(); });
window.addEventListener('keydown',event=>{
  if (window.ChartArcade?.isActive() || window.AdvancedStudio?.isVisible() || $('studio-view').hidden) return;
  if (event.target.matches('input,select,textarea,button,[contenteditable="true"]')) return;
  if (event.code==='Space' && report) { event.preventDefault(); togglePreviewPlayback(); }
  if (report && event.key==='ArrowLeft') { event.preventDefault(); audio.currentTime=Math.max(0,audio.currentTime-5); }
  if (report && event.key==='ArrowRight') { event.preventDefault(); audio.currentTime=Math.min(audio.duration||report.duration,audio.currentTime+5); }
  if (event.key==='Escape' && document.body.classList.contains('focus-preview')) { document.body.classList.remove('focus-preview'); $('preview-focus').setAttribute('aria-pressed','false'); $('preview-focus').textContent='专注预览'; drawChart(); }
});
audio.addEventListener('play',()=>{ syncPreviewToggle(); requestAnimationFrame(animateChart); });
audio.addEventListener('pause',()=>{ syncPreviewToggle(); drawChart(); });
audio.addEventListener('ended',syncPreviewToggle);
$('engine-state').setAttribute('aria-expanded','false');
$('engine').onchange();
loadQueue();

function clearMusicSelection() {
  importedTrack = null; pendingImport = null; clearTimeout(musicTimer);
  $('music-selected').hidden = true; $('music-preview').pause(); $('music-preview').removeAttribute('src');
  $('generate').disabled = !ready || Boolean(pendingImport);
}
function musicDuration(seconds) { return seconds ? formatTime(seconds) : '时长待检查'; }
function showMusicResults(items, options={}) {
  if (!options.append) {
    musicItems = [];
    musicCursor = 0;
    musicHasMore = false;
    musicSelection.clear();
  }
  const known = new Set(musicItems.map(item => item.id));
  for (const item of items || []) if (!known.has(item.id)) { musicItems.push(item); known.add(item.id); }
  if (options.cursor !== undefined) musicCursor = options.cursor;
  if (options.hasMore !== undefined) musicHasMore = options.hasMore;
  renderMusicResults();
}
function renderMusicResults() {
  $('music-pagination').hidden = !musicHasMore;
  $('music-page').textContent = musicItems.length ? `${musicItems.length} 个结果` : '';
  $('music-next').disabled = $('music-next').dataset.loading === 'true';
  $('music-selection-count').textContent = `已选 ${musicSelection.size} / 20 首`;
  $('music-batch-submit').disabled = !musicSelection.size;
  $('music-results').replaceChildren(...musicItems.map(item => {
    const row = document.createElement('article'); row.className = 'music-result';
    const image=document.createElement('img'); image.src=item.thumbnail || `https://i.ytimg.com/vi/${item.id}/hqdefault.jpg`; image.alt=''; image.loading='lazy';
    const description=document.createElement('div');
    const title = document.createElement('strong'); title.textContent = item.title;
    const meta = document.createElement('p'); meta.textContent = `${item.channel || item.artist || '未知频道'} · ${musicDuration(item.duration)}${item.channel_verified?' · 已认证':''}`;
    const selection = document.createElement('label'); selection.className='music-select';
    const checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.checked=musicSelection.has(item.id); checkbox.setAttribute('aria-label',`选择 ${item.title} 加入批量生成`);
    checkbox.disabled=!checkbox.checked && musicSelection.size>=20;
    checkbox.onchange=()=>{ if(checkbox.checked){if(musicSelection.size>=20){checkbox.checked=false;return;}musicSelection.add(item.id);}else musicSelection.delete(item.id);renderMusicResults(); };
    selection.append(checkbox,document.createTextNode('批量选择'));
    const actions = document.createElement('div'); actions.className = 'music-actions';
    const source = document.createElement('a'); source.textContent = 'YouTube'; source.href = item.url; source.target = '_blank'; source.rel = 'noopener noreferrer';
    const button = document.createElement('button'); button.type = 'button'; button.textContent = item.status === 'ready' ? '使用这首音乐' : '下载并导入';
    button.onclick = () => importMusic(item); actions.append(selection,source,button); description.append(title,meta); row.append(image,description,actions); return row;
  }));
}
async function searchMusicPage(append=false) {
  const number = ++searchNumber;
  showError('music-error','');
  if (!append) {
    musicSearchQuery=$('music-query').value.trim();
    musicSearchRange=$('music-duration').value.split(',').map(Number);
    $('music-search-status').textContent='正在搜索 YouTube…';
  }
  const [minDuration,maxDuration]=musicSearchRange;
  const params=new URLSearchParams({q:musicSearchQuery,cursor:String(append?musicCursor:0),limit:'20',min_duration:String(minDuration),max_duration:String(maxDuration)});
  if(append){$('music-next').dataset.loading='true';$('music-next').textContent='正在加载…';}
  else $('music-search-button').disabled=true;
  try {
    const result=await request('/api/music/search?'+params);
    if(number!==searchNumber)return;
    showMusicResults(result.results,{append,cursor:result.next_cursor??musicCursor,hasMore:result.has_more});
    const filtered=result.filtered||{}, filteredCount=Object.values(filtered).reduce((a,b)=>a+b,0);
    if(!musicItems.length) $('music-search-status').textContent=filteredCount?`扫描了 ${result.scanned} 条，按时长或直播规则过滤后没有可用结果。`:'没有找到符合条件的音乐，试试原文曲名或音乐人。';
    else $('music-search-status').textContent=`已显示 ${musicItems.length} 首${result.has_more?' · 可继续加载':''}${filteredCount?`；本页过滤 ${filteredCount} 条（直播 ${filtered.live||0}、时长 ${filtered.duration||0}、无效/重复 ${ (filtered.invalid||0)+(filtered.duplicate||0)}）`:''}。范围 ${minDuration} 秒–${Math.floor(maxDuration/60)} 分钟。`;
  } catch(error) { if(number===searchNumber){showError('music-error',error.message);if(!append)$('music-search-status').textContent='';} }
  finally { if(number===searchNumber){$('music-search-button').disabled=false;$('music-next').dataset.loading='false';$('music-next').textContent='继续加载 YouTube 结果';renderMusicResults();} }
}
$('music-next').onclick=()=>searchMusicPage(true);
$('music-duration').onchange=()=>{if($('music-query').value.trim())$('music-search-form').requestSubmit();};
$('music-search-form').onsubmit = async e => { e.preventDefault(); await searchMusicPage(false); };
$('music-batch-submit').onclick=async()=>{
  if(!musicSelection.size)return;
  const button=$('music-batch-submit');button.disabled=true;button.textContent='提交批次…';showError('music-error','');
  try{
    const result=await request('/api/batches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_ids:[...musicSelection],settings:generationSettingsSnapshot()})});
    musicSelection.clear();renderMusicResults();$('music-search-status').textContent=`已将 ${result.count} 首加入生成队列；本批共用当前模型、难度与参数。`;
    showView('queue');
  }catch(error){showError('music-error',error.message);}
  finally{button.textContent='加入生成队列';renderMusicResults();}
};
$('music-library').onclick = async () => {
  ++searchNumber; $('music-search-button').disabled = false; showError('music-error','');
  try { const items = await request('/api/music'); showMusicResults(items,{hasMore:false}); $('music-search-status').textContent = items.length ? '已下载的音乐可重复使用；也可勾选多首加入生成队列。' : '还没有下载音乐，先搜索一首歌。'; }
  catch(error) { showError('music-error',error.message); }
};
async function loadQueue() {
  clearTimeout(queueTimer);
  const sequence = ++queueRequest;
  try {
    const queue=await request('/api/queue');
    if (sequence !== queueRequest || $('queue-view').hidden) return;
    const items=(queue.items||[]).filter(item=>['queued','running','paused','needs_source'].includes(item.status));
    $('queue-count').textContent=String(items.filter(item=>['queued','running','paused'].includes(item.status)).length);
    $('queue-active-count').textContent=items.length ? String(items.length) : '';
    $('queue-summary').textContent=`${queue.running} 个任务生成中 · ${queue.waiting} 个等待${queue.paused?` · ${queue.paused} 个已暂停`:''}`;
    $('queue-parallel-note').textContent=queue.parallel_reason;
    $('queue-concurrency-field').hidden=!queue.parallel_enabled;
    const parallelOption=$('queue-concurrency').querySelector('option[value="2"]');parallelOption.disabled=!queue.parallel_enabled;
    $('queue-concurrency').value=String(queue.concurrency||1);
    $('queue-resume').hidden=!queue.paused;
    $('queue-empty').hidden=items.length>0;
    $('queue-items').hidden=!items.length;
    $('queue-items').replaceChildren(...items.map(item=>{
      const card=document.createElement('article');card.className='queue-item';card.dataset.status=item.status;
      const order=document.createElement('span');order.className='queue-order';order.textContent=String(item.queue_order||'—').padStart(2,'0');
      const detail=document.createElement('div');detail.className='queue-item-detail';
      const title=document.createElement('strong');title.textContent=item.title;
      const artist=document.createElement('span');artist.textContent=item.artist||'未知音乐人';
      const state=document.createElement('small');state.textContent=`${statusLabels[item.status]||item.status} · ${item.message||''}${item.progress?` · ${item.progress}%`:''}`;
      detail.append(title,artist,state);card.append(order,detail);
      if(item.status==='running'){const progress=document.createElement('progress');progress.max=100;progress.value=item.progress||0;card.append(progress);}
      const actions=document.createElement('div');actions.className='queue-item-actions';
      if(['queued','paused'].includes(item.status)){const cancel=document.createElement('button');cancel.textContent='取消';cancel.onclick=async()=>{cancel.disabled=true;try{await request(`/api/jobs/${item.id}`,{method:'DELETE'});await loadQueue();}catch(error){showError('queue-error',error.message);cancel.disabled=false;}};actions.append(cancel);}
      card.append(actions);return card;
    }));
    showError('queue-error','');
  }catch(error){if(sequence===queueRequest&&!$('queue-view').hidden)showError('queue-error',error.message);}
  if(sequence===queueRequest&&!$('queue-view').hidden)queueTimer=setTimeout(loadQueue,5000);
}
$('queue-refresh').onclick=()=>{loadQueue();window.TaskHistory?.refresh();};
$('queue-concurrency').onchange=async()=>{try{await request('/api/queue/concurrency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:Number($('queue-concurrency').value)})});await loadQueue();}catch(error){showError('queue-error',error.message);await loadQueue();}};
$('queue-resume').onclick=async()=>{const button=$('queue-resume');button.disabled=true;try{const result=await request('/api/queue/resume',{method:'POST'});if(result.needs_source.length)showError('queue-error',`${result.needs_source.length} 条旧任务缺少音乐来源，请重新导入。`);await loadQueue();}catch(error){showError('queue-error',error.message);}finally{button.disabled=false;}};
async function importMusic(item) {
  clearMusicSelection(); pendingImport = item.id; selectedFile = null; useReference = false; $('file').value = '';
  $('title').value = item.title; $('artist').value = item.artist || ''; $('file-label').textContent = item.title; $('file-size').textContent = '正在导入';
  $('song-title').textContent=item.title; $('song-artist').textContent=item.artist || 'YouTube 音源';
  setCover(item.thumbnail || `https://i.ytimg.com/vi/${item.id}/hqdefault.jpg`);
  $('music-selected').hidden = false; $('music-preview').hidden = true; $('music-save').hidden = true;
  $('music-import-status').textContent = '正在获取音源…'; $('generate').disabled = true; showError('music-error','');
  try { const track = await request(`/api/music/${item.id}/import`, {method:'POST'}); if(pendingImport === item.id) await watchMusic(track); }
  catch(error) { if(pendingImport === item.id) { pendingImport = null; $('generate').disabled = !ready; $('file-size').textContent = '导入失败'; showError('music-error',error.message); $('music-import-status').textContent = '导入失败，请重试。'; } }
}
async function watchMusic(track) {
  if(pendingImport !== track.id) return;
  $('music-import-status').textContent = track.message;
  if(track.status === 'ready') {
    importedTrack = track; pendingImport = null; $('generate').disabled = !ready;
    $('file-size').textContent = musicDuration(track.duration); $('file-label').textContent = track.title;
    $('music-preview').src = track.preview; $('music-preview').hidden = false;
    $('music-save').href = track.file; $('music-save').download = ''; $('music-save').hidden = false;
    showError('form-error',''); return;
  }
  if(track.status === 'failed') { pendingImport = null; $('generate').disabled = !ready; $('file-size').textContent = '导入失败'; showError('music-error',track.message); return; }
  musicTimer = setTimeout(async () => { try { await watchMusic(await request(`/api/music/${track.id}`)); }
    catch(error) { if(pendingImport === track.id) { pendingImport = null; $('generate').disabled = !ready; showError('music-error','下载状态读取失败，请重新选择这首音乐。'); } } }, 1800);
}

// A selector replaces overflowing pattern tabs without making the song header taller.
function syncPatternSelector(){
  const tabs=$('pattern-tabs'),host=$('pattern-selector'),select=$('preview-pattern');
  const children=[...tabs.children];
  const compact=window.GameAppearance.needsPatternSelector(children.length,tabs.parentElement.clientWidth);
  host.hidden=!compact;tabs.hidden=compact;
  const signature=children.map(b=>b.dataset.pattern+'|'+b.textContent).join(';');
  if(select.dataset.signature!==signature){select.replaceChildren(...children.map(b=>{const o=document.createElement('option');o.value=b.dataset.pattern;o.textContent=b.textContent;return o;}));select.dataset.signature=signature;}
  if(activePattern)select.value=activePattern;
}
$('preview-pattern').addEventListener('change',()=>renderPatternCharts($('preview-pattern').value));
new MutationObserver(syncPatternSelector).observe($('pattern-tabs'),{childList:true,subtree:true,attributes:true,attributeFilter:['aria-pressed']});
new ResizeObserver(syncPatternSelector).observe($('pattern-tabs').parentElement);
new MutationObserver(()=>{$('song-title').title=$('song-title').textContent;}).observe($('song-title'),{childList:true});
syncPatternSelector();
