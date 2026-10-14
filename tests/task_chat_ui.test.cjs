const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/index.html','utf8');

function setup(options={}){
  const calls=[];
  const data=new Map(options.sessions||[]);
  let counter=data.size;
  const dom=new JSDOM(html,{url:'https://example.onrender.com/',runScripts:'dangerously',beforeParse(window){
    window.TextDecoder=TextDecoder;
    if(options.saved)window.localStorage.setItem('karry-session',options.saved);
    window.fetch=async(url,request={})=>{
      calls.push({url,request});
      if(options.fetch){const result=await options.fetch(url,request);if(result)return result;}
      const body=request.body?JSON.parse(request.body):{};
      if(url==='/api/sessions'&&request.method==='POST'){
        const sid='task-'+(++counter);const record={session_id:sid,title:body.title,messages:[],latest_url:'',last_run:null,persistence:'durable'};data.set(sid,record);
        return json({session_id:sid,title:record.title,persistence:'durable'});
      }
      if(url==='/api/sessions')return json([...data.values()].map(({session_id,title})=>({session_id,title})));
      if(url.startsWith('/api/sessions/')){const sid=decodeURIComponent(url.split('/').at(-1));return data.has(sid)?json(data.get(sid)):json({detail:'Session not found'},404);}
      if(url==='/api/chat'){
        const record=data.get(body.session_id);const reply='Answer for '+record.title;
        record.messages=[...record.messages,{role:'user',content:body.prompt},{role:'assistant',content:reply}];
        const result={...record,text:reply,response:reply,status:'completed',steps:1,tool_calls:0,metrics:{api_calls:1},trace:[]};
        return new Response('event: started\ndata: {}\n\nevent: completed\ndata: '+JSON.stringify(result)+'\n\n',{headers:{'content-type':'text/event-stream'}});
      }
      throw new Error('Unexpected URL: '+url);
    };
  }});
  const win=dom.window,doc=win.document;
  function authenticate(){doc.getElementById('apiToken').value='synthetic-owner';doc.getElementById('apiToken').dispatchEvent(new win.Event('input'));}
  return {dom,win,doc,calls,data,authenticate};
}
function json(data,status=200){return {ok:status>=200&&status<300,status,json:async()=>JSON.parse(JSON.stringify(data))};}
function record(id,title,content){return {session_id:id,title,messages:content?[{role:'assistant',content}]:[],latest_url:'',last_run:null,persistence:'durable'};}


test('initial render performs no network or model calls',()=>{
  const s=setup();assert.equal(s.calls.length,0);assert.equal(s.doc.getElementById('send').disabled,true);s.dom.window.close();
});

test('first connection creates only one empty task and never chats',async()=>{
  const s=setup();s.authenticate();await s.win.connectSessions();
  assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);
  assert.equal(s.calls.filter(c=>c.request.method==='POST').length,1);
  assert.match(s.doc.getElementById('currentTask').textContent,/最初のタスク/);
  assert.match(s.doc.getElementById('storageHint').textContent,/永続保存/);
  assert.equal(s.doc.getElementById('send').disabled,false);s.dom.window.close();
});

test('switching fetches and replaces history, without posting',async()=>{
  const s=setup({sessions:[['a',record('a','A','Only A')],['b',record('b','B','Only B')]]});
  s.authenticate();await s.win.connectSessions();assert.match(s.doc.getElementById('messages').textContent,/Only A/);
  await s.win.selectSession('b');const content=s.doc.getElementById('messages').textContent;
  assert.match(content,/Only B/);assert.doesNotMatch(content,/Only A/);
  assert.equal(s.calls.filter(c=>c.request.method==='POST').length,0);
  assert.equal(s.doc.querySelector('[data-session-id="b"]').getAttribute('aria-current'),'true');s.dom.window.close();
});

test('authentication input and reload restore the saved selection',async()=>{
  const s=setup({saved:'b',sessions:[['a',record('a','A','A')],['b',record('b','B','B')]]});
  s.authenticate();await s.win.connectSessions();assert.equal(s.doc.getElementById('session').textContent,'b');
  assert.equal(s.win.localStorage.getItem('karry-session'),'b');assert.equal(s.win.localStorage.length,1);
  assert.equal(s.calls[0].request.headers.Authorization,'Bearer synthetic-owner');s.dom.window.close();
});

test('creating a new task clears the selected task history',async()=>{
  const s=setup({sessions:[['a',record('a','A','Old history')]]});s.authenticate();await s.win.connectSessions();
  s.doc.getElementById('taskTitle').value='New task';await s.win.createNewSession();
  assert.equal(s.doc.getElementById('currentTask').textContent,'New task');
  assert.doesNotMatch(s.doc.getElementById('messages').textContent,/Old history/);
  assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);s.dom.window.close();
});

test('SSE request targets the selected task and restores server messages',async()=>{
  const s=setup({sessions:[['a',record('a','A','A history')],['b',record('b','B','B history')]]});s.authenticate();await s.win.connectSessions();await s.win.selectSession('b');
  s.doc.getElementById('prompt').value='Continue';await s.win.sendMessage({preventDefault(){}});
  const post=s.calls.find(c=>c.url==='/api/chat');assert.equal(JSON.parse(post.request.body).session_id,'b');
  assert.equal(JSON.parse(post.request.body).stream,true);assert.match(s.doc.getElementById('messages').textContent,/Answer for B/);
  assert.doesNotMatch(s.doc.getElementById('messages').textContent,/A history/);
  assert.equal(s.doc.getElementById('apiCalls').textContent,'1');s.dom.window.close();
});

test('task switching is disabled while a message is in flight',async()=>{
  let finish;
  const pending=new Promise(resolve=>{finish=resolve;});
  const s=setup({sessions:[['a',record('a','A')],['b',record('b','B')]],fetch:async(url)=>{if(url==='/api/chat')await pending;}});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Wait';
  const sending=s.win.sendMessage({preventDefault(){}});const before=s.calls.length;await s.win.selectSession('b');
  assert.equal(s.calls.length,before);assert.equal(s.doc.querySelector('[data-session-id="b"]').disabled,true);
  finish();await sending;assert.equal(s.doc.getElementById('session').textContent,'a');s.dom.window.close();
});

test('authentication errors do not create tasks or reveal old messages',async()=>{
  const s=setup({fetch:async()=>json({detail:'Invalid bearer token'},401)});s.authenticate();await s.win.connectSessions();
  assert.match(s.doc.getElementById('messages').textContent,/APIトークン/);
  assert.equal(s.doc.getElementById('send').disabled,true);assert.equal(s.calls.length,1);s.dom.window.close();
});

test('titles and messages are text, not executable HTML',async()=>{
  const attack='<img src=x onerror="window.bad=true">';
  const s=setup({sessions:[['a',record('a',attack,attack)]]});s.authenticate();await s.win.connectSessions();
  assert.equal(s.doc.getElementById('currentTask').textContent,attack);assert.equal(s.doc.querySelector('img'),null);assert.equal(s.win.bad,undefined);s.dom.window.close();
});

test('restored latest URL is displayed and unsafe URL schemes are ignored',async()=>{
  const r=record('a','A');r.latest_url='https://example.onrender.com/static/instant/demo.html';
  const s=setup({sessions:[['a',r]]});s.authenticate();await s.win.connectSessions();
  assert.equal(s.doc.getElementById('latestBox').hidden,false);assert.equal(s.doc.getElementById('latestUrl').href,r.latest_url);
  s.win.showLatest('javascript:alert(1)');assert.equal(s.doc.getElementById('latestBox').hidden,true);s.dom.window.close();
});

test('storage failure SSE is not shown as a successful reply',async()=>{
  const s=setup({sessions:[['a',record('a','A')]],fetch:async(url)=>url==='/api/chat'?new Response('event: error\ndata: {"status_code":503,"detail":"save failed"}\n\n'):null});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Generate once';await s.win.sendMessage({preventDefault(){}});
  assert.match(s.doc.getElementById('messages').textContent,/送信エラー: save failed/);
  assert.equal(s.doc.getElementById('prompt').value,'Generate once');assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,1);s.dom.window.close();
});


test('manual retry keeps the same request ID and never automatically resends',async()=>{
  let first=true;
  const s=setup({sessions:[['a',record('a','A')]],fetch:async(url)=>{if(url==='/api/chat'&&first){first=false;throw new Error('connection lost');}}});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Retry once';
  await s.win.sendMessage({preventDefault(){}});assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,1);
  await s.win.sendMessage({preventDefault(){}});const calls=s.calls.filter(c=>c.url==='/api/chat');
  assert.equal(calls.length,2);assert.equal(JSON.parse(calls[0].request.body).request_id,JSON.parse(calls[1].request.body).request_id);s.dom.window.close();
});
