const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function app({deferFetch = false, deferPlay = false, chart: customChart} = {}) {
  const elements = new Map(), timers = new Map(), events = new Map(), drawCalls = [], visualEvents = [], documentEvents = new Map();
  let nextTimer = 1, resolveFetch, raf = null, now = 1000, sounds = 0; const resolvePlays = [], effectSets = [];
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const listeners = new Map();
    const el = {id, tagName: id.includes('audio') ? 'AUDIO' : 'DIV', hidden: false, value: '0', dataset: {}, currentTime: 0, readyState: 2, paused: true, ended: false, seeking: false, plays: 0,
      clientWidth: 640, clientHeight: 480, classList: {add() {}, remove() {}},
      append(...nodes) { this.children = [...(this.children || []), ...nodes]; for (const node of nodes) if (node.id) elements.set(node.id, node); },
      replaceChildren(...nodes) { this.children = nodes; for (const node of nodes) if (node.id) elements.set(node.id, node); },
      focus() {}, load() {}, removeAttribute() { this.src = ''; }, setAttribute() {},
      pause() { this.paused = true; this.trigger('pause', {target: this}); }, play() { this.plays++; this.paused = false; return deferPlay && id === 'arcade-audio' ? new Promise(resolve => resolvePlays.push(resolve)) : Promise.resolve(); },
      getBoundingClientRect() { return {width: 640, height: 480}; },
      getContext() { return new Proxy({}, {get: (obj, key) => key === 'createLinearGradient' ? () => ({addColorStop() {}}) : obj[key] || ((...args) => drawCalls.push({method: key, args, fillStyle: obj.fillStyle})), set: (obj, key, value) => (obj[key] = value, true)}); },
      addEventListener(name, handler, options = {}) { const list = listeners.get(name) || []; list.push({handler, once: Boolean(options.once)}); listeners.set(name, list); },
      removeEventListener(name, handler) { listeners.set(name, (listeners.get(name) || []).filter(item => item.handler !== handler)); },
      trigger(name, event = {}) { for (const item of [...(listeners.get(name) || [])]) { item.handler(event); if (item.once) this.removeEventListener(name, item.handler); } },
      click() { this.trigger('click', {preventDefault() {}}); }};
    elements.set(id, el); return el;
  }
  const chart = customChart || {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 0, beat: [2, 0, 1]}]};
  const response = {ok: true, json: async () => ({chart, audio_url: '/test.ogg', title: 'Test', artist: 'Artist', pattern: 'balanced', difficulty: 'easy'})};
  const doc = {documentElement: {dataset: {theme: 'light'}}, hidden: false, getElementById: element, querySelector: () => ({value: '8'}),
    querySelectorAll: selector => selector === 'audio' ? [...elements.values()].filter(item => item.tagName === 'AUDIO') : [],
    createElement: () => element(`new${elements.size}`), addEventListener(name, handler) { documentEvents.set(name, handler); }};
  const win = {devicePixelRatio: 1, PlayfieldRenderer: {...require('../web/playfield-renderer.js'), createEffects() { const e = require('../web/playfield-renderer.js').createEffects(); effectSets.push(e); const emit = e.emit; e.emit = (lane, kind, time) => { visualEvents.push({lane, kind}); emit(lane, kind, time); }; return e; }},
    GameAppearance: {get: () => ({skin: 'float', hitEffects: 'standard'})}, KeySounds: {prepare: async () => {}, play: () => sounds++},
    addEventListener(name, handler) { const list = events.get(name) || []; list.push(handler); events.set(name, list); }, dispatchEvent() {}};
  const context = {console, AbortController, DOMException, performance: {now: () => now, timeOrigin: 0}, document: doc, window: win,
    localStorage: {getItem: () => null, setItem() {}}, ResizeObserver: class {observe() {}},
    setTimeout: (fn, delay) => { const id = nextTimer++; timers.set(id, {fn, delay}); return id; }, clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { const id = nextTimer++; timers.set(id, {fn, delay, interval: true}); return id; }, clearInterval: id => timers.delete(id),
    requestAnimationFrame: fn => (raf = fn, 1), cancelAnimationFrame() { raf = null; },
    fetch: () => deferFetch ? new Promise(resolve => { resolveFetch = resolve; }) : Promise.resolve(response)};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/music-playback.js'), 'utf8'), context);
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/arcade.js'), 'utf8'), context);
  return {el: element, api: win.ChartArcade, manager: win.MusicPlayback, timers, events, drawCalls, visualEvents, sounds: () => sounds, effectItems: () => effectSets.flatMap(effect => effect.items),
    documentEvent: (name, event) => documentEvents.get(name)?.(event),
    input: (kind, code, time, repeat = false) => events.get(kind)?.forEach(handler => handler({code, timeStamp: 1000 + time, repeat, preventDefault() {}, stopPropagation() {}})),
    key: (code, time) => events.get('keydown')?.forEach(handler => handler({code, timeStamp: 1000 + time, preventDefault() {}, stopPropagation() {}})),
    step: time => { now = 1000 + time; if (!element('arcade-audio').paused && !element('arcade-audio').seeking) element('arcade-audio').currentTime = time / 1000; const callback = raf; raf = null; callback?.(); },
    setPerf: value => { now = value; },
    resolve: () => resolveFetch(response), resolvePlay: index => resolvePlays[index]?.(), async countdown() {
      for (let i = 0; i < 3; i++) { const entry = [...timers.entries()].find(([, timer]) => timer.delay === 1000); assert.ok(entry, 'countdown is running'); timers.delete(entry[0]); entry[1].fn(); }
      await new Promise(resolve => setImmediate(resolve));
    }};
}

test('play preloads before countdown; Escape cancels countdown and Space never controls the game', async () => {
  const a = app(); await a.api.open('job', 'balanced--easy');
  assert.equal(a.el('arcade-panel').hidden, false); assert.equal(a.el('arcade-overlay-title').textContent, '3');
  assert.equal(a.el('arcade-start').hidden, true); assert.equal(a.el('arcade-audio').plays, 0);
  assert.equal(a.el('arcade-exit').textContent, '退出试玩'); assert.equal(a.el('arcade-pause').textContent, '取消 Esc');
  a.key('Escape', 0);
  assert.equal(a.api.isActive(), true); assert.equal(a.api.state(), 'ready'); assert.equal(a.el('arcade-overlay-title').textContent, '倒计时已取消');
  a.key('Space', 0); assert.equal(a.api.state(), 'ready', 'Space is suppressed even while the game panel has focus');
  a.el('arcade-exit').click(); assert.equal(a.api.isActive(), false); assert.equal(a.timers.size, 0);
});

test('Autoplay follows media time and completes same-lane holds once after a delayed frame', async () => {
  const chart = {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 0, beat: [2, 0, 1], endbeat: [4, 0, 1]}, {column: 0, beat: [6, 0, 1], endbeat: [8, 0, 1]}, {column: 1, beat: [10, 0, 1]}]};
  const a = app({chart}); await a.api.open('job', 'chart', {autoplay: true}); await a.countdown();
  a.el('arcade-audio').currentTime = 4.1; a.step(4100);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 4, 'both heads and both held tails are counted once');
  a.input('keydown', 'KeyD', 4100); assert.equal(Number(a.el('arcade-combo-number').textContent), 4, 'human input is ignored in autoplay');
  a.el('arcade-audio').currentTime = 5.1; a.step(5100);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 5); assert.equal(a.sounds(), 3);
});

test('advanced chart data mounts in its preview host without a legacy fetch and emits one shared frame snapshot', async () => {
  const a = app({deferFetch: true}), data = {chart: {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 1, beat: [2, 0, 1]}]}, audio_url: '/api/advanced/audio', title: 'Segment', artist: 'Test', pattern: 'balanced', difficulty: 'easy'};
  const frames = [];
  await a.api.open('advanced', 'variant', {data, host: 'adv-game-host', speedInput: {value: '16'}, onFrame: (snapshot, projection) => frames.push({snapshot, projection})});
  assert.equal(a.el('arcade-overlay-title').textContent, '3'); assert.equal(a.el('arcade-audio').src, data.audio_url);
  assert.equal(frames.at(-1).snapshot.mediaTimeMs, 0); assert.equal(frames.at(-1).snapshot.displayTimeMs, 0);
  assert.equal(frames.at(-1).projection.travelMs, 312.5);
  await a.countdown(); a.input('keydown', 'KeyF', 1000); assert.equal(Number(a.el('arcade-combo-number').textContent), 1);
  a.api.close(); assert.equal(a.api.isActive(), false);
});

test('audio preloading waits for a readiness event before starting the countdown', async () => {
  const a = app(); a.el('arcade-audio').readyState = 0;
  const opening = a.api.open('job', 'loading'); await Promise.resolve();
  assert.equal(a.api.state(), 'loading'); assert.equal(a.el('arcade-overlay-title').textContent, '正在读取谱面');
  a.el('arcade-audio').readyState = 2; a.el('arcade-audio').trigger('loadeddata'); await opening;
  assert.equal(a.api.state(), 'countdown'); assert.equal(a.el('arcade-overlay-title').textContent, '3');
});

const longChart = {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 0, beat: [2, 0, 1], endbeat: [6, 0, 1]}, {column: 1, beat: [10, 0, 1]}]};
test('long note remains visible after head and completes only when held through tail', async () => {
  const a = app({chart: longChart}); await a.api.open('job', 'hold'); await a.countdown();
  a.input('keydown', 'KeyD', 1000); a.input('keydown', 'KeyD', 1050, true); a.drawCalls.length = 0; a.step(1500);
  assert.ok(a.drawCalls.some(call => call.method === 'fillRect' && call.fillStyle === '#62d6d150' && call.args[3] > 100), 'held body is visible');
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1); assert.equal(a.sounds(), 1, 'key repeat does not repeat the sound');
  a.step(3050); assert.equal(Number(a.el('arcade-combo-number').textContent), 2, 'holding through tail succeeds without keyup');
  a.input('keyup', 'KeyD', 3200); assert.equal(Number(a.el('arcade-combo-number').textContent), 2, 'late release does not add a judgment');
});

test('early long-note release breaks combo but preserves max combo', async () => {
  const a = app({chart: longChart}); await a.api.open('job', 'hold'); await a.countdown(); a.input('keydown', 'KeyD', 1000); a.input('keyup', 'KeyD', 1400);
  assert.equal(a.el('arcade-judge').textContent, 'Miss'); assert.equal(Number(a.el('arcade-combo-number').textContent), 0);
  assert.equal(a.el('arcade-max-combo').textContent, '最高连击 1'); a.step(3200); assert.equal(Number(a.el('arcade-combo-number').textContent), 0);
});

test('simultaneous holds remain independent when one lane releases early', async () => {
  const chart = {...longChart, note: [longChart.note[0], {column: 1, beat: [2, 0, 1], endbeat: [6, 0, 1]}, longChart.note[1]]};
  const a = app({chart}); await a.api.open('job', 'hold-chord'); await a.countdown();
  a.input('keydown', 'KeyD', 1000); a.input('keydown', 'KeyF', 1000); a.input('keyup', 'KeyD', 1400); a.step(3050);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1); assert.equal(a.el('arcade-max-combo').textContent, '最高连击 2'); assert.equal(a.sounds(), 2);
});

test('pause and re-hold never judge a long-note head twice', async () => {
  const a = app({chart: longChart}); await a.api.open('job', 'hold'); await a.countdown(); a.input('keydown', 'KeyD', 1000); a.step(1500);
  a.key('Escape', 1500); a.input('keyup', 'KeyD', 1500); await a.el('arcade-pause').trigger('click'); await a.countdown(); a.input('keydown', 'KeyD', 1700);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1); a.step(3050); assert.equal(Number(a.el('arcade-combo-number').textContent), 2);
});

test('Perfect, Great and Good build one streak; Miss resets current combo only', async () => {
  const chart = {time: [{beat: [0, 0, 1], bpm: 120}], note: [2, 4, 6, 8, 10].map(beat => ({column: 0, beat: [beat, 0, 1]}))};
  const a = app({chart}); await a.api.open('job', 'grades'); await a.countdown();
  for (const [time, grade] of [[1000, 'Perfect'], [2060, 'Great'], [3090, 'Good']]) { a.input('keydown', 'KeyD', time); assert.equal(a.el('arcade-judge').textContent, grade); a.input('keyup', 'KeyD', time + 5); }
  assert.equal(Number(a.el('arcade-combo-number').textContent), 3); a.step(4200);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 0); assert.equal(a.el('arcade-max-combo').textContent, '最高连击 3');
});

test('waiting freezes media judgments and resume reanchors to the new media time', async () => {
  const frames = [], a = app({chart: longChart}); await a.api.open('job', 'buffer', {onFrame: snapshot => frames.push(snapshot)}); await a.countdown();
  a.el('arcade-audio').currentTime = 0.95; a.el('arcade-audio').trigger('waiting');
  a.setPerf(8000); a.input('keydown', 'KeyD', 7000); assert.equal(Number(a.el('arcade-combo-number').textContent), 0, 'a key during buffering is not judged');
  assert.ok(frames.some(snapshot => !snapshot.valid && snapshot.mediaTimeMs === 950), 'buffering reports the frozen media snapshot as invalid');
  a.el('arcade-audio').currentTime = 1.01; a.setPerf(9000); a.el('arcade-audio').trigger('playing'); a.input('keydown', 'KeyD', 8000);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1, 'input uses the resumed audio anchor');
});

test('finished result screen reports judgments and offers retry, return and chart-position jump', async () => {
  const chart = {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 0, beat: [2, 0, 1]}]};
  let jumped = null;
  const a = app({chart}); await a.api.open('job', 'results', {sourceTimeOffsetMs: 5000, onPositionJump: (position, detail) => { jumped = {position, detail}; }}); await a.countdown();
  a.step(1210);
  assert.equal(a.api.state(), 'finished'); assert.equal(a.el('arcade-overlay-title').textContent, '本次试玩完成');
  assert.equal(a.el('arcade-overlay').children[2].children[0].textContent, 'Perfect 0');
  assert.equal(a.el('arcade-overlay').children[2].children[3].textContent, 'Miss 1');
  a.el('arcade-overlay').children[3].children[1].click();
  assert.equal(jumped.position, 1000); assert.equal(jumped.detail.positionMs, 6000); assert.equal(jumped.detail.gamePositionMs, 1000); assert.equal(a.api.isActive(), false);
});

test('a late fetch response cannot reopen a closed game or start audio', async () => {
  const a = app({deferFetch: true}); const opening = a.api.open('job', 'balanced--easy'); a.api.close(); a.resolve(); await opening;
  assert.equal(a.api.isActive(), false); assert.equal(a.el('arcade-panel').hidden, true); assert.equal(a.el('arcade-audio').plays, 0);
});

test('a stale game play promise cannot release a newer retry lease', async () => {
  const a = app({deferPlay: true});
  await a.api.open('job', 'first'); await a.countdown();
  const firstLease = a.manager.current(); a.api.close();
  await a.api.open('job', 'second'); await a.countdown();
  const secondLease = a.manager.current(); assert.notEqual(firstLease.generation, secondLease.generation);
  a.resolvePlay(0); await new Promise(resolve => setImmediate(resolve));
  assert.equal(a.manager.current().generation, secondLease.generation);
  a.resolvePlay(1); await new Promise(resolve => setImmediate(resolve));
  assert.equal(a.api.state(), 'playing'); assert.equal(a.el('arcade-audio').paused, false);
});

test('pause, resume countdown and explicit exit leave no game audio running', async () => {
  const a = app(); await a.api.open('job', 'balanced--easy'); await a.countdown(); assert.equal(a.el('arcade-audio').paused, false);
  a.key('Escape', 100); assert.equal(a.el('arcade-audio').paused, true); assert.equal(a.el('arcade-overlay-title').textContent, '已暂停');
  a.key('Escape', 200); await a.countdown(); assert.equal(a.el('arcade-audio').paused, false);
  a.el('arcade-exit').click(); assert.equal(a.el('arcade-audio').paused, true); assert.equal(a.el('arcade-panel').hidden, true);
});

test('native audio pause enters the paused game state and resumes without judging through the interruption', async () => {
  const frames = [], a = app({chart: longChart});
  await a.api.open('job', 'native-pause', {onFrame: snapshot => frames.push(snapshot)}); await a.countdown();
  a.setPerf(2400); a.input('keydown', 'KeyD', 1000); a.step(1500);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1);
  assert.ok(a.effectItems().length, 'the successful hit has a live visual effect');

  const audio = a.el('arcade-audio'); audio.paused = true;
  a.documentEvent('pause', {target: audio}); audio.trigger('pause', {target: audio});
  assert.equal(a.api.state(), 'paused');
  assert.equal(a.el('arcade-overlay').hidden, false);
  assert.equal(a.el('arcade-overlay-title').textContent, '已暂停');
  assert.equal(a.el('arcade-pause').textContent, '继续 Esc');
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1, 'an interruption freezes rather than resets combo');
  assert.equal(a.effectItems().length, 0, 'effects are cleared when native playback stops');
  assert.equal(frames.at(-1).state, 'paused'); assert.equal(frames.at(-1).valid, false);

  a.input('keyup', 'KeyD', 1600); a.input('keydown', 'KeyF', 1601);
  assert.equal(Number(a.el('arcade-combo-number').textContent), 1, 'input during the pause cannot judge notes');
  a.el('arcade-pause').click(); await a.countdown();
  assert.equal(a.api.state(), 'playing'); assert.equal(audio.paused, false);
  a.api.close(); assert.equal(a.api.state(), 'idle');
  assert.notEqual(a.el('arcade-overlay-title').textContent, '已暂停', 'internal close does not reopen the pause overlay');
});

test('empty presses and repeats do not build combo; hit and release effects remain separate', async () => {
  const a = app({chart: longChart}); await a.api.open('job', 'effects'); await a.countdown();
  a.input('keydown', 'KeyK', 400); a.input('keydown', 'KeyK', 450, true); a.input('keyup', 'KeyK', 500);
  assert.deepEqual(a.visualEvents.map(e => e.kind), ['press', 'release']); assert.equal(a.el('arcade-combo-number').textContent, '0');
  a.input('keydown', 'KeyD', 1000); a.input('keydown', 'KeyD', 1050, true);
  assert.equal(a.visualEvents.filter(e => e.kind === 'hit').length, 1); assert.equal(Number(a.el('arcade-combo-number').textContent), 1);
});

test('autoplay result calls itself a demonstration and says it is not a human score', async () => {
  const a = app({chart: {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 0, beat: [2, 0, 1]}]}});
  await a.api.open('job', 'demo', {autoplay: true}); await a.countdown(); a.step(1210);
  assert.equal(a.api.state(), 'finished'); assert.match(a.el('arcade-overlay-title').textContent, /演示/);
  assert.match(a.el('arcade-overlay').children.at(-1).textContent, /不代表人工/);
});

test('retry from the inline result screen clears the old summary before its new countdown', async () => {
  const a = app({chart: {time: [{beat: [0, 0, 1], bpm: 120}], note: [{column: 0, beat: [2, 0, 1]}]}});
  await a.api.open('job', 'retry'); await a.countdown(); a.step(1210);
  assert.equal(a.api.state(), 'finished');
  a.el('arcade-overlay').children[3].children[0].click();
  assert.equal(a.api.state(), 'countdown'); assert.equal(a.el('arcade-overlay').children.length, 2);
  assert.equal(a.el('arcade-overlay-title').textContent, '3'); assert.equal(a.el('arcade-score').children[0].children[0].textContent, 0);
  await a.countdown(); assert.equal(a.api.state(), 'playing'); assert.equal(a.el('arcade-audio').plays, 2);
});
