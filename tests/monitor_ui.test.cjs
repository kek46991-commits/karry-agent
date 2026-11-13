const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/monitor.html','utf8');
const tick=(ms=10)=>new Promise(r=>setTimeout(r,ms));
const ok=(body,status=200)=>({ok:status<400,status,json:async()=>body});
const event=(seq,summary)=>({seq,at:1,kind:'log',source:'backend',level:'info',summary,data:null});
const sse=e=>new TextEncoder().encode(`event: activity\ndata: ${JSON.stringify(e)}\n\n`);
function body(chunks,{hang=false}={}){
  return {getReader:()=>({read:()=>chunks.length?Promise.resolve({done:false,value:chunks.shift()}):hang?new Promise(()=>{}):Promise.resolve({done:true})})};
}

test('after a server restart the feed resumes from the new sequence instead of the old one',async()=>{
  const streams=[];let startedAt=100,latest=5;
  const dom=new JSDOM(html,{url:'https://karry.example/monitor',runScripts:'dangerously',beforeParse(window){
    const later=window.setTimeout.bind(window);window.setTimeout=(fn,ms)=>later(fn,Math.min(ms,5));
    window.setInterval=()=>0;
    window.sessionStorage.setItem('karry-monitor-token','owner-secret');
    window.fetch=async url=>{
      if(url.startsWith('/api/monitor/state'))return ok({started_at:startedAt,latest_seq:latest,components:{},levels:{}});
      if(url.startsWith('/api/monitor/events'))return ok({events:[1,2,3,4,5].map(n=>event(n,`before ${n}`))});
      if(url.startsWith('/api/monitor/stream')){
        streams.push(url);
        if(streams.length===1){startedAt=200;latest=1;return {ok:true,status:200,body:body([])};}
        return {ok:true,status:200,body:body([sse(event(1,'after restart'))],{hang:true})};
      }
      return ok({},404);};
  }});
  await tick(80);
  const doc=dom.window.document;
  assert.deepEqual(streams.slice(0,2),['/api/monitor/stream?after=5','/api/monitor/stream?after=0']);
  const rows=[...doc.querySelectorAll('#feed .ev summary')].map(r=>r.textContent);
  assert.equal(rows.length,1);assert.ok(rows[0].includes('after restart'));
  dom.window.close();
});
