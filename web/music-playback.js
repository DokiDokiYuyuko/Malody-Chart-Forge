(function (root) {
  'use strict';

  let generation = 0;
  let active = null;
  let owner = null;

  function mediaElements() {
    return typeof document !== 'undefined' && document.querySelectorAll
      ? [...document.querySelectorAll('audio')]
      : [];
  }

  function stopOthers(keep) {
    for (const audio of mediaElements()) {
      if (audio !== keep) audio.pause();
    }
  }

  function notify() {
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      window.dispatchEvent(new CustomEvent('musicplaybackchange', {detail: snapshot()}));
    }
  }

  function claim(audio, nextOwner = 'music') {
    if (!audio) throw new TypeError('A music audio element is required');
    generation++;
    stopOthers(audio);
    active = audio;
    owner = nextOwner;
    notify();
    return Object.freeze({audio, owner: nextOwner, generation});
  }

  function isCurrent(lease) {
    return Boolean(lease && lease.audio === active && lease.generation === generation);
  }

  function play(leaseOrAudio, nextOwner) {
    const lease = leaseOrAudio?.audio ? leaseOrAudio : claim(leaseOrAudio, nextOwner);
    if (!isCurrent(lease)) return Promise.resolve(false);
    stopOthers(lease.audio);
    let attempt;
    try { attempt = lease.audio.play(); }
    catch (error) { return Promise.reject(error); }
    return Promise.resolve(attempt).then(() => {
      if (!isCurrent(lease)) {
        if (active !== lease.audio) lease.audio.pause();
        return false;
      }
      return true;
    }, error => {
      if (!isCurrent(lease)) return false;
      throw error;
    });
  }

  function pause(leaseOrAudio) {
    const audio = leaseOrAudio?.audio || leaseOrAudio;
    if (!audio) return false;
    if (leaseOrAudio?.audio && !isCurrent(leaseOrAudio)) return false;
    audio.pause();
    if (audio === active && (!leaseOrAudio?.audio || isCurrent(leaseOrAudio))) {
      active = null;
      owner = null;
      generation++;
      notify();
    }
    return true;
  }

  function release(lease, {pause: shouldPause = true} = {}) {
    if (!isCurrent(lease)) return false;
    if (shouldPause) lease.audio.pause();
    active = null;
    owner = null;
    generation++;
    notify();
    return true;
  }

  function snapshot(audio = active) {
    const perfTimeMs = typeof performance !== 'undefined' ? performance.now() : 0;
    return {
      audio: audio || null,
      owner: audio === active ? owner : null,
      generation,
      mediaTimeMs: Number(audio?.currentTime || 0) * 1000,
      perfTimeMs,
      paused: audio?.paused !== false,
      seeking: Boolean(audio?.seeking),
      readyState: Number(audio?.readyState || 0),
      valid: Boolean(audio && audio === active && !audio.paused && !audio.seeking && audio.readyState >= 2)
    };
  }

  function current() {
    return {audio: active, owner, generation};
  }

  const api = {claim, play, pause, release, isCurrent, snapshot, current};
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MusicPlayback = api;

  // Keep legacy call sites safe while app.js and advanced.js migrate to the API.
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('play', event => {
      const audio = event.target;
      if (!audio || String(audio.tagName || '').toLowerCase() !== 'audio') return;
      if (audio === active) return;
      generation++;
      stopOthers(audio);
      active = audio;
      owner = 'native';
      notify();
    }, true);
    document.addEventListener('pause', event => {
      if (event.target === active && event.target.paused) {
        active = null;
        owner = null;
        generation++;
        notify();
      }
    }, true);
  }
})(typeof window === 'object' ? window : globalThis);
