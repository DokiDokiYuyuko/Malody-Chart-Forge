/* Dedicated local YouTube login. Never opens the user's normal Edge profile. */
'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),http=require('node:http');
const {spawn,execFile}=require('node:child_process');
const {randomBytes}=require('node:crypto');
const {promisify}=require('node:util');
const runFile=promisify(execFile);
const ROOT=path.resolve(__dirname,'..');
const YOUTUBE_URLS=['https://www.youtube.com/','https://music.youtube.com/'];

function youtubeUrl(value){try{const u=new URL(value);return u.protocol==='https:'&&['www.youtube.com','youtube.com','music.youtube.com'].includes(u.hostname)&&u.pathname==='/watch'&&/^[A-Za-z0-9_-]{11}$/.test(u.searchParams.get('v')||'')?'https://www.youtube.com/watch?v='+u.searchParams.get('v'):null;}catch{return null;}}
function youtubeCookies(cookies,now=Date.now()/1000){
  return cookies.filter(c=>typeof c.domain==='string'&&/^(?:[a-z0-9-]+\.)*youtube\.com$/i.test(c.domain.replace(/^\./,''))&&
    typeof c.name==='string'&&c.name&&typeof c.value==='string'&&!/[\r\n\t]/.test(c.name+c.value+c.domain+(c.path||'/'))&&
    (!Number.isFinite(c.expires)||c.expires<=0||c.expires>now));
}
function authenticated(cookies){const names=new Set(cookies.filter(c=>c.value).map(c=>c.name));return names.has('SID')&&(names.has('SAPISID')||names.has('__Secure-3PAPISID')||names.has('LOGIN_INFO'));}
function netscape(cookies){
  const kept=youtubeCookies(cookies);
  if(!authenticated(kept))throw new Error('还没有识别到 YouTube 登录状态。请先登录，再回到这里点击连接。');
  return '# Netscape HTTP Cookie File\n# Local YouTube login; do not share this file.\n'+kept.map(c=>[
    (c.httpOnly?'#HttpOnly_':'')+c.domain,c.domain.startsWith('.')?'TRUE':'FALSE',c.path||'/',c.secure?'TRUE':'FALSE',
    Number.isFinite(c.expires)&&c.expires>0?Math.floor(c.expires):0,c.name,c.value].join('\t')).join('\n')+'\n';
}
function launchArgs(profile,url,{headless=false}={}){
  return ['--user-data-dir='+path.resolve(profile),'--remote-debugging-pipe',
    '--no-first-run','--no-default-browser-check',...(headless?['--headless=new','--disable-gpu']:['--new-window']),url];
}
function plainLoginArgs(profile,url){
  return ['--user-data-dir='+path.resolve(profile),'--no-first-run','--no-default-browser-check','--new-window',url];
}
async function plainLogin(root,edge,video){
  const session=randomBytes(8).toString('hex');
  const profile=path.join(root,'private','youtube-login',session);await fs.mkdir(profile,{recursive:true});
  console.log('请在新开的普通 Edge 窗口登录 YouTube，确认歌曲能播放，然后关闭这个专用窗口。关闭后会自动连接，无需导出文件。');
  const child=spawn(edge,plainLoginArgs(profile,video),{stdio:'ignore',windowsHide:false});
  await new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',(code)=>code===0?resolve():reject(new Error('登录窗口异常退出，请重试。')));});
  let browser,staged;
  try{
    browser=await startBrowser(root,edge,{headless:true,url:'https://www.youtube.com/',session});
    const text=netscape(await readYoutubeCookies(browser.client));
    staged=path.join(root,'private','youtube-login','verify-'+randomBytes(8).toString('hex')+'.txt');
    await fs.writeFile(staged,text,{flag:'wx',mode:0o600});
    await validateCookies(root,staged,video);await installCookies(root,text);
    console.log('连接成功。请回到制谱台重试下载，无需重启。');
  }finally{
    if(staged)await fs.unlink(staged).catch(()=>{});
    if(browser){await browser.client.call('Browser.close').catch(()=>{});browser.client.close();}
  }
}
async function findEdge(){for(const base of [process.env['ProgramFiles(x86)'],process.env.ProgramFiles,process.env.LOCALAPPDATA]){
  if(!base)continue;const target=path.join(base,'Microsoft','Edge','Application','msedge.exe');try{await fs.access(target);return target;}catch{}}
  throw new Error('没有找到 Microsoft Edge。请先安装或修复 Edge。');
}
class PipeSocket extends EventTarget{
  constructor(child){super();this.child=child;this.buffer=Buffer.alloc(0);this.closed=false;
    child.stdio[4].on('data',chunk=>{this.buffer=Buffer.concat([this.buffer,chunk]);if(this.buffer.length>2*1024*1024){this.close();return;}let index;
      while((index=this.buffer.indexOf(0))>=0){const data=this.buffer.subarray(0,index).toString('utf8');this.buffer=this.buffer.subarray(index+1);this.dispatchEvent(new MessageEvent('message',{data}));}});
    const ended=()=>{if(!this.closed){this.closed=true;this.dispatchEvent(new Event('close'));}};
    child.once('close',ended);child.once('error',ended);child.stdio[3].on('error',ended);child.stdio[4].on('error',ended);
  }
  send(text){if(this.closed)throw new Error('closed');this.child.stdio[3].write(text+'\0');}
  close(){if(this.closed)return;this.closed=true;this.child.stdio[3].end();this.dispatchEvent(new Event('close'));}
}
class CDP{
  constructor(socket){this.socket=socket;this.next=0;this.pending=new Map();socket.addEventListener('message',event=>{
    let message;try{message=JSON.parse(String(event.data));}catch{return;}const pending=this.pending.get(message.id);if(!pending||pending.sessionId!==message.sessionId)return;
    this.pending.delete(message.id);clearTimeout(pending.timer);if(message.error)pending.reject(new Error('登录窗口暂时无法读取，请回到 YouTube 页面重试。'));else pending.resolve(message.result||{});
  });socket.addEventListener('close',()=>{for(const pending of this.pending.values()){clearTimeout(pending.timer);pending.reject(new Error('登录窗口已关闭，请重新运行一键连接。'));}this.pending.clear();});}
  static fromPipe(child){return new CDP(new PipeSocket(child));}
  call(method,params={},sessionId){const id=++this.next;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('登录窗口响应超时。'));},10000);
    this.pending.set(id,{resolve,reject,timer,sessionId});try{this.socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));}catch{clearTimeout(timer);this.pending.delete(id);reject(new Error('登录窗口连接已断开。'));}});}
  close(){this.socket.close();}
}
async function startBrowser(root,edge,{headless=false,url='https://www.youtube.com/',session=randomBytes(8).toString('hex')}={}){
  const profile=path.join(root,'private','youtube-login',session);await fs.mkdir(profile,{recursive:true});
  const child=spawn(edge,launchArgs(profile,url,{headless}),{stdio:['ignore','ignore','ignore','pipe','pipe'],windowsHide:headless});
  const client=CDP.fromPipe(child);
  try{await client.call('Browser.getVersion');return {client,profile,child};}
  catch{client.close();child.kill();throw new Error('专用 Edge 登录窗口没有就绪，请重新尝试。');}
}
async function readYoutubeCookies(client){
  const {targetInfos}=await client.call('Target.getTargets');
  const target=targetInfos.find(t=>{try{return t.type==='page'&&YOUTUBE_URLS.some(url=>new URL(url).hostname===new URL(t.url).hostname);}catch{return false;}});
  if(!target)throw new Error('请先打开 YouTube 并登录，保持 YouTube 页签打开，再回来点击连接。');
  const {sessionId}=await client.call('Target.attachToTarget',{targetId:target.targetId,flatten:true});
  try{return youtubeCookies((await client.call('Network.getCookies',{urls:YOUTUBE_URLS},sessionId)).cookies||[]);}
  finally{await client.call('Target.detachFromTarget',{sessionId}).catch(()=>{});}
}
async function latestVideo(root){
  const directory=path.join(root,'logs','music-source-errors');try{
    const files=await fs.readdir(directory);const rows=await Promise.all(files.filter(f=>f.endsWith('.json')).map(async name=>({name,mtime:(await fs.stat(path.join(directory,name))).mtimeMs})));
    for(const row of rows.sort((a,b)=>b.mtime-a.mtime).slice(0,50))try{const record=JSON.parse(await fs.readFile(path.join(directory,row.name),'utf8'));for(const arg of record.arguments||[]){const url=youtubeUrl(arg);if(url)return url;}}catch{}
  }catch{}return 'https://www.youtube.com/watch?v=UKZt1vq8bKI';
}
async function validateCookies(root,file,url){
  const exe=path.join(root,'runtime','bin','yt-dlp.exe');
  const args=['--ignore-config','--no-cache-dir','--no-playlist','--socket-timeout','15','--retries','1','--cookies',file,
    '--js-runtimes','node:'+process.execPath,'--skip-download','--dump-single-json',url];
  try{const {stdout}=await runFile(exe,args,{windowsHide:true,timeout:65000,maxBuffer:8*1024*1024,encoding:'utf8'});
    const result=JSON.parse(stdout);if(!Array.isArray(result.formats)||!result.formats.some(f=>f.acodec&&f.acodec!=='none'&&f.url))throw new Error('formats');
    return {id:result.id,title:String(result.title||'YouTube 音乐').slice(0,120)};
  }catch(error){const text=String(error.stderr||'').toLowerCase();
    if(/not a bot|confirm.*bot/.test(text))throw new Error('YouTube 仍要求机器人验证。请在登录窗口打开这首音乐、完成验证，再点击重试。');
    if(/private video|not available|removed|copyright/.test(text))throw new Error('这首音乐当前不可访问。可以换一首公开音乐验证连接。');
    if(error.code==='ENOENT')throw new Error('项目中的音乐下载组件缺失。');
    throw new Error('登录凭据已读取，但音乐读取验证暂未通过。请在 YouTube 确认能播放该歌曲，再重试。');
  }
}
async function installCookies(root,text){
  const privateDir=path.join(root,'private');await fs.mkdir(privateDir,{recursive:true});
  const cookiePath=path.join(privateDir,'youtube-cookies.txt'),settingsPath=path.join(root,'runtime','music-source-settings.json');
  await fs.mkdir(path.dirname(settingsPath),{recursive:true});
  const stamp=Date.now()+'-'+randomBytes(4).toString('hex');
  let oldCookie,oldSettings;try{oldCookie=await fs.readFile(cookiePath);}catch(error){if(error.code!=='ENOENT')throw error;}
  try{oldSettings=await fs.readFile(settingsPath);}catch(error){if(error.code!=='ENOENT')throw error;}
  if(oldCookie)await fs.writeFile(path.join(privateDir,'youtube-cookies-'+stamp+'.previous'),oldCookie,{flag:'wx',mode:0o600});
  if(oldSettings)await fs.writeFile(path.join(privateDir,'music-source-settings-'+stamp+'.previous'),oldSettings,{flag:'wx',mode:0o600});
  const stagedCookie=cookiePath+'.'+stamp+'.tmp',stagedSettings=settingsPath+'.'+stamp+'.tmp';
  try{
    await fs.writeFile(stagedCookie,text,{flag:'wx',mode:0o600});
    await fs.writeFile(stagedSettings,JSON.stringify({cookies_file:'private/youtube-cookies.txt'},null,2)+'\n',{flag:'wx',mode:0o600});
    await fs.rename(stagedCookie,cookiePath);
    try{await fs.rename(stagedSettings,settingsPath);}catch(error){if(oldCookie)await fs.writeFile(cookiePath,oldCookie);else await fs.unlink(cookiePath);throw error;}
  }finally{await fs.unlink(stagedCookie).catch(()=>{});await fs.unlink(stagedSettings).catch(()=>{});}
}

function page(token,url){return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>连接 YouTube · 星轨谱面工坊</title>
<style>body{font:18px/1.8 system-ui,"Microsoft YaHei UI";background:#eef7f7;color:#244c61;max-width:720px;margin:8vh auto;padding:24px}h1{font-size:30px}a,button{display:inline-block;background:#327d89;color:white;border:0;border-radius:9px;padding:12px 20px;font:inherit;text-decoration:none;cursor:pointer;margin:8px 12px 8px 0}button:disabled{opacity:.5}#status{padding:20px;background:white;border-radius:12px;white-space:pre-wrap}small{display:block;color:#587383}</style>
<h1>连接 YouTube</h1><p>1. 打开 YouTube，在新页签登录你的账号，并确认歌曲可以播放。</p><a href="${url}" target="_blank" rel="noopener noreferrer">打开 YouTube 登录</a>
<p>2. 登录完成后，回到本页点击连接。其余操作会自动完成。</p><label for="video">验证歌曲链接</label><input id="video" value="${url}" style="display:block;width:95%;font:inherit;padding:8px" aria-label="验证歌曲链接"><button id="connect">我已登录，自动连接</button><button id="close">关闭登录窗口</button>
<p id="status">等待登录。日常使用的 Edge 可以保持打开。</p><small>只读取这个专用窗口中的 YouTube 登录凭据，保存在本机项目 private 目录。不会显示凭据内容。</small>
<script>const base='/${token}';const status=document.querySelector('#status'),button=document.querySelector('#connect');
button.onclick=async()=>{button.disabled=true;status.textContent='正在读取登录状态并验证音乐访问，请稍候…';try{const response=await fetch(base+'/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#video').value})});const data=await response.json();status.textContent=data.message;if(data.ok){button.textContent='已连接';}else button.disabled=false;}catch{status.textContent='连接中断，请重新运行一键连接。';button.disabled=false;}};
document.querySelector('#close').onclick=()=>fetch(base+'/close',{method:'POST'}).catch(()=>{});</script></html>`;}

async function main(argv=process.argv.slice(2)){
  if(typeof EventTarget!=='function')throw new Error('本机 Node.js 版本过旧，请更新项目运行环境。');
  const edge=await findEdge();await fs.access(path.join(ROOT,'runtime','bin','yt-dlp.exe'));
  if(argv.includes('--check')){console.log('运行环境就绪：Edge、Node.js、音乐下载组件可用。');return;}
  const position=argv.indexOf('--url');const video=position>=0?youtubeUrl(argv[position+1]):await latestVideo(ROOT);
  if(!video)throw new Error('请使用有效的 YouTube 单曲 HTTPS 链接。');
  if(argv.includes('--plain-login')){await plainLogin(ROOT,edge,video);return;}
  const token=randomBytes(24).toString('hex');let browser,busy=false,connected=false,closing=false;
  const server=http.createServer(async(req,res)=>{
    const host='127.0.0.1:'+server.address().port;
    res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Referrer-Policy','no-referrer');
    if(req.headers.host!==host||(req.headers.origin&&req.headers.origin!=='http://'+host)){res.writeHead(403);res.end();return;}
    if(req.method==='GET'&&req.url==='/'+token){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(page(token,video));return;}
    res.setHeader('Content-Type','application/json; charset=utf-8');
    if(req.method==='POST'&&req.url==='/'+token+'/close'){res.end(JSON.stringify({ok:true}));void cleanup();return;}
    if(req.method!=='POST'||req.url!=='/'+token+'/connect'){res.writeHead(404);res.end('{}');return;}
    if(busy||!browser||closing){res.writeHead(409);res.end(JSON.stringify({ok:false,message:'登录窗口正在准备或验证中，请稍后再试。'}));return;}
    if(connected){res.end(JSON.stringify({ok:true,message:'已经连接，请回到制谱台重试下载。'}));return;}
    busy=true;let staged;
    try{let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>4096)throw new Error('歌曲链接过长。');}
      let requested;try{requested=body?JSON.parse(body).url:video;}catch{throw new Error('歌曲链接格式无效。');}
      const selectedVideo=youtubeUrl(requested);if(!selectedVideo)throw new Error('请输入有效的 YouTube 单曲 HTTPS 链接。');
      const text=netscape(await readYoutubeCookies(browser.client));
      staged=path.join(ROOT,'private','youtube-login','verify-'+randomBytes(8).toString('hex')+'.txt');await fs.writeFile(staged,text,{flag:'wx',mode:0o600});
      const result=await validateCookies(ROOT,staged,selectedVideo);
      if(closing)throw new Error('连接已取消，原配置保持不变。');
      // Install only the explicitly scoped YouTube cookies from this browser.
      await installCookies(ROOT,text);connected=true;
      try{const {targetInfos}=await browser.client.call('Target.getTargets');for(const target of targetInfos)if(target.type==='page'&&/^https:\/\/(?:www\.|music\.)?youtube\.com\//.test(target.url))await browser.client.call('Target.closeTarget',{targetId:target.targetId}).catch(()=>{});}catch{}
      res.end(JSON.stringify({ok:true,message:'连接成功，已读到歌曲信息：'+result.title+'\n可以关闭这个登录窗口，回到制谱台重试下载。无需重启制谱台。'}));
      console.log('YouTube 连接成功；凭据已保存在本机，原配置已备份。');
    }catch(error){res.end(JSON.stringify({ok:false,message:error.message||'连接未完成，请重试。'}));}
    finally{busy=false;if(staged)await fs.unlink(staged).catch(()=>{});}
  });
  const cleanup=async()=>{if(closing)return;closing=true;clearInterval(watch);clearTimeout(lifetime);if(browser){await browser.client.call('Browser.close').catch(()=>{});browser.client.close();}server.close();};
  let watch,lifetime;
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  try{browser=await startBrowser(ROOT,edge,{url:'http://127.0.0.1:'+server.address().port+'/'+token});}
  catch(error){server.close();throw error;}
  console.log('请在新开的专用 Edge 窗口中完成登录，再点击“我已登录，自动连接”。');
  watch=setInterval(async()=>{if(busy||closing)return;try{const {targetInfos}=await browser.client.call('Target.getTargets');if(!targetInfos.some(t=>t.type==='page'))await cleanup();}catch{await cleanup();}},3000);
  lifetime=setTimeout(()=>{console.log('登录窗口会话已结束；需要时可以再次运行。');void cleanup();},30*60*1000);
  process.once('SIGINT',()=>{void cleanup();});process.once('SIGTERM',()=>{void cleanup();});
}
module.exports={youtubeUrl,youtubeCookies,authenticated,netscape,launchArgs,plainLoginArgs,CDP,startBrowser,readYoutubeCookies,installCookies,validateCookies,page,findEdge,latestVideo};
if(require.main===module)main().catch(error=>{console.error('连接未完成：'+(error.message||'请重新尝试。'));process.exitCode=1;});
