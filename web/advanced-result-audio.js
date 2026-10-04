/* Result audition follows the selected revision; manual overrides are temporary. */
(function attach(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AdvancedResultAudio = api;
})(typeof window === 'object' ? window : globalThis, function createResultAudio() {
  'use strict';

  const original = 'original';
  const stemRoles = ['vocals', 'accompaniment'];

  function revisionOf(candidate) {
    return candidate?.revision || candidate;
  }

  function revisionId(candidate) {
    return revisionOf(candidate)?.id || null;
  }

  function alignedSource(source) {
    return typeof source === 'string' && source.length > 0 &&
      source !== 'vocals_accompaniment' && !/^(fusion:|dual:|assembly:)/.test(source);
  }

  function defaultSource(candidate) {
    const revision = revisionOf(candidate), provenance = revision?.provenance || {};
    // A fusion source identifies a chart recipe, not a playable audio file.
    if (provenance.source_role === 'fusion' || ['fusion', 'fusion_candidate'].includes(revision?.kind)) return original;
    if (alignedSource(candidate?.audio_source_id)) return candidate.audio_source_id;
    return stemRoles.includes(provenance.source_role) && alignedSource(provenance.source_id)
      ? provenance.source_id : original;
  }

  function sourceLabel(source, stemSets = []) {
    if (!source || source === original) return '原曲';
    if (source.startsWith('assembly:')) return '剪辑成品';
    for (const set of stemSets) {
      const stem = (set.stems || []).find(row => row.source_id === source);
      if (!stem) continue;
      const role = stem.role === 'vocals' ? '人声' : stem.role === 'accompaniment' ? '伴奏' : '声部';
      const name = set.label || set.name || set.title;
      return name ? role + ' · ' + name : role;
    }
    return source.endsWith(':vocals') ? '人声' : source.endsWith(':accompaniment') ? '伴奏' : '声部';
  }

  function overriddenUrl(url, source) {
    const parsed = new URL(url, 'https://result-audio.invalid');
    parsed.searchParams.set('source_id', source);
    if (/^[a-z][a-z\d+.-]*:/i.test(url)) return parsed.href;
    const prefix = url.startsWith('//') ? '//' + parsed.host : '';
    return prefix + parsed.pathname + parsed.search + parsed.hash;
  }

  function create() {
    let sequence = 0, token = null, candidate = null, explicitSource = null, pending = false;

    function begin(context = {}) {
      token = Object.freeze({sequence: ++sequence, project: context.project ?? null,
        segment: context.segment ?? null, variant: context.variant ?? null,
        revisionId: context.revisionId ?? null, slot: context.slot || 'a'});
      candidate = null;
      explicitSource = null;
      pending = true;
      return token;
    }

    function matches(expected) {
      return expected != null && expected === token;
    }

    function commit(expected, data) {
      if (!matches(expected) || revisionId(data) !== expected.revisionId) return false;
      candidate = data;
      pending = false;
      return true;
    }

    function select(data, context = {}) {
      const expected = begin({...context, revisionId: revisionId(data)});
      commit(expected, data);
      return expected;
    }

    function reset() {
      ++sequence;
      token = null;
      candidate = null;
      explicitSource = null;
      pending = false;
    }

    function override(source) {
      if (pending || !revisionId(candidate) || !alignedSource(source)) return false;
      explicitSource = source;
      return true;
    }

    function clearOverride() {
      explicitSource = null;
    }

    function resolve(stemSets = []) {
      const recommended = defaultSource(candidate), source = explicitSource || recommended;
      return Object.freeze({revisionId: revisionId(candidate), source, label: sourceLabel(source, stemSets),
        defaultSource: recommended, explicit: explicitSource !== null, pending});
    }

    function gameAudioUrl(data = candidate) {
      const url = data?.audio_url;
      if (!url || !explicitSource || pending || revisionId(data) !== revisionId(candidate)) return url;
      return overriddenUrl(url, explicitSource);
    }

    return {begin, matches, commit, select, reset, override, clearOverride, resolve, gameAudioUrl};
  }

  return {create, defaultSource, sourceLabel};
});
