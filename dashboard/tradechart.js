/* Trade chart window: one TradingView-style chart per trade.
 *
 * Candles (TradingView colours), volume, the opening range, the position zones like
 * TradingView's "Long Position" tool (green: entry -> target, red: stop -> entry, the red
 * zone follows every stop move), the stop as a staircase line, entry / partial / exit
 * markers, and an OHLC legend under the crosshair.
 * Built on TradingView Lightweight Charts (Apache 2.0, vendor/lightweight-charts.js).
 */
(function () {
  "use strict";
  const LWC = window.LightweightCharts;
  const UP = "#26a69a", DOWN = "#ef5350", BLUE = "#2962ff", GREEN = "#089981", RED = "#f23645";

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const f2 = (v) => v == null || isNaN(v) ? "–" : (+v).toFixed(2);

  /* "10:15" on "2026-10-19" -> New York wall-clock seconds, snapped to the candle it falls in */
  function tsOf(date, hhmm, step) {
    if (!date || !hhmm) return null;
    const [y, m, d] = date.split("-").map(Number), [h, mi] = hhmm.split(":").map(Number);
    const t = Date.UTC(y, m - 1, d, h, mi) / 1000;
    return t - (t % step);
  }

  function slData0(span, closed, step) {
    const ts = span.map((b) => b.time);
    if (!closed && ts.length) ts.push(ts[ts.length - 1] + step);
    return ts;
  }

  function tradeId(t) { return [t.date, t.symbol, t.at].join("|"); }

  const EXIT_TXT = { target: "Ziel", stop: "Stop", trail: "Trailing-Stop", flat: "Flat 15:55" };

  class TradeChart {
    constructor(host, trade, opts = {}) {
      this.trade = trade;
      this.interval = opts.interval || "5m";
      this.compact = !!opts.compact;
      host.innerHTML = `
        <div class="tc-head">
          <div class="tc-title"><b>${esc(trade.symbol)}</b> <span class="tc-badge"></span> <span class="tc-sub"></span></div>
          <div class="tc-tools">
            <button class="tc-iv" data-iv="1m">1m</button><button class="tc-iv" data-iv="5m">5m</button>
            <button class="tc-fit" title="Ganzen Tag zeigen">⤢</button>
          </div>
        </div>
        <div class="tc-wrap"><div class="tc-legend"></div><div class="tc-chart"></div><div class="tc-msg">Lade Kerzen …</div></div>
        <div class="tc-foot"></div>`;
      this.host = host;
      this.el = host.querySelector(".tc-chart");
      this.legend = host.querySelector(".tc-legend");
      this.msg = host.querySelector(".tc-msg");
      host.querySelectorAll(".tc-iv").forEach((b) => b.onclick = () => { this.interval = b.dataset.iv; this.load(true); });
      host.querySelector(".tc-fit").onclick = () => this.chart && this.chart.timeScale().fitContent();
      this.build();
      this.load(true);
    }

    build() {
      const surface = css("--surface") || "#131722", grid = css("--grid") || "#2a2e39", muted = css("--muted") || "#787b86",
            ink = css("--ink-2") || "#d1d4dc", border = css("--border") || "#2a2e39";
      this.chart = LWC.createChart(this.el, {
        autoSize: true,
        layout: { background: { type: "solid", color: surface }, textColor: ink, fontSize: 11,
                  fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif", attributionLogo: true },
        grid: { vertLines: { color: grid }, horzLines: { color: grid } },
        rightPriceScale: { borderColor: border },
        timeScale: { borderColor: border, timeVisible: true, secondsVisible: false, rightOffset: 4 },
        crosshair: { mode: 0, vertLine: { color: muted, labelBackgroundColor: "#363a45" },
                     horzLine: { color: muted, labelBackgroundColor: "#363a45" } },
        localization: { locale: "de-DE", priceFormatter: (p) => p.toFixed(2) },
      });
      const c = this.chart;
      // zones first, so the candles are drawn on top of them
      this.tpZone = c.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 1, topLineColor: GREEN,
        topFillColor1: "rgba(8,153,129,.22)", topFillColor2: "rgba(8,153,129,.22)", bottomLineColor: GREEN,
        bottomFillColor1: "rgba(8,153,129,.22)", bottomFillColor2: "rgba(8,153,129,.22)",
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: "" });
      this.slZone = c.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 2, lineType: 1,
        topLineColor: RED, topFillColor1: "rgba(242,54,69,.20)", topFillColor2: "rgba(242,54,69,.20)",
        bottomLineColor: RED, bottomFillColor1: "rgba(242,54,69,.20)", bottomFillColor2: "rgba(242,54,69,.20)",
        priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, title: "Stop" });
      this.orHigh = c.addLineSeries({ color: muted, lineWidth: 1, lineStyle: 2, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false });
      this.orLow = c.addLineSeries({ color: muted, lineWidth: 1, lineStyle: 2, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false });
      this.candles = c.addCandlestickSeries({ upColor: UP, downColor: DOWN, borderVisible: false,
        wickUpColor: UP, wickDownColor: DOWN });
      this.volume = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol",
        lastValueVisible: false, priceLineVisible: false });
      c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      this.candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
      c.subscribeCrosshairMove((p) => this.showLegend(p));
    }

    async load(refit) {
      const t = this.trade;
      this.host.querySelectorAll(".tc-iv").forEach((b) => b.classList.toggle("on", b.dataset.iv === this.interval));
      try {
        const r = await fetch(`api/chart?symbol=${encodeURIComponent(t.symbol)}&date=${encodeURIComponent(t.date)}&interval=${this.interval}`, { cache: "no-store" });
        const js = await r.json();
        if (!r.ok) throw new Error(js.error || r.status);
        this.bars = js.bars || [];
      } catch (e) {
        this.msg.style.display = "flex";
        this.msg.textContent = "Keine Kerzen: " + e.message;
        return;
      }
      this.msg.style.display = this.bars.length ? "none" : "flex";
      if (!this.bars.length) this.msg.textContent = "Noch keine Kerzen für diesen Tag.";
      this.candles.setData(this.bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      this.volume.setData(this.bars.map((b) => ({ time: b.time, value: b.volume,
        color: b.close >= b.open ? "rgba(38,166,154,.45)" : "rgba(239,83,80,.45)" })));
      this.draw();
      if (refit) this.chart.timeScale().fitContent();
    }

    update(trade) {
      this.trade = trade;
      this.draw();
    }

    draw() {
      const t = this.trade, bars = this.bars || [];
      if (!bars.length) return;
      const step = this.interval === "1m" ? 60 : 300;
      const p = t.plan || {}, fill = t.fill_price || p.entry, risk = p.risk_per_share || 0;
      const trailing = (t.exit_mode || "").startsWith("trail");
      const tIn = tsOf(t.date, t.at, step);
      const closed = !["open", "pending"].includes(t.status);
      const tOut = closed ? tsOf(t.date, t.closed_at, step) : bars[bars.length - 1].time;
      const span = bars.filter((b) => b.time >= tIn && b.time <= (tOut ?? tIn));
      if (span.length && span[span.length - 1].time < bars[bars.length - 1].time && !closed) span.push(bars[bars.length - 1]);

      // the stop in effect at each candle, from the bot's stop log
      const log = (t.stop_log && t.stop_log.length ? t.stop_log : [[t.at, p.stop]])
        .map(([hhmm, v]) => [tsOf(t.date, hhmm, step), v]).sort((a, b) => a[0] - b[0]);
      const stopAt = (time) => { let v = log[0][1]; for (const [ts, s] of log) if (ts <= time) v = s; return v; };

      const target = trailing ? null : p.target;
      this.tpZone.applyOptions({ baseValue: { type: "price", price: fill }, visible: target != null });
      this.tpZone.setData(target != null ? slData0(span, closed, step).map((time) => ({ time, value: target })) : []);
      this.slZone.applyOptions({ baseValue: { type: "price", price: fill } });
      const slData = span.map((b) => ({ time: b.time, value: stopAt(b.time) }));
      if (!closed && slData.length) {
        // an open trade: carry the current stop one candle to the right, so its last step is visible
        const lastT = slData[slData.length - 1].time;
        slData.push({ time: lastT + step, value: stopAt(lastT) });
      }
      this.slZone.setData(slData);

      // the entry price line, and the target as a labelled price line
      (this.lines || []).forEach((l) => this.candles.removePriceLine(l));
      this.lines = [this.candles.createPriceLine({ price: fill, color: BLUE, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Einstieg" })];
      if (target != null) this.lines.push(this.candles.createPriceLine({ price: target, color: GREEN, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Ziel" }));

      // opening range, from 9:30 to the entry
      const or = t.or || {};
      const orBars = bars.filter((b) => b.time <= (tIn ?? bars[0].time));
      this.orHigh.setData(or.high ? orBars.map((b) => ({ time: b.time, value: or.high })) : []);
      this.orLow.setData(or.low ? orBars.map((b) => ({ time: b.time, value: or.low })) : []);

      // markers: buy, partial sells, the exit
      const m = [];
      const short = t.side === "sell", sgn = short ? -1 : 1;
      const IN = short ? { position: "aboveBar", shape: "arrowDown", text: `Short ${t.qty} @ ${f2(fill)}` }
                       : { position: "belowBar", shape: "arrowUp", text: `Kauf ${t.qty} @ ${f2(fill)}` };
      const OUT = short ? { position: "belowBar", shape: "arrowUp" } : { position: "aboveBar", shape: "arrowDown" };
      if (tIn) m.push({ time: tIn, color: BLUE, ...IN });
      (t.partials || []).forEach((x) => x.at && m.push({ time: tsOf(t.date, x.at, step), color: GREEN, ...OUT,
        text: `${short ? "Teil-Rückkauf" : "Teilverkauf"} ${x.qty} @ ${f2(x.price)}` }));
      log.slice(1).forEach(([ts, v]) => m.push({ time: ts, position: short ? "aboveBar" : "belowBar", color: RED, shape: "circle", text: `Stop ${f2(v)}` }));
      if (closed && tOut && t.exit_price != null) m.push({ time: tOut, color: (t.pnl || 0) >= 0 ? GREEN : RED, ...OUT,
        text: `${EXIT_TXT[t.status] || t.status} @ ${f2(t.exit_price)}` });
      const times = new Set(bars.map((b) => b.time));
      this.candles.setMarkers(m.filter((x) => times.has(x.time)).sort((a, b) => a.time - b.time));

      // header and footer
      const last = bars[bars.length - 1].close;
      const pnl = closed ? t.pnl : sgn * ((t.partials || []).reduce((a, x) => a + (x.price - fill) * x.qty, 0) + (last - fill) * (t.qty_open || t.qty));
      const rNow = risk ? (pnl / (risk * t.qty)) : null;
      const badge = this.host.querySelector(".tc-badge");
      badge.textContent = closed ? (EXIT_TXT[t.status] || t.status) : "offen";
      badge.className = "tc-badge " + (closed ? ((t.pnl || 0) >= 0 ? "win" : "loss") : "open");
      this.host.querySelector(".tc-sub").textContent = `${short ? "SHORT · " : "LONG · "}${t.date} · ${t.qty} Stück · Einstieg ${f2(fill)} um ${t.at}`;
      const stopNow = stopAt(tOut || bars[bars.length - 1].time);
      this.host.querySelector(".tc-foot").innerHTML =
        `<span>Stop <b>${f2(stopNow)}</b>${log.length > 1 ? ` (${log.length - 1}× nachgezogen)` : ""}</span>` +
        `<span>${trailing ? "Trailing, kein festes Ziel" : "Ziel <b>" + f2(p.target) + "</b>"}</span>` +
        `<span>Risiko/Aktie ${f2(risk)}</span>` +
        `<span class="${pnl >= 0 ? "up" : "down"}">P&amp;L <b>${pnl >= 0 ? "+" : "−"}$${Math.abs(pnl || 0).toFixed(2)}</b>${rNow != null ? ` (${rNow >= 0 ? "+" : ""}${rNow.toFixed(2)} R)` : ""}</span>`;
      this.showLegend(null);
    }

    showLegend(param) {
      const bars = this.bars || [];
      let b = param && param.time != null ? param.seriesData.get(this.candles) : null;
      if (!b && bars.length) b = bars[bars.length - 1];
      if (!b) { this.legend.textContent = ""; return; }
      const ch = b.close - b.open, col = ch >= 0 ? UP : DOWN;
      this.legend.innerHTML = `<span>${esc(this.trade.symbol)} · ${this.interval}</span>` +
        ` O <b style="color:${col}">${f2(b.open)}</b> H <b style="color:${col}">${f2(b.high)}</b>` +
        ` L <b style="color:${col}">${f2(b.low)}</b> C <b style="color:${col}">${f2(b.close)}</b>` +
        ` <b style="color:${col}">${ch >= 0 ? "+" : ""}${f2(ch)} (${b.open ? (100 * ch / b.open).toFixed(2) : "0"} %)</b>`;
    }

    destroy() { try { this.chart.remove(); } catch (e) {} }
  }

  window.TradeChart = TradeChart;
  window.tradeId = tradeId;
})();
