const $ = id => document.getElementById(id);
let selectedFile = null, useReference = false, activeJob = null, report = null, difficulty = null, activePattern = null, timer = null, ready = false;
let importedTrack = null, pendingImport = null, musicTimer = null, searchNumber = 0;
let historyPage = 1, historyQuery = '', historyRequest = 0, musicItems = [], musicCursor = 0, musicHasMore = false, musicSearchQuery = '', musicSelection = new Set(), musicSearchRange = [5,600], queueTimer = null, queueRequest = 0;
let historyController = null, historyLoading = false, historyCommitted = null;
let healthState = null, previewSpeed = 1, previewNoteCache = null;
const emptyPreviewNotes = [];
const librarySelection = new Map(), libraryDeletionErrors = new Map(); let libraryPageItems = [], libraryDeleteItems = [];
function showView(view) {
  if (view !== 'queue') { clearTimeout(queueTimer); ++queueRequest; window.TaskHistory?.leave(); }
  if ($('music-download-view')) $('music-download-view').hidden = view !== 'music';
  if (view === 'music') window.MusicDownloads?.enter();
  if ($('advanced-view')) $('advanced-view').hidden = view !== 'advanced';
  document.body.classList.toggle('advanced-open', view === 'advanced');
  if (view !== 'advanced') window.AdvancedStudio?.leave();
  $('studio-view').hidden = view !== 'studio'; $('library-view').hidden = view !== 'library'; $('queue-view').hidden = view !== 'queue';
  for (const name of ['music','studio','advanced','library','queue']) { if (!$('nav-'+name)) continue; $('nav-'+name).classList.toggle('active', name === view); $('nav-'+name).setAttribute('aria-pressed', String(name === view)); }
  if (view === 'advanced') { audio.pause(); window.AdvancedStudio?.enter(); return; }
  if (view === 'library') { history(); loadStorage(); } else if (view === 'queue') { window.TaskHistory?.enter(); loadQueue(); } else { drawChart(); }
}
function showStep(step) {
  document.body.classList.remove('has-job');
  $('music-panel').hidden = step !== 'music'; $('settings-panel').hidden = step !== 'settings';
  for (const name of ['music','settings']) { $('step-'+name).classList.toggle('active', name === step); $('step-'+name).setAttribute('aria-pressed', String(name === step)); }
  document.querySelector('.control-scroll').scrollTop = 0;
}
function coverSource(url) {
  return String(url).replace(/^(?:http:)?\/\/((?:[a-z0-9-]+\.)*hdslb\.com)(?=\/)/i,'https://$1');
}
function setCover(url) {
  $('song-cover').hidden = !url; $('cover-placeholder').hidden = !!url;
  if (url) { $('song-cover').referrerPolicy='no-referrer'; $('song-cover').src = coverSource(url); } else $('song-cover').removeAttribute('src');
}
$('song-cover').onerror = () => setCover(null);
$('nav-studio').onclick = () => showView('studio'); $('nav-library').onclick = () => showView('library');
$('nav-queue').onclick = () => showView('queue');
$('step-music').onclick = () => showStep('music'); $('step-settings').onclick = () => showStep('settings');
$('next-settings').onclick = () => showStep('settings');
const mobileEdit = document.createElement('button'); mobileEdit.className = 'mobile-edit'; mobileEdit.textContent = '调整音乐与难度';
mobileEdit.onclick = () => { showStep('music'); window.scrollTo({top:0}); };
document.querySelector('#studio-view .workspace-heading').prepend(mobileEdit);
$('history-prev').onclick = () => { if (historyLoading || $('history-prev').disabled) return; historyPage--; return history(); };
$('history-next').onclick = () => { if (historyLoading || $('history-next').disabled) return; historyPage++; return history(); };
$('history-search-form').onsubmit = e => { e.preventDefault(); historyQuery = $('history-query').value.trim(); historyPage = 1; clearLibrarySelection(); history(); };
for (const id of ['history-engine','history-status','history-difficulty','history-sort']) $(id).onchange = () => { historyPage = 1; if (id !== 'history-sort') clearLibrarySelection(); history(); };
$('history-reset').onclick = () => { $('history-query').value = ''; historyQuery = ''; for (const id of ['history-engine','history-status','history-difficulty']) $(id).value = ''; $('history-sort').value = 'newest'; historyPage = 1; clearLibrarySelection(); history(); };
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
  button.querySelector('.sr-only').textContent=button.classList.contains('simple-listen') ? (playing ? '暂停' : '试听') : button.title;
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
const npsRangeDigits=1;
function roundNps(value){return Math.round((Number(value)+Number.EPSILON)*10**npsRangeDigits)/10**npsRangeDigits;}
function formatNps(value){return Number(value).toFixed(npsRangeDigits).replace(/\.0$/,'');}
function validNpsRange(minimum,maximum){minimum=Number(minimum);maximum=Number(maximum);return Number.isFinite(minimum)&&Number.isFinite(maximum)&&minimum>=.5&&maximum<=50&&minimum<=maximum;}
function installNpsRangeControls(){
  for(const rateInput of document.querySelectorAll('[data-rule="rate"]')){
    const difficulty=rateInput.dataset.difficulty,rate=Number(rateInput.value),container=rateInput.parentElement.parentElement;
    const min=roundNps(rate*.8),max=roundNps(rate*1.2),replacement=[];
    for(const [bound,value,labelText]of [['min',min,'NPS 下限'],['max',max,'NPS 上限']]){
      const label=document.createElement('label');label.append(document.createTextNode(labelText));
      const input=document.createElement('input');input.type='number';input.min='.5';input.max='50';input.step='.1';input.value=value;input.dataset.difficulty=difficulty;input.dataset.npsBound=bound;input.setAttribute('aria-label',difficulty+' '+labelText);label.append(input);replacement.push(label);
    }
    rateInput.type='hidden';rateInput.setAttribute('aria-hidden','true');rateInput.tabIndex=-1;
    rateInput.parentElement.replaceWith(...replacement,rateInput);
  }
}
function npsRangeInputs(difficulty){return Object.fromEntries([...document.querySelectorAll(`[data-difficulty="${difficulty}"][data-nps-bound]`)].map(input=>[input.dataset.npsBound,input]));}
function npsRangeValues(){const ranges={};for(const input of document.querySelectorAll('[data-difficulty][data-nps-bound]'))(ranges[input.dataset.difficulty]||={})[input.dataset.npsBound]=Number(input.value);return ranges;}
function syncNpsRangeSummary(difficulty){
  const fields=npsRangeInputs(difficulty),minimum=Number(fields.min?.value),maximum=Number(fields.max?.value),rate=document.querySelector(`[data-difficulty="${difficulty}"][data-rule="rate"]`);
  if(rate&&Number.isFinite(minimum)&&Number.isFinite(maximum))rate.value=roundNps((minimum+maximum)/2);
  const target=document.querySelector(`input[name="difficulty"][value="${difficulty}"]`)?.closest('label')?.querySelector('em');
  if(target&&Number.isFinite(minimum)&&Number.isFinite(maximum))target.textContent=`${formatNps(minimum)}–${formatNps(maximum)} NPS`;
}
installNpsRangeControls();
const preferenceIds = ['creator','ln-ratio','pattern','pattern-strength','engine','steps','mug-difficulty','mug-style','mug-guidance','mug-eta','v32-temperature','v32-top-p','v32-column-temperature','v32-cfg-scale','v32-year','v32-descriptors','v32-negative-descriptors','bpm','seed','preview-speed'];
function readPreferences() { return {version:1, mode:$('mode-custom')?.getAttribute('aria-pressed') === 'true', values:Object.fromEntries(preferenceIds.map(id => [id,$(id)?.value]).filter(([,value]) => value !== undefined)), patterns:selectedPatterns(), difficulties:[...document.querySelectorAll('input[name="difficulty"]:checked')].map(input => input.value), rules:Object.fromEntries([...document.querySelectorAll('[data-difficulty][data-rule]')].map(input => [`${input.dataset.difficulty}.${input.dataset.rule}`,input.value])), ranges:npsRangeValues()}; }
const preferenceDefaults = readPreferences();
function applyPreferences(values) { if (!values || values.version !== 1) return; for (const [id,value] of Object.entries(values.values || {})) if ($(id) && value !== undefined) $(id).value = id==='preview-speed'?window.PlayfieldRenderer.speedValue(value):value; const patterns=Array.isArray(values.patterns)?values.patterns:(values.values?.pattern?[values.values.pattern]:['balanced']); for (const input of document.querySelectorAll('input[name="pattern-choice"]')) input.checked=patterns.includes(input.value); if (Array.isArray(values.difficulties)) for (const input of document.querySelectorAll('input[name="difficulty"]')) input.checked = values.difficulties.includes(input.value); for (const input of document.querySelectorAll('[data-difficulty][data-rule]')) { const value=values.rules?.[`${input.dataset.difficulty}.${input.dataset.rule}`]; if (value !== undefined) input.value=value; } for(const difficulty of ['easy','medium','hard','expert','master','lunatic']){const saved=values.ranges?.[difficulty],inputs=npsRangeInputs(difficulty);if(validNpsRange(saved?.min,saved?.max)) {inputs.min.value=roundNps(saved.min);inputs.max.value=roundNps(saved.max);}else{const oldRate=Number(document.querySelector(`[data-difficulty="${difficulty}"][data-rule="rate"]`)?.value);const base=Number.isFinite(oldRate)&&oldRate>0?oldRate:(Number(inputs.min.value)+Number(inputs.max.value))/2;inputs.min.value=roundNps(base*.8);inputs.max.value=roundNps(base*1.2);}syncNpsRangeSummary(difficulty);} }
$('creator').addEventListener('input',()=>persistPreferences());
function persistPreferences() { try { localStorage.setItem('malody-chart-forge.preferences.v1', JSON.stringify(readPreferences())); } catch {} }
function setSettingsMode(custom, save=true) { $('settings-panel').classList.toggle('recommended-mode', !custom); $('mode-recommended').classList.toggle('selected', !custom); $('mode-custom').classList.toggle('selected', custom); $('mode-recommended').setAttribute('aria-pressed', String(!custom)); $('mode-custom').setAttribute('aria-pressed', String(custom)); for (const detail of document.querySelectorAll('#settings-panel details.advanced')) detail.open = custom; if (save) persistPreferences(); }
try { const saved=JSON.parse(localStorage.getItem('malody-chart-forge.preferences.v1') || 'null'); applyPreferences(saved); setSettingsMode(Boolean(saved?.mode), false);if(saved&&['easy','medium','hard','expert','master','lunatic'].some(key=>!validNpsRange(saved.ranges?.[key]?.min,saved.ranges?.[key]?.max)))persistPreferences(); } catch { setSettingsMode(false, false); }
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
  for(const difficulty of ['easy','medium','hard','expert','master','lunatic'])syncNpsRangeSummary(difficulty);
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
for(const input of document.querySelectorAll('[data-difficulty][data-nps-bound]'))input.addEventListener('input',()=>syncNpsRangeSummary(input.dataset.difficulty));
for (const input of document.querySelectorAll('#settings-panel input:not([type="file"]), #settings-panel select')) { input.addEventListener('input', persistPreferences); input.addEventListener('change', persistPreferences); }
for (const input of document.querySelectorAll('input[name="difficulty"]')) {
  input.closest('label').dataset.key = input.value;
  input.addEventListener('change', () => { updateDifficultyRuleVisibility(); persistPreferences(); });
}
function updateDifficultyRuleVisibility() { for (const row of document.querySelectorAll('[data-rule-difficulty]')) row.hidden = !document.querySelector(`input[name="difficulty"][value="${row.dataset.ruleDifficulty}"]`)?.checked; }
updateDifficultyRuleVisibility();
function showError(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
let localMusicRequest = 0;
const localMetadataEdits={title:0,artist:0,cover:0};
for(const field of ['title','artist','artwork-url']) $(field).addEventListener('input',()=>{localMetadataEdits[field==='artwork-url'?'cover':field]++;if(field!=='artwork-url'){ $('song-title').textContent=$('title').value; $('song-artist').textContent=$('artist').value||'音乐人未填写';}});
async function fileSelected(file) {
  if (!file) return;
  clearMusicSelection();
  const sequence = ++localMusicRequest;
  selectedFile = file; useReference = false; $('artist').value = ''; $('artwork-url').value = '';
  $('file-label').textContent = file.name;
  $('file-size').textContent = (file.size / 1048576).toFixed(1) + ' MB';
  $('title').value = file.name.replace(/\.[^.]+$/, '');
  $('song-title').textContent = $('title').value; $('song-artist').textContent = '已选择本地音乐'; setCover(null);
  showError('form-error', '');
  if(file.size>200*1048576){showError('form-error','音乐不能超过 200 MB。');return;}
  pendingImport='identify:'+sequence; $('generate').disabled=true;
  const edits={...localMetadataEdits};
  const form = new FormData(); form.append('file', file);
  try { const result = await request('/api/music-assets/identify', {method:'POST', body:form}); if(sequence !== localMusicRequest || selectedFile !== file) return; const asset=result.asset; if(asset){if(edits.title===localMetadataEdits.title)$('title').value=asset.title;if(edits.artist===localMetadataEdits.artist)$('artist').value=asset.artist||'';$('song-title').textContent=$('title').value;$('song-artist').textContent=$('artist').value||'音乐人未填写';if(edits.cover===localMetadataEdits.cover)setCover(asset.thumbnail||null);} } catch(error) { if(sequence === localMusicRequest) showError('form-error', '素材信息读取失败：'+error.message+'。仍可填写歌曲信息继续。'); } finally {if(sequence===localMusicRequest && selectedFile===file){pendingImport=null;$('generate').disabled=!ready;}}
}
$('file').addEventListener('change', e => fileSelected(e.target.files[0]));
['dragenter','dragover'].forEach(name => $('dropzone').addEventListener(name, e => { e.preventDefault(); $('dropzone').classList.add('dragging'); }));
['dragleave','drop'].forEach(name => $('dropzone').addEventListener(name, e => { e.preventDefault(); $('dropzone').classList.remove('dragging'); }));
$('dropzone').addEventListener('drop', e => fileSelected(e.dataTransfer.files[0]));
$('ln-ratio').addEventListener('input', () => $('ln-value').textContent = Math.round($('ln-ratio').value * 100) + '%');
$('pattern-strength').addEventListener('input', () => $('pattern-strength-value').textContent = $('pattern-strength').value);
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
  if(options?.method==='POST' && ($('engine')?.value==='v32') && (/^\/api\/(jobs|reference|batches)$/.test(url)||/^\/api\/music\/[^/]+\/generate$/.test(url))){
    const current=await fetch('/api/health').then(r=>r.json());
    if(!(current.nps_star_direct_version>=1))throw new Error('请重启本地制谱服务，加载 NPS 范围独立生成版本。');
  }
  const response = await fetch(url, options);
  let data; try { data = await response.json(); } catch { throw new Error('服务返回了无效响应'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || '请求失败，请检查输入后重试');
  return data;
}
function buildGenerationFormData(includeSource=false) {
  const creator=$('creator').value.trim();
  if(!creator||creator.length>120)throw new Error('谱师名字需填写 1–120 个字符。');
  const data = new FormData();
  data.append('creator',creator);
  for (const id of ['title','artist','steps','seed','engine','pattern','pattern-strength',
    'mug-difficulty','mug-style','mug-guidance','mug-eta','v32-temperature',
    'v32-top-p','v32-column-temperature','v32-cfg-scale','v32-year','v32-descriptors',
    'v32-negative-descriptors']) data.append(id.replaceAll('-', '_'), $(id).value);
  const rules = {};
  for (const input of document.querySelectorAll('[data-difficulty][data-rule]')) {
    const key = input.dataset.difficulty;
    (rules[key] ||= {})[input.dataset.rule] = Number(input.value);
  }
  data.append('tail_trim_enabled',String($('tail-trim-enabled').checked));data.append('dynamic_enabled',String($('dynamic-enabled').checked));data.append('difficulty_rules', JSON.stringify(rules));data.append('nps_ranges',JSON.stringify(npsRangeValues()));
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
  snapshot.tail_trim_enabled=snapshot.tail_trim_enabled==='true';snapshot.dynamic_enabled=snapshot.dynamic_enabled==='true';snapshot.patterns = JSON.parse(snapshot.patterns);
  snapshot.difficulty_rules = JSON.parse(snapshot.difficulty_rules);snapshot.nps_ranges=JSON.parse(snapshot.nps_ranges);
  delete snapshot.title; delete snapshot.artist;
  return snapshot;
}
$('generate-form').onsubmit = async e => {
  e.preventDefault(); showError('form-error', '');
  const chosen = [...document.querySelectorAll('input[name=difficulty]:checked')].map(x => x.value);
  if (!$('title').value.trim()) { showStep('music'); return showError('form-error', '先选择音乐并填写曲名。'); }
  if (!chosen.length) return showError('form-error', '请至少选择一个难度。');
  const npsRanges=npsRangeValues();for(const key of chosen){const range=npsRanges[key];if(!validNpsRange(range?.min,range?.max))return showError('form-error',`${key} 的 NPS 范围需满足 0.5–50，且下限不高于上限。`);}
  if (!selectedPatterns().length) return showError('form-error', '请至少选择一种排键方式。');
  if (pendingImport) return showError('form-error', '正在识别本地音乐，请等待完成。');
  if (!useReference && !selectedFile && !importedTrack) return showError('form-error', '请先选择本地音乐。');
  if (selectedFile?.size > 200 * 1048576) return showError('form-error', '音乐不能超过 200 MB。');
  let data;
  try { data = buildGenerationFormData(!useReference && !importedTrack); }
  catch(error) { showStep('music'); return showError('form-error',error.message); }
  $('generate').disabled = true; $('generate').textContent = '提交音乐…';
  try { const endpoint = importedTrack ? `/api/music/${importedTrack.id}/generate` : (useReference ? '/api/reference' : '/api/jobs'); const job = await request(endpoint, {method:'POST',body:data}); window.notifyGenerationChanged?.(); await watchJob(job.id); }
  catch (error) { showError('form-error', error.message); }
  finally { $('generate').disabled = !ready; $('generate').textContent = '生成 4K 曲包'; }
};
async function watchJob(id) {
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
    if (job.status === 'completed') { display(job); window.notifyGenerationChanged?.(); void history(); }
    else if (job.status === 'failed') { showError('job-error', job.error || '请重试或检查服务日志。'); displayQualityFailure(job); window.notifyGenerationChanged?.(); void history(); }
    else { timer = setTimeout(() => poll(id), 1800); }
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
  window.activeArcadeJob=job.id;window.SimpleSpectrum?.load(job.id);
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
  for (const text of ['难度', '音符数', '平均/秒', '峰值/秒', '目标 / 实际']) {const cell=document.createElement('th');cell.scope='col';cell.textContent=text;head.append(cell);}
  const thead=document.createElement('thead');thead.append(head);table.append(thead);const tbody=document.createElement('tbody');
  for(const rowData of chartRows){
    const id=rowData.chart_id||rowData.key,row=document.createElement('tr');row.dataset.key=id;
    const labelCell=document.createElement('td'),badge=document.createElement('span');badge.className='difficulty-badge';badge.dataset.key=rowData.difficulty||rowData.key;badge.textContent=difficultyLabel(rowData.label);labelCell.append(badge);row.append(labelCell);
    for(const value of [rowData.notes,Number(rowData.average_nps).toFixed(2),rowData.peak_nps]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}
    const densityCell=document.createElement('td');densityCell.textContent=window.MalodyDensity?.compact(rowData.density_validation)||'未评估';row.append(densityCell);tbody.append(row);
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
    await request(`/api/jobs/${activeJob}/charts/${encodeURIComponent(key)}/regenerate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({creator:snapshot.creator,seed:Number(snapshot.seed),rule:snapshot.difficulty_rules[selected?.difficulty||key]||{}})});
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
  if(!window.ChartArcade?.isActive())window.SimpleSpectrum?.render({displayTimeMs:(audio.currentTime||0)*1000},window.PlayfieldRenderer.geometry(w,h),5000/previewSpeed);
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
  historyController?.abort();
  const controller = new AbortController();
  historyController = controller;
  historyLoading = true;
  $('history-list').setAttribute('aria-busy','true');
  $('history-page').setAttribute('role','status');
  $('history-page').setAttribute('aria-live','polite');
  $('history-page').textContent = `正在加载第 ${historyPage} 页…`;
  $('history-prev').disabled = true; $('history-next').disabled = true;
  const params = new URLSearchParams({page:String(historyPage),page_size:'12',q:historyQuery,
    engine:$('history-engine').value,status:$('history-status').value,
    difficulty:$('history-difficulty').value,sort:$('history-sort').value});
  try {
    const result = await request('/api/history?' + params, {signal:controller.signal});
    if (number !== historyRequest) return;
    historyPage = result.page; libraryPageItems = result.items; $('history-count').textContent = result.total_all;
    $('history-page').textContent = `${result.page} / ${result.pages} 页 · ${result.total} 个曲包`;
    showError('history-error',libraryDeletionMessage());
    $('history-list').replaceChildren(...result.items.map(item => {
      const card=document.createElement('article'); card.className='history-card'; card.dataset.status=item.status; card.dataset.recordId=item.id;
      const coverUrl = item.cover || item.thumbnail;
      const cover=document.createElement(coverUrl?'img':'div'); cover.className='history-cover';
      if (coverUrl) { cover.referrerPolicy='no-referrer'; cover.src=coverSource(coverUrl); cover.alt=item.title+' 封面'; cover.loading='lazy'; }
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
      const open=document.createElement('button'); open.className='preview-action'; open.textContent='预览'; open.title=item.status==='completed'?'打开预览':'查看进度'; open.setAttribute('aria-label',open.title); open.onclick=()=>item.type==='advanced' ? window.AdvancedStudio?.openResult({projectId:item.project_id,assemblyId:item.assembly_id}) : watchJob(item.job_id || item.id); actions.append(open);
      if (item.status==='completed' && item.download) { const download=document.createElement('a'); download.href=item.download; download.textContent='下载'; download.title='下载 MCZ 曲包'; download.className='history-download'; actions.append(download); }
      const more=document.createElement('details');more.className='history-more';const summary=document.createElement('summary');summary.textContent='更多';more.append(summary);
      if(item.status==='completed'){const folder=document.createElement('button');folder.textContent='打开文件夹';folder.onclick=async()=>{try{await request('/api/library/open-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({record_id:item.id})});}catch(e){showError('history-error',e.message);}};actions.append(folder);}
      if (item.type!=='advanced' && ['completed','failed','interrupted'].includes(item.status)) { const rerun=document.createElement('button'); rerun.textContent=item.status==='completed'?'重生成':'重试'; rerun.title=item.status==='completed'?'按原设置重新生成':'按原设置重试'; rerun.setAttribute('aria-label',rerun.title); rerun.onclick=async()=>{ rerun.disabled=true; try { const job=await request(`/api/jobs/${item.job_id || item.id}/regenerate`,{method:'POST'}); await watchJob(job.id); } catch(error) { showError('history-error',error.message); } finally { rerun.disabled=false; } }; more.append(rerun); }
      if (item.status === 'completed' && item.download) { const remove=document.createElement('button'); remove.textContent='删除'; remove.className='danger-button'; remove.onclick=()=>confirmLibraryDelete([item]); more.append(remove); }
      if(more.children.length>1)actions.append(more);
      const state=document.createElement('small'); state.textContent=statusLabels[item.status]; state.className='history-state';
      if(item.status === 'completed' && item.download){const label=document.createElement('label'); label.className='library-card-select'; const check=document.createElement('input'); check.type='checkbox'; check.checked=librarySelection.has(item.id); check.setAttribute('aria-label','选择曲包 '+item.title); check.onchange=()=>{if(check.checked)librarySelection.set(item.id,item);else librarySelection.delete(item.id);updateLibrarySelection();}; label.append(check,document.createTextNode('选择')); art.append(label); }
      art.append(cover,engine,state); body.append(name,artist,levels,meta,actions); card.append(art,body); return card;
    }));
    updateLibrarySelection();
    if (!result.items.length) { const empty=document.createElement('p'); empty.className='history-empty'; empty.textContent=(historyQuery||$('history-engine').value||$('history-status').value||$('history-difficulty').value)?'没有匹配的曲包，试试清除筛选。':'还没有曲包，去工作台生成第一首。'; $('history-list').append(empty); }
    historyCommitted = {page:result.page, pages:result.pages, label:$('history-page').textContent};
  } catch (error) {
    if (number !== historyRequest) return;
    historyPage = historyCommitted?.page || 1;
    $('history-page').textContent = historyCommitted ? `${historyCommitted.label} · 加载失败` : '第 1 页 · 加载失败，请重试';
    showError('history-error', error.message);
  } finally {
    // An aborted request may settle after its replacement has started.
    if (number === historyRequest) {
      historyLoading = false;
      historyController = null;
      $('history-list').setAttribute('aria-busy','false');
      $('history-prev').disabled = !historyCommitted || historyCommitted.page <= 1;
      $('history-next').disabled = !historyCommitted || historyCommitted.page >= historyCommitted.pages;
    }
  }
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
  $('v32-difficulty').closest('.field').hidden=true;
  $('pattern-strength-field').hidden=v32;
  $('engine-hint').textContent=v32?'V32 按每档 NPS 范围自动映射难度条件，并结合 BPM 桶独立生成；不再设置共享母谱星级。':'MuG 生成共享母谱；这里的参考星级是模型输入，上方六档决定最终谱面规则。可继续调整风格、键型和采样方式。';
  const state=healthState?.engines?.[$('engine').value]; ready=Boolean(state?.ready)&&(!v32||healthState?.nps_star_direct_version>=1);
  const serviceReason=v32&&!(healthState?.nps_star_direct_version>=1)?'请重启本地制谱服务以加载独立生成版本':state?.reason;
  $('generate').disabled=!ready || Boolean(pendingImport) || !selectedPatterns().length || !document.querySelector('input[name="difficulty"]:checked');
  const statusText=ready?`本机已就绪 · ${state.label}`:(serviceReason || '点击检查本机生成环境');
  const statusTitle=ready?`当前设备：${healthState.gpu}`:(serviceReason || '查看本机模型状态');
  for (const button of [$('engine-state'),$('engine-state-mobile')]) { button.textContent=statusText; button.title=statusTitle; }
  syncPatternSelection(false);
  renderDiagnostics();
}
$('engine').onchange=()=>{ updateEngine(); persistPreferences(); };
$('preview-speed').oninput=()=>{ previewSpeed=Number($('preview-speed').value); syncPreviewSpeedControl(); persistPreferences(); drawChart(); };
for(const button of document.querySelectorAll('.preview-speed-presets button')) button.onclick=()=>{ $('preview-speed').value=button.dataset.speed; previewSpeed=Number(button.dataset.speed); syncPreviewSpeedControl(); persistPreferences(); drawChart(); };
$('preview-focus').onclick=()=>{ const on=document.body.classList.toggle('focus-preview'); $('preview-focus').setAttribute('aria-pressed',String(on)); $('preview-focus').textContent=on?'展开面板':'收起面板'; drawChart(); };
$('playfield').addEventListener('click', event=>{if (!window.ChartArcade?.isActive() && !event.target.closest('#arcade-panel')) togglePreviewPlayback();});
$('preview-toggle').addEventListener('click', event=>{ event.stopPropagation(); togglePreviewPlayback(); });
window.addEventListener('keydown',event=>{
  if (window.ChartArcade?.isActive() || window.AdvancedStudio?.isVisible() || $('studio-view').hidden) return;
  if (event.target.matches('input,select,textarea,button,[contenteditable="true"]')) return;
  if (event.code==='Space' && report) { event.preventDefault(); togglePreviewPlayback(); }
  if (report && event.key==='ArrowLeft') { event.preventDefault(); audio.currentTime=Math.max(0,audio.currentTime-5); }
  if (report && event.key==='ArrowRight') { event.preventDefault(); audio.currentTime=Math.min(audio.duration||report.duration,audio.currentTime+5); }
  if (event.key==='Escape' && document.body.classList.contains('focus-preview')) { document.body.classList.remove('focus-preview'); $('preview-focus').setAttribute('aria-pressed','false'); $('preview-focus').textContent='收起面板'; drawChart(); }
});
audio.addEventListener('play',()=>{ syncPreviewToggle(); requestAnimationFrame(animateChart); });
audio.addEventListener('pause',()=>{ syncPreviewToggle(); drawChart(); });
audio.addEventListener('ended',syncPreviewToggle);
$('engine-state').setAttribute('aria-expanded','false');
$('engine').onchange();
loadQueue();

function clearMusicSelection() { importedTrack = null; pendingImport = null; clearTimeout(musicTimer); $('generate').disabled = !ready; }
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
    $('queue-summary').hidden=!items.length;
    $('queue-concurrency-field').hidden=!queue.parallel_enabled;
    const parallelOption=$('queue-concurrency').querySelector('option[value="2"]');parallelOption.disabled=!queue.parallel_enabled;
    $('queue-concurrency').value=String(queue.concurrency||1);
    $('queue-resume').hidden=!queue.paused;
    $('queue-items').hidden=!items.length;
    $('queue-items').replaceChildren(...items.map(item=>{
      const card=document.createElement('article');card.className='queue-item';card.dataset.status=item.status;
      const order=document.createElement('span');order.className='queue-order';order.textContent=String(items.indexOf(item)+1).padStart(2,'0');
      const detail=document.createElement('div');detail.className='queue-item-detail';
      const title=document.createElement('strong');title.textContent=item.title;
      const artist=document.createElement('span');artist.textContent=item.artist||'未知音乐人';
      const state=document.createElement('small');state.textContent=`${statusLabels[item.status]||item.status} · ${item.message||''}${item.progress?` · ${item.progress}%`:''}`;
      const combinations=(item.variants||[]).map(v=>String(v).replace('--',' · ')).join('、');
      detail.append(title,artist);if(combinations){const variants=document.createElement('span');variants.textContent=combinations;detail.append(variants);}detail.append(state);card.append(order,detail);
      if(item.status==='running'){const progress=document.createElement('progress');progress.max=100;progress.value=item.progress||0;card.append(progress);}
      const actions=document.createElement('div');actions.className='queue-item-actions';
      if(['queued','paused'].includes(item.status)){const cancel=document.createElement('button');cancel.textContent='取消';cancel.onclick=async()=>{cancel.disabled=true;try{await request(`/api/jobs/${item.id}`,{method:'DELETE'});await loadQueue();}catch(error){showError('queue-error',error.message);cancel.disabled=false;}};actions.append(cancel);}
      card.append(actions);return card;
    }));
    showError('queue-error','');
  }catch(error){if(sequence===queueRequest&&!$('queue-view').hidden)showError('queue-error',error.message);}
  if(sequence===queueRequest&&!$('queue-view').hidden)queueTimer=setTimeout(loadQueue,1800);
}
window.notifyGenerationChanged = () => {
  if (!$('queue-view').hidden) loadQueue();
  window.TaskHistory?.refresh();
};
document.addEventListener('malody-jobs-changed', () => window.notifyGenerationChanged());
$('queue-refresh').onclick=()=>{loadQueue();window.TaskHistory?.refresh();};
$('queue-concurrency').onchange=async()=>{try{await request('/api/queue/concurrency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:Number($('queue-concurrency').value)})});await loadQueue();}catch(error){showError('queue-error',error.message);await loadQueue();}};
$('queue-resume').onclick=async()=>{const button=$('queue-resume');button.disabled=true;try{const result=await request('/api/queue/resume',{method:'POST'});if(result.needs_source.length)showError('queue-error',`${result.needs_source.length} 条旧任务缺少音乐来源，请重新导入。`);await loadQueue();}catch(error){showError('queue-error',error.message);}finally{button.disabled=false;}};
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

function libraryDeletionMessage(){return [...libraryDeletionErrors].filter(([id])=>librarySelection.has(id)).map(([,message])=>message).join('；');}
function updateLibrarySelection(){
  $('library-selected-count').textContent=`已选 ${librarySelection.size} 个曲包`;
  $('library-delete-selected').disabled=!librarySelection.size;
  showError('history-error',libraryDeletionMessage());
  for(const card of $('history-list').querySelectorAll('.history-card')) { const id=card.dataset.recordId; const check=card.querySelector('.library-card-select input'); if(check)check.checked=librarySelection.has(id); }
}
function clearLibrarySelection(){librarySelection.clear();libraryDeletionErrors.clear();updateLibrarySelection();}
function confirmLibraryDelete(items){libraryDeleteItems=items.slice();$('library-delete-list').replaceChildren(...items.map(item=>{const row=document.createElement('li');row.textContent=item.title+(item.artist?' — '+item.artist:'')+' · '+(item.engine==='advanced'?'高级制谱':'普通制谱');return row;}));showError('library-delete-error','');$('library-delete-dialog').showModal();}
$('library-select-page').onclick=()=>{for(const item of libraryPageItems)if(item.status==='completed'&&item.download)librarySelection.set(item.id,item);updateLibrarySelection();};
$('library-clear-selection').onclick=clearLibrarySelection;
$('library-delete-selected').onclick=()=>confirmLibraryDelete([...librarySelection.values()]);
$('library-delete-cancel').onclick=()=>$('library-delete-dialog').close();
$('library-delete-confirm').onclick=async()=>{
 const button=$('library-delete-confirm');button.disabled=true;$('library-delete-cancel').disabled=true;
 try{const result=await request('/api/library/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({record_ids:libraryDeleteItems.map(item=>item.id)})});const failures=[];
 for(const item of libraryDeleteItems){const outcome=(result.results||[]).find(x=>x.record_id===item.id);if(outcome?.deleted){librarySelection.delete(item.id);libraryDeletionErrors.delete(item.id);}else{librarySelection.set(item.id,item);const message=item.title+'：'+(outcome?.error||'未返回删除结果');libraryDeletionErrors.set(item.id,message);failures.push(message);}}
 updateLibrarySelection();$('library-delete-dialog').close();await history();await loadStorage();
 }catch(error){showError('library-delete-error',error.message);}finally{button.disabled=false;$('library-delete-cancel').disabled=false;updateLibrarySelection();}
};
$('library-delete-dialog').addEventListener('cancel',event=>{if($('library-delete-confirm').disabled)event.preventDefault();});
$('music-open-folder').onclick=async()=>{try{await request('/api/music-assets/open-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(error){showError('form-error',error.message);}};

$('download').onclick=async event=>{if(!activeJob)return;event.preventDefault();const link=$('download');if(link.dataset.exporting==='true')return;link.dataset.exporting='true';try{const result=await request(`/api/jobs/${activeJob}/export`,{method:'POST'});link.href=result.download;const save=document.createElement('a');save.href=result.download;save.download='';document.body.append(save);save.click();save.remove();await history();}catch(error){showError('job-error',error.message);}finally{link.dataset.exporting='false';}};
