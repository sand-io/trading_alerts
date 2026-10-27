// Run: node kite/tests/test_daily_alerts_ui.js
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(`${__dirname}/../alerts.html`,'utf8');
const mainHtml=fs.readFileSync(`${__dirname}/../dashboard.html`,'utf8');
function node(){return {children:[],value:'',textContent:'',hidden:false,append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items}}}
const nodes=new Map(),document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node};
const ctx=vm.createContext({document,URLSearchParams,Intl,Date,console,fetch:()=>new Promise(()=>{})});
vm.runInContext(html.split('<script>')[1].split('</script>')[0],ctx);
const event={instrument_token:999,symbol:'MCX:GOLD26DECFUT',strategy_id:'nnf',strategy_name:'NNF <test>',
 alert_side:'bullish',confirmation_timeframe:'3m',evaluation_mode:'closed_candle',price:100,
 market_time:'2026-10-27T10:03:00+05:30',threshold:100,candle:{time:100,close:99},
 bullish:{passed:16,total:16,percentage:100,rules:[{passed:true,text:'Close > EMA <script>test</script>'}]}};
ctx.item={id:42,day:'2026-10-27',event};
const card=vm.runInContext('renderAlert(item)',ctx);
assert.equal(card.className,'alert-card bullish');
assert.equal(card.children[0].children[0].textContent,'🟢 Bullish · MCX:GOLD26DECFUT');
assert.equal(card.children[1].textContent,'NNF <test>');
assert.equal(card.children[4].children[0].href,'/?alert_id=42');
assert.equal(card.children[4].children[0].target,'_blank');
assert.equal(card.children[4].children[0].rel,'noopener');
assert.ok(!html.includes('innerHTML')); // Saved text never becomes executable markup.

async function testList(){
 const pending=[];ctx.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
 const response=data=>({ok:true,json:async()=>data});
 const page={alerts:[ctx.item],total:2,bullish:1,bearish:1,strategies:[{id:'nnf',name:'NNF',alert_count:2},{id:'btst_setup',name:'BTST',alert_count:0},{id:'default',name:'Default',alert_count:0}],next_cursor:42};
 nodes.get('day').value='2026-10-27';nodes.get('side').value='bullish';nodes.get('search').value='GOLD & CRUDE';
 const first=vm.runInContext('loadAlerts()',ctx);
 let request=pending.shift();assert.ok(request.url.includes('side=bullish'));assert.ok(request.url.includes('search=GOLD+%26+CRUDE'));
 request.resolve(response(page));await first;assert.equal(nodes.get('alerts').children.length,1);assert.equal(nodes.get('more').hidden,false);
 assert.equal(nodes.get('strategy').children.length,4);
 assert.equal(nodes.get('strategy').children[2].textContent,'BTST (0 alerts)');
 const next=vm.runInContext('loadAlerts(true)',ctx);request=pending.shift();assert.ok(request.url.includes('before_id=42'));
 request.resolve(response({...page,alerts:[{...ctx.item,id:41}],next_cursor:null}));await next;
 assert.equal(nodes.get('alerts').children.length,2);assert.equal(nodes.get('more').hidden,true);
 const old=vm.runInContext('loadAlerts()',ctx),oldRequest=pending.shift();
 nodes.get('side').value='bearish';const latest=vm.runInContext('loadAlerts()',ctx),latestRequest=pending.shift();
 latestRequest.resolve(response({...page,alerts:[],total:0,bullish:0,bearish:0,next_cursor:null}));await latest;
 oldRequest.resolve(response(page));await old;
 assert.equal(nodes.get('alerts').children.length,1);assert.equal(nodes.get('alerts').children[0].className,'empty');
 const failed=vm.runInContext('loadAlerts()',ctx);pending.shift().resolve({ok:false,text:async()=>'Temporary database error'});await failed;
 assert.ok(nodes.get('status').textContent.includes('Could not load alerts'));assert.equal(nodes.get('refresh').disabled,false);
}

async function testChartLink(){
 const fields=new Map(),doc={getElementById(id){if(!fields.has(id))fields.set(id,node());return fields.get(id)},
  createElement:node,querySelectorAll:()=>[]};
 const calls=[],state={document:doc,fetch:async()=>({ok:true,json:async()=>({id:42,day:'2026-10-27',event})}),
  reviewMode:false,reviewRequest:0,snapshotRequest:0,currentToken:1,allSymbols:[],symbolSelect:node(),symbolSearch:node(),
  reviewStrategyId:null,currentTimeframe:'5m',activeStrategyId:'default',statusEl:node(),candleData:[],signalMarkers:[],
  COLORS:{green:'green',red:'red'},refreshMarkers(){calls.push('markers')},
  loadSnapshot:async token=>{calls.push(token);assert.equal(state.reviewMode,true);assert.equal(state.reviewStrategyId,'nnf');assert.equal(state.currentTimeframe,'3m');state.candleData=[event.candle]},
  inspectSaved:(saved,source)=>{calls.push(source);assert.equal(saved,event)},
  chart:{timeScale:()=>({setVisibleLogicalRange:range=>calls.push(range)})}};
 const context=vm.createContext(state);
 vm.runInContext(mainHtml.slice(mainHtml.indexOf('async function openSavedAlert('),mainHtml.indexOf('async function initializeDashboard(')),context);
 await vm.runInContext('openSavedAlert(42)',context);
 assert.equal(state.currentToken,999);assert.equal(state.activeStrategyId,'default');
 assert.equal(fields.get('reviewDate').value,'2026-10-27');assert.equal(fields.get('historyPanel').hidden,false);
 assert.ok(calls.includes(999));assert.ok(calls.includes('Saved alert'));assert.equal(state.signalMarkers[0].text,'Bullish alert');
 // A missing saved chart never substitutes a live candle or a different alert.
 state.candleData=[];state.loadSnapshot=async()=>{};calls.length=0;
 await vm.runInContext('openSavedAlert(42)',context);assert.ok(!calls.includes('Saved alert'));
 let release;state.loadSnapshot=()=>new Promise(resolve=>{release=resolve});
 const pending=vm.runInContext('openSavedAlert(42)',context);
 for(let i=0;i<10&&!release;i++)await Promise.resolve();
 state.currentToken=123;state.candleData=[event.candle];release();await pending;
 assert.ok(!calls.includes('Saved alert')); // A late request cannot overwrite another symbol.
 state.fetch=async()=>({ok:false,text:async()=>'Alert expired'});
 await assert.rejects(vm.runInContext('openSavedAlert(42)',context),/Alert expired/);
}
(async()=>{await testList();await testChartLink();console.log('Daily alert list, pagination, filters, request isolation, safe rendering, and saved-chart links passed')})().catch(error=>{console.error(error);process.exitCode=1});
