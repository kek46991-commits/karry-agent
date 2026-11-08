const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const html=fs.readFileSync('static/shop.html','utf8');
const tick=(ms=10)=>new Promise(r=>setTimeout(r,ms));
const A='a'.repeat(32),B='b'.repeat(32);
const products=[{id:A,title:'青いマグカップ',category:'キッチン用品',price_jpy:2489,images:[`/api/shop/images/${'c'.repeat(32)}.jpg`]},
  {id:B,title:'木製ブックスタンド',category:'インテリア',price_jpy:3980,images:[]}];

function setup(path,extra={}){
  const calls=[];
  const dom=new JSDOM(html,{url:'https://shop.example'+path,runScripts:'dangerously',beforeParse(window){
    window.setInterval=()=>0;
    window.fetch=async url=>{calls.push(url);
      if(url==='/api/shop/products')return {ok:true,status:200,json:async()=>({products,categories:['インテリア','キッチン用品']})};
      if(extra[url])return {ok:true,status:200,json:async()=>({product:extra[url]})};
      return {ok:false,status:404,json:async()=>({detail:'not found'})};};
  }});
  return {dom,win:dom.window,doc:dom.window.document,$:id=>dom.window.document.getElementById(id),calls};
}

test('catalog cards link to /products/:id and open the detail with a shareable URL; closing returns to /shop',async()=>{
  const s=setup('/shop');await tick(20);
  const cards=[...s.doc.querySelectorAll('#grid .card')];
  assert.deepEqual(cards.map(c=>c.getAttribute('href')),[`/products/${A}`,`/products/${B}`]);
  cards[1].dispatchEvent(new s.win.MouseEvent('click',{bubbles:true,cancelable:true,button:0}));await tick();
  assert.equal(s.win.location.pathname,`/products/${B}`);assert.ok(s.$('detail').open);
  assert.equal(cards[1].querySelector('img'),null);assert.equal(cards[1].querySelector('.noimg').textContent,'写真準備中');
  assert.ok(cards[0].querySelector('img'));
  assert.ok(s.$('mainImage').hidden);assert.equal(s.doc.activeElement,s.$('detailTitle'));
  assert.equal(s.$('detailTitle').textContent,'木製ブックスタンド');assert.match(s.doc.title,/木製ブックスタンド｜KERRY ショップ/);
  s.$('closeDetail').click();await tick();
  assert.equal(s.win.location.pathname,'/shop');assert.ok(!s.$('detail').open);s.dom.window.close();
});

test('opening /products/:id directly shows that product; ?id= also works; unknown IDs fall back to the list',async()=>{
  let s=setup(`/products/${A}`);await tick(20);
  assert.ok(s.$('detail').open);assert.equal(s.$('detailTitle').textContent,'青いマグカップ');assert.match(s.$('detailPrice').textContent,/2,489円/);
  s.dom.window.close();
  const extraId='d'.repeat(32),fresh={id:extraId,title:'新着ランプ',price_jpy:5000,images:[]};
  s=setup(`/shop?id=${extraId}`,{[`/api/shop/products/${extraId}`]:fresh});await tick(20);
  assert.equal(s.$('detailTitle').textContent,'新着ランプ');assert.ok(s.calls.includes(`/api/shop/products/${extraId}`));s.dom.window.close();
  s=setup(`/products/${'e'.repeat(32)}`);await tick(20);
  assert.ok(!s.$('detail').open);assert.equal(s.win.location.pathname,'/shop');assert.match(s.$('notice').textContent,/見つかりませんでした/);
  assert.equal(s.doc.querySelectorAll('#grid .card').length,2);s.dom.window.close();
});

function stripeSetup(path,{config={checkout:'stripe',stripe_mode:'test'},items=products,checkout,confirm}={}){
  const posts=[];let assigned=null;
  const dom=new JSDOM(html,{url:'https://shop.example'+path,runScripts:'dangerously',beforeParse(window){
    window.setInterval=()=>0;
    window.fetch=async(url,opts={})=>{
      if(opts.method==='POST')posts.push([url,JSON.parse(opts.body)]);
      if(url==='/api/shop/products')return {ok:true,status:200,json:async()=>({products:items,categories:[]})};
      if(url==='/api/shop/config')return {ok:true,status:200,json:async()=>config};
      if(url==='/api/shop/checkout')return checkout||{ok:true,status:201,json:async()=>({url:'https://checkout.stripe.com/c/pay/cs_test_abc'})};
      if(url==='/api/shop/checkout/confirm')return confirm||{ok:true,status:200,json:async()=>({order_id:'0123456789abcdef',title:'青いマグカップ',quantity:2,total_jpy:4978,status:'paid'})};
      if(url==='/api/shop/orders')return {ok:true,status:201,json:async()=>({order_id:'x',total_jpy:2489})};
      return {ok:false,status:404,json:async()=>({})};};
  }});
  dom.window.__go=u=>{assigned=u;};dom.window.eval('shop.go=u=>window.__go(u)');
  return {dom,win:dom.window,doc:dom.window.document,$:id=>dom.window.document.getElementById(id),posts,assigned:()=>assigned};
}

test('two-column orange grid shows % OFF / 売れ筋 only from data, 送料無料 always',async()=>{
  assert.match(html,/\.grid\{display:grid;grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);assert.match(html,/--red:#f2292e/);
  const items=[{...products[0],list_price_jpy:3200,discount_percent:22,bestseller:true,free_shipping:true},{...products[1],list_price_jpy:null,discount_percent:null,bestseller:false,free_shipping:true}];
  const s=stripeSetup('/shop',{items});await tick(20);
  const [a,b]=[...s.doc.querySelectorAll('#grid .card')];
  assert.equal(a.querySelector('.off').textContent,'22% OFF');assert.equal(a.querySelector('.was').textContent,'3,200円');assert.equal(a.querySelector('.hot').textContent,'売れ筋');
  assert.equal(b.querySelector('.off'),null);assert.equal(b.querySelector('.hot'),null);assert.equal(b.querySelector('.was'),null);
  assert.ok([a,b].every(c=>[...c.querySelectorAll('.tag')].some(t=>t.textContent==='送料無料')));
  s.dom.window.close();
});

test('購入手続きへ進む creates a Stripe Checkout session from the product id and redirects to checkout.stripe.com',async()=>{
  const s=stripeSetup(`/products/${A}`);await tick(20);
  assert.equal(s.$('checkoutButton').textContent,'購入手続きへ進む');assert.ok(!s.$('testMode').hidden);assert.ok(s.$('orderForm').hidden);
  s.$('buyQuantity').value='2';s.$('checkoutButton').click();await tick(20);
  assert.deepEqual(s.posts,[['/api/shop/checkout',{product_id:A,quantity:2,variant:''}]]);
  assert.equal(s.assigned(),'https://checkout.stripe.com/c/pay/cs_test_abc');s.dom.window.close();
  const bad=stripeSetup(`/products/${A}`,{checkout:{ok:true,status:201,json:async()=>({url:'https://evil.example/pay'})}});await tick(20);
  bad.$('checkoutButton').click();await tick(20);
  assert.equal(bad.assigned(),null);assert.ok(!bad.$('checkoutButton').disabled);bad.dom.window.close();
});

test('success return confirms the paid session; cancel return says nothing was charged',async()=>{
  let s=stripeSetup('/shop?checkout=success&session_id=cs_test_abc123');await tick(20);
  assert.deepEqual(s.posts,[['/api/shop/checkout/confirm',{session_id:'cs_test_abc123'}]]);
  assert.match(s.$('notice').textContent,/ご注文ありがとうございます.*4,978円/);assert.equal(s.win.location.search,'');s.dom.window.close();
  s=stripeSetup(`/products/${A}?checkout=cancel`);await tick(20);
  assert.match(s.$('notice').textContent,/キャンセル/);assert.ok(s.$('detail').open);assert.equal(s.posts.length,0);s.dom.window.close();
});

test('without Stripe keys the same button falls back to the order request form',async()=>{
  const s=stripeSetup(`/products/${A}`,{config:{checkout:'request',stripe_mode:null}});await tick(20);
  assert.ok(s.$('testMode').hidden);s.$('checkoutButton').click();await tick();
  assert.ok(!s.$('orderForm').hidden);assert.equal(s.posts.length,0);
  s.$('orderName').value='山田';s.$('orderContact').value='a@example.com';s.$('checkoutButton').click();await tick(20);
  assert.equal(s.posts[0][0],'/api/shop/orders');assert.equal(s.posts[0][1].name,'山田');assert.match(s.$('orderStatus').textContent,/受け付けました/);
  s.dom.window.close();
});
