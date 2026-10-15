const test=require('node:test'),assert=require('node:assert/strict');
const playwright=(()=>{try{return require('playwright');}catch{return null;}})();
const chromium=playwright?.chromium;
const origin=process.env.MALODY_BROWSER_URL;

test('desktop simple storage switch is off and sends explicit opt-in',{skip:!origin||!chromium,timeout:60000},async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin,{waitUntil:'networkidle'});
  await page.locator('#nav-studio').click();
  await page.locator('#step-settings').click();
  await page.locator('#mode-custom').click();
  await page.locator('#retain-debug-artifacts').evaluate(input=>{input.closest('details').open=true;});
  assert.equal(await page.locator('#retain-debug-artifacts').isChecked(),false);
  assert.equal(await page.evaluate(()=>buildGenerationFormData(false).get('retain_debug_artifacts')),'false');
  await page.locator('#retain-debug-artifacts').check();
  assert.equal(await page.evaluate(()=>buildGenerationFormData(false).get('retain_debug_artifacts')),'true');
  assert.match(await page.locator('.control-scroll').innerText(),/中间 WAV.*自动清理/);
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
});

test('desktop Advanced project exposes explicit debug retention setting',{skip:!origin||!chromium,timeout:60000},async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const defaults=await (await page.request.get(origin+'/api/advanced/defaults')).json();
  assert.equal(defaults.settings.retain_debug_artifacts,false);
  const pid='f'.repeat(32),project={id:pid,revision:0,title:'Storage fixture',artist:'Test',duration:2,samples:88200,
   sample_rate:44100,settings:defaults.settings,profile:'keyboard',variants:[{key:'balanced--hard',pattern:'balanced',difficulty:'hard'}],
   segments:[],assemblies:[],tempo:{bpm:120,points:[[0,120]],uncertain:true},waveform:[.1,.2],tail_trim:{enabled:true,cutoff_sample:88200}};
  const wav=Buffer.alloc(44+88200*2);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);
  wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(44100,24);
  wav.writeUInt32LE(88200,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
  await page.addInitScript(id=>localStorage.setItem('startrail.advanced-project',id),pid);
  await page.route('**/api/advanced/projects**',route=>{
   const path=new URL(route.request().url()).pathname;
   if(path.endsWith('/audio'))return route.fulfill({contentType:'audio/wav',body:wav});
   const data=path==='/api/advanced/projects'?{projects:[project]}:path==='/api/advanced/projects/'+pid?project:
    {plans:[],tasks:[],stem_sets:[],trials:[],sources:[],layouts:[]};
   return route.fulfill({json:data});
  });
  await page.goto(origin,{waitUntil:'networkidle'});
  await page.locator('#nav-advanced').click();
  await page.locator('#aw-retain-debug-artifacts').waitFor({state:'attached'});
  assert.equal(await page.locator('#aw-retain-debug-artifacts').isChecked(),false);
  assert.match(await page.locator('#aw-generation-params').textContent(),/重跑时从原曲重建/);
 } finally {await browser.close();}
});
