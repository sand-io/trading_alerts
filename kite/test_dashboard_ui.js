// Run: node kite/test_dashboard_ui.js
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {createStrategyIndicator}=require('./strategy_indicator');
const html=fs.readFileSync(`${__dirname}/dashboard.html`,'utf8');
new vm.Script(html.split('<script>')[1].split('</script>')[0]);
const series=[],scales=new Map();
const chart={addLineSeries(options){const line={options,applyOptions(value){Object.assign(this.options,value)},setData(points){this.points=points},createPriceLine(options){return {options,applyOptions(value){Object.assign(this.options,value)}}},removePriceLine(){}};series.push(line);return line},priceScale(id){return {applyOptions(options){scales.set(id,options)}}}};
const indicator=createStrategyIndicator(chart);
const event=(time,bull,bear,source='live')=>({candle:{time,open:100,high:102,low:99,close:101},
 bullish:{percentage:bull,passed:bull/100*14,total:14,triggered:bull>=30},
 bearish:{percentage:bear,passed:bear/100*14,total:14,triggered:bear>=30},evaluation_source:source});
const candles=[{time:1,open:100,high:102,low:99,close:101},{time:2,open:101,high:103,low:99,close:102}];
const original=JSON.stringify(candles);
indicator.load({threshold:30,indicator_history:[event(1,50,25,'reconstructed')],signals:[],strategy:event(2,0,100)},candles,'5m');
assert.equal(series.length,2);
assert.equal(series[0].options.priceScaleId,'left');
assert.equal(series[1].options.priceScaleId,'left');
assert.equal(series[0].options.lineType,1);
assert.ok(!html.includes('const combinedStrategy=')); // no strategy overlay on price chart
assert.equal(series[0].points[0].value,50);
assert.equal(series[1].points[0].value,25);
assert.equal(series[0].points[1].value,0);
assert.equal(series[1].points[1].value,100);
assert.equal(scales.has('right'),false);
assert.equal(scales.get('left').mode,0);
assert.equal(scales.get('left').minimumWidth,60);
assert.deepEqual(series[0].options.autoscaleInfoProvider().priceRange,{minValue:0,maxValue:100});
assert.ok(indicator.readout(1).includes('reconstructed history'));
indicator.update({...event(2,50,50),confirmed_signal:event(2,50,50)},candles,'5m');
indicator.update(event(2,0,0),candles,'5m');
assert.equal(series[0].points[1].value,50); // confirmed data does not repaint
assert.ok(indicator.readout(2).includes('recorded close'));
indicator.setVisible(false);
assert.equal(series[0].options.visible,false);
assert.equal(scales.get('left').visible,false);
indicator.setVisible(true);
assert.equal(series[1].options.visible,true);
const scoreBeforeStyle = JSON.stringify(series.map(item => item.points));
indicator.setStyle({steps:false,bearish:false,threshold:false});
assert.equal(series[0].options.lineType,0);
assert.equal(series[1].options.visible,false);
assert.equal(JSON.stringify(series.map(item => item.points)),scoreBeforeStyle);
assert.equal(series[0].options.priceFormat.formatter(0),'0.0%');
assert.equal(series[0].options.priceFormat.formatter(100),'100.0%');
assert.ok(!html.includes('priceFormatter:')); // global formatter must not override percentage scale
indicator.setStyle({steps:true,bearish:true,threshold:true});
indicator.update(event(2,50,50),candles,'1h');
assert.equal(series[0].points.length,0); // no invented timeframe resampling
assert.equal(scales.get('left').visible,false);
assert.equal(JSON.stringify(candles),original);
indicator.load({threshold:100,indicator_history:[],signals:[],strategy:null},candles,'5m');
assert.equal(series[0].points[0].value,undefined);
assert.ok(indicator.readout().includes('No strategy evaluation'));
let chartCount=0;
const chartMocks=[];
const nodes=new Map(),node=()=>({style:{},classList:{toggle(){}},children:[],value:'',append(...items){this.children.push(...items)},replaceChildren(...children){this.children=children;this.value=children[0]?.value??''},setAttribute(key,value){this[key]=value},removeAttribute(key){delete this[key]}});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,addEventListener(){},querySelectorAll(){return []}};
const context=vm.createContext({document,KiteStrategyIndicator:{createStrategyIndicator},
 LightweightCharts:{CrosshairMode:{Normal:0},createChart(){chartCount++;const mock={...chart,range:null,removeSeries(){},timeScale(){return {subscribeVisibleLogicalRangeChange(callback){mock.rangeCallback=callback},getVisibleLogicalRange(){return mock.range},setVisibleLogicalRange(range){mock.range=range;mock.rangeCallback?.(range)}}},addCandlestickSeries(){return {setData(){},setMarkers(){},update(){}}},subscribeClick(callback){mock.clickCallback=callback},subscribeCrosshairMove(){},applyOptions(){}};chartMocks.push(mock);return mock}},
 ResizeObserver:class{observe(){}},setInterval(){},clearInterval(){},clearTimeout(){},
 fetch(){return new Promise(()=>{})},window:{},console});
vm.runInContext(html.split('<script>')[1].split('</script>')[0],context);
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

vm.runInContext("reviewMode=true;reviewEvents=[{candle:{time:1},market_time:'2026-10-08T10:00:00+05:30',instrument_token:1,price:100,vwap:99,threshold:70,bullish:{passed:1,total:1,percentage:100,rules:[]},bearish:{passed:0,total:1,percentage:0,rules:[]}}];currentTimeframe='5m'",context);
chartMocks[0].clickCallback({time:1});
assert.ok(nodes.get('strategyContext').textContent.includes('Saved live evaluation'));

// No strategy HTTP polling: sync only on a change, with duplicate events combined.
assert.ok(!/setInterval\([^\n]*api\/strategies/.test(html));
assert.ok(html.includes("data.type==='strategy_changed'"));
async function testStrategySync(){
 let calls=0,release;
 const syncContext=vm.createContext({Promise,activeStrategyId:'default',activeStrategyVersion:'v1',currentToken:null,
   loadStrategies:async()=>{calls++;await new Promise(resolve=>{release=resolve});syncContext.activeStrategyId='trend';syncContext.activeStrategyVersion='v2'},
   loadSnapshot:async()=>{throw new Error('No symbol selected')}});
 vm.runInContext(html.slice(html.indexOf('let strategyRefresh='),html.indexOf("document.getElementById('activateStrategy').onclick")),syncContext);
 await vm.runInContext("refreshStrategyState({id:'default',version:'v1'})",syncContext);
 assert.equal(calls,0);
 const pending=vm.runInContext("refreshStrategyState({id:'trend',version:'v2'})",syncContext);
 for(let i=0;i<20;i++)vm.runInContext("refreshStrategyState({id:'trend',version:'v2'})",syncContext);
 assert.equal(calls,1);release();await pending;assert.equal(calls,1);
 await vm.runInContext("refreshStrategyState({id:'trend',version:'v2'})",syncContext);assert.equal(calls,1);
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
  candles:{setData(){}},refreshMarkers(){},configureOverlays(){},configureStudies(){},renderOhlc(){},applyLevels(){},resetVisibleRange(){},
  markers:()=>[],renderSide(){},renderLive(){},renderMarket(){},inspectSaved(event){sandbox.inspected=event},
  fetch:url=>new Promise(resolve=>pending.push({url,resolve}))};
 const session=vm.createContext(sandbox);
 vm.runInContext(html.slice(html.indexOf('function resetHistory('),html.indexOf('chart.subscribeClick')),session);
 vm.runInContext(html.slice(html.indexOf('let snapshotRequest='),html.indexOf('function updateCandle(')),session);
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
