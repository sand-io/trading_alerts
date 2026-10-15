// Run: node kite/tests/test_dashboard_ui.js
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(`${__dirname}/../dashboard.html`,'utf8');
new vm.Script(html.split('<script>')[1].split('</script>')[0]);
const series=[],scales=new Map();
const chart={addLineSeries(options){const line={options,applyOptions(value){Object.assign(this.options,value)},setData(points){this.points=points},update(point){this.points??=[];assert.ok(!this.points.length||point.time>=this.points.at(-1).time,'series updates must be chronological');if(this.points.at(-1)?.time===point.time)this.points[this.points.length-1]=point;else this.points.push(point)},createPriceLine(options){return {options,applyOptions(value){Object.assign(this.options,value)}}},removePriceLine(){}};series.push(line);return line},priceScale(id){return {applyOptions(options){scales.set(id,options)}}}};
const event=(time,bull,bear,source='live')=>({candle:{time,open:100,high:102,low:99,close:101},
 bullish:{percentage:bull,passed:bull/100*14,total:14,triggered:bull>=30},
 bearish:{percentage:bear,passed:bear/100*14,total:14,triggered:bear>=30},evaluation_source:source});
assert.ok(!html.includes('const combinedStrategy=')); // no strategy overlay on price chart
assert.ok(!html.includes('priceFormatter:')); // preserve per-series indicator units
let chartCount=0;
const chartMocks=[];
const nodes=new Map(),node=()=>({style:{},classList:{toggle(){}},children:[],value:'',append(...items){this.children.push(...items)},replaceChildren(...children){this.children=children;this.value=children[0]?.value??''},setAttribute(key,value){this[key]=value},removeAttribute(key){delete this[key]}});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,addEventListener(){},querySelectorAll(){return []}};
const context=vm.createContext({document,
 LightweightCharts:{CrosshairMode:{Normal:0},createChart(){chartCount++;const mock={...chart,range:null,removeSeries(){},timeScale(){return {subscribeVisibleLogicalRangeChange(callback){mock.rangeCallback=callback},getVisibleLogicalRange(){return mock.range},setVisibleLogicalRange(range){mock.range=range;mock.rangeCallback?.(range)}}},addCandlestickSeries(){return {setData(){},setMarkers(){},update(){},createPriceLine(options){return options},removePriceLine(){}}},subscribeClick(callback){mock.clickCallback=callback},subscribeCrosshairMove(callback){(mock.crosshairCallbacks??=[]).push(callback)},applyOptions(){}};chartMocks.push(mock);return mock}},
 ResizeObserver:class{observe(){}},setInterval(){},clearInterval(){},clearTimeout(){},
 fetch(){return new Promise(()=>{})},window:{},console});
vm.runInContext(html.split('<script>')[1].split('</script>')[0],context);
vm.runInContext("renderLive({strategy_applicable:false,scope_message:'Only MCX Gold and Crude Oil futures.'})",context);
assert.equal(nodes.get('bullRules').children.length,0);
assert.equal(nodes.get('bearRules').children.length,0);
assert.equal(nodes.get('strategyContext').textContent,'Only MCX Gold and Crude Oil futures.');
vm.runInContext("feedMarketOpen=true;setStatus('connected');",context);
assert.equal(nodes.get('status').textContent,'Kite feed connected');
vm.runInContext("feedMarketOpen=false;setStatus('connected');",context);
assert.equal(nodes.get('status').textContent,'Feed connected · outside regular hours');
vm.runInContext('feedMarketOpen=null',context);
assert.equal(chartCount,2);
chartMocks[0].rangeCallback({from:0,to:5});
assert.deepEqual(chartMocks[1].range,{from:0,to:5});
chartMocks[1].rangeCallback({from:1,to:4});
assert.deepEqual(chartMocks[0].range,{from:1,to:4});
vm.runInContext("candleData=[{time:1},{time:2}];configureStudies([{id:'adx',label:'ADX',default_visible:true,lines:[{id:'adx',label:'ADX',color:'#38c7d9',points:[{time:1,value:0}]}],levels:[]}])",context);
assert.equal(series.at(-1).points[0].value,0);
assert.equal(series.at(-1).points[1].value,undefined);
assert.ok(html.includes('syncRange(chart,studyChart);syncRange(studyChart,chart)'));
assert.ok(!html.includes('strategyDialog'));
const matches=vm.runInContext("matchingSymbols([{token:1,symbol:'NFO:RELIANCE26OCTFUT'},{token:2,symbol:'NFO:TCS26OCTFUT'}],'rel')",context);
assert.equal(JSON.stringify(matches),JSON.stringify([{token:1,symbol:'NFO:RELIANCE26OCTFUT'}]));
assert.equal(vm.runInContext("matchingSymbols([{token:1,symbol:'NFO:RELIANCE26OCTFUT'}],'missing').length",context),0);
assert.ok(html.includes('aria-label="Search trading symbols"'));
assert.ok(html.includes('renderOhlc(candleData.at(-1))'));
assert.ok(html.includes('renderOhlc(candle);refreshMarkers()'));
vm.runInContext("renderMarket({instrument_token:1,market_time:'2026-10-08T13:43:00+05:30',price:100,vwap:99});renderLive({type:'candle_update',instrument_token:1,market_time:'2026-10-08T13:39:00+05:30',price:90,vwap:89})",context);
assert.equal(nodes.get('price').textContent,'100.00');
assert.equal(vm.runInContext("marketIsCurrent({instrument_token:1,market_time:'2026-10-08T13:39:00+05:30'})",context),false);
async function testSearchSelection(){
 const pickerNodes=new Map(),listeners={};
 const pickerDocument={getElementById(id){if(!pickerNodes.has(id))pickerNodes.set(id,node());return pickerNodes.get(id)},createElement:node,addEventListener(type,callback){listeners[type]=callback}};
 const picker={document:pickerDocument,currentToken:null,allSymbols:[{token:1,symbol:'NFO:RELIANCE26OCTFUT'},{token:2,symbol:'NFO:TCS26OCTFUT'},{token:3,symbol:'NSE:RELIANCE'}],reviewMode:false,socket:null,
  loadSnapshot:async token=>picker.loaded.push(token),loadRecordedSelection:async token=>picker.reviewed.push(token),connect:token=>picker.connected.push(token),setStatus(){},loaded:[],reviewed:[],connected:[]};
 picker.symbolSelect=pickerDocument.getElementById('symbol');picker.symbolSearch=pickerDocument.getElementById('symbolSearch');
 const search=vm.createContext(picker);
 vm.runInContext(html.slice(html.indexOf('async function chooseSymbol('),html.indexOf("document.querySelectorAll('.tf-btn')")),search);
 await vm.runInContext('selectSymbol(allSymbols[0])',search);
 assert.equal(picker.currentToken,1);assert.deepEqual(picker.loaded,[1]);
 assert.equal(picker.symbolSearch.value,'NFO:RELIANCE26OCTFUT');
 picker.symbolSearch.onfocus();assert.equal(pickerNodes.get('symbolResults').hidden,false);
 assert.equal(picker.symbolSearch['aria-expanded'],'true');
 picker.symbolSearch.value='tcs';picker.symbolSearch.oninput();
 const results=pickerNodes.get('symbolOptions');
 assert.equal(results.children.length,1);assert.equal(results.children[0].children[0].children[0].textContent,'TCS26OCTFUT');
 assert.equal(picker.currentToken,1);assert.deepEqual(picker.loaded,[1]); // typing never changes the chart
 await results.children[0].onclick();
 assert.equal(picker.currentToken,2);assert.deepEqual(picker.loaded,[1,2]);
 assert.equal(pickerNodes.get('symbolResults').hidden,true);assert.equal(picker.symbolSearch.value,'NFO:TCS26OCTFUT');
 // Exact symbols precede prefixes; exchange and punctuation are optional.
 assert.equal(vm.runInContext("matchingSymbols(allSymbols,'reliance')[0].token",search),3);
 assert.equal(vm.runInContext("matchingSymbols(allSymbols,'nfo tcs')[0].token",search),2);
 picker.symbolSearch.value='missing';picker.symbolSearch.oninput();
 assert.ok(results.children[0].textContent.includes('No matching stocks'));
 picker.symbolSearch.onkeydown({key:'Enter',preventDefault(){}});
 assert.deepEqual(picker.loaded,[1,2]);assert.equal(picker.currentToken,2);
 picker.symbolSearch.onkeydown({key:'Escape',preventDefault(){}});
 assert.equal(picker.symbolSearch.value,'NFO:TCS26OCTFUT');assert.equal(pickerNodes.get('symbolResults').hidden,true);
 pickerDocument.getElementById('symbolClear').onclick();
 assert.equal(picker.symbolSearch.value,'');assert.equal(results.children.length,3);
 picker.symbolSearch.onkeydown({key:'ArrowDown',preventDefault(){}});
 assert.equal(picker.symbolSearch['aria-activedescendant'],'symbol-option-0');
 picker.symbolSearch.onkeydown({key:'ArrowUp',preventDefault(){}}); // wrap to last
 assert.equal(picker.symbolSearch['aria-activedescendant'],'symbol-option-2');
 picker.symbolSearch.onkeydown({key:'Enter',preventDefault(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(picker.currentToken,3);assert.deepEqual(picker.loaded,[1,2,3]);
 // The same selection makes no redundant snapshot call.
 await vm.runInContext('selectSymbol(allSymbols[2])',search);assert.deepEqual(picker.loaded,[1,2,3]);
 picker.reviewMode=true;
 picker.symbolSearch.onfocus();picker.symbolSearch.value='tcs';picker.symbolSearch.oninput();
 await results.children[0].onclick();assert.deepEqual(picker.reviewed,[2]);
 picker.symbolSearch.onfocus();picker.symbolSearch.value='unfinished';
 listeners.click({target:{closest:()=>null}});
 assert.equal(pickerNodes.get('symbolResults').hidden,true);assert.equal(picker.symbolSearch.value,'NFO:TCS26OCTFUT');
 console.log('Symbol picker immediate results, click selection, keyboard navigation, ranking, empty states, and recorded review tests passed');
}
testSearchSelection().catch(error=>{console.error(error);process.exitCode=1});

vm.runInContext("reviewMode=true;reviewEvents=[{candle:{time:1},market_time:'2026-10-08T10:00:00+05:30',instrument_token:1,price:100,vwap:99,threshold:70,bullish:{passed:1,total:1,percentage:100,rules:[]},bearish:{passed:0,total:1,percentage:0,rules:[]}}];currentTimeframe='5m';renderHistory({evaluations:reviewEvents});loadingSnapshot=false",context);
chartMocks[0].clickCallback({time:1});
assert.ok(nodes.get('strategyContext').textContent.includes('Saved candle evaluation'));
// Clicking uses the exact saved rule state, with confirmed candles taking priority.
vm.runInContext(`
 const savedEval={...reviewEvents[0],bullish:{passed:0,total:1,percentage:0,rules:[{text:'Bull rule',passed:false}]},bearish:{passed:1,total:1,percentage:100,rules:[{text:'Bear rule',passed:true}]}};
 const confirmedEval={...savedEval,bullish:{passed:1,total:1,percentage:100,rules:[{text:'Bull rule',passed:true}]},bearish:{passed:0,total:1,percentage:0,rules:[{text:'Bear rule',passed:false}]}};
 const latestEval={...savedEval,candle:{time:2},threshold:80};
 renderHistory({evaluations:[savedEval,latestEval],signals:[confirmedEval],strategy:latestEval});
`,context);
const hover=time=>chartMocks[0].crosshairCallbacks.forEach(callback=>callback({time}));
const click=time=>chartMocks[0].clickCallback({time});
click(1);
assert.equal(nodes.get('bullScore').textContent,'1 / 1 (100.0%)');
assert.equal(nodes.get('bearScore').textContent,'0 / 1 (0.0%)');
assert.ok(nodes.get('strategyContext').textContent.includes('Saved confirmed candle state'));
assert.equal(nodes.get('bullRules').children.length,1);
click(2); // No confirmation: use the last recorded evaluation for the candle.
assert.equal(nodes.get('bullScore').textContent,'0 / 1 (0.0%)');
assert.equal(nodes.get('bearScore').textContent,'1 / 1 (100.0%)');
assert.equal(nodes.get('threshold').textContent,'80%');
click(3); // Never leave another candle's rules visible for a recording gap.
assert.equal(nodes.get('bullRules').children.length,0);
assert.equal(nodes.get('bearRules').children.length,0);
assert.equal(nodes.get('strategyContext').textContent,'No recorded evaluation for this candle.');
// Moving or leaving the chart must retain the clicked rules and scores.
click(1);
const clickedState=()=>JSON.stringify(['bullScore','bearScore','overlayBullScore','overlayBearScore','strategyContext','threshold'].map(id=>nodes.get(id).textContent));
const selection=clickedState();
for(const time of [2,3,undefined]){hover(time);assert.equal(clickedState(),selection)}
click(undefined);assert.equal(clickedState(),selection);
for(const settings of ["reviewMode=false", "reviewMode=true;loadingSnapshot=true"]){
 vm.runInContext(settings,context);click(2);assert.equal(clickedState(),selection);
}
vm.runInContext("loadingSnapshot=false;reviewMode=true;currentTimeframe='5m';resetHistory('loading')",context);
click(1);
assert.equal(nodes.get('strategyContext').textContent,'No recorded evaluation for this candle.');
console.log('Candle click confirmation, fallback, missing data, persistent selection, and live/loading isolation tests passed');
// Native 3m/15m confirmation and legacy 5m history coexist without remapping.
vm.runInContext(`
 const legacyConfirmed={...confirmedEval,bullish:{...confirmedEval.bullish,triggered:true}};
 const threeMinute={...legacyConfirmed,confirmation_timeframe:'3m',threshold:60};
 const fifteenMinute={...latestEval,candle:{time:1},confirmation_timeframe:'15m',threshold:90,bearish:{...latestEval.bearish,triggered:true}};
 const nativeHistory={evaluations:[threeMinute,fifteenMinute],signals:[legacyConfirmed,threeMinute,fifteenMinute]};
 renderHistory(nativeHistory);candleData=[{time:1}];
 currentTimeframe='3m';
`,context);
click(1);
assert.equal(nodes.get('threshold').textContent,'60%');
assert.ok(nodes.get('strategyContext').textContent.includes('confirmation 3m'));
assert.equal(vm.runInContext('markers(nativeHistory.signals).length',context),1);
vm.runInContext("currentTimeframe='15m'",context);click(1);
assert.equal(nodes.get('threshold').textContent,'90%');
assert.equal(nodes.get('bearScore').textContent,'1 / 1 (100.0%)');
assert.equal(vm.runInContext('markers(nativeHistory.signals).length',context),1);
vm.runInContext("currentTimeframe='5m'",context);click(1);
assert.equal(nodes.get('threshold').textContent,'70%');
assert.equal(vm.runInContext('markers(nativeHistory.signals).length',context),1);
// A rule entry selection must not prevent clicking the same candle again.
vm.runInContext('inspectSaved(fifteenMinute)',context);click(1);
assert.equal(nodes.get('threshold').textContent,'70%');
console.log('Strategy-native candle clicks, timeframe marker filtering, and legacy history tests passed');
// A delayed result from the same strategy's previous version must not repaint
// the current rules or its confirmation interval after a configuration reload.
vm.runInContext("reviewMode=false;activeStrategyId='default';activeStrategyVersion='new';renderLive({...confirmedEval,strategy_id:'default',strategy_version:'new',confirmation_timeframe:'3m'})",context);
const currentRuleState=clickedState();
vm.runInContext("renderLive({...latestEval,strategy_id:'default',strategy_version:'old',confirmation_timeframe:'5m'})",context);
assert.equal(clickedState(),currentRuleState);
vm.runInContext("reviewMode=true;activeStrategyVersion=null",context);




// No strategy HTTP polling: sync only on a change, with duplicate events combined.
assert.ok(!/setInterval\([^\n]*api\/strategies/.test(html));
assert.ok(html.includes("data.type==='strategy_changed'"));
async function testStrategySync(){
 let calls=0,release;
 const syncContext=vm.createContext({Promise,activeStrategyId:'default',activeStrategyVersion:'v1',currentToken:null,reviewMode:false,
   loadStrategies:async()=>{calls++;await new Promise(resolve=>{release=resolve});syncContext.activeStrategyId='trend';syncContext.activeStrategyVersion='v2'},
   loadSnapshot:async()=>{throw new Error('No symbol selected')}});
 vm.runInContext(html.slice(html.indexOf('let strategyRefresh='),html.indexOf("document.getElementById('activateStrategy').onclick")),syncContext);
 await vm.runInContext("refreshStrategyState({id:'default',version:'v1'})",syncContext);
 assert.equal(calls,0);
 const pending=vm.runInContext("refreshStrategyState({id:'trend',version:'v2'})",syncContext);
 for(let i=0;i<20;i++)vm.runInContext("refreshStrategyState({id:'trend',version:'v2'})",syncContext);
 assert.equal(calls,1);release();await pending;assert.equal(calls,1);
 await vm.runInContext("refreshStrategyState({id:'trend',version:'v2'})",syncContext);assert.equal(calls,1);
 // An active-strategy change must preserve a saved candle the user is inspecting.
 syncContext.reviewMode=true;syncContext.currentToken=1;
 const reviewRefresh=vm.runInContext("refreshStrategyState({id:'trend',version:'v3'})",syncContext);
 assert.equal(calls,2);release();await reviewRefresh;
}
testStrategySync().catch(error=>{console.error(error);process.exitCode=1});

async function testRecordedStrategySelection(){
 const reviewNodes=new Map(),requests=[];
 const reviewDocument={getElementById(id){if(!reviewNodes.has(id))reviewNodes.set(id,node());return reviewNodes.get(id)},createElement:node};
 reviewDocument.getElementById('reviewDate').value='2026-10-12';
 const reviewContext=vm.createContext({document:reviewDocument,activeStrategyId:'default',reviewStrategyId:null,currentToken:1,reviewRequest:0,reviewMode:true,
  fetch:async url=>{requests.push(url);return {ok:true,json:async()=>[{id:'default',name:'Default',evaluations:10},{id:'removed',name:'Archived profile',evaluations:8}]}},
  loadSnapshot:async token=>requests.push('snapshot:'+token)});
 vm.runInContext(html.slice(html.indexOf('async function loadReviewStrategies('),html.indexOf("document.getElementById('reviewButton').onclick")),reviewContext);
 await vm.runInContext('loadReviewStrategies(1)',reviewContext);
 assert.equal(reviewContext.reviewStrategyId,'default');
 await reviewDocument.getElementById('reviewStrategy').onchange({target:{value:'removed'}});
 assert.equal(reviewContext.reviewStrategyId,'removed');
 assert.equal(reviewContext.activeStrategyId,'default');
 assert.equal(requests.at(-1),'snapshot:1');
 assert.ok(requests.every(url=>!url.includes('/activate')));
 assert.equal(reviewDocument.getElementById('reviewStrategy').children[1].textContent,'Archived profile (8 evaluations)');
}
testRecordedStrategySelection().catch(error=>{console.error(error);process.exitCode=1});

async function testRecordedSessionIsolation(){
 const sessionNodes=new Map(),pending=[];
 const sessionDocument={getElementById(id){if(!sessionNodes.has(id))sessionNodes.set(id,node());return sessionNodes.get(id)},createElement:node};
 sessionDocument.getElementById('reviewDate').value='2026-10-12';
 const sandbox={document:sessionDocument,console,window:{},reviewMode:true,reviewEvents:[],reviewAlerts:[],reviewFilter:'alerts',reviewRequest:0,
  currentToken:1,currentTimeframe:'5m',reviewStrategyId:'default',activeStrategyId:'default',allSymbols:[{token:1,symbol:'FIRST'},{token:2,symbol:'SECOND'}],
  candles:{setData(){}},refreshMarkers(){},configureOverlays(){},configureStudies(){},renderOhlc(){},applyLevels(){},setStatus(){},resetVisibleRange(){},
  eventTimeframe:event=>event.confirmation_timeframe||'5m',markers:()=>[],renderSide(){},renderLive(){},renderMarket(){},inspectSaved(event){sandbox.inspected=event},
  fetch:url=>new Promise(resolve=>pending.push({url,resolve}))};
 const session=vm.createContext(sandbox);
 vm.runInContext(html.slice(html.indexOf('let reviewCandleStates='),html.indexOf('chart.subscribeClick')),session);
 vm.runInContext(html.slice(html.indexOf('function clearChartSelection('),html.indexOf('function updateCandle(')),session);
 const selectionCode=html.slice(html.indexOf('async function loadReviewStrategies('),html.indexOf('function matchingSymbols('));
 vm.runInContext(selectionCode,session);
 const evaluation={...event(1,100,0),market_time:'2026-10-12T10:00:00+05:30',alert_side:'bullish'};
 const recorded={candles:[],signals:[],overlays:[],studies:[],evaluations:[evaluation],alerts:[evaluation],threshold:90};
 vm.runInContext('renderHistory',session)(recorded);
 assert.equal(sessionNodes.get('historyEvents').children.length,1);
 sessionNodes.get('historyEvents').children[0].onclick();assert.equal(sandbox.inspected,evaluation);
 // Switching clears the old events immediately, before any network response.
 sandbox.currentToken=2;
 const missing=vm.runInContext('loadRecordedSelection(2)',session);
 assert.equal(sessionNodes.get('historyEvents').children.length,0);
 assert.equal(sessionNodes.get('sessionAlertCount').textContent,'—');
 assert.equal(sessionNodes.get('sessionContext').textContent,'SECOND · 2026-10-12 · IST');
 assert.equal(sandbox.reviewEvents.length,0);
 pending.shift().resolve({ok:true,json:async()=>[]});await new Promise(resolve=>setImmediate(resolve));
 pending.shift().resolve({ok:false,text:async()=>'{"detail":"No recorded chart for this date"}'});await missing;
 assert.equal(sessionNodes.get('historyEvents').children.length,0);
 assert.equal(sessionNodes.get('sessionState').textContent,'No recording');
 assert.equal(sessionNodes.get('sessionAlertCount').textContent,'0');
 // A valid recording with evaluations and zero alerts has an explicit alert empty state.
 const zero=vm.runInContext('loadSnapshot(2)',session);
 pending.shift().resolve({ok:true,json:async()=>({...recorded,alerts:[]})});await zero;
 assert.equal(sessionNodes.get('historyEvents').children.length,0);
 assert.equal(sessionNodes.get('sessionEmptyTitle').textContent,'No alerts sent');
 sessionNodes.get('sessionEvaluations').onclick();
 assert.equal(sessionNodes.get('historyEvents').children.length,1);
 assert.equal(sessionNodes.get('sessionEvaluationCount').textContent,1);
 // Slow old symbol responses cannot restore their events over the current selection.
 sandbox.currentToken=1;const old=vm.runInContext('loadSnapshot(1)',session);const oldRequest=pending.shift();
 sandbox.currentToken=2;const fresh=vm.runInContext('loadSnapshot(2)',session);
 pending.shift().resolve({ok:true,json:async()=>({...recorded,evaluations:[],alerts:[]})});await fresh;
 oldRequest.resolve({ok:true,json:async()=>recorded});await old;
 assert.equal(sessionNodes.get('historyEvents').children.length,0);
 assert.equal(sandbox.reviewEvents.length,0);
 // The profile discovery response is also guarded, including changes of date.
 const oldProfiles=vm.runInContext('loadReviewStrategies(2)',session);const profileRequest=pending.shift();
 sessionNodes.get('reviewDate').value='2026-10-13';
 const newProfiles=vm.runInContext('loadReviewStrategies(2)',session);
 pending.shift().resolve({ok:true,json:async()=>[{id:'new',name:'New session',evaluations:1}]});await newProfiles;
 profileRequest.resolve({ok:true,json:async()=>[{id:'old',name:'Old session',evaluations:1}]});await oldProfiles;
 assert.equal(sandbox.reviewStrategyId,'new');
 assert.equal(sessionNodes.get('reviewStrategy').children[0].textContent,'New session (1 evaluations)');
 // Errors clear both the list and inspected data rather than leaving prior alerts behind.
 const failed=vm.runInContext('loadSnapshot(2)',session);
 pending.shift().resolve({ok:false,text:async()=> 'Server unavailable'});
 await assert.rejects(failed,/Server unavailable/);
 assert.equal(sessionNodes.get('historyEvents').children.length,0);
 assert.equal(sessionNodes.get('sessionState').textContent,'Unavailable');
 console.log('Recorded session empty states, immediate clearing, event filters, and request race tests passed');
}
testRecordedSessionIsolation().catch(error=>{console.error(error);process.exitCode=1});

// Live study lines may share IDs (for example OBV with different EMA periods).
// Only the selected study may write to the displayed series.
vm.runInContext(`
 reviewMode=false;loadingSnapshot=false;currentToken=1;currentTimeframe='3m';
 candleData=[{time:1,open:100,high:101,low:99,close:100}];window.lastCandleTime=1;
 renderedMarketToken=null;clearOverlays();
 configureStudies([
  {id:'obv5',label:'OBV 5',lines:[{id:'obv',label:'OBV',points:[{time:1,value:10}]}]},
  {id:'obv9',label:'OBV 9',lines:[{id:'obv',label:'OBV',points:[{time:1,value:20}]}]}
 ]);
 updateCandle({instrument_token:1,candles_by_timeframe:{'3m':{time:2,open:101,high:102,low:100,close:101}}});
`,context);
assert.equal(series.at(-1).points.at(-1).time,2);
assert.equal(series.at(-1).points.at(-1).value,undefined); // retain timeline before the evaluation arrives
vm.runInContext(`updateStudy({instrument_token:1,study_values:{timeframe:'3m',studies:[
 {id:'obv5',lines:[{id:'obv',time:2,value:11}]},
 {id:'obv9',lines:[{id:'obv',time:2,value:99}]}
]}})`,context);
assert.equal(series.at(-1).points.at(-1).value,11);
vm.runInContext(`updateCandle({instrument_token:1,candles_by_timeframe:{'3m':{time:3,open:102,high:103,low:101,close:102}}});
 updateStudy({instrument_token:1,study_values:{timeframe:'3m',studies:[{id:'obv5',lines:[{id:'obv',time:2,value:12}]}]}});`,context);
assert.equal(series.at(-1).points.at(-1).time,3); // late study cannot update behind the chart's last point
assert.equal(series.at(-1).points.at(-1).value,undefined);
vm.runInContext("selectedStudy='obv9';drawSelectedStudy()",context);
assert.equal(series.at(-1).points[1].value,99); // unselected studies still receive their own values

// Evaluations can lag ticks without being stale relative to the last evaluation.
vm.runInContext(`
 renderedEvaluationToken=null;activeStrategyVersion=null;
 renderMarket({instrument_token:1,market_time:'2026-10-12T10:05:00+05:30',price:105});
 renderLive({...confirmedEval,instrument_token:1,market_time:'2026-10-12T10:04:00+05:30'});
`,context);
assert.equal(nodes.get('bullScore').textContent,'1 / 1 (100.0%)');
vm.runInContext("renderLive({...latestEval,instrument_token:1,market_time:'2026-10-12T10:03:00+05:30'})",context);
assert.equal(nodes.get('bullScore').textContent,'1 / 1 (100.0%)');
assert.equal(nodes.get('price').textContent,'105.00');
vm.runInContext("reviewMode=true;candleData=[{time:1,high:100,low:100}];levelsVisible=true;applyLevels({previous_high:110})",context);
assert.equal(vm.runInContext('liveFlatMarker().length',context),0);
assert.equal(vm.runInContext('priceLines.length',context),1);
vm.runInContext('resetHistory()',context);
assert.equal(vm.runInContext('priceLines.length',context),0);
assert.equal(nodes.get('threshold').textContent,'—');
console.log('Study isolation, candle alignment, delayed evaluations, and saved-chart clearing tests passed');

async function testLiveSnapshotFailure(){
 const pending=[];
 context.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
 vm.runInContext("reviewMode=false;currentToken=1;candleData=[{time:1}];document.getElementById('price').textContent='999'",context);
 const failed=vm.runInContext('loadSnapshot(1)',context);
 assert.equal(vm.runInContext('candleData.length',context),0);
 assert.equal(nodes.get('price').textContent,'—');
 pending.shift().resolve({ok:false,text:async()=>'Snapshot unavailable'});
 await assert.rejects(failed,/Snapshot unavailable/);
 vm.runInContext("updateCandle({instrument_token:1,candles_by_timeframe:{'3m':{time:4}}})",context);
 assert.equal(vm.runInContext('candleData.length',context),0);
 assert.ok(nodes.get('strategyContext').textContent.includes('Use Live to retry'));
 const retried=vm.runInContext('loadSnapshot(1)',context);
 pending.shift().resolve({ok:true,json:async()=>({candles:[{time:5,open:100,high:102,low:99,close:101}],overlays:[],studies:[],signals:[],levels:{},threshold:90})});
 await retried;
 assert.equal(vm.runInContext('loadingSnapshot',context),false);
 assert.equal(vm.runInContext('candleData[0].time',context),5);
 console.log('Live snapshot failure, immediate clearing, and retry tests passed');
}
testLiveSnapshotFailure().catch(error=>{console.error(error);process.exitCode=1});

// Exercise the real WebSocket handler, including rule ticks/crosses, while
// chart rendering fails. Saved-day inspection must remain isolated.
function testLiveRuleDelivery(){
 const liveNodes=new Map();
 const liveDocument={getElementById(id){if(!liveNodes.has(id))liveNodes.set(id,node());return liveNodes.get(id)},createElement:node};
 const clients=[],errors=[];
 const live={document:liveDocument,console:{error:(...args)=>errors.push(args)},location:{protocol:'http:',host:'localhost'},
  WebSocket:class{constructor(){clients.push(this)}close(){}},socket:null,heartbeat:null,reconnectTimer:null,
  clearInterval(){},clearTimeout(){},setStatus(){},currentToken:1,currentTimeframe:'3m',reviewMode:false,loadingSnapshot:false,
  matchesActiveStrategy:()=>true,renderMarket(){},renderedEvaluationToken:null,renderedEvaluationTime:0,
  refreshStrategyState:async()=>{},eventTimeframe:event=>event.confirmation_timeframe||'5m',
  updateCandle(){throw new Error('candle failure')},updateStudy(){throw new Error('study failure')}};
 const ctx=vm.createContext(live);
 vm.runInContext(html.slice(html.indexOf('function timeframe('),html.indexOf('function ema(')),ctx);
 vm.runInContext(html.slice(html.indexOf('function renderLive('),html.indexOf('function clearOverlays(')),ctx);
 vm.runInContext(html.slice(html.indexOf('function connect('),html.indexOf('function inspectSaved(')),ctx);
 vm.runInContext('connect(1)',ctx);
 const send=(passed,token=1)=>clients[0].onmessage({data:JSON.stringify({
  type:'strategy_update',instrument_token:token,market_time:'2026-10-13T11:00:00+05:30',
  confirmation_timeframe:'3m',bullish:{passed:passed?1:0,total:1,percentage:passed?100:0,rules:[{text:'3-minute bull rule',passed}]},
  bearish:{passed:passed?0:1,total:1,percentage:passed?0:100,rules:[{text:'3-minute bear rule',passed:!passed}]}
 })});
 send(true);
 assert.equal(liveNodes.get('bullRules').children[0].children[0].textContent,'✓');
 assert.equal(liveNodes.get('bearRules').children[0].children[0].textContent,'×');
 send(false);
 assert.equal(liveNodes.get('bullRules').children[0].children[0].textContent,'×');
 assert.equal(liveNodes.get('bearRules').children[0].children[0].textContent,'✓');
 assert.equal(errors.length,4);
 send(true,2); // another symbol cannot alter the panels
 live.reviewMode=true;send(true);
 live.reviewMode=false;live.loadingSnapshot=true;send(true);
 assert.equal(liveNodes.get('bullRules').children[0].children[0].textContent,'×');
 live.loadingSnapshot=false;
 const nnfEvent={type:'strategy_update',instrument_token:1,market_time:'2026-10-13T11:03:01+05:30',
  evaluation_mode:'closed_candle',as_of:Math.floor(Date.parse('2026-10-13T11:03:00+05:30')/1000),
  nnf:{warning:'Literal Stochastic: K equals D; activation cannot occur.'},confirmation_timeframe:'3m',
  bullish:{passed:0,total:9,percentage:0,rules:[]},bearish:{passed:0,total:9,percentage:0,rules:[]}};
 clients[0].onmessage({data:JSON.stringify(nnfEvent)});
 assert.ok(liveNodes.get('strategyContext').textContent.includes('Completed candle rules as of'));
 assert.ok(liveNodes.get('strategyContext').textContent.includes('K equals D'));
 assert.ok(!liveNodes.get('strategyContext').textContent.includes('may be provisional'));
 console.log('WebSocket rule ticks/crosses update despite chart failures; review and symbol isolation passed');
}
testLiveRuleDelivery();
