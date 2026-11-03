const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/admin.html','utf8');
const tick=(ms=10)=>new Promise(r=>setTimeout(r,ms));
const json=(body,status=200)=>({ok:status<400,status,json:async()=>body});

function setup(options={}){
  const calls=[],state={drafts:[],products:[],verdict:options.verdict||'SAFE',untitled:false};
  const dom=new JSDOM(html,{url:'https://example.onrender.com/admin',runScripts:'dangerously',beforeParse(window){
    if(options.saved)window.sessionStorage.setItem('karry-admin-token',options.saved);
    window.fetch=async(url,request={})=>{
      const auth=request.headers?.Authorization;calls.push({url,method:request.method||'GET',auth,body:request.body?JSON.parse(request.body):null});
      if(auth!=='Bearer good-token')return json({detail:'unauthorized'},401);
      if(url==='/api/admin/me')return json({admin:true});
      if(url==='/api/alibaba/config')return json({pricing:{cny_jpy_rate:22,agent_fee_rate:0.05,shipping_jpy:1200,markup:1.4}});
      if(url==='/api/alibaba/drafts')return json({drafts:state.drafts});
      if(url==='/api/shop/admin/products')return json({products:state.products});
      if(url==='/api/shop/admin/orders')return json({orders:[]});
      if(url==='/api/alibaba/logs?limit=50')return json({entries:[]});
      if(url==='/api/alibaba/evaluate'){
        const body=JSON.parse(request.body),cost=body.cost_cny;
        const draft={id:'d'.repeat(32),product:{product_id:'573741401425',title:body.title||'1688 商品 573741401425',title_missing:!body.title,notes:['入力した値で計算しました。']},
          review:{verdict:state.verdict,checks:{pse:{label:'PSE（電気用品安全法）',status:state.verdict,reason:'r'}},disclaimer:'参考'},
          pricing:cost?{cost_cny:cost,goods_jpy:2200,agent_fee_jpy:110,shipping_jpy:1200,landed_cost_jpy:3510,selling_price_jpy:4914,profit_jpy:1404,margin_rate:0.2857}:null};
        state.drafts=[draft];return json({draft,pricing_error:cost?null:'原価（元）を入力すると販売価格を計算できます。'});
      }
      if(url==='/api/alibaba/publish'){const d=state.drafts[0];if(d.review.verdict!=='SAFE')return json({published:false,reason:'法的審査で REJECT のため登録できません。'},409);
        state.drafts=[];state.products=[{id:'p'.repeat(32),title:d.product.title,category:'杯子',price_jpy:4914}];return json({published:true,listing:{price_jpy:4914}},201);}
      if(request.method==='DELETE'){state.products=[];return json({removed:true});}
      return json({},404);
    };
  }});
  return {dom,win:dom.window,$:id=>dom.window.document.getElementById(id),calls,state};
}

test('dashboard stays hidden until the admin token is verified and has no OCR, preview or external links',async()=>{
  const s=setup();const {$}=s;
  assert.equal($('dashboard').hidden,true);assert.equal($('login').hidden,false);
  assert.equal(s.dom.window.document.querySelector('input[type=file]'),null,'no screenshot upload');
  assert.equal(s.dom.window.document.querySelectorAll('a').length,0,'no external links');
  assert.equal(s.dom.window.document.querySelector('iframe'),null);
  assert.doesNotMatch(s.dom.window.document.body.textContent,/スクショ|OCR|プレビュー一覧|ショップを開く/);
  $('adminToken').value='wrong';$('loginForm').dispatchEvent(new s.win.Event('submit',{cancelable:true}));await tick();
  assert.equal($('dashboard').hidden,true);assert.match($('loginStatus').textContent,/ログイン/);
  assert.equal(s.calls.filter(c=>c.url!=='/api/admin/me').length,0,'nothing but the auth check without a valid token');
  $('adminToken').value='Bearer good-token';$('loginForm').dispatchEvent(new s.win.Event('submit',{cancelable:true}));await tick(30);
  assert.equal($('dashboard').hidden,false);assert.equal($('login').hidden,true);assert.equal($('adminToken').value,'');
  assert.equal(s.win.sessionStorage.getItem('karry-admin-token'),'good-token');assert.equal($('rate').value,'22');
  $('logout').click();assert.equal($('dashboard').hidden,true);assert.equal(s.win.sessionStorage.getItem('karry-admin-token'),null);
  s.dom.window.close();
});

test('manual URL + cost: live price, 取得して審査・価格計算 saves a draft, ショップに登録 publishes it',async()=>{
  const s=setup({saved:'good-token'});const {$}=s;await tick(30);
  $('costCny').value='100';$('costCny').dispatchEvent(new s.win.Event('input'));
  assert.match($('pricePreview').textContent,/販売価格（目安） 4,914円/);
  $('productUrl').value='';$('evaluate').click();await tick();assert.match($('entryStatus').textContent,/URL を入れて/);
  $('productUrl').value='https://detail.1688.com/offer/573741401425.html';$('evaluate').click();await tick(30);
  const post=s.calls.find(c=>c.url==='/api/alibaba/evaluate');
  assert.equal(post.body.cost_cny,100);assert.equal(post.body.pricing.markup,1.4);assert.equal('screenshots' in post.body,false);
  assert.match($('entryStatus').textContent,/下書きに保存.*商品名/);assert.equal($('register').disabled,true);
  assert.match($('drafts').textContent,/SAFE｜1688 商品 573741401425｜4,914円/);
  $('title').value='硅胶折叠水杯';$('evaluate').click();await tick(30);
  assert.match($('entryStatus').textContent,/✅ 下書きに保存/);assert.equal($('register').disabled,false);
  assert.match($('result').textContent,/販売価格4,914円/);assert.equal($('result').querySelector('.verdict').dataset.verdict,'SAFE');
  $('register').click();await tick(30);
  assert.match($('entryStatus').textContent,/ショップに登録しました（4,914円）/);assert.match($('products').textContent,/硅胶折叠水杯｜杯子｜4,914円/);
  assert.match($('drafts').textContent,/まだ下書きはありません/);
  $('products').querySelector('button').click();await tick(30);assert.match($('products').textContent,/まだ掲載していません/);
  assert.ok(s.calls.every(c=>c.auth==='Bearer good-token'));s.dom.window.close();
});

test('REJECT drafts cannot be registered and an expired token returns to the login screen',async()=>{
  const s=setup({saved:'good-token',verdict:'REJECT'});const {$}=s;await tick(30);
  $('productUrl').value='573741401425';$('costCny').value='10';$('title').value='充电宝';$('evaluate').click();await tick(30);
  assert.match($('entryStatus').textContent,/REJECT のため登録できません/);assert.equal($('register').disabled,true);
  assert.equal($('drafts').querySelector('button').disabled,true);
  s.win.sessionStorage.clear();
  const bad=setup({saved:'stale'});await tick(30);
  assert.equal(bad.$('dashboard').hidden,true);assert.match(bad.$('loginStatus').textContent,/ログイン/);
  s.dom.window.close();bad.dom.window.close();
});
