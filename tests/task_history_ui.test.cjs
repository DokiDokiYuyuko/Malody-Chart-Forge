const {test} = require('node:test');
const assert = require('node:assert/strict');
const {historyQuery, summary, resultDestination, folderIdentity, createController} = require('../web/task-history.js');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.events = {}; this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this.scrollTop = 0; }
  append(...nodes) { nodes.forEach(node => { node.remove(); node.parentElement = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children.forEach(node => { node.parentElement = null; }); this.children = []; this.append(...nodes); }
  insertBefore(node, next) { node.remove(); const index = this.children.indexOf(next); node.parentElement = this; this.children.splice(index < 0 ? this.children.length : index, 0, node); }
  remove() { if (this.parentElement) { this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1); this.parentElement = null; } }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, action) { this.events[key] = action; }
  focus() { this.focused = true; }
}
function deferred() { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail; }); return {promise,resolve,reject}; }
function fixture({request,openResult,openJob} = {}) {
  const elements = new Map(), scheduled = new Map(); let timerId = 0;
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const pending = [], opened = [], apiCalls = [];
  const api = request || ((url, options) => { const next = deferred(); pending.push(next); apiCalls.push({url,options}); return next.promise; });
  const list = get('task-history-items'); new Element().append(list);
  const controller = createController({document:{getElementById:get,createElement:tag=>new Element(tag)},request:api,advanced:()=>({openResult:openResult || (target=>opened.push(target))}),openJob:openJob || (id=>opened.push({jobId:id})),setTimeout:action=>{const id=++timerId; scheduled.set(id,action); return id;},clearTimeout:id=>scheduled.delete(id)});
  function begin() { controller.enter(); controller.selectTab('completed'); }
  async function tick() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }
  return {controller,get,pending,opened,scheduled,apiCalls,begin,tick};
}
const record = overrides => ({id:'batch:p:b',record_id:'batch:p:b',title:'D/N/A',type:'advanced',project_id:'p',batch_id:'b',status:'completed',created_at:'2026-10-04T10:00:00Z',section_count:7,combination_count:1,success_count:7,failed_count:0,source_label:'人声 + 伴奏',jobs:[{id:'j',segment_name:'主歌',status:'completed',variants:['balanced--expert']}],...overrides});
const response = (items, page=1, total=items.length) => ({items,page,page_size:10,pages:Math.max(1,Math.ceil(total/10)),total});

test('history requests always use ten records and encoded title/type filters', () => {
  const params = new URLSearchParams(historyQuery({page:3,query:'D/N/A & Rain',type:'advanced'}));
  assert.equal(params.get('page_size'),'10'); assert.equal(params.get('page'),'3'); assert.equal(params.get('q'),'D/N/A & Rain'); assert.equal(params.get('type'),'advanced');
});

test('advanced, legacy, single-song and separated results navigate to their recorded identity', () => {
  assert.deepEqual(resultDestination(record()),{type:'advanced',projectId:'p',batchId:'b',jobId:undefined});
  assert.deepEqual(resultDestination(record({batch_id:null,job_id:'old'})),{type:'advanced',projectId:'p',batchId:null,jobId:'old'});
  assert.deepEqual(resultDestination({type:'song',job_id:'song'}),{type:'job',jobId:'song'});
  assert.deepEqual(resultDestination({type:'separation',project_id:'p',job_id:'sep',jobs:[{stem_set_id:'voices'}]}),{type:'advanced',projectId:'p',mode:'prepare',jobId:'sep',stemSetId:'voices'});
  assert.equal(resultDestination({type:'advanced',project_id:'p'}),null,'unlinked historical results never borrow another batch');
});

test('folder actions contain server-resolved record and task identities without paths', () => {
  const item = record({output_dir:'X:/example/anywhere'});
  assert.deepEqual(folderIdentity(item),{record_id:'batch:p:b'});
  assert.deepEqual(folderIdentity(item,{id:'j',output_dir:'X:/example/another'}),{record_id:'batch:p:b',job_id:'j'});
});

test('partially failed history summaries report delivered success and failure counts', () => {
  const text = summary(record({success_count:5,failed_count:2,status:'partial'}));
  assert.match(text,/7 段/); assert.match(text,/1 个组合/); assert.match(text,/成功 5 · 失败 2/);
  assert.doesNotMatch(text,/成功 7/);
});

test('late filter and page responses cannot replace current records', async () => {
  const f=fixture(); f.begin();
  f.get('task-history-type').value='song'; f.get('task-history-type').onchange();
  assert.equal(f.pending.length,2);
  f.pending[1].resolve(response([record({id:'job:song',record_id:'job:song',title:'当前筛选',type:'song',job_id:'song'})],1,15)); await f.tick();
  f.pending[0].resolve(response([record({title:'过期高级结果'})])); await f.tick();
  assert.equal(f.get('task-history-items').children[0].children[0].children[1].textContent,'当前筛选');
  assert.equal(f.get('task-history-next').disabled,false);
  f.get('task-history-next').onclick();
  assert.equal(new URLSearchParams(f.apiCalls[2].url.split('?')[1]).get('page'),'2');
  f.pending[2].resolve(response([record({title:'第二页'})],2,15)); await f.tick();
  assert.equal(f.get('task-history-page').textContent,'2 / 2'); assert.equal(f.get('task-history-next').disabled,true); assert.equal(f.get('task-history-prev').disabled,false);
});

test('background refresh preserves expanded task details and an unfinished search', async () => {
  const f=fixture(); f.begin(); f.pending[0].resolve(response([record()])); await f.tick();
  const card=f.get('task-history-items').children[0], details=card.children[3]; details.open=true;
  f.get('task-history-query').value='尚未提交的搜索';
  const polling=[...f.scheduled.values()][0]; polling(); f.pending[1].resolve(response([record({status:'partial',failed_count:1})])); await f.tick();
  assert.equal(f.get('task-history-items').children[0],card); assert.equal(card.children[3],details); assert.equal(details.open,true);
  assert.equal(f.get('task-history-query').value,'尚未提交的搜索');
  assert.match(card.children[1].children[1].textContent,/失败 1/);
});

test('leaving the queue or changing tabs invalidates pending history and stops polling', async () => {
  const f=fixture(); f.begin(); f.controller.leave(); f.pending[0].resolve(response([record()])); await f.tick();
  assert.equal(f.get('task-history-items').children.length,0); assert.equal(f.scheduled.size,0);
  f.controller.enter(); f.controller.selectTab('running'); f.pending[1].reject(new Error('旧请求故障')); await f.tick();
  assert.equal(f.get('task-history-error').textContent,''); assert.equal(f.scheduled.size,0);
});

test('result and folder buttons use the exact batch and task shown in the record', async () => {
  const calls=[],opened=[];
  const f=fixture({request:async(url,options)=>{calls.push({url,options});return url.startsWith('/api/task-history?')?response([record()]):{};},openResult:async target=>opened.push(target)});
  f.begin(); await f.tick(); const card=f.get('task-history-items').children[0];
  await card.children[2].children[0].onclick(); assert.deepEqual(opened,[{projectId:'p',batchId:'b',jobId:undefined}]);
  await card.children[2].children[1].onclick(); assert.deepEqual(JSON.parse(calls[1].options.body),{record_id:'batch:p:b'});
  await card.children[3].children[1].children[0].children[1].onclick(); assert.deepEqual(JSON.parse(calls[2].options.body),{record_id:'batch:p:b',job_id:'j'});
});

test('folder errors remain visible through a successful background refresh', async () => {
  const f=fixture({request:async(url)=>{if(url==='/api/task-history/open-folder')throw new Error('输出目录不存在');return response([record()]);}});
  f.begin(); await f.tick(); await f.get('task-history-items').children[0].children[2].children[1].onclick();
  assert.match(f.get('task-history-error').textContent,/输出目录不存在/); await f.controller.refresh();
  assert.equal(f.get('task-history-error').hidden,false);
});

test('a project history shortcut uses the project identity and resetting restores all records', async () => {
  const f=fixture(); f.controller.openCompleted({projectId:'p',type:'advanced',query:'D/N/A'});
  assert.equal(f.get('task-history-type').value,'advanced'); assert.equal(f.get('task-history-query').value,'D/N/A');
  assert.equal(new URLSearchParams(f.apiCalls[0].url.split('?')[1]).get('project_id'),'p');
  f.pending[0].resolve(response([record()])); await f.tick(); assert.match(f.get('task-history-summary').textContent,/当前项目/);
  f.get('task-history-reset').onclick();
  assert.equal(new URLSearchParams(f.apiCalls[1].url.split('?')[1]).has('project_id'),false);
  f.pending[1].resolve(response([])); await f.tick();
});
