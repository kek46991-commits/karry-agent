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
    if(options.permission)Object.defineProperty(window.navigator,'permissions',{configurable:true,value:{query:async()=>({state:options.permission.state})}});
    if(options.microphonePolicy!==undefined)Object.defineProperty(window.document,'featurePolicy',{value:{allowsFeature:()=>options.microphonePolicy}});
    if(options.microphone){
      const microphone=options.microphone===true?{}:options.microphone;
      microphone.tracks=microphone.tracks||[{stopped:0,stop(){this.stopped++;}}];
      class MockRecognition{
        constructor(){this.started=0;this.stopped=0;this.aborted=0;microphone.instance=this;}
        start(){this.started++;this.onstart?.();}
        stop(){this.stopped++;this.onend?.();}
        abort(){this.aborted++;this.onend?.();}
        emitResult(...transcripts){this.onresult?.({results:transcripts.map(transcript=>[{transcript}])});}
        emitError(error){this.onerror?.({error});}
      }
      window.SpeechRecognition=MockRecognition;
      Object.defineProperty(window.navigator,'mediaDevices',{configurable:true,value:{getUserMedia:microphone.getUserMedia||(async()=>({getTracks:()=>microphone.tracks}))}});
      window.__microphone=microphone;
    }
    if(options.speech){
      window.speechSynthesis={spoken:[],cancelled:0,current:null,getVoices:()=>options.speech.voices||[],
        speak(utterance){this.spoken.push(utterance);this.current=utterance;},
        finish(){const utterance=this.current;this.current=null;utterance?.onend?.();},
        cancel(){this.cancelled++;const utterance=this.current;this.current=null;utterance?.onerror?.({error:'interrupted'});utterance?.onend?.();},
        addEventListener(){}};
      window.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
    }
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
      if(url.startsWith('/api/sessions/')&&url.endsWith('/messages')&&request.method==='DELETE'){
        const sid=decodeURIComponent(url.split('/').at(-2));if(!data.has(sid))return json({detail:'Session not found'},404);
        const cleared={...data.get(sid),messages:[],last_run:null,artifacts:[]};data.set(sid,cleared);return json(cleared);
      }
      if(url.startsWith('/api/sessions/')&&request.method==='DELETE'){
        const sid=decodeURIComponent(url.split('/').at(-1));if(!data.has(sid))return json({detail:'Session not found'},404);
        data.delete(sid);return json({session_id:sid,deleted:true});
      }
      if(url.startsWith('/api/sessions/')){const sid=decodeURIComponent(url.split('/').at(-1));return data.has(sid)?json(data.get(sid)):json({detail:'Session not found'},404);}
      if(url==='/api/secret/forget'){
        assert.equal(request.method,'POST');assert.match(JSON.parse(request.body).secret_session_id,/^[a-f0-9]{32}$/);return json({status:'forgotten',persistence:'none'});
      }
      if(url==='/api/chat'){
        const record=data.get(body.session_id);const reply='Answer for '+record.title;
        if(body.is_secret===true||body.isSecret===true){
          const result={session_id:body.session_id,status:'completed',is_secret:true,isSecret:true,secret_session_id:body.secret_session_id,persistence:'none',messages:[{role:'user',content:body.prompt},{role:'assistant',content:'Secret answer for '+record.title}],text:'Secret answer for '+record.title,response:'Secret answer for '+record.title,steps:0,tool_calls:0,metrics:{api_calls:0},trace:[]};
          return new Response('event: started\ndata: {}\n\nevent: completed\ndata: '+JSON.stringify(result)+'\n\n',{headers:{'content-type':'text/event-stream'}});
        }
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

test('task switching, typing, new sends and the microphone stay usable while a message is in flight',async()=>{
  let finish;
  const pending=new Promise(resolve=>{finish=resolve;});
  const microphone={};
  const s=setup({microphone,sessions:[['a',record('a','A')],['b',record('b','B','Only B')]],fetch:async(url,request)=>{if(url==='/api/chat'&&JSON.parse(request.body).session_id==='a')await pending;}});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Wait';
  const sending=s.win.sendMessage({preventDefault(){}});
  assert.equal(s.doc.getElementById('send').disabled,true);assert.equal(s.doc.getElementById('prompt').disabled,false);
  assert.equal(s.doc.getElementById('mic').disabled,false);assert.equal(s.doc.querySelector('[data-session-id="b"]').disabled,false);
  assert.equal(s.doc.querySelector('[data-delete-id="a"]').disabled,true);
  await s.win.startMicrophone();assert.equal(microphone.instance.started,1);microphone.instance.emitResult('次の指示');
  assert.equal(s.doc.getElementById('prompt').value,'次の指示');
  await s.win.selectSession('b');assert.equal(s.doc.getElementById('session').textContent,'b');assert.match(s.doc.getElementById('messages').textContent,/Only B/);
  assert.equal(s.doc.getElementById('send').disabled,false);
  s.doc.getElementById('prompt').value='B task';await s.win.sendMessage({preventDefault(){}});assert.match(s.doc.getElementById('messages').textContent,/Answer for B/);
  finish();await sending;
  assert.equal(s.doc.getElementById('session').textContent,'b');assert.doesNotMatch(s.doc.getElementById('messages').textContent,/Answer for A/);
  assert.doesNotMatch(s.doc.getElementById('trace').textContent,/Answer for A/);
  await s.win.selectSession('a');assert.match(s.doc.getElementById('messages').textContent,/Answer for A/);
  assert.equal(s.doc.getElementById('send').disabled,false);s.dom.window.close();
});

test('a reply for a task the user returned to is shown without wiping the next draft',async()=>{
  let finish;const pending=new Promise(resolve=>{finish=resolve;});
  const s=setup({sessions:[['a',record('a','A')],['b',record('b','B')]],fetch:async(url)=>{if(url==='/api/chat')await pending;}});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('prompt').value='Wait';
  const sending=s.win.sendMessage({preventDefault(){}});await s.win.selectSession('b');await s.win.selectSession('a');
  assert.match(s.doc.getElementById('messages').textContent,/処理中/);assert.equal(s.doc.getElementById('send').disabled,true);
  s.doc.getElementById('prompt').value='下書き';finish();await sending;
  assert.match(s.doc.getElementById('messages').textContent,/Answer for A/);assert.equal(s.doc.getElementById('prompt').value,'下書き');
  assert.equal(s.doc.getElementById('send').disabled,false);s.dom.window.close();
});

test('pressing the microphone again cancels a stuck permission request',async()=>{
  let resolveMedia;const microphone={getUserMedia:()=>new Promise(resolve=>{resolveMedia=resolve;})};
  const s=setup({microphone,sessions:[['a',record('a','A')]]});s.authenticate();await s.win.connectSessions();
  const starting=s.win.startMicrophone();await new Promise(r=>setTimeout(r,0));
  assert.equal(s.doc.getElementById('mic').disabled,false);await s.win.startMicrophone();
  assert.match(s.doc.getElementById('voiceStatus').textContent,/停止/);
  resolveMedia({getTracks:()=>microphone.tracks});await starting;assert.equal(microphone.instance.started,0);
  assert.equal(microphone.tracks[0].stopped,1);s.dom.window.close();
});

test('speech strips markdown, prefers a low male Japanese voice and speaks faster',async()=>{
  const voices=[{name:'Kyoko',voiceURI:'com.apple.voice.Kyoko',lang:'ja-JP'},{name:'Otoya',voiceURI:'com.apple.voice.Otoya',lang:'ja-JP'},{name:'Samantha',voiceURI:'en',lang:'en-US'}];
  const s=setup({speech:{voices},sessions:[['a',record('a','A')]]});
  const clean=s.win.speechText('# 見出し\n**太字**と*斜体*と`code`\n- 項目1\n1. 手順\n> 引用\n[リンク先](https://example.com) と https://example.com/x\n| 列A | 列B |\n|---|---|\n| 1 | 2 |\n```js\nconst a=1;\n```\n---\nsnake_case_name ~~取消~~');
  assert.doesNotMatch(clean,/[*#`>~]|\]\(|https?:|---|\|/);
  assert.match(clean,/見出し/);assert.match(clean,/太字と斜体とcode/);assert.match(clean,/項目1/);assert.match(clean,/リンク先/);
  assert.match(clean,/コードは画面で確認してください/);assert.match(clean,/snake_case_name/);assert.doesNotMatch(clean,/const a/);
  s.win.speak('**こんにちは** ジミーやで');const spoken=s.win.speechSynthesis.spoken.at(-1);
  assert.equal(spoken.text,'こんにちは ジミーやで');assert.equal(spoken.voice.name,'Otoya');assert.equal(spoken.pitch,0.8);assert.equal(spoken.rate,1.5);
  assert.equal(s.doc.getElementById('speechRateValue').textContent,'1.5倍');assert.equal(s.doc.getElementById('voiceHint').hidden,true);
  const options=[...s.doc.getElementById('voiceSelect').options].map(o=>o.textContent);assert.deepEqual(options,['自動（男性の低い声を優先）','Kyoko','Otoya（男性）']);
  s.doc.getElementById('speechRate').value='2';s.doc.getElementById('speechRate').dispatchEvent(new s.win.Event('input'));s.win.speak('速く');
  assert.equal(s.win.speechSynthesis.spoken.at(-1).rate,2);assert.equal(s.win.localStorage.getItem('karry-speech-rate'),'2');
  s.dom.window.close();
});

test('automatic voice never picks a known female voice and recognises male voices across platforms',()=>{
  const pick=voices=>{const s=setup({speech:{voices}});s.win.speak('テスト');const spoken=s.win.speechSynthesis.spoken.at(-1);const result={voice:spoken.voice?.name,pitch:spoken.pitch,hint:s.doc.getElementById('voiceHint').hidden};s.dom.window.close();return result;};
  assert.deepEqual(pick([{name:'Microsoft Nanami Online (Natural) - Japanese (Japan)',voiceURI:'nanami',lang:'ja-JP'},{name:'Microsoft Keita Online (Natural) - Japanese (Japan)',voiceURI:'keita',lang:'ja-JP'}]),{voice:'Microsoft Keita Online (Natural) - Japanese (Japan)',pitch:0.8,hint:true});
  assert.deepEqual(pick([{name:'日本語 日本',voiceURI:'ja-jp-x-jab-local',lang:'ja-JP'},{name:'日本語 日本 3',voiceURI:'ja-jp-x-jac-local',lang:'ja_JP'}]),{voice:'日本語 日本 3',pitch:0.8,hint:true});
  assert.deepEqual(pick([{name:'ja-JP-Wavenet-B',voiceURI:'b',lang:'ja-JP'},{name:'ja-JP-Wavenet-C',voiceURI:'c',lang:'ja-JP'}]),{voice:'ja-JP-Wavenet-C',pitch:0.8,hint:true});
  assert.deepEqual(pick([{name:'Google 日本語',voiceURI:'Google 日本語',lang:'ja-JP'}]),{voice:undefined,pitch:0.6,hint:false});
  assert.deepEqual(pick([{name:'Kyoko',voiceURI:'kyoko',lang:'ja-JP'},{name:'Japanese Voice',voiceURI:'generic',lang:'ja-JP'}]),{voice:'Japanese Voice',pitch:0.7,hint:false});
});

test('long replies are spoken in short chunks and the stop button ends playback immediately',async()=>{
  const s=setup({speech:{voices:[{name:'Otoya',voiceURI:'otoya',lang:'ja-JP'}]},sessions:[['a',record('a','A')]]});
  const synth=s.win.speechSynthesis,stop=s.doc.getElementById('stopSpeech');
  assert.equal(stop.hidden,true);
  s.win.speak(Array.from({length:6},(_,i)=>`これは${i+1}番目の長めの文章で、読み上げを途中で止められるか確認するためのテキストです。`).join(''));
  assert.equal(stop.hidden,false);assert.equal(synth.spoken.length,1);assert.ok(synth.spoken[0].text.length<=120);
  synth.finish();assert.equal(synth.spoken.length,2);
  const stale=synth.current;stop.click();
  assert.equal(stop.hidden,true);assert.match(s.doc.getElementById('voiceStatus').textContent,/止めました/);
  stale.onend();assert.equal(synth.spoken.length,2);
  s.win.speak('短い');while(synth.current)synth.finish();assert.equal(stop.hidden,true);
  s.dom.window.close();
});

test('typing, tapping the prompt, switching or creating tasks and the microphone all interrupt speech without locking the UI',async()=>{
  const microphone={};
  const s=setup({microphone,speech:{voices:[{name:'Otoya',voiceURI:'otoya',lang:'ja-JP'}]},sessions:[['a',record('a','A')],['b',record('b','B')]]});
  s.authenticate();await s.win.connectSessions();
  const synth=s.win.speechSynthesis,long='一文目です。二文目です。三文目です。';
  const interrupts={
    input:()=>s.doc.getElementById('prompt').dispatchEvent(new s.win.Event('input')),
    keydown:()=>s.doc.getElementById('prompt').dispatchEvent(new s.win.KeyboardEvent('keydown',{key:'a'})),
    pointerdown:()=>s.doc.getElementById('prompt').dispatchEvent(new s.win.Event('pointerdown')),
    switchTask:()=>s.win.selectSession(s.doc.getElementById('session').textContent==='a'?'b':'a'),
    newTask:()=>s.win.createNewSession(),
    mic:async()=>{s.doc.getElementById('mic').click();await new Promise(r=>setTimeout(r,200));s.win.stopMicrophone();},
  };
  for(const [name,interrupt] of Object.entries(interrupts)){
    s.win.speak(long);const stale=synth.current,count=synth.spoken.length,cancelled=synth.cancelled;
    for(const id of ['prompt','send','mic'])assert.equal(s.doc.getElementById(id).disabled,false,`${name}: ${id} locked while speaking`);
    for(const item of s.doc.querySelectorAll('.task-item'))assert.equal(item.disabled,false,`${name}: task locked while speaking`);
    await interrupt();
    assert.ok(synth.cancelled>cancelled,`${name} did not cancel speech`);
    assert.equal(s.doc.getElementById('stopSpeech').hidden,true,name);
    stale.onend?.();assert.equal(synth.spoken.length,count,`${name}: stale speech continued`);
  }
  s.dom.window.close();
});

test('the microphone still starts after interrupting speech',async()=>{
  const microphone={};const s=setup({microphone,speech:{voices:[{name:'Kyoko',voiceURI:'kyoko',lang:'ja-JP'}]},sessions:[['a',record('a','A')]]});
  s.authenticate();await s.win.connectSessions();s.win.speak('テストです。もう一文。');
  const before=s.win.speechSynthesis.cancelled;await s.win.startMicrophone();
  assert.ok(s.win.speechSynthesis.cancelled>before);assert.equal(microphone.instance.started,1);
  microphone.instance.emitResult('割り込み');assert.equal(s.doc.getElementById('prompt').value,'割り込み');s.dom.window.close();
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


test('missing session shows server recovery guidance and never creates a replacement',async()=>{
  const s=setup({sessions:[['a',record('a','A')]],fetch:async(url)=>url.startsWith('/api/sessions/')?json({status:'not_found',detail:'指定されたタスクが見つかりません。',hint:'一覧を再読み込みしてください。',error_code:'not_found'},404):null});
  s.authenticate();await s.win.connectSessions();
  assert.match(s.doc.getElementById('connectionNotice').textContent,/指定されたタスク.*一覧を再読み込み/);
  assert.equal(s.doc.getElementById('send').disabled,true);assert.equal(s.calls.filter(c=>c.request.method==='POST').length,0);s.dom.window.close();
});

test('missing image keeps image-specific guidance rather than blaming task loss',async()=>{
  const s=setup({fetch:async()=>json({status:'not_found',detail:'指定された画像が見つかりません。',hint:'画像をもう一度添付してください。'},404)});s.authenticate();
  await assert.rejects(s.win.requestJson('/api/images/missing?session_id=a'),error=>{
    assert.match(error.message,/指定された画像.*画像をもう一度添付/);assert.doesNotMatch(error.message,/再起動後はタスク/);assert.equal(error.status,404);return true;
  });assert.equal(s.calls.length,1);s.dom.window.close();
});

const DRAFT_ID='d'.repeat(32);
const DRAFT_HTML='<!doctype html><html><head><title>Saved website</title></head><body><h1>Private homepage</h1><script>window.parent.bad=true</script></body></html>';
function draftMeta(){return {artifact_id:DRAFT_ID,state:'draft',filename:'site_'+DRAFT_ID+'.html',sha256:'a'.repeat(64),public_url:null};}
function draftReply(){return {...draftMeta(),status:'ready',html_content:DRAFT_HTML,planned_public_url:'https://example.onrender.com/static/instant/site_'+DRAFT_ID+'.html'};}
const tick=()=>new Promise(resolve=>setTimeout(resolve,20));

test('saved HTML preview restores without visiting an unpublished public URL',async()=>{
 const r=record('a','A');r.artifacts=[draftMeta()];
 const s=setup({sessions:[['a',r]],fetch:async(url)=>url.startsWith('/api/website-artifacts/')?json(draftReply()):null});
 s.authenticate();await s.win.connectSessions();await tick();
 const frame=s.doc.querySelector('iframe');assert.ok(frame);assert.equal(frame.getAttribute('sandbox'),'allow-scripts');
 assert.match(frame.srcdoc,/Private homepage/);assert.match(frame.srcdoc,/connect-src 'none'/);assert.equal(frame.getAttribute('src'),null);
 assert.equal(s.doc.getElementById('latestBox').hidden,true);
 assert.equal(s.doc.querySelector('a[href*="site_" ]'),null);assert.equal(s.win.bad,undefined);
 assert.equal(s.calls.filter(c=>c.url.startsWith('/static/instant/')).length,0);
 assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);s.dom.window.close();
});

test('preview 404 stays in chat with retry instead of navigation or fake success',async()=>{
 const r=record('a','A');r.artifacts=[draftMeta()];
 const s=setup({sessions:[['a',r]],fetch:async(url)=>url.startsWith('/api/website-artifacts/')?json({detail:'下書きを確認してください。'},404):null});
 s.authenticate();await s.win.connectSessions();await tick();
 assert.match(s.doc.getElementById('messages').textContent,/プレビューを読み込めません/);assert.equal(s.doc.querySelector('iframe'),null);
 assert.equal(s.win.location.pathname,'/');assert.equal(s.doc.getElementById('send').disabled,false);s.dom.window.close();
});

test('switching tasks cancels display of delayed private HTML',async()=>{
 let release;const wait=new Promise(resolve=>release=resolve);
 const a=record('a','A');a.artifacts=[draftMeta()];
 const s=setup({sessions:[['a',a],['b',record('b','B')]],fetch:async(url)=>{if(url.startsWith('/api/website-artifacts/')){await wait;return json(draftReply());}}});
 s.authenticate();await s.win.connectSessions();await s.win.selectSession('b');release();await tick();
 assert.equal(s.doc.querySelector('iframe'),null);assert.doesNotMatch(s.doc.getElementById('messages').textContent,/Private homepage/);s.dom.window.close();
});

test('public URL appears only after fresh confirmation and successful publication',async()=>{
 const r=record('a','A');r.artifacts=[draftMeta()];const url=draftReply().planned_public_url;
 const s=setup({sessions:[['a',r]],fetch:async(path)=>{
  if(path.includes('/prepare?'))return json({success:false,confirmation_required:true,approval_id:'fresh',preview:{sha256:'a'.repeat(64)}});
  if(path==='/api/agent/websites/confirm')return json({success:true,status:'published',public_url:url});
  if(path.startsWith('/api/website-artifacts/'))return json(draftReply());
 }});
 s.authenticate();await s.win.connectSessions();await tick();
 const box=s.doc.querySelector('[data-artifact-id]'),check=box.querySelector('input[type="checkbox"]');
 const button=[...box.querySelectorAll('button')].find(b=>b.textContent==='確認した内容を即時公開');assert.equal(button.disabled,true);
 assert.equal(s.calls.filter(c=>c.request.method==='POST').length,0);
 check.checked=true;check.onchange();await button.onclick();
 assert.equal(s.doc.getElementById('latestUrl').href,url);
 assert.equal(s.calls.filter(c=>c.url==='/api/agent/websites/confirm').length,1);s.dom.window.close();
});

function withGeneratedImage(r,id=IMAGE_ID){const meta={...imageMeta(id),filename:'generated-1.jpg',mime_type:'image/jpeg'};r.messages=[{role:'user',content:'Draw'},{role:'assistant',content:'Done',images:[id]}];r.image_assets={[id]:meta};return r;}

test('generated images in replies load inline from the private image API',async()=>{
  const s=setup({sessions:[['a',withGeneratedImage(record('a','A'))]],fetch:async(url)=>url.startsWith('/api/images/')?json({image_url:IMAGE_URL}):null});
  s.authenticate();await s.win.connectSessions();await new Promise(resolve=>setTimeout(resolve,10));
  const img=s.doc.querySelector('#messages .generated-image');assert.equal(img.src,IMAGE_URL);assert.equal(img.alt,'generated-1.jpg');
  assert.ok(s.calls.some(c=>c.url==='/api/images/'+IMAGE_ID+'?session_id=a'));assert.equal(s.calls.filter(c=>c.url.includes('/static/instant/')).length,0);s.dom.window.close();
});

test('missing generated image shows guidance instead of a broken image',async()=>{
  const s=setup({sessions:[['a',withGeneratedImage(record('a','A'))]],fetch:async(url)=>url.startsWith('/api/images/')?json({detail:'Image not found'},404):null});
  s.authenticate();await s.win.connectSessions();await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(s.doc.querySelector('#messages .generated-image'),null);assert.match(s.doc.getElementById('messages').textContent,/生成画像を表示できませんでした/);s.dom.window.close();
});

test('unavailable public file keeps private HTML preview but never displays a publish link',async()=>{
 const r=record('a','A');r.artifacts=[{...draftMeta(),state:'unavailable'}];
 const s=setup({sessions:[['a',r]],fetch:async(url)=>url.startsWith('/api/website-artifacts/')?json({...draftReply(),state:'unavailable'}):null});
 s.authenticate();await s.win.connectSessions();await tick();
 assert.ok(s.doc.querySelector('iframe'));assert.equal(s.doc.getElementById('latestBox').hidden,true);
 assert.equal(s.doc.querySelector('[data-artifact-id] input[type="checkbox"]'),null);
 assert.match(s.doc.getElementById('messages').textContent,/公開リンクの実体を確認できません/);s.dom.window.close();
});

test('clear history asks first and cancelling keeps messages without calling the API',async()=>{
  const s=setup({sessions:[['a',record('a','A','Keep me')]]});
  assert.equal(s.doc.getElementById('clearHistory').disabled,true);
  s.authenticate();await s.win.connectSessions();assert.equal(s.doc.getElementById('clearHistory').disabled,false);
  const asked=[];s.win.confirm=message=>{asked.push(message);return false;};
  await s.win.clearHistory();
  assert.match(asked[0],/会話履歴を消去しますか？/);
  assert.equal(s.calls.filter(c=>c.request.method==='DELETE').length,0);
  assert.match(s.doc.getElementById('messages').textContent,/Keep me/);s.dom.window.close();
});

test('confirmed clear resets server history for the selected task and keeps the task',async()=>{
  const s=setup({sessions:[['a',record('a','A','Old answer')],['b',record('b','B','Other task')]]});
  s.authenticate();await s.win.connectSessions();s.win.confirm=()=>true;
  s.doc.getElementById('clearHistory').click();await new Promise(resolve=>setTimeout(resolve,0));
  const call=s.calls.find(c=>c.request.method==='DELETE');
  assert.equal(call.url,'/api/sessions/a/messages');assert.equal(call.request.headers.Authorization,'Bearer synthetic-owner');
  const content=s.doc.getElementById('messages').textContent;
  assert.doesNotMatch(content,/Old answer/);assert.ok(s.doc.getElementById('empty'));
  assert.equal(s.doc.getElementById('currentTask').textContent,'A');assert.equal(s.doc.querySelectorAll('.task-item').length,2);
  assert.equal(s.data.get('b').messages.length,1);assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);
  assert.equal(s.doc.getElementById('send').disabled,false);s.dom.window.close();
});

test('failed clear keeps the conversation and explains the error',async()=>{
  const s=setup({sessions:[['a',record('a','A','Still here')]],fetch:async(url,request)=>{if(request.method==='DELETE')return json({detail:'session is already running'},409);}});
  s.authenticate();await s.win.connectSessions();s.win.confirm=()=>true;await s.win.clearHistory();
  const content=s.doc.getElementById('messages').textContent;
  assert.match(content,/Still here/);assert.match(content,/履歴の消去エラー/);s.dom.window.close();
});

test('deleting a task asks first and cancelling keeps it',async()=>{
  const s=setup({sessions:[['a',record('a','A','Keep A')],['b',record('b','B','B')]]});
  s.authenticate();await s.win.connectSessions();
  const asked=[];s.win.confirm=message=>{asked.push(message);return false;};
  s.doc.querySelector('[data-delete-id="a"]').click();await new Promise(resolve=>setTimeout(resolve,0));
  assert.match(asked[0],/このタスクを削除しますか？/);assert.match(asked[0],/「A」/);
  assert.equal(s.calls.filter(c=>c.request.method==='DELETE').length,0);
  assert.equal(s.doc.querySelectorAll('.task-item').length,2);
  assert.match(s.doc.getElementById('messages').textContent,/Keep A/);s.dom.window.close();
});

test('deleting the selected task removes it and focuses the next task',async()=>{
  const s=setup({sessions:[['a',record('a','A','Only A')],['b',record('b','B','Only B')],['c',record('c','C','Only C')]]});
  s.authenticate();await s.win.connectSessions();s.win.confirm=()=>true;
  await s.win.deleteSession('a');
  const call=s.calls.find(c=>c.request.method==='DELETE');
  assert.equal(call.url,'/api/sessions/a');assert.equal(call.request.headers.Authorization,'Bearer synthetic-owner');
  assert.deepEqual([...s.doc.querySelectorAll('.task-item')].map(b=>b.textContent),['B','C']);
  assert.equal(s.doc.getElementById('currentTask').textContent,'B');
  const content=s.doc.getElementById('messages').textContent;assert.match(content,/Only B/);assert.doesNotMatch(content,/Only A/);
  assert.equal(s.win.localStorage.getItem('karry-session'),'b');
  assert.equal(s.doc.activeElement.dataset.sessionId,'b');
  assert.equal(s.doc.getElementById('send').disabled,false);s.dom.window.close();
});

test('deleting another task keeps the current conversation',async()=>{
  const s=setup({sessions:[['a',record('a','A','Only A')],['b',record('b','B','Only B')]]});
  s.authenticate();await s.win.connectSessions();s.win.confirm=()=>true;
  await s.win.deleteSession('b');
  assert.deepEqual([...s.doc.querySelectorAll('.task-item')].map(b=>b.textContent),['A']);
  assert.equal(s.doc.getElementById('currentTask').textContent,'A');
  assert.match(s.doc.getElementById('messages').textContent,/Only A/);
  assert.equal(s.calls.filter(c=>c.url==='/api/sessions/b'&&c.request.method!=='DELETE').length,0);s.dom.window.close();
});

test('deleting the last task leaves an empty state without chatting',async()=>{
  const s=setup({sessions:[['a',record('a','A','Only A')]]});
  s.authenticate();await s.win.connectSessions();s.win.confirm=()=>true;
  await s.win.deleteSession('a');
  assert.equal(s.doc.querySelectorAll('.task-item').length,0);
  assert.equal(s.doc.getElementById('currentTask').textContent,'タスクがありません');
  assert.equal(s.doc.getElementById('send').disabled,true);assert.equal(s.doc.getElementById('newTask').disabled,false);
  assert.equal(s.win.localStorage.getItem('karry-session'),null);
  assert.equal(s.calls.filter(c=>c.url==='/api/chat').length,0);s.dom.window.close();
});

test('failed delete keeps the task and explains the error',async()=>{
  const s=setup({sessions:[['a',record('a','A','Still here')]],fetch:async(url,request)=>{if(request.method==='DELETE')return json({detail:'session is already running'},409);}});
  s.authenticate();await s.win.connectSessions();s.win.confirm=()=>true;
  await s.win.deleteSession('a');
  assert.equal(s.doc.querySelectorAll('.task-item').length,1);
  const content=s.doc.getElementById('messages').textContent;assert.match(content,/Still here/);assert.match(content,/タスク削除エラー/);s.dom.window.close();
});

test('secret chat sends an isolated flag and never persists its text, trace, or ID',async()=>{
  const s=setup({sessions:[['a',record('a','A','Normal history')]]});const consoleCalls=[];
  s.win.console.log=(...args)=>consoleCalls.push(args);s.authenticate();await s.win.connectSessions();
  s.doc.getElementById('secretToggle').checked=true;await s.win.toggleSecretMode();
  assert.match(s.doc.getElementById('secretNotice').textContent,/この会話は保存しません/);
  assert.equal(s.doc.getElementById('imageFile').disabled,true);
  assert.equal(s.doc.getElementById('newTask').disabled,true);assert.equal(s.doc.querySelector('.task-delete').disabled,true);
  s.doc.getElementById('prompt').value='秘匿プロンプト';await s.win.sendMessage({preventDefault(){}});
  const post=s.calls.filter(call=>call.url==='/api/chat').at(-1),body=JSON.parse(post.request.body);
  assert.equal(body.is_secret,true);assert.equal(body.isSecret,true);assert.deepEqual(body.image_ids,[]);assert.match(body.secret_session_id,/^[a-f0-9]{32}$/);
  assert.equal(s.data.get('a').messages.length,1);assert.match(s.doc.getElementById('messages').textContent,/Secret answer/);
  assert.doesNotMatch(s.doc.getElementById('trace').textContent,/秘匿|Secret answer/);
  assert.equal(s.win.localStorage.length,1);assert.equal(s.win.sessionStorage.length,0);assert.equal(consoleCalls.length,0);
  s.doc.getElementById('secretToggle').checked=false;await s.win.toggleSecretMode();
  const forget=s.calls.find(call=>call.url==='/api/secret/forget');assert.equal(JSON.parse(forget.request.body).secret_session_id,body.secret_session_id);
  for(const call of s.calls.filter(call=>call.url!=='/api/chat'&&call.url!=='/api/secret/forget'))assert.doesNotMatch(String(call.request.body||''),/秘匿|Secret answer|secret_session_id/);
  assert.match(s.doc.getElementById('messages').textContent,/Normal history/);assert.doesNotMatch(s.doc.getElementById('messages').textContent,/秘匿|Secret answer/);
  assert.equal(s.doc.getElementById('secretNotice').hidden,true);
  s.doc.getElementById('secretToggle').checked=true;await s.win.toggleSecretMode();s.doc.getElementById('prompt').value='別の秘密会話';await s.win.sendMessage({preventDefault(){}});
  const renewed=JSON.parse(s.calls.filter(call=>call.url==='/api/chat').at(-1).request.body);assert.notEqual(renewed.secret_session_id,body.secret_session_id);s.dom.window.close();
});

test('secret retry keeps its own ID, busy blocks mode changes, and task switching drops its view',async()=>{
  let first=true,release;const pending=new Promise(resolve=>release=resolve);
  const s=setup({sessions:[['a',record('a','A')],['b',record('b','B','Only B')]],fetch:async(url,request)=>{
    if(url==='/api/chat'&&JSON.parse(request.body).is_secret&&first){first=false;throw new Error('model-private-detail');}
  }});
  s.authenticate();await s.win.connectSessions();s.doc.getElementById('secretToggle').checked=true;await s.win.toggleSecretMode();
  s.doc.getElementById('prompt').value='retry secret';await s.win.sendMessage({preventDefault(){}});
  assert.match(s.doc.getElementById('messages').textContent,/シークレット会話を送信できません/);assert.doesNotMatch(s.doc.getElementById('messages').textContent,/model-private-detail/);
  await s.win.sendMessage({preventDefault(){}});const secretPosts=s.calls.filter(call=>call.url==='/api/chat').map(call=>JSON.parse(call.request.body));
  assert.equal(secretPosts.length,2);assert.equal(secretPosts[0].request_id,secretPosts[1].request_id);assert.equal(secretPosts[0].secret_session_id,secretPosts[1].secret_session_id);
  s.doc.getElementById('prompt').value='busy secret';
  const originalFetch=s.win.fetch;s.win.fetch=async(url,request={})=>{if(url==='/api/chat'&&JSON.parse(request.body).is_secret){await pending;}return originalFetch(url,request);};
  const sending=s.win.sendMessage({preventDefault(){}});assert.equal(s.doc.getElementById('secretToggle').disabled,true);
  s.doc.getElementById('secretToggle').checked=false;await s.win.toggleSecretMode();assert.equal(s.doc.getElementById('secretToggle').checked,true);
  release();await sending;await s.win.selectSession('b');
  assert.equal(s.doc.getElementById('secretToggle').checked,false);assert.match(s.doc.getElementById('messages').textContent,/Only B/);assert.doesNotMatch(s.doc.getElementById('messages').textContent,/retry secret|busy secret/);
  s.dom.window.close();
});

test('microphone permission stops every track, preserves typing, and ignores delayed result after task change',async()=>{
  const microphone={};const s=setup({microphone,sessions:[['a',record('a','A')],['b',record('b','B')]]});s.authenticate();await s.win.connectSessions();
  s.doc.getElementById('prompt').value='手入力';await s.win.startMicrophone();
  assert.equal(microphone.tracks[0].stopped,1);assert.equal(microphone.instance.started,1);assert.equal(s.doc.getElementById('mic').classList.contains('active'),true);
  microphone.instance.emitResult('音声');assert.equal(s.doc.getElementById('prompt').value,'手入力音声');
  await s.win.selectSession('b');assert.ok(microphone.instance.aborted>=1);microphone.instance.emitResult('遅延結果');
  assert.doesNotMatch(s.doc.getElementById('prompt').value,/遅延結果/);assert.equal(s.calls.some(call=>call.url==='/api/upload_audio'),false);s.dom.window.close();
});

test('microphone rejection and unsupported browsers present Japanese reasons without starting recording',async()=>{
  const denied=new Error('denied');denied.name='NotAllowedError';
  const s=setup({microphone:{getUserMedia:async()=>{throw denied;}},sessions:[['a',record('a','A')]]});s.authenticate();await s.win.connectSessions();await s.win.startMicrophone();
  assert.match(s.doc.getElementById('voiceStatus').textContent,/未許可・ブラウザ制限/);assert.equal(s.doc.getElementById('micHelp').open,true);assert.equal(s.win.__microphone.instance.started,0);s.dom.window.close();
  const unsupported=setup({sessions:[['a',record('a','A')]]});unsupported.authenticate();await unsupported.win.connectSessions();
  assert.equal(unsupported.doc.getElementById('mic').disabled,true);assert.match(unsupported.doc.getElementById('voiceStatus').textContent,/未対応/);unsupported.dom.window.close();
});

test('secret switch is beside composer with visible setup guidance before connection',async()=>{
  const s=setup({sessions:[['a',record('a','A')]]});
  assert.equal(s.doc.querySelectorAll('#secretToggle').length,1);
  assert.ok(s.doc.getElementById('chatTools').contains(s.doc.getElementById('secretToggle')));
  assert.equal(s.doc.getElementById('secretToggle').getAttribute('role'),'switch');
  assert.match(s.doc.getElementById('chatTools').textContent,/🔒 シークレットモード/);
  assert.equal(s.doc.getElementById('secretSetupHint').hidden,false);
  assert.equal(s.doc.querySelector('.feature-jump a').getAttribute('href'),'#chatTools');
  s.authenticate();await s.win.connectSessions();
  assert.equal(s.doc.getElementById('secretSetupHint').hidden,true);
  s.doc.getElementById('secretToggle').checked=true;await s.win.toggleSecretMode();
  assert.match(s.doc.getElementById('secretModeStatus').textContent,/ON・保存しない/);s.dom.window.close();
});

test('stored microphone denial shows permission steps and retries only after user changes permission',async()=>{
  let mediaCalls=0;const permission={state:'denied'};const microphone={getUserMedia:async()=>{mediaCalls++;return {getTracks:()=>microphone.tracks};}};
  const s=setup({permission,microphone,sessions:[['a',record('a','A')]]});s.authenticate();await s.win.connectSessions();await s.win.startMicrophone();
  assert.equal(mediaCalls,0);assert.equal(s.doc.getElementById('micHelp').open,true);assert.match(s.doc.getElementById('voiceStatus').textContent,/未許可/);
  assert.match(s.doc.getElementById('openBrowser').href,/^https:\/\/karry-agent-1\.onrender\.com\//);
  assert.equal(s.doc.getElementById('micRetry').disabled,false);permission.state='granted';s.doc.getElementById('micRetry').click();await new Promise(r=>setTimeout(r,0));
  assert.equal(mediaCalls,1);assert.equal(microphone.tracks[0].stopped,1);assert.equal(microphone.instance.started,1);s.dom.window.close();
});

test('iframe microphone policy block is not misreported as user permission refusal',async()=>{
  let calls=0;const s=setup({microphonePolicy:false,microphone:{getUserMedia:async()=>{calls++;throw Error('must not call');}},sessions:[['a',record('a','A')]]});
  s.authenticate();await s.win.connectSessions();await s.win.startMicrophone();
  assert.equal(calls,0);assert.equal(s.doc.getElementById('micHelp').open,true);assert.match(s.doc.getElementById('voiceStatus').textContent,/埋め込み画面/);assert.doesNotMatch(s.doc.getElementById('voiceStatus').textContent,/権限が拒否/);s.dom.window.close();
});

test('speech service refusal and device busy have distinct actionable reasons',async()=>{
  const microphone={};const s=setup({microphone,sessions:[['a',record('a','A')]]});s.authenticate();await s.win.connectSessions();await s.win.startMicrophone();
  microphone.instance.emitError('service-not-allowed');assert.match(s.doc.getElementById('voiceStatus').textContent,/音声認識サービス/);assert.doesNotMatch(s.doc.getElementById('voiceStatus').textContent,/権限が拒否/);assert.equal(s.doc.getElementById('micHelp').open,true);
  assert.match(s.win.micStatusForError({name:'NotReadableError'}),/他のアプリ/);s.dom.window.close();
});
