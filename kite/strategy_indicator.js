/* Combined strategy visualization. Scores are scanner outputs, not prices. */
(function (root) {
  function createStrategyIndicator(chart) {
    const options = color => ({priceScaleId: 'left', color, lineWidth: 2, lineType: 1,
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerRadius: 3,
      priceFormat: {type: 'custom', minMove: .01, formatter: value => value.toFixed(1) + '%'},
      autoscaleInfoProvider: () => ({priceRange: {minValue: 0, maxValue: 100}})});
    const bull = chart.addLineSeries(options('#24c59a'));
    const bear = chart.addLineSeries(options('#f15b70'));
    const states = new Map();
    let visible = true, timeframe = '5m', timeline = [], threshold = null;
    const style = {steps: true, bullish: true, bearish: true, threshold: true};
    function accept(event, confirmed = false) {
      if (!event?.candle || !Number.isFinite(event.bullish?.percentage) ||
          !Number.isFinite(event.bearish?.percentage)) return;
      const key = event.candle.time, previous = states.get(key);
      // Recorded confirmed evaluations never repaint from previews or replay.
      if (previous?.confirmed && previous.source === 'live') return;
      const source = event.evaluation_source || 'live';
      states.set(key, {event, confirmed, source});
    }
    function draw() {
      const active = visible && timeframe === '5m';
      bull.applyOptions({visible: active && style.bullish, lineType: style.steps ? 1 : 0});
      bear.applyOptions({visible: active && style.bearish, lineType: style.steps ? 1 : 0});
      chart.priceScale('left').applyOptions({visible: active, autoScale: true,
        mode: 0, invertScale: false, scaleMargins: {top: 0, bottom: 0},
        borderVisible: true, borderColor: '#223246', textColor: '#708196',
        ticksVisible: true, minimumWidth: 60});
      for (const [series, side] of [[bull, 'bullish'], [bear, 'bearish']]) {
        series.setData(timeframe === '5m' ? timeline.map(candle => {
          const state = states.get(candle.time);
          if (!state) return {time: candle.time};
          const color = side === 'bullish' ? '#24c59a' : '#f15b70';
          const suffix = state.source === 'live' ? (state.confirmed ? '' : 'b0') : '70';
          return {time: candle.time, value: state.event[side].percentage,
            color: color + suffix};
        }) : []);
      }
      threshold?.applyOptions({lineVisible: active && style.threshold && style.bullish});
    }
    return {
      load(data, candles, tf) {
        states.clear(); timeline = candles; timeframe = tf;
        for (const event of data.indicator_history || []) accept(event, true);
        for (const event of data.signals || []) accept(event, true);
        accept(data.strategy);
        if (threshold) bull.removePriceLine(threshold);
        threshold = bull.createPriceLine({price: data.threshold, color: '#f2b84b',
          lineWidth: 1, lineStyle: 2, axisLabelVisible: false,
          title: `Alert threshold ${data.threshold}%`});
        draw();
      },
      update(event, candles, tf) {
        timeline = candles; timeframe = tf;
        accept(event.confirmed_signal, true); accept(event); draw();
      },
      setVisible(value) {visible = value; draw();},
      setStyle(value) {
        for (const key of Object.keys(style)) if (typeof value[key] === 'boolean') style[key] = value[key];
        draw();
      },
      readout(time) {
        const state = states.get(time ?? timeline.at(-1)?.time);
        if (timeframe !== '5m') return 'Combined strategy is evaluated on 5m candles';
        if (!state) return 'No strategy evaluation available for this candle';
        const {bullish: b, bearish: s} = state.event;
        const source = state.source === 'live' ? (state.confirmed ? 'recorded close' : 'live preview') : 'reconstructed history';
        return `Bull ${b.passed}/${b.total} (${b.percentage.toFixed(1)}%) · Bear ${s.passed}/${s.total} (${s.percentage.toFixed(1)}%) · ${source}`;
      }
    };
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {createStrategyIndicator};
  else root.KiteStrategyIndicator = {createStrategyIndicator};
})(typeof globalThis !== 'undefined' ? globalThis : this);
