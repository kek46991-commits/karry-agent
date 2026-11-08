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
