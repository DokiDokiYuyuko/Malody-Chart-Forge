const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../web/advanced.js'),'utf8');
// Execute the transport functions from the actual workbench with isolated media.
function transport(offset=null){
  const audio={currentTime:12,duration:300}, s={project:{duration:300},position:0,submittedBatch:{autoAllowed:true}};
  let gameTime=4000, draws=0;
  const arcade={isActive:()=>offset!==null,position:()=>gameTime,seek:ms=>{gameTime=Math.max(0,Math.min(30000,ms));return true;}};
  const context={audio,s,window:{ChartArcade:arcade},arcadeTransport:offset===null?null:{offset},pendingSource:null,activeTrial:null,draw:()=>draws++};
  for(const name of ['position','seek']) vm.runInNewContext(source.match(new RegExp('  function '+name+'\\([^\\n]+'))[0],context);
  return {context,audio,s,position:()=>context.position(),seek:value=>context.seek(value),draws:()=>draws};
}
test('advanced game transport maps fragment local audio to source clock and seeks only game media',()=>{
  const t=transport(81087);
  assert.equal(t.position(),85.087);t.seek(90);
  assert.equal(t.position(),90);assert.equal(t.audio.currentTime,12);assert.equal(t.s.position,90);
  t.seek(10);assert.equal(t.position(),81.087,'fragment seeks clamp to playable fragment');
  assert.equal(t.s.submittedBatch.autoAllowed,false);
});
test('assembly transport retains package preroll clock without subtracting 1.5 seconds',()=>{
  const t=transport(0);t.seek(1.5);assert.equal(t.position(),1.5);t.seek(0);assert.equal(t.position(),0);
});
test('preview and local stem audition keep their existing offset and pause-independent seek',()=>{
  const t=transport();t.seek(22);assert.equal(t.audio.currentTime,22);
  t.context.activeTrial={offset:80};t.seek(90);assert.equal(t.audio.currentTime,10);assert.equal(t.position(),90);
  t.context.pendingSource={at:100};assert.equal(t.position(),100);
});

function waveTransport({assembly=false,game=false,mode='finish'}={}){
  const waveform={getBoundingClientRect:()=>({left:0,top:0,width:100}),setPointerCapture(){}}, values=[];
  const context={s:{project:{segments:[],duration:100},assembly:assembly?{mapping:[{source_start:0,output_start:1500,output_end:11500},{source_start:20000,output_start:11500,output_end:21500}]}:null,mode,draft:null},SR:1000,drag:null,
    window:{ChartArcade:{isActive:()=>game},SpectrumPane:require('../web/spectrum.js')}, $:()=>waveform,
    windowRange:()=>[0,30],F:{scrubPosition:(x,left,width,length)=>Math.max(0,Math.min(length,(x-left)/width*length))},seek:t=>values.push(t),setRange:(a,b)=>values.push([a,b])};
  vm.runInNewContext(source.match(/  function seekSourceTime\([^\n]+/)[0],context);
  vm.runInNewContext(source.slice(source.indexOf("  const wave=$('adv-wave');"),source.indexOf("$('adv-zoom').oninput=$('adv-pan').oninput=drawTimeline;")),context);
  const event=x=>({button:0,clientX:x,clientY:60,pointerId:1,preventDefault(){}});
  return {context,values,down:x=>waveform.onpointerdown(event(x)),move:x=>waveform.onpointermove(event(x)),cancel:()=>waveform.onpointercancel()};
}
test('whole waveform continuously scrubs source and assembled clocks; cancellation ends dragging',()=>{
  const original=waveTransport({game:true});original.down(10);original.move(20);assert.deepEqual(original.values,[3,6]);original.cancel();original.move(30);assert.equal(original.values.length,2);
  const assembled=waveTransport({assembly:true,game:true});assembled.down(10);assembled.move(80);assert.deepEqual(assembled.values,[4.5,15.5]);
});
test('segment mode keeps range selection except during an active game',()=>{
  const selection=waveTransport({mode:'segment'});selection.context.$=()=>({checked:false});selection.down(10);selection.move(20);assert.deepEqual(JSON.parse(JSON.stringify(selection.values)),[3,[3,6]]);
  const game=waveTransport({mode:'segment',game:true});game.down(10);game.move(20);assert.deepEqual(game.values,[3,6]);
});

test('advanced frame clock survives reopen and obsolete callbacks cannot clear a newer transport',async()=>{
  const payload={chart:{},range:[81087,90000],revision:{id:'revision'}}, frames=[],canvas={getBoundingClientRect:()=>({top:0})};
  const c={s:{project:{id:'project'},variant:'master',assembly:{},position:0},SR:44100,arcadeTransport:null,audio:{currentTime:0,duration:300},activeTrial:null,
    selectedData:()=>payload,pauseAudio(){},draw(){c.drawCount=(c.drawCount||0)+1;},spectrumFrame(){c.spectrumCount=(c.spectrumCount||0)+1;},spectrumCanvas:canvas,$:()=>({value:8}),seek(){},
    resultAudio:{gameAudioUrl:()=>'/audio'},window:{ChartArcade:{close(){if(c.callback){c.callback.onClose();c.callback=null;}},async open(job,variant,options){c.callback=options;frames.push(options);}}}};
  const start=source.indexOf('  async function playGame('),end=source.indexOf("  $('adv-play-game').onclick",start);
  vm.runInNewContext(source.slice(start,end),c);
  await c.playGame(true);const first=frames[0];
  first.onFrame({displayTimeMs:18667.924},{travelMs:625,header:20,hit:400,runway:380},canvas);
  assert.equal(c.s.position,18.667924);assert.equal(c.drawCount,1);assert.equal(c.spectrumCount,1);
  await c.playGame(false);const current=c.arcadeTransport;
  first.onClose();assert.equal(c.arcadeTransport,current);
  const previousPosition=c.s.position;first.onFrame({displayTimeMs:100},{},canvas);assert.equal(c.s.position,previousPosition);
  frames[1].onFrame({displayTimeMs:40000},{travelMs:625,header:20,hit:400,runway:380},canvas);assert.equal(c.s.position,40);
  frames[1].onClose();assert.equal(c.arcadeTransport,null);assert.equal(c.audio.currentTime,40);
});

test('transport resources have a new cache identity in the served HTML',()=>{
  const html=fs.readFileSync(require.resolve('../web/index.html'),'utf8');
  for(const name of ['advanced.js','arcade.js','spectrum.js','advanced-workbench.css'])assert.ok(html.includes(`/static/${name}?v=20261007-transport-v1`));
});
