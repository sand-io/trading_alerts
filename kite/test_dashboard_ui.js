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
const nodes=new Map(),node=()=>({style:{},classList:{toggle(){}},children:[],value:'',append(){},replaceChildren(...children){this.children=children;this.value=children[0]?.value??''},setAttribute(){}});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,querySelectorAll(){return []}};
const context=vm.createContext({document,KiteStrategyIndicator:{createStrategyIndicator},
 LightweightCharts:{CrosshairMode:{Normal:0},createChart(){chartCount++;const mock={...chart,range:null,removeSeries(){},timeScale(){return {subscribeVisibleLogicalRangeChange(callback){mock.rangeCallback=callback},getVisibleLogicalRange(){return mock.range},setVisibleLogicalRange(range){mock.range=range;mock.rangeCallback?.(range)}}},addCandlestickSeries(){return {setData(){},setMarkers(){},update(){}}},subscribeCrosshairMove(){},applyOptions(){}};chartMocks.push(mock);return mock}},
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
 vm.runInContext("allSymbols=[{token:1,symbol:'NFO:RELIANCE26OCTFUT'},{token:2,symbol:'NFO:TCS26OCTFUT'}];currentToken=null;renderSymbolOptions()",context);
 const select=nodes.get('symbol');
 assert.equal(select.value,'1'); // startup still selects the first configured symbol
 vm.runInContext("currentToken=1;renderSymbolOptions('tcs')",context);
 assert.equal(select.value,''); // filtering must not silently select an unloaded chart
 assert.equal(select.children[0].disabled,true);
 assert.equal(select.children[1].value,'2');
 assert.equal(vm.runInContext('currentToken',context),1);
 vm.runInContext("globalThis.loadedTokens=[];globalThis.connectedTokens=[];loadSnapshot=async token=>loadedTokens.push(token);connect=token=>connectedTokens.push(token)",context);
 select.value='2';
 await vm.runInContext('chooseSymbol()',context);
 assert.equal(vm.runInContext('currentToken',context),2);
 assert.equal(vm.runInContext('loadedTokens.join()',context),'2');
 assert.equal(vm.runInContext('connectedTokens.join()',context),'2');
 vm.runInContext("renderSymbolOptions('')",context);
 assert.equal(select.value,'2'); // clearing the search preserves the newly loaded chart
 vm.runInContext("renderSymbolOptions('missing')",context);
 assert.equal(select.disabled,true);
 assert.equal(select.value,'');
 await vm.runInContext('chooseSymbol()',context);
 assert.equal(vm.runInContext('loadedTokens.length',context),1);
 assert.equal(vm.runInContext('currentToken',context),2);
 vm.runInContext("symbolSearch.value='rel'",context);
 await vm.runInContext('selectFirstSearchResult()',context);
 assert.equal(select.disabled,false);
 assert.equal(vm.runInContext('currentToken',context),1);
 assert.equal(vm.runInContext('loadedTokens.join()',context),'2,1');
 vm.runInContext("renderSymbolOptions('')",context);
 assert.equal(select.value,'1');
 console.log('Dashboard layout, symbol selection and search clearing, studies, and legacy score fidelity tests passed');
}
testSearchSelection().catch(error=>{console.error(error);process.exitCode=1});
