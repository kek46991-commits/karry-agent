const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/admin.html','utf8');
const tick=(ms=10)=>new Promise(r=>setTimeout(r,ms));
const json=(body,status=200)=>({ok:status<400,status,json:async()=>body});

function setup(options={}){
  const calls=[],state={drafts:[],products:[],verdict:options.verdict||'SAFE',untitled:false};
  const dom=new JSDOM(html,{url:options.url||'https://example.onrender.com/admin',runScripts:'dangerously',beforeParse(window){
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
      if(url==='/api/alibaba/fetch'){const body=JSON.parse(request.body);state.fetched=body.product;
        if(options.blocked)return json({detail:'1688 のページを読めませんでした（ボット確認で止められました）。'},502);
        return json({product:{product_id:'573741401425',title:'硅胶折叠水杯',title_missing:false,min_price_cny:12.5,category:'杯子',images:['https://cbu01.alicdn.com/a.jpg']}});}
      if(url==='/api/alibaba/evaluate'){
        const body=JSON.parse(request.body),cost=body.cost_cny;
        state.lastBody=body;
        const draft={id:'d'.repeat(32),product:{product_id:'573741401425',title:body.title||'1688 商品 573741401425',title_missing:!body.title,notes:['入力した値で計算しました。'],images:body.photos?.length?['/api/shop/images/'+'a'.repeat(32)+'.jpg','https://evil.example/x.jpg']:[],image_processing:body.photos?.length?{engine:body.enhance_images?'gemini':null,sources:body.photos.length,enhanced:body.enhance_images?body.photos.length:0}:undefined},
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
  assert.deepEqual([...s.dom.window.document.querySelectorAll('input[type=file]')].map(i=>i.id),['photos'],'only plain product photos, no OCR screenshot input');
  assert.equal($('enhance').checked,true);
  assert.equal(s.dom.window.document.querySelectorAll('a:not(#bookmarklet)').length,0,'no external links');
  assert.match(s.$('bookmarklet').getAttribute('href'),/^javascript:/);
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

test('product photos are posted as data URLs with the Gemini enhance flag and shown after review',async()=>{
  const s=setup({saved:'good-token'});const {$}=s;await tick(30);
  Object.defineProperty($('photos'),'files',{configurable:true,value:[new s.win.File([new Uint8Array([255,216,255])],'a.jpg',{type:'image/jpeg'})]});
  $('productUrl').value='573741401425';$('costCny').value='100';$('title').value='陶瓷杯';$('evaluate').click();await tick(40);
  assert.equal(s.state.lastBody.enhance_images,true);assert.match(s.state.lastBody.photos[0],/^data:image\/jpeg;base64,/);
  const imgs=[...$('result').querySelectorAll('img')];assert.equal(imgs.length,1,'only shop-hosted or alicdn images');assert.match(imgs[0].src,/\/api\/shop\/images\/a{32}\.jpg$/);
  assert.match($('result').textContent,/Gemini で写真 1 枚の光と背景を整えました/);
  $('enhance').checked=false;$('evaluate').click();await tick(40);
  assert.equal(s.state.lastBody.enhance_images,false);assert.match($('result').textContent,/写真 1 枚をそのまま使います/);
  Object.defineProperty($('photos'),'files',{configurable:true,value:Array.from({length:5},()=>new s.win.File(['x'],'a.jpg',{type:'image/jpeg'}))});
  $('evaluate').click();await tick(10);assert.match($('entryStatus').textContent,/4 枚まで/);s.dom.window.close();
});

test('pasting a Rakumart URL auto-fills title, cost and category without overwriting typed values',async()=>{
  const s=setup({saved:'good-token'});const {$}=s;await tick(30);
  const url='https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688';
  $('category').value='キッチン';$('productUrl').value=url;$('productUrl').dispatchEvent(new s.win.Event('change'));await tick(30);
  assert.equal(s.state.fetched,url);assert.equal($('title').value,'硅胶折叠水杯');assert.equal($('costCny').value,'12.5');assert.equal($('category').value,'キッチン');
  assert.match($('autofillStatus').textContent,/✅ 自動入力しました（商品名・原価・写真 1 枚）/);assert.match($('pricePreview').textContent,/販売価格（目安）/);
  $('productUrl').dispatchEvent(new s.win.Event('change'));await tick(30);
  assert.equal(s.calls.filter(c=>c.url==='/api/alibaba/fetch').length,1);s.dom.window.close();
  const b=setup({saved:'good-token',blocked:true});await tick(30);
  b.$('productUrl').value=url;b.$('productUrl').dispatchEvent(new b.win.Event('change'));await tick(30);
  assert.match(b.$('autofillStatus').textContent,/自動入力できませんでした.*手で入れて/);assert.equal(b.$('title').value,'');b.dom.window.close();
});

test('the Rakumart bookmarklet reads the product page and /admin fills the form from the #rakumart= fragment',async()=>{
  const s=setup({saved:'good-token'});await tick(30);
  const href=s.$('bookmarklet').href;assert.match(href,/^javascript:/);assert.ok(!href.includes('good-token'));
  const code=decodeURIComponent(href.slice('javascript:'.length));assert.ok(code.includes("A='https://example.onrender.com'")&&code.includes("'/admin#rakumart='"));s.dom.window.close();
  const page=new JSDOM('<div id="app"></div>',{url:'https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688',runScripts:'outside-only'});
  let opened=null;page.window.open=u=>{opened=u;};page.window.alert=()=>{throw new Error('alert');};
  page.window.document.getElementById('app').__vue__={$children:[{$children:[{details:{goodsId:'573741401425',fromPlatform:'1688',titleT:'シリコン折りたたみカップ',titleC:'硅胶折叠水杯',
    images:['https://cbu01.alicdn.com/img/a.jpg','https://evil.example/x.jpg'],goodsInfo:{priceRanges:[{priceMin:'15.5'},{priceMin:'12.8'}]}}}]}]};
  page.window.eval(code);page.window.close();
  assert.match(opened,/^https:\/\/example\.onrender\.com\/admin#rakumart=/);
  const t=setup({saved:'good-token',url:opened});const {$}=t;await tick(30);
  assert.equal(t.win.location.hash,'');assert.equal($('title').value,'シリコン折りたたみカップ');assert.equal($('costCny').value,'12.8');
  assert.equal($('productUrl').value,'https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688');
  assert.match($('autofillStatus').textContent,/✅ ラクマートから取り込みました（商品名・原価・写真 1 枚）/);
  $('evaluate').click();await tick(30);
  assert.deepEqual(t.state.lastBody.image_urls,['https://cbu01.alicdn.com/img/a.jpg']);assert.equal(t.state.lastBody.cost_cny,12.8);
  assert.equal(t.calls.filter(c=>c.url==='/api/alibaba/fetch').length,0);t.dom.window.close();
  const bad=setup({saved:'good-token',url:'https://example.onrender.com/admin#rakumart='+encodeURIComponent(JSON.stringify({goods_id:'573741401425',platform:'taobao'}))});await tick(30);
  assert.match(bad.$('autofillStatus').textContent,/タオバオ/);assert.equal(bad.$('productUrl').value,'');bad.dom.window.close();
});
