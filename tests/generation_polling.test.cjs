const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const advanced=fs.readFileSync(require.resolve('../web/advanced.js'),'utf8');
const app=fs.readFileSync(require.resolve('../web/app.js'),'utf8');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.scrollTop=240;this.hidden=false;}
  append(...items){this.children.push(...items);for(const item of items)if(item&&typeof item==='object')item.parentNode=this;}
  replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(key,value){this[key]=value;}
  closest(){return this.scrollParent;}
  querySelector(selector){if(selector==='option[value="2"]')return this.option||(this.option=new Element('option'));const matches=row=>selector.startsWith('.')?String(row.className||'').split(' ').includes(selector.slice(1)):row.tag===selector;for(const row of this.children){if(matches(row))return row;const found=row.querySelector?.(selector);if(found)return found;}return null;}
  querySelectorAll(selector){if(selector===':scope>.aw-task')return this.children.filter(row=>row.className==='aw-task');return [];}
  remove(){this.parentNode.children=this.parentNode.children.filter(row=>row!==this);}
}
function fixture(){
  const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const node=(tag,text,cls)=>Object.assign(new Element(tag),{textContent:text,className:cls});
  const state={project:{id:'p'},submittedBatch:{id:'b'},tasks:[{id:'j',status:'running',progress:25}],resultBatch:null};
  let resolve,reject;const requests=[];
  const context={s:state,$:get,node,base:()=>'/projects/p',api:()=>{const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});requests.push(promise);return promise;},taskCard:t=>{const card=node('article',t.id,'aw-task');card.dataset.taskId=t.id;card.append(node('details'));return card;},updateTaskCard:(card,t)=>{card.dataset.progress=t.progress;},renderDelivery:()=>{},renderVersions:()=>{},W:{completionCanOpen:()=>false},visible:true,playbackIntent:false,audio:{paused:true},window:{},openBatch:async()=>{}};
  vm.createContext(context);
  vm.runInContext(advanced.slice(advanced.indexOf('  function syncTaskCards('),advanced.indexOf('  async function pollTasks(')),context);
  vm.runInContext(advanced.slice(advanced.indexOf('  async function updateBatchDelivery('),advanced.indexOf("  $('aw-result-history').onclick")),context);
  const host=get('aw-generation-tasks');host.scrollParent=new Element();host.append(node('section','previous progress'));
  return{state,host,context,resolve:value=>resolve(value),reject:error=>reject(error),get};
}
const batch=()=>({id:'b',jobs:[{id:'j',status:'running'}]});
test('polling keeps progress visible while waiting and preserves expanded details across updates',async()=>{
  const f=fixture(),previous=f.host.children[0];
  const pending=f.context.updateBatchDelivery('p');assert.equal(f.host.children[0],previous);
  f.resolve(batch());await pending;
  const current=f.host.children[0],detail=f.host.querySelector('details');detail.open=true;
  const repeat=f.context.updateBatchDelivery('p');assert.equal(f.host.children[0],current);f.resolve(batch());await repeat;
  assert.equal(f.host.children[0],current);assert.equal(detail.open,true);
  f.state.tasks[0].progress=55;const changed=f.context.updateBatchDelivery('p');f.resolve(batch());await changed;
  assert.equal(f.host.querySelector('details').open,true);assert.equal(f.host.scrollParent.scrollTop,240);
});
test('failed or stale polling never clears the last progress snapshot',async()=>{
  const f=fixture(),previous=f.host.children[0];
  const failure=f.context.updateBatchDelivery('p');f.reject(new Error('network'));await assert.rejects(failure,/network/);
  assert.equal(f.host.children[0],previous);
  const stale=f.context.updateBatchDelivery('p');f.state.project={id:'other'};f.resolve(batch());await stale;
  assert.equal(f.host.children[0],previous);
});
test('queue renders active cards without library-only variables',async()=>{
  const f=fixture(),errors=[];Object.assign(f.context,{queueTimer:0,queueRequest:0,clearTimeout:()=>{},setTimeout:()=>1,statusLabels:{running:'正在生成',queued:'等待中'},showError:(id,message)=>errors.push(message),document:{createElement:tag=>new Element(tag),addEventListener(){}},request:async()=>({running:1,waiting:1,paused:0,concurrency:1,items:[{id:'run',title:'song',status:'running',progress:35},{id:'next',title:'song2',status:'queued'}]})});
  vm.runInContext(app.slice(app.indexOf('async function loadQueue()'),app.indexOf("$('queue-refresh').onclick")),f.context);
  await f.context.loadQueue();assert.deepEqual(errors,['']);assert.equal(f.get('queue-items').children.length,2);
  assert.equal(f.get('queue-items').children[1].children.at(-1).children[0].textContent,'取消');
});

test('queue shows every waiting segment, combinations and cancellation alongside the running task',async()=>{
  const f=fixture(),errors=[];const items=Array.from({length:5},(_,i)=>({id:String(i),title:'Song · segment '+i,status:i===0?'running':i===4?'paused':'queued',variants:['balanced--hard'],queue_order:100+i}));
  Object.assign(f.context,{queueTimer:0,queueRequest:0,clearTimeout:()=>{},setTimeout:()=>1,statusLabels:{running:'生成中',queued:'等待中',paused:'暂停'},showError:(id,message)=>errors.push(message),document:{createElement:tag=>new Element(tag),addEventListener(){}},request:async()=>({running:1,waiting:4,paused:1,concurrency:1,items})});
  vm.runInContext(app.slice(app.indexOf('async function loadQueue()'),app.indexOf("$('queue-refresh').onclick")),f.context);
  await f.context.loadQueue();assert.deepEqual(errors,['']);assert.equal(f.get('queue-items').hidden,false);assert.equal(f.get('queue-items').children.length,5);
  f.get('queue-items').children.forEach((card,i)=>{assert.equal(card.children[0].textContent,String(i+1).padStart(2,'0'));assert.ok(card.children[1].children.some(c=>c.textContent==='balanced · hard'));if(i>0)assert.equal(card.children.at(-1).children[0].textContent,'取消');});
  f.context.request=async()=>({running:0,waiting:0,paused:0,items:[]});await f.context.loadQueue();assert.equal(f.get('queue-summary').hidden,true);assert.equal(f.get('queue-items').children.length,0);
});

test('preview redraws cannot take the spectrum away from an active countdown or game',()=>{
  const f=fixture(),frames=[];Object.assign(f.context,{document:{documentElement:{dataset:{theme:'light'}}},seekDragging:false,visible:true,followPlayback:()=>{},selectedData:()=>null,continuousEnabled:()=>false,wholeEvents:[],segment:()=>null,position:()=>44.7,canvasSize:()=>({ctx:{},w:640,h:480}),prepare:x=>x,drawTimeline:()=>{},T:{time:x=>String(x)},R:{render:()=>{},geometry:()=>({hit:416})},spectrumFrame:(...args)=>frames.push(args),window:{ChartArcade:{isActive:()=>true}}});
  for(const id of ['adv-speed','adv-preview-empty'])f.get(id).querySelector=()=>new Element();
  f.get('adv-speed').value='8';
  vm.runInContext(advanced.slice(advanced.indexOf('  function draw(){'),advanced.indexOf('  function spectrumFrame(')),f.context);
  f.context.draw();assert.equal(frames.length,0);
  f.context.window.ChartArcade.isActive=()=>false;f.context.draw();assert.equal(frames.length,1);
});
test('left segment regeneration targets its own segment and excludes trimmed or busy ranges',async()=>{
  const f=fixture(),clicked=[];f.state.variant='balanced--hard';f.state.segmentId='a';f.state.tasks=[{segment_id:'b',status:'running'}];
  f.state.project={segments:[{id:'a',name:'片段 1',start_sample:0,end_sample:44100,active:{}},{id:'b',name:'片段 2',start_sample:44100,end_sample:88200,active:{}},{id:'tail',name:'片段 3',start_sample:88200,end_sample:132300,active:{}}],variants:[{}]};
  Object.assign(f.context,{SR:44100,T:{time:x=>String(x)},W:{contentEnd:()=>88200,taskFor:()=>null},F:{taskPresentation:()=>({status:'正在生成'})},busy:new Set(),chooseSegment:()=>{},regenerateCurrent:(sid,key)=>clicked.push([sid,key]),action:(text,fn,cls)=>Object.assign(new Element('button'),{textContent:text,onclick:fn,className:cls})});
  vm.runInContext(advanced.slice(advanced.indexOf('  function renderSegments(){'),advanced.indexOf('  async function chooseSegment(')),f.context);
  f.context.renderSegments();const rows=f.get('adv-segments').children;
  rows[0].children.at(-1).onclick();assert.deepEqual(clicked,[['a','balanced--hard']]);
  assert.equal(rows[1].children.at(-1).disabled,true);assert.equal(rows[2].children.length,3);
});
