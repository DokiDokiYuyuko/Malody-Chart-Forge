const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../web/app.js'), 'utf8');

// Run the production handlers, renderer, selection/deletion logic and HTTP wrapper.
// Fetch deliberately ignores cancellation so sequence guards must also work.
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {};
    this.textContent = ''; this.value = ''; this.disabled = false;
    this.classList = {add: () => {}, toggle: () => {}};
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  querySelectorAll(selector) {
    const matches = element => selector.startsWith('.')
      ? (element.className || '').split(' ').includes(selector.slice(1))
      : element.tag === selector;
    return this.children.flatMap(child => child instanceof Element
      ? [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)] : []);
  }
  querySelector(selector) {
    if (selector === '.library-card-select input')
      return this.querySelector('.library-card-select')?.querySelector('input');
    return this.querySelectorAll(selector)[0] || null;
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  addEventListener() {}
}
function fixture() {
  const elements = new Map(), calls = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    URLSearchParams, AbortController, console,
    document: {getElementById: get, createElement: tag => new Element(tag),
      createTextNode: text => text, body: new Element()},
    window: {}, clearTimeout() {}, drawChart() {}, loadStorage: async () => {},
    difficultyLabel: value => value, statusLabels: {completed: '完成'},
    showError: (id, message) => { get(id).textContent = message; },
    fetch: (url, options = {}) => new Promise((resolve, reject) => {
      calls.push({url, options, reject, respond: (data, ok = true, invalidJson = false) =>
        resolve({ok, json: async () => { if (invalidJson) throw new SyntaxError('invalid JSON'); return data; }})});
    })
  });
  const run = code => vm.runInContext(code, context);
  run(source.slice(0, source.indexOf('function showView(')));
  run(source.slice(source.indexOf('async function request('), source.indexOf('\n}', source.indexOf('async function request(')) + 2));
  run(source.slice(source.indexOf('async function history()'), source.indexOf('function renderDiagnostics(')));
  run(source.slice(source.indexOf("$('history-prev').onclick"), source.indexOf("for (const id of ['engine-state'")));
  run(source.slice(source.indexOf('function libraryDeletionMessage('), source.indexOf("$('music-open-folder').onclick")));
  run(source.slice(source.indexOf('function showView('), source.indexOf('function showStep(')));
  get('history-sort').value = 'newest';
  return {get, calls, context, run, history: () => context.history(),
    state: () => run('({page:historyPage, loading:historyLoading, selected:[...librarySelection.keys()]})')};
}
const item = id => ({id, title: id, status: 'completed', download: '/download/' + id,
  engine: 'mug', type: 'song', created: '2026-10-07', difficulties: ['hard']});
const result = (page, pages = 3, ids = ['song-' + page]) =>
  ({page, pages, total: pages * 12, total_all: pages * 12, items: ids.map(item)});
const flush = () => new Promise(resolve => setImmediate(resolve));
async function seed(f, page = 1, pages = 3, ids) {
  const pending = f.history(); f.calls.at(-1).respond(result(page, pages, ids)); await pending;
}
function busy(f, page) {
  assert.equal(f.get('history-list').getAttribute('aria-busy'), 'true');
  assert.equal(f.get('history-page').getAttribute('role'), 'status');
  assert.equal(f.get('history-page').getAttribute('aria-live'), 'polite');
  assert.match(f.get('history-page').textContent, new RegExp(`加载第 ${page} 页`));
  assert.equal(f.get('history-prev').disabled, true);
  assert.equal(f.get('history-next').disabled, true);
}

test('pagination immediately announces loading and ignores repeated next/prev events', async () => {
  const f = fixture(); await seed(f);
  const cards = f.get('history-list').children;
  const pending = f.get('history-next').onclick(); busy(f, 2);
  f.get('history-next').onclick(); f.get('history-prev').onclick();
  assert.equal(f.calls.length, 2); assert.equal(f.state().page, 2);
  assert.equal(f.get('history-list').children, cards);
  f.calls[1].respond(result(2)); await pending;
  assert.equal(f.get('history-list').getAttribute('aria-busy'), 'false');
  assert.equal(f.get('history-prev').disabled, false);
  assert.equal(f.get('history-next').disabled, false);
  assert.match(f.get('history-page').textContent, /^2 \/ 3 页/);
});

test('network, HTTP and invalid JSON failures restore committed page and both boundaries', async () => {
  for (const page of [1, 2, 3]) for (const failure of ['network', 'http', 'json']) {
    const f = fixture(); await seed(f, page);
    const oldCards = f.get('history-list').children;
    const pending = f.get(page === 3 ? 'history-prev' : 'history-next').onclick();
    if (failure === 'network') f.calls.at(-1).reject(new Error('offline'));
    if (failure === 'http') f.calls.at(-1).respond({detail: 'server failed'}, false);
    if (failure === 'json') f.calls.at(-1).respond(null, true, true);
    await pending;
    assert.equal(f.state().page, page);
    assert.equal(f.get('history-prev').disabled, page === 1);
    assert.equal(f.get('history-next').disabled, page === 3);
    assert.equal(f.get('history-list').children, oldCards);
    assert.match(f.get('history-page').textContent, /加载失败/);
    assert.equal(f.state().loading, false);
  }
});

test('replacement aborts old fetch; its late success cannot overwrite latest cards', async () => {
  const f = fixture(); await seed(f);
  const old = f.get('history-next').onclick();
  f.get('history-query').value = 'new query';
  f.get('history-search-form').onsubmit({preventDefault() {}});
  assert.equal(f.calls[1].options.signal.aborted, true);
  assert.equal(f.calls[2].options.signal.aborted, false);
  const params = new URL(f.calls[2].url, 'http://local').searchParams;
  assert.equal(params.get('page'), '1'); assert.equal(params.get('q'), 'new query');
  f.calls[2].respond(result(1, 1, ['new'])); await flush();
  f.calls[1].respond(result(2, 3, ['stale'])); await old;
  assert.equal(f.get('history-list').children[0].dataset.recordId, 'new');
  assert.equal(f.get('history-next').disabled, true);
});

test('stale success leaves latest loading intact; stale abort cannot overwrite latest success', async () => {
  for (const staleSuccess of [true, false]) {
    const f = fixture(); await seed(f);
    const old = f.get('history-next').onclick(), latest = f.history();
    if (staleSuccess) {
      f.calls[1].respond(result(2, 3, ['old'])); await old; busy(f, 2);
      assert.equal(f.get('history-list').children[0].dataset.recordId, 'song-1');
    }
    f.calls[2].respond(result(2, 3, ['latest'])); await latest;
    if (!staleSuccess) {
      f.calls[1].reject(Object.assign(new Error('aborted'), {name: 'AbortError'})); await old;
    }
    assert.equal(f.get('history-list').children[0].dataset.recordId, 'latest');
    assert.equal(f.get('history-error').textContent, '');
    assert.equal(f.get('history-list').getAttribute('aria-busy'), 'false');
  }
});

test('failed page turn retries adjacent page without skipping; empty results disable both boundaries', async () => {
  const f = fixture(); await seed(f, 2);
  const failed = f.get('history-next').onclick();
  f.calls[1].reject(new Error('offline')); await failed;
  const retry = f.get('history-next').onclick(); busy(f, 3);
  assert.equal(new URL(f.calls[2].url, 'http://local').searchParams.get('page'), '3');
  f.calls[2].respond(result(3)); await retry;
  f.get('history-next').onclick(); assert.equal(f.calls.length, 3);
  const previous = f.get('history-prev').onclick(); busy(f, 2);
  f.get('history-prev').onclick(); assert.equal(f.calls.length, 4);
  f.calls[3].respond(result(2)); await previous;
  f.get('history-query').value = 'no matches';
  f.get('history-search-form').onsubmit({preventDefault() {}});
  f.calls[4].respond({page: 1, pages: 1, total: 0, total_all: 36, items: []}); await flush();
  assert.equal(f.get('history-prev').disabled, true); assert.equal(f.get('history-next').disabled, true);
  assert.equal(f.get('history-list').children[0].className, 'history-empty');
  assert.match(f.get('history-list').children[0].textContent, /没有匹配/);
  assert.equal(f.get('history-count').textContent, 36);
});

test('late old error/finally cannot clear replacement loading or overwrite its error', async () => {
  for (const finishOldFirst of [true, false]) {
    const f = fixture(); await seed(f);
    const old = f.get('history-next').onclick(), latest = f.history();
    assert.equal(f.calls[1].options.signal.aborted, true);
    if (finishOldFirst) {
      f.calls[1].reject(new Error('stale failure')); await old; busy(f, 2);
      assert.equal(f.get('history-error').textContent, '');
    }
    f.calls[2].reject(new Error('current failure')); await latest;
    if (!finishOldFirst) { f.calls[1].reject(new Error('stale failure')); await old; }
    assert.equal(f.get('history-error').textContent, 'current failure');
    assert.equal(f.state().page, 1); assert.equal(f.state().loading, false);
  }
});

test('first load failure is announced and entering library retries without cache', async () => {
  const f = fixture(), pending = f.history(); busy(f, 1);
  f.calls[0].reject(new Error('offline')); await pending;
  assert.equal(f.get('history-prev').disabled, true); assert.equal(f.get('history-next').disabled, true);
  assert.match(f.get('history-page').textContent, /加载失败/);
  f.context.showView('library'); busy(f, 1);
  f.calls[1].respond(result(1)); await flush();
  assert.equal(f.get('history-error').textContent, '');
});

test('selection survives page changes, sorting and return to current library page', async () => {
  const f = fixture(); await seed(f);
  f.get('library-select-page').onclick();
  const pending = f.get('history-next').onclick(); f.calls[1].respond(result(2)); await pending;
  const check = f.get('history-list').children[0].querySelector('.library-card-select input');
  check.checked = true; check.onchange();
  assert.deepEqual([...f.state().selected], ['song-1', 'song-2']);
  f.context.showView('library');
  assert.equal(new URL(f.calls[2].url, 'http://local').searchParams.get('page'), '2');
  f.calls[2].respond(result(2)); await flush();
  assert.equal(f.get('history-list').children[0].querySelector('.library-card-select input').checked, true);
  f.get('history-sort').value = 'title'; f.get('history-sort').onchange();
  const params = new URL(f.calls[3].url, 'http://local').searchParams;
  assert.equal(params.get('page'), '1'); assert.equal(params.get('sort'), 'title');
  f.calls[3].respond(result(1)); await flush();
  assert.deepEqual([...f.state().selected], ['song-1', 'song-2']);
});

test('filters and reset clear selection and supersede in-flight pagination', async () => {
  for (const id of ['history-engine', 'history-status', 'history-difficulty', 'history-query', 'history-reset']) {
    const f = fixture(); await seed(f); f.get('library-select-page').onclick();
    const old = f.get('history-next').onclick();
    if (id === 'history-query') {
      f.get(id).value = 'song'; f.get('history-search-form').onsubmit({preventDefault() {}});
    } else if (id === 'history-reset') f.get(id).onclick();
    else { f.get(id).value = 'chosen'; f.get(id).onchange(); }
    assert.equal(f.calls[1].options.signal.aborted, true);
    assert.equal(f.state().selected.length, 0); busy(f, 1);
    const params = new URL(f.calls[2].url, 'http://local').searchParams;
    assert.equal(params.get('page'), '1'); assert.equal(params.get('page_size'), '12');
    if (id.startsWith('history-') && !['history-query', 'history-reset'].includes(id))
      assert.equal(params.get(id.slice(8)), 'chosen');
    f.calls[2].respond(result(1)); await flush();
    f.calls[1].reject(new Error('old')); await old;
    assert.equal(f.get('history-error').textContent, '');
  }
});

test('deletion refresh fetches current data, clamps removed last page, retains partial failures', async () => {
  const f = fixture(); await seed(f, 3, 3, ['remove', 'locked']);
  f.get('library-select-page').onclick(); f.get('library-delete-selected').onclick();
  const deleting = f.get('library-delete-confirm').onclick();
  assert.deepEqual(JSON.parse(f.calls[1].options.body).record_ids, ['remove', 'locked']);
  f.calls[1].respond({results: [{record_id: 'remove', deleted: true}, {record_id: 'locked', error: '文件占用'}]});
  await flush(); busy(f, 3);
  assert.equal(new URL(f.calls[2].url, 'http://local').searchParams.get('page'), '3');
  f.calls[2].respond(result(2, 2, ['locked'])); await deleting;
  assert.equal(f.state().page, 2); assert.equal(f.get('history-next').disabled, true);
  assert.deepEqual([...f.state().selected], ['locked']);
  assert.match(f.get('history-error').textContent, /文件占用/);
  const refresh = f.history(); assert.equal(f.calls.length, 4);
  f.calls[3].respond(result(2, 2, ['locked', 'newly-generated'])); await refresh;
  assert.equal(f.get('history-list').children.length, 2);
  assert.match(f.get('history-error').textContent, /文件占用/);
});
