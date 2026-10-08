const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const app=fs.readFileSync(require.resolve('../web/app.js'),'utf8');
const downloads=fs.readFileSync(require.resolve('../web/music-download.js'),'utf8');
const url='http://i0.hdslb.com/bfs/archive/c8c6b651cb24b0241caa9fb77abfad02a800b916.jpg';
const secure=url.replace('http:','https:');

function node(tag){return {tag,hidden:false,children:[],classList:{add(){}},append(...items){this.children.push(...items);},removeAttribute(name){delete this[name];},set src(value){this.assignedPolicy=this.referrerPolicy;this.url=value;},get src(){return this.url;}};}

test('music download result and saved library card set no-referrer before loading Bilibili cover',()=>{
  const c={el:node,duration:String,openFolder(){},confirmDownload(){}};
  vm.createContext(c);
  const start=downloads.indexOf(' function coverSource('),end=downloads.indexOf(' function render()',start);
  vm.runInContext(downloads.slice(start,end),c);
  for(const saved of [false,true]){
    const card=c.card({thumbnail:url,title:'焚音打',duration:60,artist:'MyGO',filename:'music.m4a'},saved);
    const image=card.children[0].children[0];
    assert.equal(image.tag,'img');assert.equal(image.src,secure);
    assert.equal(image.assignedPolicy,'no-referrer');assert.equal(image.loading,'lazy');
    image.onerror();assert.equal(image.alt,'封面暂时不可用');
  }
  assert.equal(c.card({title:'No cover'},false).children[0].children[0].tag,'div');
});

test('Simple identified music cover uses no-referrer and retains the empty-cover fallback',()=>{
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,node('img'));return elements.get(id);};
  const c={$};vm.createContext(c);
  const start=app.indexOf('function coverSource('),end=app.indexOf("$('song-cover').onerror",start);
  vm.runInContext(app.slice(start,end),c);
  c.setCover(url);assert.equal($('song-cover').src,secure);
  assert.equal($('song-cover').assignedPolicy,'no-referrer');assert.equal($('song-cover').hidden,false);
  assert.equal($('cover-placeholder').hidden,true);
  c.setCover(null);assert.equal($('song-cover').hidden,true);assert.equal($('cover-placeholder').hidden,false);
});

test('chart package library cover applies the same policy before src',()=>{
  const start=app.indexOf('      const coverUrl = item.cover || item.thumbnail;');
  const end=app.indexOf('      const body=',start);
  assert.ok(start>0&&end>start);
  const c={item:{thumbnail:url,title:'焚音打'},document:{createElement:node}};vm.createContext(c);
  vm.runInContext(app.slice(app.indexOf('function coverSource('),app.indexOf('function setCover(')),c);
  vm.runInContext(app.slice(start,end)+'\nresult=cover;',c);
  assert.equal(c.result.src,secure);assert.equal(c.result.assignedPolicy,'no-referrer');
});

test('cover URL normalization is restricted to the Bilibili image domain',()=>{
  const c={};vm.createContext(c);
  vm.runInContext(app.slice(app.indexOf('function coverSource('),app.indexOf('function setCover(')),c);
  for(const input of [url,'//i0.hdslb.com/bfs/archive/x.jpg'])assert.ok(c.coverSource(input).startsWith('https://'));
  for(const input of ['/api/jobs/id/files/background.jpg','https://i.ytimg.com/a.jpg','http://hdslb.com.evil.example/a.jpg','http://example.com/hdslb.com/a.jpg'])assert.equal(c.coverSource(input),input);
});
