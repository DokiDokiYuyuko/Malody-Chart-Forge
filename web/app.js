const $ = id => document.getElementById(id);
let selectedFile = null, useReference = false, activeJob = null, report = null, difficulty = null, timer = null, ready = false;
let importedTrack = null, pendingImport = null, musicTimer = null, searchNumber = 0;
let historyPage = 1, historyQuery = '', historyRequest = 0, musicItems = [], musicPage = 1;
let healthState = null, previewSpeed = 1, previewNoteCache = null;
const emptyPreviewNotes = [];
function showView(view) {
  $('studio-view').hidden = view !== 'studio'; $('library-view').hidden = view !== 'library';
  for (const name of ['studio','library']) { $('nav-'+name).classList.toggle('active', name === view); $('nav-'+name).setAttribute('aria-pressed', String(name === view)); }
  if (view === 'library') { history(); loadStorage(); } else { drawChart(); }
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
$('reset-settings').onclick = () => { const currentEngine=$('engine').value; applyPreferences(preferenceDefaults); if ($('engine').selectedOptions[0]?.disabled) $('engine').value=currentEngine; syncSettingsDisplays(); previewSpeed=Number($('preview-speed').value); syncPreviewSpeedControl(); setSettingsMode(false,false); updateDifficultyRuleVisibility(); updateEngine(); persistPreferences(); drawChart(); };
const audio = $('audio');
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
    try { await audio.play(); } catch { $('preview-toggle').title = '音乐暂时无法播放'; }
  } else audio.pause();
}
const statusLabels = {queued:'等待生成', running:'生成中', completed:'已完成', failed:'失败'};
const preferenceIds = ['ln-ratio','pattern','pattern-strength','engine','steps','mug-difficulty','mug-style','mug-guidance','mug-eta','v32-difficulty','v32-temperature','v32-top-p','v32-column-temperature','v32-cfg-scale','v32-year','v32-descriptors','v32-negative-descriptors','bpm','seed','preview-speed'];
function readPreferences() { return {version:1, mode:$('mode-custom')?.getAttribute('aria-pressed') === 'true', values:Object.fromEntries(preferenceIds.map(id => [id,$(id)?.value]).filter(([,value]) => value !== undefined)), difficulties:[...document.querySelectorAll('input[name="difficulty"]:checked')].map(input => input.value), rules:Object.fromEntries([...document.querySelectorAll('[data-difficulty][data-rule]')].map(input => [`${input.dataset.difficulty}.${input.dataset.rule}`,input.value]))}; }
const preferenceDefaults = readPreferences();
function applyPreferences(values) { if (!values || values.version !== 1) return; for (const [id,value] of Object.entries(values.values || {})) if ($(id) && value !== undefined) $(id).value = value; if (Array.isArray(values.difficulties)) for (const input of document.querySelectorAll('input[name="difficulty"]')) input.checked = values.difficulties.includes(input.value); for (const input of document.querySelectorAll('[data-difficulty][data-rule]')) { const value=values.rules?.[`${input.dataset.difficulty}.${input.dataset.rule}`]; if (value !== undefined) input.value=value; } }
function persistPreferences() { try { localStorage.setItem('malody-chart-forge.preferences.v1', JSON.stringify(readPreferences())); } catch {} }
function setSettingsMode(custom, save=true) { $('settings-panel').classList.toggle('recommended-mode', !custom); $('mode-recommended').classList.toggle('selected', !custom); $('mode-custom').classList.toggle('selected', custom); $('mode-recommended').setAttribute('aria-pressed', String(!custom)); $('mode-custom').setAttribute('aria-pressed', String(custom)); for (const detail of document.querySelectorAll('#settings-panel details.advanced')) detail.open = custom; if (save) persistPreferences(); }
try { const saved=JSON.parse(localStorage.getItem('malody-chart-forge.preferences.v1') || 'null'); applyPreferences(saved); setSettingsMode(Boolean(saved?.mode), false); } catch { setSettingsMode(false, false); }
function syncSettingsDisplays() {
  $('ln-value').textContent=Math.round(Number($('ln-ratio').value)*100)+'%';
  $('pattern-strength-value').textContent=$('pattern-strength').value;
  $('preview-speed-value').textContent=Number($('preview-speed').value).toFixed(1)+'×';
  for (const input of document.querySelectorAll('[data-rule="rate"]')) {
    const target=document.querySelector(`input[name="difficulty"][value="${input.dataset.difficulty}"]`)?.closest('label')?.querySelector('em');
    if (target) target.textContent=Number(input.value).toFixed(1).replace(/\.0$/,'')+' NPS';
  }
  for (const [id,digits] of [['mug-guidance',1],['mug-eta',2],['v32-temperature',2],['v32-top-p',2],['v32-column-temperature',2],['v32-cfg-scale',2]]) $(id+'-value').textContent=Number($(id).value).toFixed(digits);
}
syncSettingsDisplays(); previewSpeed=Number($('preview-speed').value);
$('preview-speed-value').textContent=previewSpeed.toFixed(1)+'×';
function syncPreviewSpeedControl() {
  const slider=$('preview-speed'), value=Number(slider.value), percent=(value-Number(slider.min))/(Number(slider.max)-Number(slider.min))*100;
  slider.style.setProperty('--speed-progress',`${percent}%`);
  $('preview-speed-value').textContent=value.toFixed(1)+'×';
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
$('generate-form').onsubmit = async e => {
  e.preventDefault(); showError('form-error', '');
  const chosen = [...document.querySelectorAll('input[name=difficulty]:checked')].map(x => x.value);
  if (!$('title').value.trim()) { showStep('music'); return showError('form-error', '先选择音乐并填写曲名。'); }
  if (!chosen.length) return showError('form-error', '请至少选择一个难度。');
  if (pendingImport) return showError('form-error', '音乐仍在下载，请等待完成。');
  if (!useReference && !selectedFile && !importedTrack) return showError('form-error', '请先上传音乐，或搜索并导入一首音乐。');
  if (selectedFile?.size > 200 * 1048576) return showError('form-error', '音乐不能超过 200 MB。');
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
  data.append('difficulty_rules', JSON.stringify(rules));
  data.append('difficulties', JSON.stringify(chosen)); data.append('ln_ratio', $('ln-ratio').value);
  if ($('artwork-url').value && !useReference && !importedTrack) data.append('artwork_url', $('artwork-url').value.trim());
  if (!$('bpm').disabled && $('bpm').value) data.append('bpm', $('bpm').value);
  if (!useReference && !importedTrack) data.append('file', selectedFile);
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
  $('preview-empty').hidden = false; $('difficulty-tabs').replaceChildren();
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
    else if (job.status === 'failed') { showError('job-error', job.error || '请重试或检查服务日志。'); await history(); }
    else { timer = setTimeout(() => poll(id), 1800); await history(); }
  } catch (error) { showError('job-error', '无法连接服务：' + error.message); timer = setTimeout(() => poll(id), 5000); }
}
function link(text, href) { const a = document.createElement('a'); a.textContent = text; a.href = href; a.download = ''; return a; }
function difficultyLabel(label) { return String(label||'').replace(/\s*[·•]\s*发狂$/,'').trim(); }
function display(job) {
  report = job.report;
  $('progress-area').hidden = true;
  $('result-empty').hidden = true;
  setCover(report.artwork?.status === 'ready' ? `/api/jobs/${job.id}/files/background.jpg` : null);
  $('download').href = job.download; $('download').download = ''; $('download').hidden = false;
  $('results').hidden = false; $('audio').hidden = false; $('seek-area').hidden = false; $('preview-toggle').hidden = false;
  $('preview-empty').hidden = true;
  audio.src = `/api/jobs/${job.id}/files/audio.ogg`;
  $('seek').max = report.duration; $('seek').value = 0; $('duration').textContent=formatTime(report.duration); $('time').textContent='0:00'; audio.currentTime = 0; syncSeekControl();
  $('difficulty-tabs').replaceChildren();
  for (const d of report.difficulties) {
    const button = document.createElement('button'); button.textContent = difficultyLabel(d.label); button.dataset.key = d.key;
    button.onclick = () => selectDifficulty(d.key); $('difficulty-tabs').append(button);
  }
  $('report-link').href = `/api/jobs/${job.id}/files/report.json`; $('report-link').download = 'report.json';
  $('chart-links').replaceChildren(...report.difficulties.map(d => {
    const a = link(difficultyLabel(d.label) + ' .mc', `/api/jobs/${job.id}/files/${d.key}.mc`);
    a.dataset.key = d.key; a.setAttribute('aria-label', `下载 ${difficultyLabel(d.label)} 谱面 .mc`); return a;
  }));
  const table = document.createElement('table'); table.className = 'difficulty-summary';
  table.setAttribute('aria-label', '各难度实测物量对比');
  const head = document.createElement('tr');
  for (const text of ['难度', '音符数', '平均/秒', '峰值/秒']) {
    const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = text; head.append(cell);
  }
  const thead = document.createElement('thead'); thead.append(head); table.append(thead);
  const tbody = document.createElement('tbody');
  for (const d of report.difficulties) {
    const row = document.createElement('tr'); row.dataset.key = d.key;
    const labelCell = document.createElement('td'), badge = document.createElement('span');
    badge.className = 'difficulty-badge'; badge.dataset.key = d.key; badge.textContent = difficultyLabel(d.label);
    labelCell.append(badge); row.append(labelCell);
    for (const value of [d.notes, Number(d.average_nps).toFixed(2), d.peak_nps]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    tbody.append(row);
  }
  table.append(tbody);
  let summary = $('difficulty-summary');
  if (!summary) { summary = document.createElement('div'); summary.id = 'difficulty-summary'; $('stats').after(summary); }
  summary.replaceChildren(table);
  $('warnings').replaceChildren(...report.warnings.map(w => { const li = document.createElement('li'); li.textContent = w; return li; }));
  $('preview-status').textContent = `${report.engine.startsWith('Mapperatorinator') ? 'V32 / 分段 BPM' : report.bpm + ' BPM'} / ${formatTime(report.duration)}`;
  selectDifficulty(report.difficulties[0].key); syncPreviewToggle();
}
function selectDifficulty(key) {
  difficulty = key;
  for (const b of $('difficulty-tabs').children) { b.classList.toggle('active', b.dataset.key === key); b.setAttribute('aria-pressed', String(b.dataset.key === key)); }
  for (const row of document.querySelectorAll('.difficulty-summary tbody tr')) {
    const selected = row.dataset.key === key; row.classList.toggle('is-current', selected);
    if (selected) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
  }
  const d = report.difficulties.find(x => x.key === key);
  const items = [['音符',d.notes,'个'],['长条',d.holds,'个'],['平均密度',Number(d.average_nps).toFixed(2),'音符/秒'],['一秒峰值',d.peak_nps,'音符/秒']];
  $('stats').replaceChildren(...items.map(([label,value,unit]) => {
    const div = document.createElement('div'); div.className = 'stat';
    const small = document.createElement('small'); small.textContent = label;
    const strong = document.createElement('strong'); strong.textContent = value + ' ';
    const span = document.createElement('span'); span.textContent = unit; strong.append(span); div.append(small,strong); return div;
  })); drawChart();
}
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
  const [ctx,w,h]=canvasSize($('chart-canvas')); ctx.clearRect(0,0,w,h);
  if (!w || !h) return;
  const lane=w/4, railTop=28, hit=h-78, runway=Math.max(1,hit-railTop), now=(audio.currentTime||0)*1000;
  const travelMs=5000/previewSpeed;
  const night=document.documentElement.dataset.theme==='dark';
  const laneColors=['#65c9dc','#84aef2','#dc8bb7','#ef7eaa'];
  const laneEdges=['#91e6f0','#b4ceff','#f4b4d3','#ffaac8'];
  const yFor=time=>hit-(time-now)/travelMs*runway;
  const roundedRect=(x,y,width,height,radius)=>{ctx.beginPath();ctx.roundRect(x,y,width,height,Math.min(radius,width/2,height/2));};
  ctx.fillStyle=night?'#07100e':'#081522'; ctx.fillRect(0,0,w,h);
  ctx.fillStyle=night?'#0a1913':'#0b1b2a'; ctx.fillRect(0,0,w,railTop);
  for (let i=0;i<4;i++) {
    const x=i*lane;
    ctx.fillStyle=night?(i%2?'#112b22':'#0d221c'):(i%2?'#12293b':'#102437'); ctx.fillRect(x,railTop,lane,hit-railTop+18);
    const wash=ctx.createLinearGradient(0,railTop,0,hit);
    wash.addColorStop(0,'rgba(255,255,255,.012)'); wash.addColorStop(1,'rgba(0,0,0,.08)');
    ctx.fillStyle=wash; ctx.fillRect(x,railTop,lane,hit-railTop+18);
  }
  const sourceNotes=report&&difficulty?(report.previews[difficulty]||emptyPreviewNotes):emptyPreviewNotes;
  if(previewNoteCache?.source!==sourceNotes){
    const laneNotes=[[],[],[],[]], chartNotes=sourceNotes.map(([start,col,end])=>({start:Number(start),col:Number(col),end:end?Number(end):null,spacingMs:Infinity}));
    for(const note of chartNotes)if(note.col>=0&&note.col<4)laneNotes[note.col].push(note);
    for(const items of laneNotes){
      items.sort((a,b)=>a.start-b.start);
      for(let i=0;i<items.length;i++){
        const before=i?items[i].start-items[i-1].start:Infinity,after=i<items.length-1?items[i+1].start-items[i].start:Infinity;
        items[i].spacingMs=Math.min(before,after);
      }
    }
    previewNoteCache={source:sourceNotes,chartNotes,laneNotes};
  }
  const {chartNotes,laneNotes}=previewNoteCache;
  // Faint second marks give the scroll a readable reference without turning the field into a grid.
  ctx.lineWidth=1; ctx.font='10px Bahnschrift, sans-serif'; ctx.textAlign='left'; ctx.textBaseline='middle';
  const firstMark=Math.floor(now/1000)*1000+1000;
  for(let mark=firstMark;mark<=now+travelMs;mark+=1000){
    const y=yFor(mark); if(y<railTop||y>hit)continue;
    ctx.strokeStyle='rgba(179,205,226,.12)'; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke();
    ctx.fillStyle='rgba(176,199,218,.48)'; ctx.fillText('+'+Math.round((mark-now)/1000)+'s',8,y-7);
  }
  for(let i=0;i<=4;i++){
    const x=i*lane; ctx.beginPath(); ctx.moveTo(x,railTop); ctx.lineTo(x,hit+18);
    ctx.strokeStyle=i===2?'rgba(169,197,221,.42)':'rgba(108,145,174,.25)'; ctx.lineWidth=i===2?2:1; ctx.stroke();
  }
  if(report&&difficulty){
    // Draw hold bodies first so their rounded note heads remain crisp and readable.
    for(const note of chartNotes){
      const {start,col,end}=note, finish=Math.max(start,end||start);
      if(!end||finish<now-50||start>now+travelMs)continue;
      const x=col*lane+lane*.20,width=lane*.60,top=yFor(finish),bottom=Math.min(hit+9,yFor(start));
      if(top>=hit+9||bottom<=railTop)continue;
      const y=Math.max(railTop,top), bodyBottom=Math.min(hit+9,bottom), bodyHeight=Math.max(2,bodyBottom-y);
      const color=laneColors[col]||laneColors[0], edge=laneEdges[col]||laneEdges[0];
      ctx.fillStyle='rgba(5,14,23,.62)'; roundedRect(x-2,y-1,width+4,bodyHeight+2,7); ctx.fill();
      ctx.fillStyle=color+'55'; roundedRect(x,y,width,bodyHeight,5); ctx.fill();
      ctx.strokeStyle=color+'aa'; ctx.lineWidth=1; roundedRect(x+.5,y+.5,width-1,Math.max(2,bodyHeight-1),5); ctx.stroke();
      ctx.fillStyle=edge; roundedRect(x+2,y+1,Math.max(2,width-4),Math.min(3,bodyHeight),2); ctx.fill();
    }
    for(let col=0;col<4;col++){
      const items=laneNotes[col], color=laneColors[col], edge=laneEdges[col], x=col*lane+lane*.18, width=lane*.64;
      for(const note of items){
        const {start}=note;
        if(start<now-20||start>now+travelMs)continue;
        const y=yFor(start); if(y<railTop-12||y>hit+5)continue;
        const spacingPx=note.spacingMs*runway/travelMs;
        const headHeight=Math.max(3.5,Math.min(19,Number.isFinite(spacingPx)?spacingPx*.78:15));
        const headY=y-headHeight/2;
        ctx.fillStyle='rgba(3,12,20,.54)'; roundedRect(x-2,headY-1,width+4,headHeight+2,Math.min(6,headHeight/2)); ctx.fill();
        ctx.fillStyle=color; roundedRect(x,headY,width,headHeight,Math.min(5,headHeight/2)); ctx.fill();
        ctx.strokeStyle=edge+'b8'; ctx.lineWidth=1; roundedRect(x+.5,headY+.5,width-1,Math.max(2,headHeight-1),Math.min(5,headHeight/2)); ctx.stroke();
        if(headHeight>7){ctx.fillStyle='rgba(255,255,255,.24)'; roundedRect(x+3,headY+1,Math.max(2,width-6),1.5,1); ctx.fill();}
      }
    }
  }
  // Judgment rail and compact keycaps echo Malody's four-lane playfield.
  ctx.fillStyle='#8caac2'; ctx.fillRect(0,hit,w,1);
  ctx.fillStyle='rgba(140,170,196,.42)'; ctx.fillRect(0,hit+1,w,1);
  const deck=ctx.createLinearGradient(0,hit+2,0,h); deck.addColorStop(0,night?'#102b20':'#10263a');deck.addColorStop(1,night?'#08170f':'#0b1927');
  ctx.fillStyle=deck; ctx.fillRect(0,hit+2,w,h-hit-2);
  for(let i=0;i<4;i++){
    const x=i*lane+lane*.25, width=lane*.50, y=hit+20, height=Math.min(34,h-hit-31), color=laneColors[i], edge=laneEdges[i];
    ctx.fillStyle='rgba(0,0,0,.38)'; roundedRect(x-1,y+2,width+2,height,8); ctx.fill();
    ctx.fillStyle=color+'27'; roundedRect(x,y,width,height,7); ctx.fill();
    ctx.strokeStyle=color+'78'; ctx.lineWidth=1; roundedRect(x+.5,y+.5,width-1,height-1,7); ctx.stroke();
    ctx.fillStyle=edge; roundedRect(x+5,y+2,width-10,2,1); ctx.fill();
    ctx.fillStyle='#d9e5ef'; ctx.font='600 12px Bahnschrift, sans-serif'; ctx.textAlign='center'; ctx.textBaseline='middle'; ctx.fillText(String(i+1),i*lane+lane/2,y+height/2+1);
  }
  ctx.fillStyle='#9db4c8'; ctx.font='600 10px Bahnschrift, sans-serif'; ctx.textAlign='left'; ctx.textBaseline='middle';
  ctx.fillText(`4K 预览  ·  ${previewSpeed.toFixed(1)}×`,12,14);
}
function animateChart(){
  syncSeekControl();
  setMascotFrame(window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : Math.floor(performance.now()/110)%8);
  drawChart();
  if(!audio.paused) requestAnimationFrame(animateChart);
}
window.addEventListener('resize',()=>{drawChart();syncSeekControl();const frame=Math.max(0,mascotFrame);mascotFrame=-1;setMascotFrame(frame);});
window.addEventListener('appearancechange',drawChart);
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
  const params = new URLSearchParams({page:String(historyPage),page_size:'6',q:historyQuery,
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
      const engine=document.createElement('span'); engine.className='history-engine'; engine.textContent=item.engine==='v32'?'V32':'MuG';
      const meta=document.createElement('p'); meta.className='history-meta';
      const created=document.createElement('time'); created.dateTime=item.created; created.textContent=new Date(item.created).toLocaleDateString('zh-CN'); meta.append(created);
      const levels=document.createElement('div'); levels.className='history-difficulties'; levels.setAttribute('aria-label','曲包包含的难度');
      const keys=item.difficulties || [], labels=item.difficulty_labels?.length ? item.difficulty_labels : keys;
      labels.forEach((label,index)=>{ const badge=document.createElement('span'); badge.className='difficulty-badge'; const key=keys[index] || ''; badge.dataset.key=key==='normal'?'medium':key; badge.textContent=difficultyLabel(label); levels.append(badge); });
      const actions=document.createElement('div'); actions.className='history-actions';
      const open=document.createElement('button'); open.className='preview-action'; open.textContent=item.status==='completed'?'打开预览':'查看进度'; open.onclick=()=>watchJob(item.id); actions.append(open);
      if (item.status==='completed' && item.download) { const download=document.createElement('a'); download.href=item.download; download.textContent='下载 MCZ'; download.className='history-download'; actions.append(download); }
      if (['completed','failed'].includes(item.status)) { const rerun=document.createElement('button'); rerun.textContent=item.status==='completed'?'按原设置再生成':'按原设置重试'; rerun.onclick=async()=>{ rerun.disabled=true; try { const job=await request(`/api/jobs/${item.id}/regenerate`,{method:'POST'}); await watchJob(job.id); } catch(error) { showError('history-error',error.message); } finally { rerun.disabled=false; } }; actions.append(rerun); }
      if (!['queued','running'].includes(item.status)) { const remove=document.createElement('button'); remove.textContent='删除'; remove.className='danger-button'; remove.onclick=async()=>{ if (!window.confirm(`删除“${item.title}”的曲包和报告，并清理本条记录独占的原始上传音乐？共享的在线下载音乐库不会受影响。此操作无法撤销。`)) return; remove.disabled=true; try { await request(`/api/jobs/${item.id}`,{method:'DELETE'}); await history(); await loadStorage(); } catch(error) { showError('history-error',error.message); remove.disabled=false; } }; actions.append(remove); }
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
  $('generate').disabled=!ready || Boolean(pendingImport);
  const statusText=ready?`本机已就绪 · ${state.label}`:(state?.reason || '点击检查本机生成环境');
  const statusTitle=ready?`当前设备：${healthState.gpu}`:(state?.reason || '查看本机模型状态');
  for (const button of [$('engine-state'),$('engine-state-mobile')]) { button.textContent=statusText; button.title=statusTitle; }
  renderDiagnostics();
}
$('engine').onchange=()=>{ updateEngine(); persistPreferences(); };
$('preview-speed').oninput=()=>{ previewSpeed=Number($('preview-speed').value); syncPreviewSpeedControl(); persistPreferences(); drawChart(); };
for(const button of document.querySelectorAll('.preview-speed-presets button')) button.onclick=()=>{ $('preview-speed').value=button.dataset.speed; previewSpeed=Number(button.dataset.speed); syncPreviewSpeedControl(); persistPreferences(); drawChart(); };
$('preview-focus').onclick=()=>{ const on=document.body.classList.toggle('focus-preview'); $('preview-focus').setAttribute('aria-pressed',String(on)); $('preview-focus').textContent=on?'退出专注预览':'专注预览'; drawChart(); };
$('playfield').addEventListener('click', togglePreviewPlayback);
$('preview-toggle').addEventListener('click', event=>{ event.stopPropagation(); togglePreviewPlayback(); });
window.addEventListener('keydown',event=>{
  if (event.target.matches('input,select,textarea,button,[contenteditable="true"]')) return;
  if (event.code==='Space' && report) { event.preventDefault(); if (audio.paused) audio.play(); else audio.pause(); }
  if (report && event.key==='ArrowLeft') { event.preventDefault(); audio.currentTime=Math.max(0,audio.currentTime-5); }
  if (report && event.key==='ArrowRight') { event.preventDefault(); audio.currentTime=Math.min(audio.duration||report.duration,audio.currentTime+5); }
  if (event.key==='Escape' && document.body.classList.contains('focus-preview')) { document.body.classList.remove('focus-preview'); $('preview-focus').setAttribute('aria-pressed','false'); $('preview-focus').textContent='专注预览'; drawChart(); }
});
audio.addEventListener('play',()=>{ syncPreviewToggle(); requestAnimationFrame(animateChart); });
audio.addEventListener('pause',()=>{ syncPreviewToggle(); drawChart(); });
audio.addEventListener('ended',syncPreviewToggle);
$('engine-state').setAttribute('aria-expanded','false');
$('engine').onchange();

function clearMusicSelection() {
  importedTrack = null; pendingImport = null; clearTimeout(musicTimer);
  $('music-selected').hidden = true; $('music-preview').pause(); $('music-preview').removeAttribute('src');
  $('generate').disabled = !ready || Boolean(pendingImport);
}
function musicDuration(seconds) { return seconds ? formatTime(seconds) : '时长待检查'; }
function showMusicResults(items) {
  musicItems = items; musicPage = 1; renderMusicResults();
}
function renderMusicResults() {
  const pages = Math.max(1, Math.ceil(musicItems.length/4));
  $('music-pagination').hidden = musicItems.length <= 4;
  $('music-page').textContent = `${musicPage} / ${pages}`;
  $('music-prev').disabled = musicPage <= 1; $('music-next').disabled = musicPage >= pages;
  $('music-results').replaceChildren(...musicItems.slice((musicPage-1)*4,musicPage*4).map(item => {
    const row = document.createElement('article'); row.className = 'music-result';
    const image=document.createElement('img'); image.src=item.thumbnail || `https://i.ytimg.com/vi/${item.id}/hqdefault.jpg`; image.alt=''; image.loading='lazy';
    const description=document.createElement('div');
    const title = document.createElement('strong'); title.textContent = item.title;
    const meta = document.createElement('p'); meta.textContent = `${item.artist || '未知音乐人'} / ${musicDuration(item.duration)}`;
    const actions = document.createElement('div'); actions.className = 'music-actions';
    const source = document.createElement('a'); source.textContent = '查看音源'; source.href = item.url; source.target = '_blank'; source.rel = 'noopener noreferrer';
    const button = document.createElement('button'); button.type = 'button'; button.textContent = item.status === 'ready' ? '使用这首音乐' : '下载并导入';
    button.onclick = () => importMusic(item); actions.append(source,button); description.append(title,meta); row.append(image,description,actions); return row;
  }));
}
$('music-prev').onclick=()=>{musicPage--;renderMusicResults();};
$('music-next').onclick=()=>{musicPage++;renderMusicResults();};
$('music-search-form').onsubmit = async e => {
  e.preventDefault(); const number = ++searchNumber; showError('music-error','');
  $('music-search-button').disabled = true; $('music-search-status').textContent = '正在搜索音乐…';
  try { const result = await request('/api/music/search?q=' + encodeURIComponent($('music-query').value.trim()));
    if(number !== searchNumber) return;
    showMusicResults(result.results); $('music-search-status').textContent = result.results.length ? `找到 ${result.results.length} 个音源，请确认曲名和版本。` : '没有找到 5 秒至 10 分钟的音源，试试音乐人和原文曲名。';
  } catch(error) { if(number === searchNumber) { showError('music-error',error.message); $('music-search-status').textContent = ''; } }
  finally { if(number === searchNumber) $('music-search-button').disabled = false; }
};
$('music-library').onclick = async () => {
  ++searchNumber; $('music-search-button').disabled = false; showError('music-error','');
  try { const items = await request('/api/music'); showMusicResults(items); $('music-search-status').textContent = items.length ? '已下载的音乐可重复使用。' : '还没有下载音乐，先搜索一首歌。'; }
  catch(error) { showError('music-error',error.message); }
};
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
