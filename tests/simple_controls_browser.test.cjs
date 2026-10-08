const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const playwright=(()=>{try{return require('playwright');}catch{return null;}})();
const root=path.resolve(__dirname,'../web');

test('simple studio controls stay visible and clickable after a completed multi-difficulty job',{skip:!playwright,timeout:90000},async()=>{
 const levels=['easy','medium','hard','expert','master','lunatic'],patterns=['balanced','technical','speed','stream'];
 const rows=patterns.flatMap(pattern=>levels.map(difficulty=>({key:pattern+'--'+difficulty,chart_id:pattern+'--'+difficulty,pattern,difficulty,label:difficulty,notes:3,holds:1,average_nps:.25,peak_nps:2})));
 const job={id:'layout-fixture',title:'桌面布局测试',artist:'音乐人',status:'completed',progress:100,download:'/test.mcz',report:{duration:12,engine:'Mapperatorinator V32',patterns,difficulties:rows,previews:Object.fromEntries(rows.map(row=>[row.key,[[1000,0,null],[2000,1,3000],[2000,2,null]]])),warnings:[],quality_alerts:[]}};
 const wav=Buffer.alloc(44+12*44100*2);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(44100,24);wav.writeUInt32LE(88200,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
 const server=http.createServer((req,res)=>{
  const pathname=new URL(req.url,'http://localhost').pathname;
  if(pathname.startsWith('/api/')){
   let data={};
   if(pathname==='/api/jobs/layout-fixture')data=job;
   else if(pathname.endsWith('/files/audio.ogg')||pathname==='/api/test-audio'){
    res.setHeader('Content-Type','audio/wav');res.setHeader('Accept-Ranges','bytes');
    const match=/bytes=(\d+)-(\d*)/.exec(req.headers.range||'');
    if(match){const start=Number(match[1]),end=match[2]?Math.min(Number(match[2]),wav.length-1):wav.length-1;res.writeHead(206,{'Content-Range':`bytes ${start}-${end}/${wav.length}`,'Content-Length':end-start+1});return res.end(wav.subarray(start,end+1));}
    res.setHeader('Content-Length',wav.length);return res.end(wav);
   }
   else if(pathname.startsWith('/api/jobs/layout-fixture/charts/'))data={title:job.title,artist:job.artist,pattern:'balanced',difficulty:'expert',audio_url:'/api/test-audio',chart:{time:[{beat:[0,0,1],bpm:120}],note:[{column:0,beat:[2,0,1]},{column:1,beat:[4,0,1],endbeat:[6,0,1]},{column:2,beat:[4,0,1]}]}};
   else if(pathname.endsWith('/analysis')){res.writeHead(503,{'Content-Type':'application/json'});return res.end(JSON.stringify({detail:'No spectrum in control fixture'}));}
   else if(pathname==='/api/history')data={items:[],page:1,pages:1,total:0,total_all:0};
   else if(pathname==='/api/health')data={nps_star_direct_version:1,engines:{v32:{ready:true,label:'V32'},mug:{ready:false,label:'MuG'}}};
   else if(pathname==='/api/queue')data={items:[],running:0,waiting:0,paused:0};
   else if(pathname==='/api/music')data=[];
   res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(data));
  }
  const target=pathname==='/'?path.join(root,'index.html'):pathname.startsWith('/static/')?path.resolve(root,pathname.slice(8)):null;
  if(!target||!target.startsWith(root+path.sep)||!fs.existsSync(target)){res.writeHead(404);return res.end();}
  res.setHeader('Content-Type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml'})[path.extname(target)]||'application/octet-stream');res.end(fs.readFileSync(target));
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 let browser;
 try{
  browser=await playwright.chromium.launch({headless:true,channel:process.env.MALODY_BROWSER_CHANNEL||'msedge'});
  const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>localStorage.setItem('startrail.appearance.v1',JSON.stringify({theme:'light',trail:false})));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.waitForFunction(()=>document.querySelector('#v32-difficulty').closest('.field').hidden);
  const densityControls=await page.evaluate(()=>{
   const data=buildGenerationFormData(false);
   return {bounds:document.querySelectorAll('[data-difficulty][data-nps-bound]').length,ranges:JSON.parse(data.get('nps_ranges')),legacyV32Star:data.has('v32_difficulty'),hint:document.querySelector('#engine-hint').textContent};
  });
  assert.equal(densityControls.bounds,12);
  assert.deepEqual(densityControls.ranges,{easy:{min:2,max:3},medium:{min:4,max:6},hard:{min:6.8,max:10.2},expert:{min:10.4,max:15.6},master:{min:14.8,max:22.2},lunatic:{min:20.8,max:31.2}});
  assert.equal(densityControls.legacyV32Star,false);assert.match(densityControls.hint,/每档 NPS 范围.*独立生成/);
  await page.evaluate(()=>watchJob('layout-fixture'));await page.locator('#difficulty-tabs button').nth(5).waitFor();
  await page.waitForFunction(()=>document.querySelector('#audio').readyState>=2);
  for(const [width,height]of [[1366,768],[1440,900],[1920,1080]]){
   await page.setViewportSize({width,height});
   for(const theme of ['light','peach','moon','sea','dark']){
    await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
    const checks=await page.evaluate(()=>{
     const rect=s=>document.querySelector(s).getBoundingClientRect(),header=rect('.simple-header'),summary=rect('.workspace-heading'),transport=rect('.simple-transport'),track=rect('#seek-track'),range=rect('#seek'),rail=rect('.seek-rail');
     const contained=(inner,outer)=>inner.top>=outer.top-1&&inner.bottom<=outer.bottom+1&&inner.left>=outer.left-1&&inner.right<=outer.right+1;
     const hitPoints=e=>{const r=e.getBoundingClientRect();return [.2,.5,.8].every(y=>{const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height*y);return hit===e||e.contains(hit);});};
     const buttons=[...document.querySelectorAll('#difficulty-tabs button')];
     return {tabs:buttons.every(e=>contained(e.getBoundingClientRect(),header)&&hitPoints(e)),actions:['preview-toggle','arcade-autoplay','arcade-open'].every(id=>hitPoints(document.getElementById(id))),summaryBelowHeader:summary.top>=header.bottom,rangeInsideTrack:contained(range,track),railInsideTrack:contained(rail,track),rangeBelowButtons:range.top>=rect('.simple-transport-actions').bottom,trackInsidePanel:contained(track,transport),noPageOverflow:document.documentElement.scrollWidth<=innerWidth,buttonHeights:['preview-toggle','arcade-autoplay','arcade-open'].map(id=>rect('#'+id).height)};
    });
    for(const [key,value]of Object.entries(checks))assert.ok(Array.isArray(value)?value.every(height=>height===36):value,JSON.stringify({key,checks,width,height,theme}));
    // Each difficulty must remain selectable, including the final tab at the narrowest desktop size.
    for(const level of levels){await page.locator(`#difficulty-tabs [data-difficulty="${level}"]`).click();assert.equal(await page.locator('#arcade-open').getAttribute('data-chart-id'),'balanced--'+level);}
    assert.notEqual(await page.locator('#difficulty-tabs button.active').evaluate(el=>getComputedStyle(el).color),await page.locator('#difficulty-tabs button.active').evaluate(el=>getComputedStyle(el).backgroundColor));
   }
  }
  await page.locator('#preview-pattern').selectOption('technical');assert.equal(await page.locator('#arcade-open').getAttribute('data-chart-id'),'technical--easy');
  await page.locator('#preview-toggle').click();await page.waitForFunction(()=>!document.querySelector('#audio').paused);assert.equal(await page.locator('#preview-toggle .sr-only').textContent(),'暂停');
  await page.locator('#preview-toggle').click();await page.waitForFunction(()=>document.querySelector('#audio').paused);assert.equal(await page.locator('#preview-toggle .sr-only').textContent(),'试听');
  await page.locator('#seek').focus();await page.keyboard.press('End');await page.waitForFunction(()=>document.querySelector('#audio').currentTime>=11.9);
  await page.keyboard.press('Home');await page.waitForFunction(()=>document.querySelector('#audio').currentTime<.1);
  for(const [id,mode]of [['arcade-autoplay','autoplay'],['arcade-open','manual']]){
   await page.locator('#'+id).click();await page.waitForFunction(()=>ChartArcade.state()==='countdown');assert.equal(await page.locator('#arcade-panel').getAttribute('data-mode'),mode);await page.locator('#arcade-exit').click();assert.equal(await page.evaluate(()=>ChartArcade.isActive()),false);
  }
  for(const id of ['simple-spectrum-toggle','preview-focus']){await page.locator('#'+id).click();await page.locator('#difficulty-tabs button').first().click();await page.locator('#arcade-autoplay').click();await page.waitForFunction(()=>ChartArcade.state()==='countdown');await page.locator('#arcade-exit').click();await page.locator('#'+id).click();}
  assert.deepEqual(errors,[]);
 }finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
});
