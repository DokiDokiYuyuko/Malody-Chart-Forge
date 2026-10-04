/* Pure state helpers shared by the advanced authoring UI and its tests. */
(function attach(root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.AdvancedState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createAdvancedState() {
  'use strict';

  function formatTime(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return '0:00.000';
    const milliseconds = Math.max(0, Math.round(value * 1000));
    const wholeSeconds = Math.floor(milliseconds / 1000);
    const minutes = Math.floor(wholeSeconds / 60);
    const remainder = wholeSeconds % 60;
    const fraction = milliseconds % 1000;
    return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}.${String(fraction).padStart(3, '0')}`;
  }

  function formatTimeMs(milliseconds) {
    const value = Number(milliseconds);
    return formatTime(Number.isFinite(value) ? value / 1000 : 0);
  }

  function boundedRange(range, duration) {
    if (!Array.isArray(range) || range.length < 2) return null;
    const start = Number(range[0]);
    const end = Number(range[1]);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
    const limit = Math.max(0, Number.isFinite(Number(duration)) ? Number(duration) : 0);
    const a = Math.min(limit, Math.max(0, start));
    const b = Math.min(limit, Math.max(0, end));
    return b > a ? [a, b] : null;
  }

  // All timeline bounds are seconds. A missing/invalid selection falls back to full audio.
  function playbackBounds({ mode = 'full', duration = 0, selectionRange = null, assemblyDuration = null } = {}) {
    const total = Math.max(0, Number.isFinite(Number(duration)) ? Number(duration) : 0);
    if (mode === 'assembly') {
      const assembly = Number(assemblyDuration);
      return [0, Math.max(0, Number.isFinite(assembly) ? assembly : total)];
    }
    if (mode === 'selection') return boundedRange(selectionRange, total) || [0, total];
    return [0, total];
  }

  function revisionMatchesRange(revisionRange, currentRange) {
    if (!Array.isArray(revisionRange) || !Array.isArray(currentRange) || revisionRange.length < 2 || currentRange.length < 2) return false;
    const a0 = Number(revisionRange[0]), a1 = Number(revisionRange[1]);
    const b0 = Number(currentRange[0]), b1 = Number(currentRange[1]);
    return Number.isFinite(a0) && Number.isFinite(a1) && Number.isFinite(b0) && Number.isFinite(b1) && a0 === b0 && a1 === b1;
  }

  function latestMatchingRevision(versions, activeId, currentRange) {
    const rows = Array.isArray(versions) ? versions : [];
    const matches = rows.filter(row => row && row.id && revisionMatchesRange(row.range, currentRange));
    return matches.find(row => row.id === activeId)?.id || matches.at(-1)?.id || null;
  }

  function summarizeSource(sourceId, stemSets = []) {
    if (!sourceId || sourceId === 'original') return '原曲';
    if (String(sourceId).startsWith('assembly:')) return '剪辑成品';
    for (const set of Array.isArray(stemSets) ? stemSets : []) {
      for (const stem of set.stems || []) {
        if (stem.source_id !== sourceId) continue;
        const role = stem.role === 'vocals' ? '人声' : stem.role === 'accompaniment' ? '伴奏' : '声部';
        const title = set.name || set.title || (set.id ? `分离 ${String(set.id).slice(0, 6)}` : '已选版本');
        return `${role} · ${title}`;
      }
    }
    return null;
  }

  // Playback bounds only control the audio clock, never which chart is drawn.
  function previewNotes({ revision = null, notes = [], adoptedNotes = [], assembly = false } = {}) {
    return assembly || revision ? notes : adoptedNotes;
  }

  function revisionSource(revision) {
    const provenance = revision?.provenance || {};
    return ['vocals', 'accompaniment'].includes(provenance.source_role) && provenance.source_id
      ? provenance.source_id : 'original';
  }

  function revisionLabel(revision) {
    const role = revision?.provenance?.source_role;
    if (role === 'vocals') return '人声谱面';
    if (role === 'accompaniment') return '伴奏谱面';
    if (['fusion', 'fusion_candidate'].includes(revision?.kind)) return '融合谱面';
    return '原曲谱面';
  }

  // Commit a network response only if all of its selection context is current.
  function selectionMatches(expected, current) {
    return ['sequence', 'project', 'segment', 'variant'].every(key => expected[key] === current[key]);
  }

  function canRestorePreview({ assembly = false, revision = null, loading = false, playing = false } = {}) {
    return !assembly && !revision && !loading && !playing;
  }

  function settingsDiff(base, value) {
    const diff = {};
    const original = base && typeof base === 'object' ? base : {};
    const next = value && typeof value === 'object' ? value : {};
    for (const [key, current] of Object.entries(next)) {
      const previous = original[key];
      if (current && typeof current === 'object' && !Array.isArray(current) && previous && typeof previous === 'object' && !Array.isArray(previous)) {
        const nested = settingsDiff(previous, current);
        if (Object.keys(nested).length) diff[key] = nested;
      } else if (JSON.stringify(current) !== JSON.stringify(previous)) {
        diff[key] = current;
      }
    }
    return diff;
  }

  return { formatTime, formatTimeMs, playbackBounds, revisionMatchesRange, latestMatchingRevision, summarizeSource, settingsDiff, previewNotes, revisionSource, revisionLabel, selectionMatches, canRestorePreview };
});
