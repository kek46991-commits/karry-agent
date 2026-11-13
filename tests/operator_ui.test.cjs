const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/operator.html','utf8');
const tick=(ms=10)=>new Promise(r=>setTimeout(r,ms));
const ok=(body,status=200)=>({ok:status<400,status,json:async()=>body});

function setup({stored,claims=[],enrollStatus=201}={}){
  const calls=[];
  const dom=new JSDOM(html,{url:'https://karry.example/operator',runScripts:'dangerously',beforeParse(window){
    window.setInterval=()=>0;
    if(stored)window.localStorage.setItem('karry-operator-v1',JSON.stringify(stored));
    window.fetch=async(url,opts={})=>{
      const body=opts.body?JSON.parse(opts.body):undefined;calls.push({url,headers:opts.headers,body});
      if(url.endsWith('/devices/enroll'))return ok({device_id:body.device_id,device_token:'device-secret'},enrollStatus);
      if(url.includes('/claim'))return ok({action:claims.shift()||null});
      if(url.includes('/report'))return ok({status:body.success?'completed':'failed'});
      return ok({detail:'not found'},404);};
  }});
  const win=dom.window;
  return {dom,win,doc:win.document,$:id=>win.document.getElementById(id),calls,store:()=>JSON.parse(win.localStorage.getItem('karry-operator-v1')||'{}')};
}
const enrolled={deviceId:'iphone',deviceToken:'device-secret'};
const action=(name,payload)=>({id:'e'.repeat(32),device_id:'iphone',action:name,payload});

test('enrollment stores only the device token, never the owner token',async()=>{
  const s=setup();
  s.$('deviceId').value='iphone';s.$('ownerToken').value='owner-secret';
  s.$('enrollBtn').click();await tick(30);
  const enroll=s.calls.find(c=>c.url.endsWith('/devices/enroll'));
  assert.equal(enroll.headers.Authorization,'Bearer owner-secret');
  assert.equal(s.$('ownerToken').value,'');
  const raw=s.win.localStorage.getItem('karry-operator-v1');
  assert.ok(raw.includes('device-secret'));assert.ok(!raw.includes('owner-secret'));
  assert.ok(!s.$('operateCard').hidden);s.dom.window.close();
});

test('a claimed action waits for the user, then runs once and reports a bounded summary',async()=>{
  const s=setup({stored:enrolled,claims:[action('open_url',{url:'https://example.com/private/path?q=secret'})]});
  await tick(30);
  const claim=s.calls.find(c=>c.url.includes('/claim'));
  assert.equal(claim.headers['X-Device-Token'],'device-secret');assert.equal(claim.headers.Authorization,undefined);
  assert.equal(s.$('actionName').textContent,'URLを開く');assert.ok(!s.$('runBtn').hidden);
  assert.ok(!s.calls.some(c=>c.url.includes('/report')));
  const opened=[];s.win.KarryOperator.env.openWindow=url=>{opened.push(url);return {};};
  s.$('runBtn').click();await tick(30);
  assert.deepEqual(opened,['https://example.com/private/path?q=secret']);
  const report=s.calls.find(c=>c.url.includes('/report'));
  assert.deepEqual(report.body,{device_id:'iphone',success:true,output:'ページを開きました: example.com',error_code:''});
  assert.equal(s.$('reportState').textContent,'送信済み');assert.equal(s.$('actionState').textContent,'完了');s.dom.window.close();
});

test('invalid payloads are reported as failures without executing anything',async()=>{
  const s=setup({stored:enrolled,claims:[action('open_url',{url:'http://example.com'})]});
  await tick(30);
  const report=s.calls.find(c=>c.url.includes('/report'));
  assert.equal(report.body.success,false);assert.equal(report.body.error_code,'invalid_payload');
  assert.ok(s.$('runBtn').hidden);s.dom.window.close();
});

test('planner enforces the allowlist, fields and HTTPS-only URLs',()=>{
  const s=setup();const {plan}=s.win.KarryOperator;
  const code=a=>{try{plan(a);return 'ok';}catch(e){return e.code;}};
  assert.equal(code(action('run_shell',{cmd:'ls'})),'invalid_payload');
  assert.equal(code(action('open_url',{url:'https://u:p@example.com'})),'invalid_payload');
  assert.equal(code(action('open_url',{url:'javascript:alert(1)'})),'invalid_payload');
  assert.equal(code(action('open_app',{bundle_id:'com.example.unknown'})),'app_unavailable');
  assert.equal(code(action('open_app',{bundle_id:'com.apple.Maps'})),'ok');
  assert.equal(code(action('create_calendar_event',{title:'t',start_at:'2026-11-14T10:00:00+09:00',extra:'x'})),'invalid_payload');
  assert.equal(code(action('create_calendar_event',{title:'t',start_at:'tomorrow'})),'invalid_payload');
  assert.equal(code(action('create_reminder',{title:'t'})),'intent_unavailable');
  assert.equal(code(action('choose_photo',{selection_limit:21})),'invalid_payload');
  assert.equal(code(action('draft_message',{recipient:'a;b',body:'hi'})),'invalid_payload');
  assert.equal(code(action('draft_message',{recipient:'+81 90',body:'x'.repeat(1001)})),'invalid_payload');
  s.dom.window.close();
});

test('calendar files escape payload text instead of interpreting it',()=>{
  const s=setup();const {plan,calendarFile}=s.win.KarryOperator;
  const ics=calendarFile(plan(action('create_calendar_event',{title:'会議;\nEND:VEVENT',start_at:'2026-11-14T10:00:00+09:00'})),'abc');
  assert.match(ics,/DTSTART:20261114T010000Z/);assert.match(ics,/DTEND:20261114T020000Z/);
  assert.match(ics,/SUMMARY:会議\\;\\nEND:VEVENT\r\n/);
  assert.equal(ics.match(/END:VEVENT/g).length,2);s.dom.window.close();
});

test('photo picker reports only count and type, and a 401 asks for re-enrollment',async()=>{
  const s=setup({stored:enrolled,claims:[action('choose_photo',{selection_limit:2})]});
  await tick(30);
  s.win.KarryOperator.env.pickFiles=async()=>[{name:'IMG_0001.JPG',type:'image/jpeg'},{name:'secret-name.heic',type:'image/heic'},{name:'x.png',type:'image/png'}];
  s.$('runBtn').click();await tick(30);
  const report=s.calls.find(c=>c.url.includes('/report'));
  assert.equal(report.body.output,'写真を2件選択しました（種類: jpg, heic）');
  s.dom.window.close();
  const denied=setup({stored:enrolled});await tick(30);denied.win.fetch=async()=>({ok:false,status:401,json:async()=>({})});
  denied.$('checkBtn').click();await tick(30);
  assert.equal(denied.$('connection').textContent,'再登録が必要です');assert.equal(denied.store().deviceToken,'');denied.dom.window.close();
});
