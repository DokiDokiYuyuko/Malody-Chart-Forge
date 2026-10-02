const $ = id => document.getElementById(id);
let selectedFile = null, useReference = false, activeJob = null, report = null, difficulty = null, timer = null, ready = false;
let importedTrack = null, pendingImport = null, musicTimer = null, searchNumber = 0;
let historyPage = 1, historyQuery = '', historyRequest = 0, musicItems = [], musicPage = 1;
function showView(view) {
  $('studio-view').hidden = view !== 'studio'; $('library-view').hidden = view !== 'library';
  for (const name of ['studio','library']) { $('nav-'+name).classList.toggle('active', name === view); $('nav-'+name).setAttribute('aria-pressed', String(name === view)); }
  if (view === 'library') history(); else drawWaveform();
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
const audio = $('audio');
const statusLabels = {queued:'等待生成', running:'生成中', completed:'已完成', failed:'失败'};
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
  ['download','results','audio','seek-area'].forEach(x => $(x).hidden = true);
  $('preview-empty').hidden = false; $('transport-hint').hidden = false; $('difficulty-tabs').replaceChildren();
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
function display(job) {
  report = job.report;
  $('progress-area').hidden = true;
  $('result-empty').hidden = true;
  setCover(report.artwork?.status === 'ready' ? `/api/jobs/${job.id}/files/background.jpg` : null);
  $('download').href = job.download; $('download').download = ''; $('download').hidden = false;
  $('results').hidden = false; $('audio').hidden = false; $('seek-area').hidden = false;
  $('preview-empty').hidden = true; $('transport-hint').hidden = true;
  audio.src = `/api/jobs/${job.id}/files/audio.ogg`;
  $('seek').max = report.duration;
  $('difficulty-tabs').replaceChildren();
  for (const d of report.difficulties) {
    const button = document.createElement('button'); button.textContent = d.label; button.dataset.key = d.key;
    button.onclick = () => selectDifficulty(d.key); $('difficulty-tabs').append(button);
  }
  $('report-link').href = `/api/jobs/${job.id}/files/report.json`; $('report-link').download = 'report.json';
  $('chart-links').replaceChildren(...report.difficulties.map(d => link(d.label + ' .mc', `/api/jobs/${job.id}/files/${d.key}.mc`)));
  const table = document.createElement('table'); table.className = 'difficulty-summary';
  table.setAttribute('aria-label', '各难度实测物量对比');
  const head = document.createElement('tr');
  for (const text of ['难度', '音符', '均值/s', '峰值/s']) {
    const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = text; head.append(cell);
  }
  const thead = document.createElement('thead'); thead.append(head); table.append(thead);
  const tbody = document.createElement('tbody');
  for (const d of report.difficulties) {
    const row = document.createElement('tr');
    for (const value of [d.label, d.notes, d.average_nps, d.peak_nps]) {
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
  selectDifficulty(report.difficulties[0].key); drawWaveform();
}
function selectDifficulty(key) {
  difficulty = key;
  for (const b of $('difficulty-tabs').children) { b.classList.toggle('active', b.dataset.key === key); b.setAttribute('aria-pressed', String(b.dataset.key === key)); }
  const d = report.difficulties.find(x => x.key === key);
  const items = [['音符',d.notes,'个'],['长条',d.holds,'个'],['平均密度',d.average_nps,'/ 秒'],['峰值密度',d.peak_nps,'/ 秒']];
  $('stats').replaceChildren(...items.map(([label,value,unit]) => {
    const div = document.createElement('div'); div.className = 'stat';
    const small = document.createElement('small'); small.textContent = label;
    const strong = document.createElement('strong'); strong.textContent = value + ' ';
    const span = document.createElement('span'); span.textContent = unit; strong.append(span); div.append(small,strong); return div;
  }));
}
function formatTime(seconds) { const s = Math.max(0, Math.floor(seconds)); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); }
$('seek').oninput = () => { if (audio.src) audio.currentTime = Number($('seek').value); };
audio.addEventListener('timeupdate', () => { $('seek').value = audio.currentTime; $('time').textContent = formatTime(audio.currentTime); });
function canvasSize(canvas) {
  const rect = canvas.getBoundingClientRect(), scale = window.devicePixelRatio || 1;
  const width = Math.round(rect.width * scale), height = Math.round(rect.height * scale);
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext('2d'); ctx.setTransform(scale,0,0,scale,0,0); return [ctx,rect.width,rect.height];
}
function drawChart() {
  const [ctx,w,h] = canvasSize($('chart-canvas')); ctx.clearRect(0,0,w,h);
  const lane = w/4, hit = h-48, windowMs = 3000, now = (audio.currentTime || 0)*1000;
  for (let i=0;i<4;i++) { ctx.fillStyle = i%2 ? '#192f4e' : '#152945'; ctx.fillRect(i*lane,0,lane,h); ctx.strokeStyle = '#2c4261'; ctx.beginPath();ctx.moveTo(i*lane,0);ctx.lineTo(i*lane,h);ctx.stroke(); }
  ctx.strokeStyle='#a8bfdf';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(0,hit);ctx.lineTo(w,hit);ctx.stroke();
  if (report && difficulty) {
    const notes = report.previews[difficulty] || [];
    for (const [start,col,end] of notes) {
      if ((end || start) < now-200 || start > now+windowMs) continue;
      const y = hit-(start-now)/windowMs*hit, x=col*lane+lane*.13, width=lane*.74;
      const color = col===0 || col===3 ? '#73a3ff' : '#ef9cbe';
      if(end) { const top = hit-(end-now)/windowMs*hit; ctx.globalAlpha=.3;ctx.fillStyle=color;ctx.fillRect(x,top,width,y-top);ctx.globalAlpha=1;ctx.fillRect(x,top,width,4); }
      ctx.fillStyle=color;ctx.fillRect(x,y-5,width,10);
    }
  }
  requestAnimationFrame(drawChart);
}
function drawWaveform() {
  const [ctx,w,h] = canvasSize($('waveform')); ctx.clearRect(0,0,w,h);
  if (!report) return;
  const values = report.analysis.waveform; ctx.fillStyle='#7796bf';
  values.forEach((v,i) => { const height=Math.max(2,v*(h-10));ctx.fillRect(i*w/values.length,(h-height)/2,Math.max(1,w/values.length-1),height); });
}
window.addEventListener('resize',drawWaveform);
async function history() {
  const number = ++historyRequest;
  try {
    const result = await request(`/api/history?page=${historyPage}&page_size=6&q=${encodeURIComponent(historyQuery)}`);
    if (number !== historyRequest) return;
    historyPage = result.page; $('history-count').textContent = result.total_all;
    $('history-page').textContent = `${result.page} / ${result.pages} 页 · ${result.total} 个曲包`;
    $('history-prev').disabled = result.page <= 1; $('history-next').disabled = result.page >= result.pages;
    showError('history-error','');
    $('history-list').replaceChildren(...result.items.map(item => {
      const card=document.createElement('article'); card.className='history-card';
      const coverUrl = item.cover || item.thumbnail;
      const cover=document.createElement(coverUrl?'img':'div'); cover.className='history-cover';
      if (coverUrl) { cover.src=coverUrl; cover.alt=item.title+' 封面'; cover.loading='lazy'; }
      else { cover.classList.add('history-cover-placeholder'); cover.textContent='4K'; }
      const body=document.createElement('div'); body.className='history-body';
      const name=document.createElement('h2'); name.textContent=item.title;
      const meta=document.createElement('p'); meta.textContent=`${item.engine==='v32'?'V32':'MuG'} / ${item.difficulties.join(', ') || statusLabels[item.status]} / ${new Date(item.created).toLocaleDateString('zh-CN')}`; meta.title=meta.textContent;
      const actions=document.createElement('div'); actions.className='history-actions';
      const button=document.createElement('button'); button.textContent=item.status==='completed'?'打开预览':'查看进度';
      const state=document.createElement('small'); state.textContent=statusLabels[item.status];
      button.onclick=()=>watchJob(item.id); actions.append(button,state); body.append(name,meta,actions); card.append(cover,body); return card;
    }));
    if (!result.items.length) { const empty=document.createElement('p'); empty.className='history-empty'; empty.textContent=historyQuery?'没有匹配的曲包，试试其他曲名。':'还没有曲包，去工作台生成第一首。'; $('history-list').append(empty); }
  } catch (error) { showError('history-error', error.message); }
}
async function connect() {
  try { const health=await request('/api/health'); ready=health.ready; $('generate').disabled=!ready;
    $('engine-state').textContent=ready ? '本机生成已就绪' : '模型正在准备'; $('engine-state').title = health.gpu;
    $('reference').hidden=!health.reference;
    for (const option of $('engine').options) {
      const item = health.engines?.[option.value];
      if (item) { option.disabled = !item.ready; option.textContent = item.label + (item.ready ? '' : ' · 准备中'); }
    }
    if ($('engine').selectedOptions[0].disabled && health.engines.mug.ready) $('engine').value = 'mug';
    updateEngine();
    if(!ready) setTimeout(connect,5000);
  } catch { $('engine-state').textContent='生成服务未连接';setTimeout(connect,5000); }
}
connect();history();drawChart();
function updateEngine() {
  const v32 = $('engine').value === 'v32';
  $('steps-field').hidden = v32;
  $('bpm').disabled = v32;
  $('mug-settings').hidden = v32; $('v32-settings').hidden = !v32;
  $('pattern-strength-field').hidden = v32;
  $('engine-hint').textContent = v32 ? 'V32 生成共享母谱；这里的参考难度是模型输入，上方六档决定最终谱面规则。' : 'MuG 生成共享母谱；这里的参考星级是模型输入，上方六档决定最终谱面规则。可继续调整风格、键型和采样方式。';
}
$('engine').onchange = updateEngine;

function clearMusicSelection() {
  importedTrack = null; pendingImport = null; clearTimeout(musicTimer);
  $('music-selected').hidden = true; $('music-preview').pause(); $('music-preview').removeAttribute('src');
  $('generate').disabled = !ready;
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
