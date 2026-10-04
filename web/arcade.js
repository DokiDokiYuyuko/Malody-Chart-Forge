(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const dialog = $('arcade-panel'), audio = $('arcade-audio'), canvas = $('arcade-canvas');
  $('playfield').append(dialog);
  const renderer = window.PlayfieldRenderer, effects = renderer.createEffects();
  const laneNames = ['D', 'F', 'J', 'K'], codes = ['KeyD', 'KeyF', 'KeyJ', 'KeyK'];
  const windows = {perfect: 36, great: 72, good: 108};
  const autoReleaseAt = [0, 0, 0, 0];
  let active = false, loadController = null, loadGeneration = 0, autoplay = false, autoCursor = 0;
  let activeHost = $('playfield'), speedSource = null, callbacks = {}, musicLease = null;
  let state = 'idle', notes = [], chartInfo = null, frame = 0, countdownTimer = 0, feedbackTimer = 0;
  let endTime = 0, combo = 0, maxCombo = 0, counts = {Perfect: 0, Great: 0, Good: 0, Miss: 0};
  let pressed = new Set(), held = new Map(), clockAnchor = null, clockValid = false, frozenMediaMs = 0, endedAtPerf = 0;
  dialog.addEventListener('click', event => event.stopPropagation());

  function beatValue(value) {
    if (!Array.isArray(value) || value.length !== 3) return 0;
    return Number(value[0]) + Number(value[1]) / Number(value[2] || 1);
  }
  function beatToMs(target, points) {
    let elapsed = 0, previous = 0, bpm = Number(points[0]?.bpm) || 120;
    for (let i = 0; i < points.length; i++) {
      const start = beatValue(points[i].beat), next = i + 1 < points.length ? beatValue(points[i + 1].beat) : target;
      if (target <= start) break;
      elapsed += Math.max(0, Math.min(target, next) - Math.max(previous, start)) * 60000 / (Number(points[i].bpm) || bpm);
      previous = Math.max(previous, next); bpm = Number(points[i].bpm) || bpm;
      if (target <= next) break;
    }
    return elapsed;
  }
  function readChart(chart) {
    const points = [...(chart.time || [])].sort((a, b) => beatValue(a.beat) - beatValue(b.beat));
    const music = (chart.note || []).find(item => item.type === 1 && item.sound);
    const origin = music ? -Number(music.offset || 0) : 0;
    const prepared = renderer.prepareNotes((chart.note || []).filter(item => Number.isInteger(item.column) && item.column >= 0 && item.column < 4)
      .map(item => ({start: origin + beatToMs(beatValue(item.beat), points), end: item.endbeat ? origin + beatToMs(beatValue(item.endbeat), points) : null,
        lane: item.column, head: false, tail: false, holding: false, headGrade: null, tailGrade: null}))
      .sort((a, b) => a.start - b.start || a.lane - b.lane));
    endTime = prepared.reduce((latest, note) => Math.max(latest, note.end ?? note.start), 0);
    return prepared;
  }
  function musicApi() { return window.MusicPlayback; }
  function mediaTimeMs() { return Math.max(0, Number(audio.currentTime || 0) * 1000); }
  function waitForAudio(signal) {
    let finish, checkReady, timeout, poll;
    const promise = new Promise((resolve, reject) => {
      finish = (error = null) => {
        clearTimeout(timeout); clearInterval(poll);
        for (const name of ['canplay', 'canplaythrough', 'loadeddata', 'loadedmetadata', 'progress', 'durationchange', 'error']) audio.removeEventListener(name, checkReady);
        signal.removeEventListener('abort', cancel);
        if (error) reject(error); else resolve();
      };
      const cancel = () => finish(new DOMException('试玩已退出', 'AbortError'));
      checkReady = () => {
        if (signal.aborted) { cancel(); return; }
        if (audio.error) { finish(new Error('音频读取失败，请退出后重试。')); return; }
        if (audio.readyState >= 2) finish();
      };
      for (const name of ['canplay', 'canplaythrough', 'loadeddata', 'loadedmetadata', 'progress', 'durationchange', 'error']) audio.addEventListener(name, checkReady);
      signal.addEventListener('abort', cancel, {once: true});
      poll = setInterval(checkReady, 80);
      timeout = setTimeout(() => finish(new Error(`音频读取失败（readyState ${audio.readyState}${audio.error?.code ? `，错误 ${audio.error.code}` : ''}），请退出后重试。`)), 15000);
    });
    return {promise, check: () => checkReady?.()};
  }
  function setAnchor(media = mediaTimeMs(), perf = performance.now()) {
    frozenMediaMs = media;
    clockAnchor = {mediaTimeMs: media, perfTimeMs: perf};
    clockValid = true;
  }
  function playable() {
    return state === 'playing' && !audio.paused && !audio.seeking && audio.readyState >= 2 && (!musicApi() || musicApi().isCurrent(musicLease));
  }
  function currentSongTime() {
    return playable() || audio.ended ? mediaTimeMs() : frozenMediaMs;
  }
  function currentOffset() { return Number($('arcade-offset').value) || 0; }
  function eventPerfTime(event) {
    const stamp = Number(event.timeStamp);
    if (!Number.isFinite(stamp)) return performance.now();
    if (stamp > 1e12 && Number.isFinite(performance.timeOrigin)) return stamp - performance.timeOrigin;
    return stamp;
  }
  function inputTime(event) {
    if (!clockValid || !clockAnchor) return null;
    const mapped = clockAnchor.mediaTimeMs + eventPerfTime(event) - clockAnchor.perfTimeMs;
    return mapped - currentOffset();
  }
  function load(jobId, chartId, signal, generation, supplied = null) {
    state = 'loading';
    $('arcade-overlay').dataset.countdown = 'false'; $('arcade-overlay').hidden = false;
    $('arcade-overlay-title').textContent = '正在读取谱面'; $('arcade-overlay-copy').textContent = '加载当前导出的 MC 数据与音频…';
    return (async () => {
      const response = supplied ? null : await fetch(`/api/jobs/${jobId}/charts/${encodeURIComponent(chartId)}`, {signal});
      const data = supplied || await response.json();
      if (response && !response.ok) throw new Error(data.detail || '谱面读取失败');
      if (!data.chart) throw new Error('这个片段为空谱，可以试听，无需试玩。');
      if (!active || generation !== loadGeneration) throw new DOMException('试玩已退出', 'AbortError');
      chartInfo = data; notes = readChart(data.chart);
      const readiness = waitForAudio(signal); audio.src = data.audio_url; audio.load(); readiness.check();
      await readiness.promise;
      if (!active || generation !== loadGeneration) throw new DOMException('试玩已退出', 'AbortError');
      $('arcade-subtitle').textContent = `${data.title} · ${data.artist} · ${data.pattern} · ${data.difficulty}`;
      $('arcade-overlay-title').textContent = '3'; $('arcade-overlay-copy').textContent = '';
      renderCounts(); state = 'ready';
    })();
  }
  function renderCounts() {
    $('arcade-score').replaceChildren(...Object.entries(counts).map(([name, value]) => {
      const item = document.createElement('div'); item.className = 'arcade-score-cell'; item.dataset.judge = name.toLowerCase();
      const strong = document.createElement('strong'); strong.textContent = value;
      const small = document.createElement('small'); small.textContent = name;
      item.append(strong, small); return item;
    }));
  }
  function showFeedback(grade) {
    const judge = $('arcade-judge'), number = $('arcade-combo-number');
    judge.textContent = grade; judge.dataset.judge = grade.toLowerCase();
    clearTimeout(feedbackTimer); feedbackTimer = setTimeout(() => { judge.textContent = ''; }, 650);
    if (!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      for (const el of [judge, number]) el.getAnimations?.().forEach(animation => animation.cancel());
      judge.animate?.([{opacity: .4, transform: 'translateY(4px) scale(.88)'}, {opacity: 1, transform: 'translateY(-1px) scale(1.06)', offset: .45}, {opacity: 1, transform: 'translateY(0) scale(1)'}], {duration: 210, easing: 'ease-out'});
      number.animate?.([{transform: 'scale(1.12)'}, {transform: 'scale(1)'}], {duration: 160, easing: 'ease-out'});
    }
  }
  function resetFeedback() {
    clearTimeout(feedbackTimer); $('arcade-combo-number').textContent = '0'; $('arcade-max-combo').textContent = '最高连击 0'; $('arcade-judge').textContent = '';
  }
  function mark(grade, note, tail = false) {
    const unit = tail ? 'tail' : 'head';
    if (note[unit]) return;
    note[unit] = true; note[tail ? 'tailGrade' : 'headGrade'] = grade; counts[grade]++;
    if (grade === 'Miss') combo = 0; else { combo++; maxCombo = Math.max(maxCombo, combo); }
    $('arcade-combo-number').textContent = combo; $('arcade-max-combo').textContent = `最高连击 ${maxCombo}`;
    effects.emit(note.lane, grade === 'Miss' ? 'miss' : tail ? 'tail' : 'hit', performance.now());
    showFeedback(grade); renderCounts();
  }
  function gradeFor(delta) {
    const absolute = Math.abs(delta);
    return absolute <= windows.perfect ? 'Perfect' : absolute <= windows.great ? 'Great' : absolute <= windows.good ? 'Good' : 'Miss';
  }
  function onDown(event) {
    if (autoplay) return;
    const index = codes.indexOf(event.code);
    if (index < 0 || state !== 'playing') return;
    event.preventDefault();
    if (event.repeat || pressed.has(index) || !clockValid) return;
    pressed.add(index); effects.emit(index, 'press', performance.now()); window.KeySounds?.play();
    const now = inputTime(event), holding = held.get(index);
    if (holding && !holding.note.tail) { holding.note.holding = true; return; }
    const candidate = notes.find(note => note.lane === index && !note.head && note.start - now >= -windows.good && note.start - now <= windows.good);
    if (!candidate) return;
    const grade = gradeFor(now - candidate.start); candidate.headGrade = grade; mark(grade, candidate);
    if (candidate.end !== null && grade !== 'Miss') { candidate.holding = true; held.set(index, {note: candidate}); }
  }
  function onUp(event) {
    if (autoplay) return;
    const index = codes.indexOf(event.code); if (index < 0) return;
    if (pressed.has(index)) effects.emit(index, 'release', performance.now());
    pressed.delete(index);
    const activeHold = held.get(index); if (!activeHold || state !== 'playing' || !clockValid) return;
    const note = activeHold.note; if (note.tail) return;
    const delta = inputTime(event) - note.end;
    if (delta < -windows.good || delta > windows.good) mark('Miss', note, true); else mark(gradeFor(delta), note, true);
    note.holding = false; held.delete(index);
  }
  function updateMisses(now) {
    const judgeNow = now - currentOffset();
    for (const note of notes) {
      if (!note.head && judgeNow - note.start > windows.good) { mark('Miss', note); if (note.end !== null) note.headMissed = true; }
      if (note.end !== null && note.head && !note.tail) {
        if (note.holding && pressed.has(note.lane) && judgeNow >= note.end) { mark('Perfect', note, true); note.holding = false; held.delete(note.lane); }
        else if (judgeNow - note.end > windows.good) { note.holding = false; held.delete(note.lane); mark('Miss', note, true); }
      }
    }
  }
  function updateAutoplay(now) {
    while (autoCursor < notes.length && notes[autoCursor].start <= now) {
      const note = notes[autoCursor++]; if (note.head) continue;
      const previous = held.get(note.lane)?.note;
      if (previous && previous.end <= now) { mark('Perfect', previous, true); previous.holding = false; held.delete(note.lane); }
      effects.emit(note.lane, 'press', performance.now()); window.KeySounds?.play();
      note.headGrade = 'Perfect'; mark('Perfect', note);
      if (note.end !== null) { note.holding = true; held.set(note.lane, {note}); }
      else autoReleaseAt[note.lane] = now + 75;
    }
    for (const [lane, item] of held) {
      const note = item.note;
      if (now >= note.end) { mark('Perfect', note, true); note.holding = false; held.delete(lane); }
      else note.holding = true;
    }
    pressed.clear();
    for (let lane = 0; lane < 4; lane++) if (held.has(lane) || autoReleaseAt[lane] > now) pressed.add(lane);
  }
  function resize() {
    const rect = canvas.getBoundingClientRect(), scale = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(rect.width * scale)), height = Math.max(1, Math.round(rect.height * scale));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
  }
  function draw(now = currentSongTime()) {
    const ctx = canvas.getContext('2d'), w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    const scale = window.devicePixelRatio || 1; ctx.setTransform(scale, 0, 0, scale, 0, 0);
    const displayTime = performance.now(), speed = speedSource?.value || document.querySelector('#preview-speed')?.value || 1;
    const geometry = renderer.geometry(w, h), projection = {...geometry, geometry, speed: Number(speed), travelMs: 5000 / renderer.speedValue(speed)};
    renderer.render(ctx, w, h, {...window.GameAppearance?.get(), notes, now, speed, pressed, labels: laneNames,
      events: effects.live(displayTime), effectTime: displayTime, reduced: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
      night: document.documentElement?.dataset.theme === 'dark', caption: (autoplay ? '4K Autoplay 演示 · ' : '4K 试玩 · ') + Number(speed).toFixed(1) + '×'});
    if (typeof callbacks.onFrame === 'function') {
      const snapshot = {mediaTimeMs: now, displayTimeMs: now, frameTimeMs: displayTime, state, generation: musicLease?.generation ?? loadGeneration,
        sourceTimeOffsetMs: Number(callbacks.sourceTimeOffsetMs) || 0, valid: clockValid};
      try { callbacks.onFrame(snapshot, projection, canvas); } catch (error) { console.error('Arcade frame callback failed', error); }
    }
  }
  function settled() { return notes.every(note => note.head && (note.end === null || note.tail)); }
  function finish() {
    cancelAnimationFrame(frame); frame = 0; clearTimeout(feedbackTimer);
    state = 'finished'; $('arcade-overlay').dataset.countdown = 'false';
    if (musicApi() && musicLease) musicApi().release(musicLease); else audio.pause();
    musicLease = null; clockValid = false; pressed.clear(); effects.clear(); held.clear(); autoReleaseAt.fill(0);
    $('arcade-start').hidden = true; $('arcade-pause').hidden = true; $('arcade-retry').hidden = false;
    renderResults();
  }
  function createButton(text, className, action) {
    const button = document.createElement('button'); button.type = 'button'; button.className = className; button.textContent = text;
    button.addEventListener('click', action); return button;
  }
  function resetOverlay() {
    const overlay = $('arcade-overlay'), title = document.createElement('strong'), copy = document.createElement('p');
    title.id = 'arcade-overlay-title'; title.textContent = '3'; copy.id = 'arcade-overlay-copy'; copy.textContent = '';
    overlay.replaceChildren(title, copy); overlay.dataset.countdown = 'false'; overlay.dataset.state = '';
  }
  function jumpToResult() {
    const failed = notes.filter(note => note.headGrade === 'Miss' || note.tailGrade === 'Miss');
    const target = failed.length ? Math.min(...failed.map(note => note.headGrade === 'Miss' ? note.start : note.end)) : endTime;
    const positionMs = (Number(callbacks.sourceTimeOffsetMs) || 0) + target;
    const detail = {positionMs, gamePositionMs: target, autoplay, chartId: callbacks.chartId || null};
    const handler = callbacks.onPositionJump;
    close();
    if (typeof handler === 'function') handler(target, detail);
    else if (typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') window.dispatchEvent(new CustomEvent('arcadepositionjump', {detail}));
  }
  function renderResults() {
    const overlay = $('arcade-overlay'); overlay.replaceChildren(); overlay.hidden = false; overlay.dataset.countdown = 'false'; overlay.dataset.state = 'results';
    const title = document.createElement('strong'); title.id = 'arcade-overlay-title';
    title.textContent = autoplay ? 'Autoplay 演示完成' : '本次试玩完成';
    const copy = document.createElement('p'); copy.id = 'arcade-overlay-copy';
    copy.textContent = `最高连击 ${maxCombo}　·　Perfect ${counts.Perfect} / Great ${counts.Great} / Good ${counts.Good} / Miss ${counts.Miss}`;
    const summary = document.createElement('div'); summary.className = 'arcade-result-counts'; summary.setAttribute('aria-label', '判定统计');
    for (const [name, value] of Object.entries(counts)) {
      const item = document.createElement('span'); item.dataset.judge = name.toLowerCase(); item.textContent = `${name} ${value}`; summary.append(item);
    }
    const actions = document.createElement('div'); actions.className = 'arcade-result-actions';
    actions.append(createButton('重新试玩', 'arcade-result-retry', begin),
      createButton('跳到判定位置', 'arcade-result-jump', jumpToResult),
      createButton('返回', 'arcade-result-return', close));
    overlay.append(title, copy, summary, actions);
    if (autoplay) { const note = document.createElement('small'); note.className = 'arcade-result-demo-note'; note.textContent = 'Autoplay 为自动演示，成绩不代表人工游玩表现。'; overlay.append(note); }
  }
  function tick() {
    if (state !== 'playing') return;
    const now = currentSongTime();
    if (playable() || audio.ended) {
      frozenMediaMs = now;
      if (audio.ended && !endedAtPerf) { endedAtPerf = performance.now(); setAnchor(now, endedAtPerf); }
      if (autoplay) updateAutoplay(now); else updateMisses(now);
      draw(now);
      if (settled() && (audio.ended || now - currentOffset() > endTime + windows.good)) { finish(); return; }
      if (audio.ended && performance.now() - endedAtPerf > windows.good) {
        updateMisses(endTime + windows.good + 1);
        for (const note of notes) if (note.end !== null && note.head && !note.tail) mark('Miss', note, true);
        finish(); return;
      }
    } else {
      clockValid = false; frozenMediaMs = mediaTimeMs(); draw(frozenMediaMs); return;
    }
    frame = requestAnimationFrame(tick);
  }
  function pause() {
    $('arcade-overlay').dataset.countdown = 'false';
    if (state === 'countdown') {
      clearTimeout(countdownTimer); countdownTimer = 0; state = 'ready'; clockValid = false;
      $('arcade-overlay').hidden = false; $('arcade-overlay-title').textContent = '倒计时已取消'; $('arcade-overlay-copy').textContent = '准备好后可以重新开始。';
      $('arcade-start').hidden = false; $('arcade-pause').hidden = true; return;
    }
    if (state !== 'playing') return;
    frozenMediaMs = mediaTimeMs(); state = 'paused'; clockValid = false; endedAtPerf = 0;
    cancelAnimationFrame(frame); frame = 0;
    if (musicApi() && musicLease) musicApi().pause(musicLease); else audio.pause();
    musicLease = null; pressed.clear(); effects.clear(); autoReleaseAt.fill(0);
    for (const item of held.values()) item.note.holding = false;
    draw(frozenMediaMs); $('arcade-overlay').hidden = false; $('arcade-overlay-title').textContent = '已暂停';
    $('arcade-overlay-copy').textContent = autoplay ? '继续后将重新倒计时，自动演示会接着播放。' : '继续后会重新倒计时；当前长条请重新按住。';
    $('arcade-pause').textContent = '继续 Esc';
  }
  function togglePause() {
    if (state === 'playing' || state === 'countdown') pause();
    else if (state === 'paused' || state === 'ready') begin();
  }
  async function begin() {
    if (!active || !chartInfo || state === 'loading' || state === 'countdown' || state === 'playing') return;
    const resume = state === 'paused';
    if (state === 'finished') resetOverlay();
    $('arcade-overlay').dataset.state = 'countdown';
    if (!resume) {
      notes = readChart(chartInfo.chart); autoCursor = 0; autoReleaseAt.fill(0); counts = {Perfect: 0, Great: 0, Good: 0, Miss: 0};
      combo = 0; maxCombo = 0; held.clear(); pressed.clear(); effects.clear(); renderCounts(); resetFeedback();
      frozenMediaMs = 0; endedAtPerf = 0;
      // Loading happened before the countdown; retries reuse the same decoded source.
      try { audio.currentTime = 0; } catch { /* A not-yet-seekable stream starts at zero. */ }
    }
    state = 'countdown'; $('arcade-start').hidden = true; $('arcade-pause').hidden = false; $('arcade-pause').textContent = '取消 Esc'; $('arcade-retry').hidden = true;
    $('arcade-overlay').hidden = false; countdown(3, resume);
  }
  function countdown(value, resume) {
    if (!active || state !== 'countdown') return;
    $('arcade-overlay').hidden = false; $('arcade-overlay').dataset.countdown = String(value > 0);
    if (value > 0) {
      $('arcade-overlay-title').textContent = String(value); $('arcade-overlay-copy').textContent = ''; draw(resume ? frozenMediaMs : 0);
      countdownTimer = setTimeout(() => countdown(value - 1, resume), 1000); return;
    }
    $('arcade-overlay').hidden = true; state = 'playing'; $('arcade-pause').hidden = false; $('arcade-pause').textContent = '暂停 Esc';
    if (resume) { for (const item of held.values()) item.note.holding = false; pressed.clear(); effects.clear(); }
    const localGeneration = loadGeneration;
    const manager = musicApi();
    if (manager) musicLease = manager.claim(audio, 'arcade');
    const playLease = musicLease;
    const playResult = manager ? manager.play(playLease) : audio.play().then(() => true);
    Promise.resolve(playResult).then(started => {
      if (!active || localGeneration !== loadGeneration || state !== 'playing') {
        if (manager && playLease) manager.release(playLease); else audio.pause();
        return;
      }
      if (started === false) { clockValid = false; return; }
      endedAtPerf = 0; setAnchor(mediaTimeMs(), performance.now()); frame = requestAnimationFrame(tick);
      $('arcade-pause').hidden = false; $('arcade-pause').textContent = '暂停 Esc';
    }).catch(() => {
      if (!active || localGeneration !== loadGeneration) return;
      state = 'ready'; clockValid = false; $('arcade-overlay').hidden = false;
      $('arcade-overlay-title').textContent = '音频播放失败'; $('arcade-overlay-copy').textContent = '请重新点击开始，或检查浏览器的音频权限。';
      $('arcade-start').hidden = false; $('arcade-pause').hidden = true;
    });
  }
  async function open(jobId, chartId, options = {}) {
    if (!chartId || !jobId) return;
    close(); active = true; autoplay = options?.autoplay === true; const generation = ++loadGeneration; loadController = new AbortController();
    callbacks = {...options}; activeHost = options.host ? $(options.host) : $('playfield'); activeHost.append(dialog); speedSource = options.speedInput || null;
    dialog.dataset.mode = autoplay ? 'autoplay' : 'manual'; $('arcade-mode').textContent = autoplay ? 'Autoplay 演示' : '现场试玩';
    $('arcade-key-hint').textContent = autoplay ? '自动演示' : 'DFJK'; $('arcade-calibration').hidden = autoplay;
    $('arcade-exit').textContent = autoplay ? '退出演示' : '退出试玩';
    dialog.hidden = false; activeHost.classList.add('arcade-active'); dialog.focus({preventScroll: true}); resetOverlay();
    if (musicApi()) musicLease = musicApi().claim(audio, 'arcade'); else $('audio').pause();
    chartInfo = null; combo = 0; maxCombo = 0; counts = {Perfect: 0, Great: 0, Good: 0, Miss: 0}; renderCounts(); resetFeedback();
    $('arcade-start').hidden = true; $('arcade-pause').hidden = true; $('arcade-retry').hidden = true; $('arcade-overlay').hidden = false;
    clockValid = false; frozenMediaMs = 0; resize(); draw(0);
    try {
      await Promise.all([load(jobId, chartId, loadController.signal, generation, options.data), window.KeySounds?.prepare()]);
      if (active && generation === loadGeneration) await begin();
    } catch (error) {
      if (!active || generation !== loadGeneration || error.name === 'AbortError') return;
      $('arcade-overlay-title').textContent = '读取失败'; $('arcade-overlay-copy').textContent = error.message; state = 'error';
    }
  }
  function close() {
    const wasActive = active, onClose = callbacks.onClose;
    active = false; ++loadGeneration; loadController?.abort(); loadController = null; clearTimeout(countdownTimer); countdownTimer = 0;
    cancelAnimationFrame(frame); frame = 0; clearTimeout(feedbackTimer);
    state = 'idle'; autoplay = false; autoCursor = 0; endedAtPerf = 0; clockValid = false; autoReleaseAt.fill(0);
    if (musicApi() && musicLease) musicApi().release(musicLease); else audio.pause();
    musicLease = null; audio.removeAttribute('src'); audio.load(); pressed.clear(); effects.clear(); held.clear();
    dialog.hidden = true; activeHost.classList.remove('arcade-active'); speedSource = null; callbacks = {};
    if (wasActive && typeof onClose === 'function') onClose();
  }
  $('arcade-open').addEventListener('click', () => open(window.activeArcadeJob, $('arcade-open').dataset.chartId));
  $('arcade-autoplay').addEventListener('click', () => open(window.activeArcadeJob, $('arcade-open').dataset.chartId, {autoplay: true}));
  $('arcade-start').addEventListener('click', begin); $('arcade-retry').addEventListener('click', begin);
  $('arcade-pause').addEventListener('click', togglePause); $('arcade-close').addEventListener('click', close); $('arcade-exit').addEventListener('click', close);
  $('arcade-offset').value = localStorage.getItem('startrail.arcade-offset') || '0';
  function offsetLabel() { $('arcade-offset-value').textContent = `${Number($('arcade-offset').value) > 0 ? '+' : ''}${$('arcade-offset').value} ms`; }
  $('arcade-offset').addEventListener('input', () => { offsetLabel(); localStorage.setItem('startrail.arcade-offset', $('arcade-offset').value); }); offsetLabel();
  window.addEventListener('keydown', event => {
    if (!active) return;
    if (event.code === 'Space') { event.preventDefault(); event.stopPropagation(); return; }
    if (event.code === 'Escape') {
      if ($('appearance-dialog')?.open) return;
      event.preventDefault(); event.stopPropagation(); togglePause(); return;
    }
    if ($('appearance-dialog')?.open) return;
    onDown(event);
  }, true);
  window.addEventListener('keyup', event => { if (active) onUp(event); });
  window.addEventListener('blur', pause); document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
  for (const eventName of ['waiting', 'stalled', 'seeking']) audio.addEventListener(eventName, () => {
    if (!active || state !== 'playing') return;
    frozenMediaMs = mediaTimeMs(); clockValid = false; cancelAnimationFrame(frame); frame = 0; pressed.clear();
    for (const item of held.values()) item.note.holding = false;
    draw(frozenMediaMs);
  });
  for (const eventName of ['playing', 'seeked', 'canplay']) audio.addEventListener(eventName, () => {
    if (!active || state !== 'playing' || audio.paused || audio.seeking || audio.readyState < 2) return;
    setAnchor(mediaTimeMs(), performance.now()); if (!frame) frame = requestAnimationFrame(tick);
  });
  audio.addEventListener('ended', () => {
    if (!active || state !== 'playing') return;
    endedAtPerf = performance.now(); setAnchor(mediaTimeMs(), endedAtPerf); if (!frame) frame = requestAnimationFrame(tick);
  });
  audio.addEventListener('pause', () => {
    if (!active || state !== 'playing' || audio.ended) return;
    pause();
  });
  window.addEventListener('resize', () => { resize(); if (state !== 'playing') draw(currentSongTime()); });
  new ResizeObserver(() => { resize(); if (state !== 'playing') draw(currentSongTime()); }).observe(canvas);
  window.addEventListener('gameappearancechange', () => { effects.clear(); if (active && state !== 'playing') draw(currentSongTime()); });
  window.addEventListener('appearancechange', () => { if (active && state !== 'playing') draw(currentSongTime()); });
  window.ChartArcade = {open, close, isActive: () => active, state: () => state};
  document.querySelectorAll('.topbar nav button').forEach(button => button.addEventListener('click', () => { if (active) close(); }));
})();
