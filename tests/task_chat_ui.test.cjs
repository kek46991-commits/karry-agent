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
        record.messages=[...record.messages,{role:'user',content:body.prompt,...(body.image_ids?.length?{images:body.image_ids}:{})},{role:'assistant',content:reply}];
        record.pending_images=(record.pending_images||[]).filter(meta=>!(body.image_ids||[]).includes(meta.id));
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

const IMAGE_ID='1'.repeat(32),IMAGE_ID2='2'.repeat(32);
const IMAGE_URL='data:image/png;base64,iVBORw0KGgo=';
function imageMeta(id=IMAGE_ID){return {id,filename:'screenshot.png',mime_type:'image/png',width:12,height:8,size_bytes:100,sha256:'a'.repeat(64)};}
function withImage(r,id=IMAGE_ID){const meta=imageMeta(id);r.pending_images=[meta];r.image_assets={[id]:meta};return r;}
function imageReply(meta=imageMeta()){return {status:'success',image_id:meta.id,image:meta,filename:meta.filename,session_id:'a',image_url:IMAGE_URL};}


test('upload uses owner auth and browser multipart boundary, and never chats',async()=>{
  const a=record('a','A');
  const s=setup({sessions:[['a',a]],fetch:async(url,request)=>{if(url==='/api/upload_image'){assert.equal(request.body.get('session_id'),'a');a.pending_images=[imageMeta()];a.image_assets={[IMAGE_ID]:imageMeta()};return json(imageReply());}if(url.startsWith('/api/images/'))return json({image_url:IMAGE_URL});}});
  s.authenticate();await s.win.connectSessions();
  await s.win.uploadSelectedImage({target:{files:[new s.win.File(['tiny image'],'screenshot.png',{type:'image/png'})]}});
  const upload=s.calls.find(c=>c.url==='/api/upload_image');assert.equal(upload.request.headers.Authorization,'Bearer synthetic-owner');assert.equal(upload.request.headers['Content-Type'],undefined);
  assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);assert.equal(s.doc.querySelector('#imagePreviews img').src,IMAGE_URL);assert.equal(s.doc.getElementById('prompt').required,false);s.dom.window.close();
});


test('removing an attachment updates server before the next text send',async()=>{
  const a=withImage(record('a','A'));
  const s=setup({sessions:[['a',a]],fetch:async(url,request)=>{if(url.startsWith('/api/images/'))return json({image_url:IMAGE_URL});if(url.startsWith('/api/pending_images/')){assert.equal(request.method,'DELETE');a.pending_images=[];return json({status:'detached'});}}});
  s.authenticate();await s.win.connectSessions();await s.win.removeImage(IMAGE_ID);
  assert.equal(s.doc.querySelector('#imagePreviews img'),null);assert.equal(s.doc.getElementById('prompt').required,true);
  s.doc.getElementById('prompt').value='Only text';await s.win.sendMessage({preventDefault(){}});
  const body=JSON.parse(s.calls.find(c=>c.url==='/api/chat').request.body);assert.deepEqual(body.image_ids,[]);s.dom.window.close();
});


test('task switching never carries another tasks attachment IDs',async()=>{
  const s=setup({sessions:[['a',withImage(record('a','A'))],['b',record('b','B')]],fetch:async(url)=>url.startsWith('/api/images/')?json({image_url:IMAGE_URL}):null});
  s.authenticate();await s.win.connectSessions();assert.equal(s.doc.querySelectorAll('#imagePreviews img').length,1);
  await s.win.selectSession('b');assert.equal(s.doc.querySelector('#imagePreviews img'),null);s.doc.getElementById('prompt').value='B only';await s.win.sendMessage({preventDefault(){}});
  const body=JSON.parse(s.calls.find(c=>c.url==='/api/chat').request.body);assert.equal(body.session_id,'b');assert.deepEqual(body.image_ids,[]);s.dom.window.close();
});


test('image-only send includes references, never data URI in the chat body',async()=>{
  const s=setup({sessions:[['a',withImage(record('a','A'))]],fetch:async(url)=>url.startsWith('/api/images/')?json({image_url:IMAGE_URL}):null});
  s.authenticate();await s.win.connectSessions();await s.win.sendMessage({preventDefault(){}});
  const post=s.calls.find(c=>c.url==='/api/chat'),body=JSON.parse(post.request.body);
  assert.equal(body.prompt,'添付画像を確認してください。');assert.deepEqual(body.image_ids,[IMAGE_ID]);assert.doesNotMatch(post.request.body,/data:image/);
  assert.equal(s.doc.querySelector('#imagePreviews img'),null);assert.match(s.doc.querySelector('.image-link').textContent,/screenshot/);s.dom.window.close();
});


test('preview rejects executable image URL schemes',async()=>{
  const s=setup({sessions:[['a',withImage(record('a','A'))]],fetch:async(url)=>url.startsWith('/api/images/')?json({image_url:'javascript:window.bad=true'}):null});
  s.authenticate();await s.win.connectSessions();await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(s.win.bad,undefined);assert.equal(s.doc.querySelector('#imagePreviews img').getAttribute('src'),null);assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);s.dom.window.close();
});


test('upload disables task switching and model send until it completes',async()=>{
  let finish;const pending=new Promise(resolve=>{finish=resolve;});
  const s=setup({sessions:[['a',record('a','A')],['b',record('b','B')]],fetch:async(url)=>{if(url==='/api/upload_image'){await pending;return json(imageReply());}}});
  s.authenticate();await s.win.connectSessions();const uploading=s.win.uploadSelectedImage({target:{files:[new s.win.File(['x'],'photo.png',{type:'image/png'})]}});
  assert.equal(s.doc.getElementById('send').disabled,true);await s.win.selectSession('b');assert.equal(s.doc.getElementById('session').textContent,'a');finish();await uploading;
  assert.equal(s.doc.getElementById('send').disabled,false);assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);s.dom.window.close();
});


test('failed generation keeps pending preview and restores text for a manual retry',async()=>{
  const a=withImage(record('a','A'));
  const s=setup({sessions:[['a',a]],fetch:async(url)=>{if(url.startsWith('/api/images/'))return json({image_url:IMAGE_URL});if(url==='/api/chat'){return new Response('event: completed\ndata: '+JSON.stringify({...a,status:'failed',text:'Model failed',response:'Model failed',trace:[],metrics:{},steps:1,tool_calls:0})+'\n\n');}}});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Read screenshot';await s.win.sendMessage({preventDefault(){}});
  assert.equal(s.doc.querySelectorAll('#imagePreviews img').length,1);assert.equal(s.doc.getElementById('prompt').value,'Read screenshot');assert.match(s.doc.getElementById('status').textContent,/失敗/);assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,1);s.dom.window.close();
});


test('changing attachments after a lost reply creates a new request ID',async()=>{
  const a=withImage(record('a','A'));let first=true;
  const s=setup({sessions:[['a',a]],fetch:async(url)=>{if(url.startsWith('/api/images/'))return json({image_url:IMAGE_URL});if(url==='/api/chat'&&first){first=false;throw new Error('lost reply');}}});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Read';await s.win.sendMessage({preventDefault(){}});
  a.pending_images=[imageMeta(IMAGE_ID2)];a.image_assets[IMAGE_ID2]=imageMeta(IMAGE_ID2);s.win.renderSession(a);s.doc.getElementById('prompt').value='Read';await s.win.sendMessage({preventDefault(){}});
  const posts=s.calls.filter(c=>c.url==='/api/chat').map(c=>JSON.parse(c.request.body));assert.notEqual(posts[0].request_id,posts[1].request_id);s.dom.window.close();
});

test('connection panel is before task grid and token is never prefilled',()=>{
  const s=setup();const panel=s.doc.getElementById('connectionPanel'),grid=s.doc.querySelector('.grid');
  assert.ok(panel.compareDocumentPosition(grid)&s.win.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.equal(s.doc.getElementById('apiToken').value,'');assert.equal(s.doc.querySelectorAll('#apiToken').length,1);
  assert.notEqual(s.doc.body.dataset.connection,'online');s.dom.window.close();
});
test('empty token stays local and never creates tasks or calls APIs',async()=>{
  const s=setup();await s.win.connectSessions();assert.equal(s.calls.length,0);
  assert.match(s.doc.getElementById('connectionNotice').textContent,/トークンを入力/);
  assert.equal(s.doc.getElementById('send').disabled,true);s.dom.window.close();
});
test('pasted Bearer prefix is normalized without changing server authentication',async()=>{
  const s=setup({sessions:[['a',record('a','A')]]});s.doc.getElementById('apiToken').value=' Bearer synthetic-owner ';
  await s.win.connectSessions();assert.equal(s.calls[0].request.headers.Authorization,'Bearer synthetic-owner');
  assert.equal(s.doc.body.dataset.connection,'online');assert.equal(s.win.localStorage.getItem('apiToken'),null);s.dom.window.close();
});
test('storage 503 explains server permission failure at the visible connection panel',async()=>{
  const s=setup({fetch:async()=>json({detail:'read failed',error_code:'storage_unavailable',hint:'対象バケットの読み取り権限を確認してください。'},503)});
  s.authenticate();await s.win.connectSessions();assert.match(s.doc.getElementById('connectionNotice').textContent,/バケットの読み取り権限/);
  assert.equal(s.doc.body.dataset.connection,'error');assert.equal(s.doc.getElementById('newTask').disabled,true);
  assert.equal(s.calls.length,1);assert.match(s.doc.getElementById('storageHint').textContent,/トークンを再入力しても/);s.dom.window.close();
});
test('image upload reuses the normalized owner token from the connection panel',async()=>{
  const a=record('a','A');
  const s=setup({sessions:[['a',a]],fetch:async(url)=>{if(url==='/api/upload_image'){a.pending_images=[imageMeta()];a.image_assets={[IMAGE_ID]:imageMeta()};return json(imageReply());}if(url.startsWith('/api/images/'))return json({image_url:IMAGE_URL});}});
  s.doc.getElementById('apiToken').value=' Bearer synthetic-owner ';await s.win.connectSessions();assert.equal(s.doc.body.dataset.connection,'online');
  await s.win.uploadSelectedImage({target:{files:[new s.win.File(['x'],'photo.png',{type:'image/png'})]}});
  const upload=s.calls.find(c=>c.url==='/api/upload_image');assert.equal(upload.request.headers.Authorization,'Bearer synthetic-owner');
  assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);assert.equal(s.doc.body.dataset.connection,'online');s.dom.window.close();
});
