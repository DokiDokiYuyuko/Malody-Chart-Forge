const test = require('node:test');
const assert = require('node:assert/strict');
const A = require('../web/advanced-result-audio.js');

const stemId = 'kim-set';
const stems = [{id: stemId, label: 'Kim RoFormer', stems: [
  {role: 'vocals', source_id: stemId + ':vocals'},
  {role: 'accompaniment', source_id: stemId + ':accompaniment'},
]}];
const context = {project: 'dna', segment: 'segment-1', variant: 'balanced--expert'};
function result(id, role) {
  const source = role === 'fusion' ? 'original' : stemId + ':' + role;
  return {audio_source_id: source, audio_url: '/api/advanced/projects/dna/revisions/' + id + '/audio?source_id=' + encodeURIComponent(source),
    revision: {id, kind: role === 'fusion' ? 'fusion' : 'stem_raw', provenance: {
      source_role: role, source_id: role === 'fusion' ? 'fusion:recipe' : source}},
    range: [0, 16 * 44100], events: [{id: 'note-' + id}]};
}
const vocal = result('vocal-1', 'vocals'), accompaniment = result('accompaniment-1', 'accompaniment');
const fusion = result('fusion-1', 'fusion');

test('fusion result uses the complete original, not its virtual fusion source or an old vocal selection', () => {
  const state = A.create();
  state.select(vocal, context);
  assert.equal(state.resolve(stems).source, stemId + ':vocals');
  state.override(stemId + ':vocals');
  state.select(fusion, context);
  assert.deepEqual(state.resolve(stems), {revisionId: 'fusion-1', source: 'original', label: '原曲',
    defaultSource: 'original', explicit: false, pending: false});
  assert.equal(A.defaultSource({...fusion, audio_source_id: 'fusion:recipe'}), 'original');
  assert.equal(A.defaultSource(fusion.revision), 'original');
});

test('stem previews use the source in the API result, with provenance as the historical fallback', () => {
  assert.equal(A.defaultSource(vocal), stemId + ':vocals');
  assert.equal(A.defaultSource(accompaniment), stemId + ':accompaniment');
  assert.equal(A.defaultSource(accompaniment.revision), stemId + ':accompaniment');
  const revised = {...accompaniment, audio_source_id: 'frozen-version:accompaniment'};
  assert.equal(A.defaultSource(revised), 'frozen-version:accompaniment');
  assert.equal(A.defaultSource({id: 'raw', kind: 'model_raw'}), 'original');
});

test('source labels describe the sound being played, including an explicit single-stem audition', () => {
  const state = A.create();
  state.select(fusion, context);
  assert.equal(state.override(stemId + ':vocals'), true);
  assert.equal(state.resolve(stems).label, '人声 · Kim RoFormer');
  assert.equal(state.resolve(stems).defaultSource, 'original');
  assert.equal(state.resolve(stems).explicit, true);
  state.clearOverride();
  assert.equal(state.resolve(stems).label, '原曲');
  assert.equal(A.sourceLabel('other:accompaniment'), '伴奏');
});

test('A/B changes clear manual audition even when returning to the same revision', () => {
  const state = A.create();
  state.select(fusion, {...context, slot: 'a'});
  state.override(stemId + ':vocals');
  state.select(accompaniment, {...context, slot: 'b'});
  assert.equal(state.resolve().source, stemId + ':accompaniment');
  assert.equal(state.resolve().explicit, false);
  state.override('original');
  state.select(fusion, {...context, slot: 'a'});
  assert.equal(state.resolve().source, 'original');
  assert.equal(state.resolve().explicit, false);
});

test('beginning another candidate clears the override immediately and rejects changes while loading', () => {
  const state = A.create();
  state.select(fusion, context);
  state.override(stemId + ':vocals');
  const token = state.begin({...context, revisionId: accompaniment.revision.id});
  assert.equal(state.resolve().pending, true);
  assert.equal(state.resolve().explicit, false);
  assert.equal(state.override(stemId + ':vocals'), false);
  assert.equal(state.commit(token, accompaniment), true);
  assert.equal(state.resolve().source, stemId + ':accompaniment');
});

test('a delayed candidate response cannot replace the newer candidate or its chosen sound', async () => {
  const state = A.create();
  let releaseVoice, releaseFusion;
  const voiceResponse = new Promise(resolve => { releaseVoice = resolve; });
  const fusionResponse = new Promise(resolve => { releaseFusion = resolve; });
  const voiceToken = state.begin({...context, revisionId: vocal.revision.id});
  const voiceLoad = voiceResponse.then(data => state.commit(voiceToken, data));
  const fusionToken = state.begin({...context, revisionId: fusion.revision.id});
  const fusionLoad = fusionResponse.then(data => state.commit(fusionToken, data));
  releaseFusion(fusion);
  assert.equal(await fusionLoad, true);
  state.override(stemId + ':accompaniment');
  releaseVoice(vocal);
  assert.equal(await voiceLoad, false);
  assert.equal(state.resolve().revisionId, fusion.revision.id);
  assert.equal(state.resolve().source, stemId + ':accompaniment');
  assert.equal(state.matches(voiceToken), false);
  assert.equal(state.matches(fusionToken), true);
});

test('reset and a different project invalidate pending responses, including reused revision IDs', () => {
  const state = A.create();
  const old = state.begin({...context, revisionId: vocal.revision.id});
  state.reset();
  assert.equal(state.commit(old, vocal), false);
  assert.equal(state.resolve().revisionId, null);
  assert.equal(state.resolve().source, 'original');
  const current = state.begin({...context, project: 'another-song', revisionId: vocal.revision.id});
  assert.equal(state.commit(old, vocal), false);
  assert.equal(state.commit(current, vocal), true);
});

test('the active request rejects a result for a different revision', () => {
  const state = A.create();
  const token = state.begin({...context, revisionId: fusion.revision.id});
  assert.equal(state.commit(token, vocal), false);
  assert.equal(state.resolve().pending, true);
  assert.equal(state.commit(token, fusion), true);
  const anotherState = A.create();
  assert.equal(anotherState.matches(token), false);
});

test('ordinary game and Autoplay requests retain the exact backend default audio URL', () => {
  const state = A.create();
  for (const data of [fusion, vocal, accompaniment]) {
    state.select(data, context);
    assert.equal(state.gameAudioUrl(data), data.audio_url);
    assert.equal(state.gameAudioUrl(), data.audio_url);
    assert.equal(state.resolve().source, data.audio_source_id);
  }
});

test('only an explicit override on this revision changes the game URL; switching results removes it', () => {
  const state = A.create();
  const before = JSON.stringify(fusion);
  state.select(fusion, context);
  state.override(stemId + ':vocals');
  const parsed = new URL(state.gameAudioUrl(), 'http://localhost');
  assert.equal(parsed.pathname, '/api/advanced/projects/dna/revisions/fusion-1/audio');
  assert.equal(parsed.searchParams.get('source_id'), stemId + ':vocals');
  assert.equal(state.gameAudioUrl(accompaniment), accompaniment.audio_url);
  assert.equal(JSON.stringify(fusion), before);
  state.select(accompaniment, context);
  assert.equal(state.gameAudioUrl(), accompaniment.audio_url);
  assert.equal(state.resolve().explicit, false);
});

test('explicit URL overrides preserve other parameters and fragments, replacing the source once', () => {
  const state = A.create();
  const data = {...fusion, audio_url: 'http://127.0.0.1:8766/audio?source_id=original&mode=test#position'};
  state.select(data, context);
  state.override(stemId + ':accompaniment');
  const parsed = new URL(state.gameAudioUrl());
  assert.equal(parsed.origin, 'http://127.0.0.1:8766');
  assert.equal(parsed.searchParams.get('mode'), 'test');
  assert.deepEqual(parsed.searchParams.getAll('source_id'), [stemId + ':accompaniment']);
  assert.equal(parsed.hash, '#position');
});

test('generator choices, virtual fusion IDs and assembly clocks cannot become fragment overrides', () => {
  const state = A.create();
  assert.equal(state.override(stemId + ':vocals'), false);
  state.select(fusion, context);
  for (const source of ['', null, 'dual:' + stemId, 'fusion:recipe', 'vocals_accompaniment', 'assembly:clip']) {
    assert.equal(state.override(source), false);
  }
  assert.equal(state.resolve().source, 'original');
  const token = state.select(null, context);
  assert.equal(state.commit(token, null), true);
  assert.equal(state.gameAudioUrl(), undefined);
  assert.equal(state.resolve().revisionId, null);
});
