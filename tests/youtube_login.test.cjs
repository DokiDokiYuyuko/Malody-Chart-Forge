'use strict';

// No browser, subprocess, network, real credentials, or real runtime settings.
// Load each subject in its own VM so injected FS/timers never mutate globals.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const vm = require('node:vm');
const { EventTarget, Event } = globalThis;
const scriptPath = path.resolve(__dirname, '../tools/youtube_login.cjs');
const fixture = overrides => ({ domain: '.youtube.com', name: 'SID', value: 'synthetic-sid', path: '/', expires: -1, ...overrides });
const auth = () => [fixture(), fixture({ name: 'SAPISID', value: 'synthetic-sapisid' })];
const plain = value => JSON.parse(JSON.stringify(value));
const forbidden = () => { throw new Error('Test forbids browser, subprocess, network, and unscoped FS access'); };

async function subject(overrides = {}) {
  const module = { exports: {} };
  const dependencies = {
    'node:fs/promises': new Proxy({}, { get: () => forbidden }),
    'node:path': path,
    'node:http': { createServer: forbidden },
    'node:child_process': { spawn: forbidden, execFile: forbidden },
    'node:crypto': require('node:crypto'),
    'node:util': require('node:util'),
    ...overrides.dependencies,
  };
  const context = vm.createContext({
    module, exports: module.exports, __dirname: path.dirname(scriptPath),
    require: name => Object.hasOwn(dependencies, name) ? dependencies[name] : forbidden(),
    URL, EventTarget, Event, MessageEvent, Buffer,
    process: { execPath: process.execPath, env: {}, argv: [] },
    console: { log: forbidden, error: forbidden },
    setTimeout: forbidden, clearTimeout: forbidden,
    setInterval: forbidden, clearInterval: forbidden,
    ...overrides.globals,
  });
  new vm.Script(await fs.readFile(scriptPath, 'utf8'), { filename: scriptPath }).runInContext(context);
  return module.exports;
}

class MockSocket extends EventTarget {
  sent = [];
  closed = false;
  send(text) { this.sent.push(JSON.parse(text)); }
  message(data) { const event = new Event('message'); event.data = typeof data === 'string' ? data : JSON.stringify(data); this.dispatchEvent(event); }
  close() { this.closed = true; this.dispatchEvent(new Event('close')); }
}

function fakeClock() {
  let next = 0;
  const timers = new Map();
  return {
    timers,
    globals: {
      setTimeout(callback, ms) { const id = ++next; timers.set(id, { callback, ms }); return id; },
      clearTimeout(id) { timers.delete(id); },
    },
    fire() { for (const [id, timer] of [...timers]) { timers.delete(id); timer.callback(); } },
  };
}

// Every delegated FS operation is confined to a fresh test-owned directory.
async function diskSubject(t, fault = () => {}) {
  const root = await fs.mkdtemp(path.join(__dirname, '.youtube-login-test-'));
  assert.equal(path.dirname(root), __dirname);
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const scoped = {};
  for (const method of ['mkdir', 'readFile', 'writeFile', 'rename', 'unlink']) {
    scoped[method] = async (...args) => {
      for (const target of method === 'rename' ? args.slice(0, 2) : args.slice(0, 1)) {
        const relative = path.relative(root, path.resolve(target));
        assert.ok(relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative)), 'FS escaped test root');
      }
      fault(method, ...args);
      return fs[method](...args);
    };
  }
  return { root, api: await subject({ dependencies: { 'node:fs/promises': scoped } }) };
}

async function seed(root, cookie = 'synthetic-old-cookie', settings = '{"custom":"preserve-backup"}\n') {
  await fs.mkdir(path.join(root, 'private'), { recursive: true });
  await fs.mkdir(path.join(root, 'runtime'), { recursive: true });
  await fs.writeFile(path.join(root, 'private/youtube-cookies.txt'), cookie);
  await fs.writeFile(path.join(root, 'runtime/music-source-settings.json'), settings);
}
async function noTemps(root) {
  for (const folder of ['private', 'runtime']) assert.deepEqual((await fs.readdir(path.join(root, folder))).filter(name => name.endsWith('.tmp')), []);
}

test('youtubeUrl canonicalizes only HTTPS YouTube watch URLs', async () => {
  const { youtubeUrl } = await subject();
  for (const input of [
    'https://youtube.com/watch?v=AbC_0123-xy&t=12&list=ignored#fragment',
    'https://MUSIC.YOUTUBE.COM/watch?feature=share&v=AbC_0123-xy',
    'https://www.youtube.com:443/watch?v=AbC_0123-xy',
  ]) assert.equal(youtubeUrl(input), 'https://www.youtube.com/watch?v=AbC_0123-xy');
  for (const input of [null, '', 'garbage', 'http://youtube.com/watch?v=AbC_0123-xy',
    'https://youtube.com.evil.test/watch?v=AbC_0123-xy',
    'https://youtube.com@evil.test/watch?v=AbC_0123-xy',
    'https://evilyoutube.com/watch?v=AbC_0123-xy',
    'https://www.youtube.com.evil.test/watch?v=AbC_0123-xy',
    'https://youtube.com./watch?v=AbC_0123-xy',
    'https://youtu.be/AbC_0123-xy', 'https://youtube.com/shorts/AbC_0123-xy',
    'https://youtube.com/watch?v=short', 'https://youtube.com/watch?v=AbC_0123-xyZ',
    'https://youtube.com/watch?v=AbC%0A0123-xy', 'javascript:alert(1)',
  ]) assert.equal(youtubeUrl(input), null, String(input));
});

test('youtubeCookies filters domain, expiry, and control-character injection', async () => {
  const { youtubeCookies } = await subject();
  const accepted = [fixture(), fixture({ domain: 'music.youtube.com', expires: 1001 }), fixture({ domain: '.WWW.YouTube.com', expires: 0 }), fixture({ expires: undefined })];
  const rejected = [
    ...['google.com', '.google.com', 'youtube.com.evil.test', 'evilyoutube.com', '..youtube.com', 'youtube.com.', ''].map(domain => fixture({ domain })),
    fixture({ expires: 999 }), fixture({ expires: 1000 }), fixture({ name: '' }),
    fixture({ name: null }), fixture({ value: 123 }), fixture({ domain: null }),
  ];
  for (const field of ['domain', 'name', 'value', 'path']) for (const control of ['\r', '\n', '\t']) rejected.push(fixture({ [field]: 'injected' + control + 'line' }));
  const input = [...accepted, ...rejected];
  const before = plain(input);
  assert.deepEqual(plain(youtubeCookies(input, 1000)), plain(accepted));
  assert.deepEqual(plain(input), before, 'filter must not mutate CDP objects');
});

test('netscape preserves HttpOnly, secure, domain scope, session, and integer expiry', async () => {
  const { netscape } = await subject();
  const text = netscape([
    fixture({ httpOnly: true, secure: true }),
    fixture({ domain: 'www.youtube.com', name: 'SAPISID', path: '/watch', expires: 4102444800.9 }),
    fixture({ domain: '.google.com', name: 'SECRET', value: 'must-not-export' }),
    fixture({ name: 'EXPIRED', expires: 1 }),
    fixture({ name: 'BAD', value: 'line\nnext' }),
  ]);
  assert.match(text, /^# Netscape HTTP Cookie File\n/);
  assert.ok(text.includes('#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsynthetic-sid\n'));
  assert.ok(text.includes('www.youtube.com\tFALSE\t/watch\tFALSE\t4102444800\tSAPISID\tsynthetic-sid\n'));
  assert.doesNotMatch(text, /SECRET|must-not-export|EXPIRED|BAD/);
});

test('netscape rejects anonymous, empty, expired, and foreign-domain login evidence', async () => {
  const { netscape } = await subject();
  for (const cookies of [[], [fixture()], [fixture({ name: 'SAPISID' })],
    [fixture(), fixture({ name: 'SAPISID', value: '' })],
    [fixture(), fixture({ name: 'SAPISID', expires: 1 })],
    [fixture(), fixture({ name: 'SAPISID', domain: '.google.com' })],
  ]) assert.throws(() => netscape(cookies), /登录状态/);
  for (const name of ['SAPISID', '__Secure-3PAPISID', 'LOGIN_INFO']) assert.doesNotThrow(() => netscape([fixture(), fixture({ name })]));
});

test('launchArgs uses an explicit dedicated profile and private pipe without debug TCP', async () => {
  const { launchArgs } = await subject();
  const profile = path.resolve(__dirname, 'synthetic-dedicated-profile');
  for (const headless of [false, true]) {
    const args = plain(launchArgs(profile, 'http://127.0.0.1:12345/test-token', { headless }));
    assert.deepEqual(args.filter(arg => arg.startsWith('--user-data-dir=')), ['--user-data-dir=' + profile]);
    assert.ok(args.includes('--remote-debugging-pipe'));
    assert.ok(args.every(arg => !/^--remote-debugging-(port|address)(?:=|$)/.test(arg)));
    assert.ok(args.includes(headless ? '--headless=new' : '--new-window'));
    assert.equal(args.at(-1), 'http://127.0.0.1:12345/test-token');
    assert.ok(args.every(arg => !/--profile-directory|--remote-allow-origins|Edge[\\/]User Data/i.test(arg)));
  }
});

test('installCookies backs up exact original bytes, overwrites, and cleans staging files', async t => {
  const { root, api } = await diskSubject(t);
  await seed(root);
  await api.installCookies(root, 'synthetic-new-cookie\n');
  assert.equal(await fs.readFile(path.join(root, 'private/youtube-cookies.txt'), 'utf8'), 'synthetic-new-cookie\n');
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(root, 'runtime/music-source-settings.json'), 'utf8')), { cookies_file: 'private/youtube-cookies.txt' });
  const backups = (await fs.readdir(path.join(root, 'private'))).filter(name => name.endsWith('.previous'));
  assert.equal(backups.length, 2);
  assert.equal(await fs.readFile(path.join(root, 'private', backups.find(name => name.startsWith('youtube-cookies-'))), 'utf8'), 'synthetic-old-cookie');
  assert.equal(await fs.readFile(path.join(root, 'private', backups.find(name => name.startsWith('music-source-settings-'))), 'utf8'), '{"custom":"preserve-backup"}\n');
  await api.installCookies(root, 'synthetic-third-cookie');
  assert.equal((await fs.readdir(path.join(root, 'private'))).filter(name => name.endsWith('.previous')).length, 4);
  assert.equal(await fs.readFile(path.join(root, 'private/youtube-cookies.txt'), 'utf8'), 'synthetic-third-cookie');
  await noTemps(root);
});

test('installCookies creates a fresh configuration without fake backups', async t => {
  const { root, api } = await diskSubject(t);
  await api.installCookies(root, 'synthetic-fresh-cookie');
  assert.equal(await fs.readFile(path.join(root, 'private/youtube-cookies.txt'), 'utf8'), 'synthetic-fresh-cookie');
  assert.equal((await fs.readdir(path.join(root, 'private'))).filter(name => name.endsWith('.previous')).length, 0);
  await noTemps(root);
});

for (const stage of ['cookie-write', 'settings-write', 'cookie-rename', 'settings-rename']) {
  test('installCookies rolls back existing files after ' + stage + ' failure', async t => {
    const { root, api } = await diskSubject(t, (method, file, destination) => {
      const cookie = path.basename(file).startsWith('youtube-cookies.txt.');
      const settings = path.basename(file).startsWith('music-source-settings.json.');
      if ((stage === 'cookie-write' && method === 'writeFile' && cookie) ||
          (stage === 'settings-write' && method === 'writeFile' && settings) ||
          (stage === 'cookie-rename' && method === 'rename' && cookie) ||
          (stage === 'settings-rename' && method === 'rename' && settings)) {
        throw Object.assign(new Error('synthetic ' + stage), { code: 'EACCES' });
      }
    });
    await seed(root);
    await assert.rejects(api.installCookies(root, 'synthetic-replacement'), /synthetic/);
    assert.equal(await fs.readFile(path.join(root, 'private/youtube-cookies.txt'), 'utf8'), 'synthetic-old-cookie');
    assert.equal(await fs.readFile(path.join(root, 'runtime/music-source-settings.json'), 'utf8'), '{"custom":"preserve-backup"}\n');
    await noTemps(root);
  });
}

test('installCookies removes newly installed cookie when fresh settings commit fails', async t => {
  const { root, api } = await diskSubject(t, (method, file) => {
    if (method === 'rename' && path.basename(file).startsWith('music-source-settings.json.')) throw new Error('synthetic commit failure');
  });
  await assert.rejects(api.installCookies(root, 'synthetic-cookie'), /synthetic commit failure/);
  await assert.rejects(fs.access(path.join(root, 'private/youtube-cookies.txt')), { code: 'ENOENT' });
  await assert.rejects(fs.access(path.join(root, 'runtime/music-source-settings.json')), { code: 'ENOENT' });
  await noTemps(root);
});

test('CDP matches out-of-order IDs, ignores events/invalid JSON, and clears timers', async () => {
  const clock = fakeClock();
  const { CDP } = await subject({ globals: clock.globals });
  const socket = new MockSocket();
  const client = new CDP(socket);
  const first = client.call('Network.getCookies', { urls: ['https://www.youtube.com/'] }, 'session-a');
  const second = client.call('Target.getTargets');
  assert.deepEqual(socket.sent[0], { id: 1, method: 'Network.getCookies', params: { urls: ['https://www.youtube.com/'] }, sessionId: 'session-a' });
  socket.message('bad JSON'); socket.message({ method: 'Network.event' }); socket.message({ id: 999, result: {} });
  assert.equal(client.pending.size, 2);
  socket.message({ id: 2, result: { targetInfos: [] } });
  assert.deepEqual(plain(await second), { targetInfos: [] });
  socket.message({ id: 1, sessionId: 'session-a', result: { cookies: [] } });
  assert.deepEqual(plain(await first), { cookies: [] });
  assert.equal(client.pending.size, 0); assert.equal(clock.timers.size, 0);
});

test('CDP rejects a response from a different session even with a matching request ID', async () => {
  const clock = fakeClock();
  const { CDP } = await subject({ globals: clock.globals });
  const socket = new MockSocket();
  const client = new CDP(socket);
  const result = client.call('Network.getCookies', {}, 'expected-session');
  // Attach a rejection handler before cleanup, even when the assertion fails.
  result.catch(() => {});
  try {
    socket.message({ id: 1, sessionId: 'other-session', result: { cookies: [] } });
    assert.equal(client.pending.size, 1, 'wrong-session response must not consume the pending request');
    socket.message({ id: 1, sessionId: 'expected-session', result: { cookies: [] } });
    assert.deepEqual(plain(await result), { cookies: [] });
    assert.equal(clock.timers.size, 0);
  } finally { client.close(); }
});

test('CDP timeout, close, send failure, and protocol errors clean pending requests', async () => {
  const clock = fakeClock();
  const { CDP } = await subject({ globals: clock.globals });
  const socket = new MockSocket(); const client = new CDP(socket);
  const timeout = assert.rejects(client.call('Slow.method'), /响应超时/);
  assert.equal([...clock.timers.values()][0].ms, 10000);
  clock.fire(); await timeout;
  assert.equal(client.pending.size, 0);
  const error = assert.rejects(client.call('Error.method'), /暂时无法读取/);
  socket.message({ id: 2, error: { message: 'synthetic-sensitive-error' } }); await error;
  assert.equal(clock.timers.size, 0);
  const closed = [assert.rejects(client.call('Pending.one'), /已关闭/), assert.rejects(client.call('Pending.two'), /已关闭/)];
  client.close(); await Promise.all(closed);
  assert.equal(client.pending.size, 0); assert.equal(clock.timers.size, 0);
  socket.send = () => { throw new Error('synthetic send failure'); };
  await assert.rejects(client.call('Disconnected.method'), /已断开/);
  assert.equal(client.pending.size, 0); assert.equal(clock.timers.size, 0);
});

test('CDP.fromPipe frames and reassembles messages using mock stdio only', async () => {
  const { EventEmitter } = require('node:events');
  const clock = fakeClock();
  const { CDP } = await subject({ globals: clock.globals });
  const child = new EventEmitter();
  const input = new EventEmitter(); const output = new EventEmitter();
  const writes = []; let ended = false;
  input.write = text => { writes.push(text); return true; };
  input.end = () => { ended = true; };
  child.stdio = [null, null, null, input, output];
  const client = CDP.fromPipe(child);
  const result = client.call('Browser.getVersion');
  assert.equal(writes[0].at(-1), '\0');
  assert.deepEqual(JSON.parse(writes[0].slice(0, -1)), { id: 1, method: 'Browser.getVersion', params: {} });
  output.emit('data', Buffer.from('{"id":1,"result":{"product":"mock'));
  assert.equal(client.pending.size, 1);
  output.emit('data', Buffer.from(' browser"}}\0{"method":"ignored.event"}\0'));
  assert.deepEqual(plain(await result), { product: 'mock browser' });
  assert.equal(clock.timers.size, 0);
  const pending = assert.rejects(client.call('Pending.method'), /已关闭/);
  child.emit('close'); await pending;
  assert.equal(clock.timers.size, 0); assert.equal(client.pending.size, 0);
  // A separate open pipe verifies explicit closure ends only the mock writer.
  const second = new EventEmitter();
  second.stdio = [null, null, null, input, output];
  CDP.fromPipe(second).close(); assert.equal(ended, true);
});

test('readYoutubeCookies scopes Network.getCookies to attached YouTube session and always detaches', async () => {
  const { readYoutubeCookies } = await subject();
  for (const fail of [false, true]) {
    const calls = [];
    const client = { async call(method, params, sessionId) {
      calls.push({ method, params, sessionId });
      if (method === 'Target.getTargets') return { targetInfos: [
        { type: 'page', targetId: 'evil', url: 'https://www.youtube.com.evil.test/' },
        { type: 'worker', targetId: 'worker', url: 'https://www.youtube.com/' },
        { type: 'page', targetId: 'youtube', url: 'https://music.youtube.com/' },
      ] };
      if (method === 'Target.attachToTarget') return { sessionId: 'attached-session' };
      if (method === 'Network.getCookies') {
        if (fail) throw new Error('synthetic read failure');
        return { cookies: [...auth(), fixture({ domain: '.google.com' })] };
      }
      return {};
    } };
    if (fail) await assert.rejects(readYoutubeCookies(client), /synthetic read failure/);
    else assert.deepEqual(plain(await readYoutubeCookies(client)), auth());
    assert.deepEqual(plain(calls[1]), { method: 'Target.attachToTarget', params: { targetId: 'youtube', flatten: true } });
    assert.deepEqual(plain(calls[2]), { method: 'Network.getCookies', params: { urls: ['https://www.youtube.com/', 'https://music.youtube.com/'] }, sessionId: 'attached-session' });
    assert.deepEqual(plain(calls.at(-1)), { method: 'Target.detachFromTarget', params: { sessionId: 'attached-session' } });
  }
  await assert.rejects(readYoutubeCookies({ call: async () => ({ targetInfos: [] }) }), /先打开 YouTube/);
});

test('HTML posts only the edited song URL as JSON and confines CSRF token to local paths', async () => {
  const { page, youtubeUrl } = await subject();
  const token = 'synthetic-csrf-token-0123456789';
  const video = youtubeUrl('https://www.youtube.com/watch?v=AbC_0123-xy&token=external-secret');
  const html = page(token, video);
  assert.ok(html.includes("const base='/" + token + "'"), 'local page requires its own CSRF token');
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map(match => match[1]);
  assert.deepEqual(hrefs, ['https://www.youtube.com/watch?v=AbC_0123-xy']);
  assert.ok(hrefs.every(href => !href.includes(token)));
  assert.doesNotMatch(html, /external-secret|synthetic-sid|synthetic-sapisid|document\.cookie|localStorage|sessionStorage|innerHTML|https?:\/\/[^"\s]*\?token=/);
  // Execute only the inline UI script with synthetic DOM/fetch: no browser or HTTP.
  const editedVideo = 'https://www.youtube.com/watch?v=zyX_9876-ab';
  const elements = { '#status': {}, '#connect': {}, '#close': {}, '#video': { value: editedVideo } };
  const requests = [];
  const document = {
    querySelector(selector) { assert.ok(Object.hasOwn(elements, selector)); return elements[selector]; },
    get cookie() { throw new Error('UI must not read document.cookie'); },
  };
  const context = vm.createContext({
    document,
    async fetch(url, options) {
      requests.push({ url, options: plain(options) });
      return { async json() { return { ok: true, message: 'synthetic-success' }; } };
    },
  });
  const inline = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(inline);
  new vm.Script(inline[1]).runInContext(context);
  await elements['#connect'].onclick();
  await elements['#close'].onclick();
  assert.deepEqual(requests.map(request => request.url), ['/' + token + '/connect', '/' + token + '/close']);
  assert.ok(requests.every(request => request.options.method === 'POST'));
  assert.equal(elements['#status'].textContent, 'synthetic-success');
  assert.equal(typeof requests[0].options.body, 'string', 'connect must send a JSON body');
  assert.deepEqual(JSON.parse(requests[0].options.body), { url: editedVideo });
  assert.deepEqual(requests[0].options, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: editedVideo }),
  });
  assert.deepEqual(requests[1].options, { method: 'POST' });
  assert.doesNotMatch(JSON.stringify(requests.map(request => request.options)), /cookie|token|synthetic-sid|synthetic-sapisid/i);
});
