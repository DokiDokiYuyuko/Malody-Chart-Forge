const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function playback() {
  const listeners = new Map(), sounds = [];
  const audio = (name, tagName = 'AUDIO') => ({name, tagName, currentTime: 0, readyState: 4, paused: true, seeking: false, pauseCount: 0,
    pause() { this.paused = true; this.pauseCount++; }, play() { this.paused = false; return this.pending || Promise.resolve(); }});
  const first = audio('preview'), second = audio('game'), third = audio('imported-preview'); sounds.push(first, second, third);
  const document = {querySelectorAll: selector => selector === 'audio' ? sounds : [], addEventListener(name, handler) { const list = listeners.get(name) || []; list.push(handler); listeners.set(name, list); }};
  const window = {dispatchEvent() {}};
  const context = {window, document, performance: {now: () => 125}, console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/music-playback.js'), 'utf8'), context);
  return {api: window.MusicPlayback, first, second, third, listeners, sounds};
}

test('claim hands the only music slot to the game while keeping independent key sounds out of the manager', () => {
  const p = playback();
  const firstLease = p.api.claim(p.first, 'preview');
  assert.equal(p.api.isCurrent(firstLease), true);
  const secondLease = p.api.claim(p.second, 'arcade');
  assert.equal(p.first.paused, true);
  assert.equal(p.api.current().audio, p.second);
  assert.equal(p.api.current().owner, 'arcade');
  assert.equal(p.api.isCurrent(firstLease), false);
  assert.equal(p.api.snapshot(p.second).valid, false, 'a claimed element is not reported as audible until play');
  p.second.paused = false;
  assert.equal(p.api.snapshot(p.second).valid, true);
  p.second.seeking = true;
  assert.equal(p.api.snapshot(p.second).valid, false, 'seeking never exposes a judgment clock');
  p.second.seeking = false; p.second.readyState = 1;
  assert.equal(p.api.snapshot(p.second).valid, false, 'buffering never exposes a judgment clock');
  p.second.readyState = 4;
  assert.equal(p.api.isCurrent(secondLease), true);
});

test('stale play promises cannot revive a previous preview after a source handoff', async () => {
  const p = playback();
  let resolveOld;
  p.first.pending = new Promise(resolve => { resolveOld = resolve; });
  const oldLease = p.api.claim(p.first, 'advanced-preview');
  const oldAttempt = p.api.play(oldLease);
  const newLease = p.api.claim(p.second, 'arcade');
  const newAttempt = p.api.play(newLease);
  assert.equal(await newAttempt, true);
  resolveOld();
  assert.equal(await oldAttempt, false);
  assert.equal(p.first.paused, true);
  assert.equal(p.api.isCurrent(newLease), true);
  assert.equal(p.second.paused, false);
});

test('native playback is adopted and pauses other DOM music elements', () => {
  const p = playback();
  p.second.paused = false;
  for (const handler of p.listeners.get('play')) handler({target: p.third});
  assert.equal(p.second.paused, true);
  assert.equal(p.api.current().audio, p.third);
  assert.equal(p.api.current().owner, 'native');
});

test('release only stops the owner lease and invalidates its pending callbacks', () => {
  const p = playback();
  const lease = p.api.claim(p.first, 'preview');
  p.first.paused = false;
  assert.equal(p.api.release(lease), true);
  assert.equal(p.first.paused, true);
  assert.equal(p.api.isCurrent(lease), false);
  const replacement = p.api.claim(p.second, 'arcade');
  assert.equal(p.api.release(lease), false, 'an old owner cannot stop a newer source');
  assert.equal(p.api.isCurrent(replacement), true);
});

test('an old lease cannot pause a newer generation of the same audio element', () => {
  const p = playback();
  const oldLease = p.api.claim(p.first, 'preview');
  const newLease = p.api.claim(p.first, 'retry');
  p.first.paused = false;
  assert.equal(p.api.pause(oldLease), false);
  assert.equal(p.first.paused, false);
  assert.equal(p.api.pause(newLease), true);
  assert.equal(p.first.paused, true);
});
