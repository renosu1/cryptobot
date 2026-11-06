/* Trend Rider dashboard */
(() => {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const LW = window.LightweightCharts;

  let S = null;              // latest /api/state
  let coin = null;           // coin shown on the dashboard chart
  let tf = '1d';
  let candleCache = {};      // key -> {t, rows}
  let lastTradeSig = '';
  let charts = {};
  let market = 'crypto';
  try { market = localStorage.getItem('tr-market') === 'stocks' ? 'stocks' : 'crypto'; } catch (e) { /* ignore */ }
  const STOCKS = () => market === 'stocks';

  const CLOUD = !!window.TREND_RIDER_CLOUD;
  const CB = 'https://api.exchange.coinbase.com';
  const DAY = 86400;

  // ------------------------------------------------------------ helpers
  async function api(path, body) {
    if (body === undefined) path += (path.includes('?') ? '&' : '?') + 'market=' + market;
    else body = Object.assign({ market }, body);
    if (CLOUD) return cloudApi(path, body);
    const opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-TrendRider': '1' }, body: JSON.stringify(body)
    };
    const r = await fetch(path, opt);
    let j = {};
    try { j = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) throw new Error(j.error || `Request failed (${r.status})`);
    return j;
  }
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const usd = (v, d) => {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const a = Math.abs(v);
    const dec = d !== undefined ? d : a >= 1000 ? 2 : a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
    return (v < 0 ? '−$' : '$') + a.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
  };
  const pct = (v, d = 1, sign = true) => {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const s = Math.abs(v * 100).toFixed(d);
    if (+s === 0) return (0).toFixed(d) + '%';
    return (sign && v > 0 ? '+' : v < 0 ? '−' : '') + s + '%';
  };
  const qty = (v) => v === 0 ? '0' : v >= 100 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v.toPrecision(5).replace(/\.?0+$/, '');
  const cls = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
  const tz = () => -new Date().getTimezoneOffset() * 60;
  const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const timeOnly = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function dur(sec) {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  // ------------------------------------------------------------ cloud (GitHub Pages) mode
  // The bot runs on GitHub's servers a few times a day and saves data/*.json.
  // This page reads those files and adds live prices straight from Coinbase.
  let dash = null, dashAt = 0, liveStatsAt = 0;
  const liveStats = {}, livePrices = {}, cloudErrors = {};
  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url.split('?')[0]} answered ${r.status}`);
    return r.json();
  }
  function repoName() {
    if (dash && dash.cloud && dash.cloud.repo) return dash.cloud.repo;
    const m = location.hostname.match(/^([^.]+)\.github\.io$/);
    const p = location.pathname.split('/').filter(Boolean)[0];
    return m && p ? `${m[1]}/${p}` : '';
  }
  async function cloudApi(path, body) {
    if (body !== undefined) throw new Error('This is the cloud version. Change settings in data/config.json on GitHub (see the Settings tab).');
    const u = new URL(path, location.origin);
    const q = Object.fromEntries(u.searchParams);
    if (u.pathname === '/api/state') return cloudState();
    const sfx = STOCKS() ? '_stocks' : '';
    if (u.pathname === '/api/news') return getJSON(`data/news${sfx}.json?t=` + Date.now());
    if (u.pathname === '/api/research') return getJSON('app/research.json');
    if (u.pathname === '/api/backtest') {
      const y = [1, 3, 5, 12].reduce((a, b) => Math.abs(b - +q.years) < Math.abs(a - +q.years) ? b : a, 5);
      return getJSON(`data/backtest/${STOCKS() ? 'stocks_' : ''}${q.coin}_${y}.json?t=${Math.floor(Date.now() / 3.6e6)}`);
    }
    if (u.pathname === '/api/candles') {
      if (STOCKS()) { const c = await getJSON(`data/candles/${q.coin}.json?t=${Math.floor(Date.now() / 6e5)}`); return { coin: q.coin, rows: q.tf === '1h' ? c.daily.slice(-60) : c.daily }; }
      const g = q.tf === '1h' ? 3600 : 86400;
      const rows = await getJSON(`${CB}/products/${q.coin}-USD/candles?granularity=${g}`);
      return { coin: q.coin, rows: rows.map((r) => [r[0], r[3], r[2], r[1], r[4], r[5]]).sort((a, b) => a[0] - b[0]) };
    }
    throw new Error('Not available in the cloud version.');
  }
  let dashMarket = null;
  async function cloudState() {
    if (!dash || Date.now() - dashAt > 120000 || dashMarket !== market) {
      dash = await getJSON(`data/dashboard${STOCKS() ? '_stocks' : ''}.json?t=` + Date.now());
      dashAt = Date.now(); dashMarket = market;
    }
    const s = JSON.parse(JSON.stringify(dash));
    const coins = STOCKS() ? [] : s.config.coins;   // stock prices can't be fetched from a browser; use the bot's last run
    await Promise.all(coins.map(async (c) => {
      try { const t = await getJSON(`${CB}/products/${c}-USD/ticker`); livePrices[c] = { price: +t.price, ts: Date.now() / 1000, src: 'Coinbase' }; delete cloudErrors.price; }
      catch (e) { cloudErrors.price = 'Live prices unavailable in this browser right now (' + e.message + ').'; }
    }));
    if (!STOCKS() && Date.now() - liveStatsAt > 60000) {
      liveStatsAt = Date.now();
      await Promise.all(Object.keys(s.coins).map(async (c) => {
        try { const t = await getJSON(`${CB}/products/${c}-USD/stats`); liveStats[c] = { open: +t.open, high: +t.high, low: +t.low, last: +t.last }; } catch (e) { /* keep saved stats */ }
      }));
    }
    if (!STOCKS()) { Object.assign(s.prices, livePrices); Object.assign(s.stats, liveStats); }
    const px = (c) => (s.prices[c] || {}).price;
    const raw = s.raw || { holdings: {}, hold_qty: {}, hold_cash: 0 };
    s.equity = s.cash + Object.entries(raw.holdings).reduce((a, [c, q]) => a + q * (px(c) || 0), 0);
    s.hold_equity = Object.keys(raw.hold_qty).length ? (raw.hold_cash || 0) + Object.entries(raw.hold_qty).reduce((a, [c, q]) => a + q * (px(c) || 0), 0) : null;
    s.positions.forEach((p) => { p.price = px(p.coin); p.value = p.qty * (p.price || 0); });
    if (s.live_on) s.live.equity = s.live.cash + Object.entries(s.live.holdings || {}).reduce((a, [c, q]) => a + q * (px(c) || 0), 0);
    s.now = Date.now() / 1000;
    if (!STOCKS()) s.next_decision = (Math.floor(s.now / DAY) + 1) * DAY + 17 * 60;
    else if (s.next_decision < s.now) s.next_decision = s.now + 3600;
    s.errors = Object.assign({}, s.errors, cloudErrors);
    const age = s.now - s.cloud.run_ts;
    if (age > (STOCKS() ? 30 : 8) * 3600) s.errors.stale = `The cloud bot last ran ${dur(age)} ago (it normally runs every 6 hours). Check the Actions tab on GitHub.`;
    return s;
  }
  function renderCloudChrome() {
    const repo = repoName();
    $('#btnCheck').hidden = true; $('#btnPause').hidden = true;
    const acts = $('.top-actions');
    if (!acts.dataset.cloud && repo) {
      acts.dataset.cloud = '1';
      acts.insertAdjacentHTML('beforeend', `<a class="btn btn-ghost" href="https://github.com/${repo}/actions/workflows/bot.yml" target="_blank" rel="noopener">Run now on GitHub</a>`);
    }
    const ageTxt = S.cloud ? `last run ${dur(S.now - S.cloud.run_ts)} ago` : '';
    if (!Object.keys(S.errors || {}).length && !S.paused) $('#statusText').textContent = `In the cloud · ${ageTxt} · next decision in ${dur(S.next_decision - S.now)}`;
    $('#modePill').textContent = S.live_on ? 'Live + paper · cloud' : 'Paper money · cloud';
    $('#footWhere').textContent = 'runs on GitHub, even when your Mac is off';
  }
  function renderCloudSettings() {
    const view = $('#view-settings');
    const repo = repoName();
    const key = JSON.stringify(S.config) + repo;
    if (view.dataset.k === key) return;
    view.dataset.k = key;
    const c = S.config;
    const live = c.live || {};
    const gh = (p) => `https://github.com/${repo}/${p}`;
    view.innerHTML = `<div class="grid-2">
      <div class="card"><h2>Settings (cloud version)</h2>
        <p class="muted">This copy of Trend Rider runs on GitHub's servers four times a day, even when your Mac is off. Its settings live in one file in your repository.</p>
        <div class="kv"><span>Strategy</span><b>${esc(stratLabel())}</b></div>
        <div class="kv"><span>Coins</span><b>${esc(c.coins.join(', '))}</b></div>
        <div class="kv"><span>Fee per trade</span><b>${c.fee_pct}%</b></div>
        <div class="kv"><span>Always invested at least</span><b>${c.min_invested_pct}%</b></div>
        <div class="kv"><span>Starting cash</span><b>${usd(c.starting_cash, 0)}</b></div>
        <div class="stack"><div><a class="btn btn-primary" href="${gh('edit/main/data/config.json')}" target="_blank" rel="noopener">Edit settings on GitHub</a> <a class="btn btn-ghost" href="${gh('actions/workflows/bot.yml')}" target="_blank" rel="noopener">Run the bot now</a></div>
        <p class="small muted">${STOCKS() ? 'The stocks account uses the <code>"stocks"</code> section of the file. ' : ''}In the file: <code>"strategy"</code> can be <code>"growth"</code>, <code>"steady"</code>, <code>"auto"</code>, <code>"hold"</code> or <code>"mix"</code> (then set <code>"mix"</code>, for example <code>{"a": "auto", "b": "steady", "a_pct": 50}</code>). <code>"coins"</code> takes ${STOCKS() ? 'any US stock or fund ticker, such as SPY, QQQ or AAPL' : 'any of ' + esc(Object.keys(S.coins).join(', '))}. Click "Commit changes" to save; the bot uses the new settings on its next run, or press "Run the bot now" and then "Run workflow".</p></div>
      </div>
      ${STOCKS() ? `<div class="card"><h2>Real money</h2><p class="muted">The stocks account is paper-only for now. In testing on 24 stock histories, trend rules cut the worst crashes but made less money than simply holding in every one.</p></div>` : `<div class="card"><h2>Real money with Kraken <span class="pill ${S.live_on ? 'pill-live' : 'pill-beta'}">${S.live_on ? 'On' : 'Off'}</span></h2>
        <div class="warn-box"><p><b>Real money can be lost.</b> In testing on 2023–2026 prices the bot made less than holding while losing less in crashes. Live trading has not been tested against a real Kraken account. Because this repository is public, the bot's real trades and balance would be visible to anyone. Paper-trade for at least a month first. You must be 18 or older.</p></div>
        <ol class="steps">
          <li>On kraken.com create an API key with only <b>Query funds</b>, <b>Query open orders &amp; trades</b>, <b>Query closed orders &amp; trades</b> and <b>Create &amp; modify orders</b>. Never allow withdrawals.</li>
          <li>In your repository: <a href="${gh('settings/secrets/actions')}" target="_blank" rel="noopener">Settings › Secrets › Actions</a>, add <code>KRAKEN_API_KEY</code> and <code>KRAKEN_API_SECRET</code>.</li>
          <li>In <a href="${gh('edit/main/data/config.json')}" target="_blank" rel="noopener">config.json</a> set <code>"live": {"enabled": true, "confirm": "REAL MONEY", "budget_usd": 100, "max_trade_usd": 100}</code>.</li>
        </ol>
        <p class="small muted">Currently: ${S.live_on ? `trading a ${usd(live.budget_usd, 0)} budget.` : S.has_keys ? 'keys found, live trading off.' : 'no Kraken keys saved.'} To stop, set <code>"enabled": false</code>; nothing is sold.</p>
      </div>`}</div>`;
  }

  // ------------------------------------------------------------ routing
  function route() {
    const v = (location.hash || '#dashboard').slice(1);
    const view = ['dashboard', 'backtest', 'research', 'settings'].includes(v) ? v : 'dashboard';
    $$('.view').forEach((el) => { el.hidden = el.id !== 'view-' + view; });
    $$('.tab').forEach((t) => t.classList.toggle('on', t.dataset.view === view));
    if (view === 'research') loadResearch();
    if (view === 'dashboard') Object.values(charts).forEach((c) => c && c.timeScale && c.applyOptions({}));
  }
  window.addEventListener('hashchange', route);

  // ------------------------------------------------------------ charts
  function chartTheme() {
    return {
      layout: { background: { type: 'solid', color: css('--surface') }, textColor: css('--muted'), fontFamily: css('--font') },
      grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
      rightPriceScale: { borderColor: css('--line') },
      timeScale: { borderColor: css('--line'), timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    };
  }
  function makeChart(el, extra = {}) {
    const c = LW.createChart(el, Object.assign({ autoSize: true }, chartTheme(), extra));
    return c;
  }
  function initCharts() {
    charts.price = makeChart($('#priceChart'), { localization: { priceFormatter: (p) => usd(p) }, rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.08 }, borderColor: css('--line') } });
    charts.candles = charts.price.addCandlestickSeries({
      upColor: css('--up'), downColor: css('--down'), borderVisible: false,
      wickUpColor: css('--up'), wickDownColor: css('--down'),
    });
    charts.equity = makeChart($('#equityChart'), { localization: { priceFormatter: (p) => usd(p, 0) } });
    charts.eqBot = charts.equity.addLineSeries({ color: css('--bot'), lineWidth: 2, title: 'Bot' });
    charts.eqHold = charts.equity.addLineSeries({ color: css('--hold'), lineWidth: 2, title: 'Hold' });
    charts.eqLive = charts.equity.addLineSeries({ color: css('--live'), lineWidth: 2, title: 'Live', visible: false });
  }
  function rethemeCharts() {
    ['price', 'equity', 'bt'].forEach((k) => charts[k] && charts[k].applyOptions(chartTheme()));
    charts.candles && charts.candles.applyOptions({ upColor: css('--up'), downColor: css('--down'), wickUpColor: css('--up'), wickDownColor: css('--down') });
    charts.eqBot && charts.eqBot.applyOptions({ color: css('--bot') });
    charts.eqHold && charts.eqHold.applyOptions({ color: css('--hold') });
    charts.eqLive && charts.eqLive.applyOptions({ color: css('--live') });
    if (charts.btSeries) charts.btSeries.forEach((s, i) => s.applyOptions({ color: css(BT_COLORS[charts.btKeys[i]]) }));
  }
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', rethemeCharts);

  async function loadCandles(force) {
    if (!coin) return;
    const key = coin + tf;
    const c = candleCache[key];
    if (!force && c && Date.now() - c.t < 60000) return drawCandles(c.rows);
    try {
      const m = market;
      const j = await api(`/api/candles?coin=${coin}&tf=${tf}`);
      if (m !== market || key !== coin + tf) return;
      candleCache[key] = { t: Date.now(), rows: j.rows };
      drawCandles(j.rows);
      $('#chartNote').textContent = STOCKS()
        ? (tf === '1d' ? 'Daily prices from Yahoo Finance, adjusted for dividends. The bot decides once a day, just after the US market closes.' : 'Hourly prices from Yahoo Finance, in your time zone.')
        : (tf === '1d' ? 'Daily candles from Coinbase (UTC days). The bot decides once a day, right after 00:00 UTC.' : 'Hourly candles from Coinbase, in your time zone.');
    } catch (e) {
      $('#chartNote').textContent = 'Could not load candles: ' + e.message;
    }
  }
  function barTime(ts) { return tf === '1d' ? Math.floor(ts / 86400) * 86400 : Math.floor(ts / 3600) * 3600 + tz(); }
  function drawCandles(rows) {
    // rows: [t, o, h, l, c, v]; update the last bar with the live price
    const data = rows.map((r) => ({ time: tf === '1d' ? r[0] : r[0] + tz(), open: r[1], high: r[2], low: r[3], close: r[4] }));
    const live = S && S.prices[coin];
    if (live && data.length) {
      const last = data[data.length - 1];
      last.close = live.price; last.high = Math.max(last.high, live.price); last.low = Math.min(last.low, live.price);
    }
    charts.candles.setData(data);
    drawMarkers(data);
  }
  function drawMarkers(data) {
    if (!S || !data || !data.length) return;
    const first = data[0].time;
    const trades = [...S.trades, ...(S.live.trades || [])].filter((t) => t.coin === coin).sort((a, b) => a.ts - b.ts);
    const m = [];
    trades.forEach((t) => {
      const bt = barTime(t.ts);
      if (bt < first) return;
      m.push({
        time: bt, position: t.side === 'buy' ? 'belowBar' : 'aboveBar', shape: t.side === 'buy' ? 'arrowUp' : 'arrowDown',
        color: t.side === 'buy' ? css('--up') : css('--down'),
        text: `${t.mode === 'live' ? 'LIVE ' : ''}${t.side === 'buy' ? 'Buy' : 'Sell'} → ${Math.round(t.target * 100)}%`,
      });
    });
    charts.candles.setMarkers(m);
  }

  // ------------------------------------------------------------ render
  function render() {
    if (!S) return;
    const cfg = S.config;
    $('#ver').textContent = 'v' + S.version;
    if (!coin || !cfg.coins.includes(coin)) { coin = cfg.coins[0]; loadCandles(true); }

    // market switch + wording
    $$('#marketTabs button').forEach((x) => x.classList.toggle('on', x.dataset.m === market));
    $('#marketNote').textContent = STOCKS() ? `Today, from Yahoo Finance (may be delayed) · US market ${S.us_market_open ? 'open' : 'closed'}` : 'Last 24 hours on Coinbase';
    $('#fngChip').hidden = STOCKS();
    const setHidden = (sel, v) => { const el = $(sel); if (el) el.hidden = v; };
    const setText = (sel, v) => { const el = $(sel); if (el) el.textContent = v; };
    setHidden('#liveSection', STOCKS()); setHidden('#stockLiveNote', !STOCKS()); setHidden('#customBox', !STOCKS());
    setText('#btIntro', STOCKS() ? 'Runs every strategy on daily prices from Yahoo Finance (dividends included) with your fee, and compares them with simply buying once and holding. Past results do not predict future results.' : 'Runs every strategy on Coinbase daily prices with your fee, and compares them with simply buying once and holding. Past results do not predict future results.');
    setText('#btCoinLabel', STOCKS() ? 'Stock or fund' : 'Coin');
    setText('#posHead', STOCKS() ? 'Stock' : 'Coin');
    setText('#assetsHeading', STOCKS() ? 'Stocks and funds the bot trades' : 'Coins the bot trades');
    setText('#assetsNote', STOCKS() ? 'Money is split evenly. Index funds hold hundreds of companies, so no single one can sink you. In testing, holding made more money than the bot on every stock and fund; the bot mainly cuts crash losses.' : 'Money is split evenly. Bitcoin + Ethereum together did best over 2017–2026 (see How it was tested). Smaller coins have short histories; check them in Backtest first.');

    // header
    const live = S.live_on;
    const pill = $('#modePill');
    pill.textContent = live ? 'Live + paper' : 'Paper money';
    pill.className = 'pill ' + (live ? 'pill-live' : 'pill-paper');
    const chip = $('#statusChip');
    const errs = Object.values(S.errors || {});
    const nextIn = S.next_decision - S.now;
    if (errs.length) { chip.className = 'status err'; $('#statusText').textContent = 'Connection problem'; }
    else if (S.paused) { chip.className = 'status paused'; $('#statusText').textContent = 'Paused: not trading'; }
    else { chip.className = 'status ok'; $('#statusText').textContent = STOCKS() ? `Running · US market ${S.us_market_open ? 'open' : 'closed'} · next decision in ${dur(nextIn)}` : `Running · next decision in ${dur(nextIn)}`; }
    $('#btnPause').textContent = S.paused ? 'Resume bot' : 'Pause bot';
    const banner = $('#banner');
    if (errs.length) { banner.hidden = false; banner.textContent = errs[0] + ' The bot keeps retrying on its own.'; } else banner.hidden = true;

    // scoreboard
    const eq = S.equity, he = S.hold_equity, start = S.start_cash;
    $('#vBot').textContent = usd(eq, 2);
    $('#vBotSub').innerHTML = eq ? `<span class="${cls(eq - start)}">${pct(eq / start - 1, 2)}</span> since ${esc(new Date(S.created * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))} · started with ${usd(start, 0)}` : '&nbsp;';
    $('#vHold').textContent = usd(he, 2);
    $('#vHoldSub').innerHTML = he ? `<span class="${cls(he - start)}">${pct(he / start - 1, 2)}</span> · ${esc(cfg.coins.join(' + '))} bought once` : 'Starts with the first price';
    if (eq && he) {
      const d = eq - he;
      $('#vDiff').innerHTML = `<span class="${cls(d)}">${d >= 0 ? '+' : ''}${usd(d, 2)}</span>`;
      $('#vDiffSub').textContent = d >= 0 ? 'The bot is ahead of plain holding' : 'Plain holding is ahead right now';
    }
    const invested = eq ? (eq - S.cash) / eq : null;
    $('#vInvested').textContent = invested === null ? '—' : Math.round(invested * 100) + '%';
    $('#vInvestedBar').style.width = invested === null ? '0' : Math.round(invested * 100) + '%';
    $('#vCash').textContent = `${usd(S.cash, 2)} in cash`;

    // coin tabs
    const tabs = $('#coinTabs');
    const want = cfg.coins.join(',');
    if (tabs.dataset.coins !== want) {
      tabs.dataset.coins = want;
      tabs.innerHTML = cfg.coins.map((c) => `<button data-coin="${c}">${c}</button>`).join('');
    }
    $$('#coinTabs button').forEach((b) => b.classList.toggle('on', b.dataset.coin === coin));
    const px = S.prices[coin], st = S.stats[coin];
    $('#pxNow').textContent = px ? usd(px.price) : '—';
    const cc = candleCache[coin + tf];
    if (px && cc && cc.rows.length) {
      const r = cc.rows[cc.rows.length - 1];
      const t0 = tf === '1d' ? r[0] : r[0] + tz();
      try { charts.candles.update({ time: t0, open: r[1], high: Math.max(r[2], px.price), low: Math.min(r[3], px.price), close: px.price }); } catch (e) { /* ignore */ }
    }
    if (st && px) { const ch = px.price / st.open - 1; $('#pxChg').className = 'chg ' + cls(ch); $('#pxChg').textContent = pct(ch, 2) + (STOCKS() ? ' today' : ' 24h'); }

    renderPortfolio();
    renderBrain();
    renderEquity();
    renderPositions();
    renderTrades();
    renderLog();
    renderMarket();
    prepareCloudBacktest();
    renderRace();
    if (CLOUD) { renderCloudChrome(); renderCloudSettings(); } else renderSettings();
    loadNews();
  }

  let otherAcct = null, otherAt = 0;
  async function loadOther() {
    if (!CLOUD || Date.now() - otherAt < 120000) return;
    otherAt = Date.now();
    try { otherAcct = await getJSON(`data/dashboard${STOCKS() ? '' : '_stocks'}.json?t=` + Date.now()); } catch (e) { otherAcct = null; }
  }
  function renderPortfolio() {
    const p = Object.assign({}, S.portfolio || {});
    const me = S.market || 'crypto', other = me === 'crypto' ? 'stocks' : 'crypto';
    p[me] = { equity: S.equity, hold_equity: S.hold_equity, start: S.start_cash };
    if (CLOUD) { loadOther(); if (otherAcct) p[other] = { equity: otherAcct.equity, hold_equity: otherAcct.hold_equity, start: otherAcct.start_cash }; }
    const bar = $('#portfolioBar');
    if (!p.crypto || !p.stocks || !p.crypto.equity || !p.stocks.equity) { bar.hidden = true; return; }
    const tot = p.crypto.equity + p.stocks.equity, start = p.crypto.start + p.stocks.start;
    const hold = (p.crypto.hold_equity || p.crypto.start) + (p.stocks.hold_equity || p.stocks.start);
    bar.hidden = false;
    bar.innerHTML = `<span>Both accounts together</span><b>${usd(tot, 0)}</b><span class="${cls(tot - start)}">${pct(tot / start - 1, 2)}</span>
      <span>Just holding both: <b>${usd(hold, 0)}</b></span>
      <span>Crypto ${Math.round(p.crypto.equity / tot * 100)}% · Stocks ${Math.round(p.stocks.equity / tot * 100)}%</span>
      <span class="muted small">Mixing crypto and stocks smoothed the ride in testing (see How it was tested)</span>`;
  }

  function renderBrain() {
    const sig = S.signals[coin];
    $('#stratName').textContent = stratLabel();
    const rc = $('#regimeChip');
    const reg = sig && sig.regime;
    rc.className = 'chip ' + (reg || '');
    rc.textContent = reg === 'bull' ? `${coin} uptrend` : reg === 'bear' ? `${coin} downtrend` : reg ? `${coin} sideways` : 'Regime —';
    rc.title = 'Uptrend / downtrend = price above / below its 200-day average, with that average rising / falling.';
    const fc = $('#fngChip');
    if (S.fng) { fc.textContent = `Fear & Greed ${S.fng.value} · ${S.fng.label}`; fc.className = 'chip ' + (S.fng.value <= 40 ? 'fear' : S.fng.value >= 60 ? 'greed' : ''); fc.title = 'Crypto Fear & Greed index (alternative.me): a daily mood score from volatility, momentum, social media and search trends.'; }
    else { fc.textContent = 'Fear & Greed —'; }
    $('#brainCoin').textContent = `${S.coins[coin]} (${coin})`;
    $('#lastDecision').textContent = S.last_decision_ts ? when(S.last_decision_ts) : '—';
    $('#nextDecision').textContent = `${timeOnly(S.next_decision)} (${dur(S.next_decision - S.now)})`;
    if (!sig) { $('#brainSentence').textContent = 'Loading signals…'; return; }
    if (!sig.ok) { $('#brainSentence').textContent = sig.why; return; }
    const t = sig.target;
    $('#tgtPct').textContent = Math.round(t * 100) + '%';
    $('#ringFill').setAttribute('stroke-dasharray', `${(t * 314.16).toFixed(1)} 314.16`);
    const votes = sig.votes.filter((v) => v.up !== null);
    const ups = votes.filter((v) => v.up).length;
    let sentence;
    if (S.config.strategy === 'hold') sentence = 'Just-hold mode: always fully invested.';
    else if (S.config.strategy === 'mix') sentence = `${Math.round(t * 100)}% invested, combining both strategies below.`;
    else if (t <= 0.001) sentence = `In cash. ${ups} of ${votes.length} trend signals point up, so the bot is waiting on the sidelines.`;
    else if (t >= 0.999) sentence = `Fully invested. ${ups} of ${votes.length} trend signals point up.`;
    else sentence = `Partly invested: ${ups} of ${votes.length} trend signals point up.`;
    const floor = S.config.min_invested_pct;
    if (floor > 0 && S.config.strategy !== 'hold') sentence += ` (Includes your ${floor}% always-invested floor.)`;
    $('#brainSentence').textContent = sentence;
    const pv = sig.preview;
    $('#brainPreview').textContent = pv === null || pv === undefined ? '' :
      Math.abs(pv - t) < 0.001 ? `If today closed at the current price, it would stay at ${Math.round(pv * 100)}%.`
        : `If today closed at the current price, it would move to ${Math.round(pv * 100)}% tonight.`;
    $('#votes').innerHTML = sig.votes.map((v) => {
      const k = v.up === null ? 'info' : v.up ? 'up' : 'down';
      const ic = v.up === null ? '~' : v.up ? '↑' : '↓';
      return `<li class="${k}"><span class="ic">${ic}</span><span>${esc(v.name)}</span><span class="d">${esc(v.detail || (v.up ? 'uptrend' : 'downtrend'))}</span></li>`;
    }).join('');
  }

  function renderEquity() {
    const pts = S.equity_curve || [];
    const off = tz();
    const dedupe = (arr) => { const out = []; arr.forEach((p) => { if (!out.length || p.time > out[out.length - 1].time) out.push(p); }); return out; };
    const now = S.now;
    const bot = pts.map((p) => ({ time: Math.floor(p[0]) + off, value: p[1] }));
    const hold = pts.filter((p) => p[2]).map((p) => ({ time: Math.floor(p[0]) + off, value: p[2] }));
    if (S.equity) bot.push({ time: Math.floor(now) + off, value: S.equity });
    if (S.hold_equity) hold.push({ time: Math.floor(now) + off, value: S.hold_equity });
    charts.eqBot.setData(dedupe(bot));
    charts.eqHold.setData(dedupe(hold));
    const lv = pts.filter((p) => p[3]).map((p) => ({ time: Math.floor(p[0]) + off, value: p[3] }));
    charts.eqLive.applyOptions({ visible: lv.length > 0 });
    $('#liveKey').hidden = lv.length === 0;
    charts.eqLive.setData(dedupe(lv));
    $('#eqSince').textContent = pts.length < 3 ? (CLOUD ? 'Adds a point every time the cloud bot runs (4 times a day)' : 'Adds a point every 10 minutes while the bot runs') : `Since ${when(pts[0][0])}`;
  }

  function renderPositions() {
    const rows = S.positions.map((p) => `<tr><td><b>${p.coin}</b> <span class="muted">${esc(p.name)}</span></td>
      <td class="r">${qty(p.qty)}</td><td class="r">${usd(p.price)}</td><td class="r">${usd(p.value, 2)}</td>
      <td class="r">${p.target === null || p.target === undefined ? '—' : Math.round(p.target * 100) + '%'}</td></tr>`);
    rows.push(`<tr><td><b>Cash</b> <span class="muted">USD</span></td><td class="r"></td><td class="r"></td><td class="r">${usd(S.cash, 2)}</td><td class="r"></td></tr>`);
    $('#posTable tbody').innerHTML = rows.join('');
    const lp = $('#livePanel');
    lp.hidden = !S.live_on;
    if (S.live_on) { $('#liveValue').textContent = usd(S.live.equity, 2); $('#liveCash').textContent = usd(S.live.cash, 2); }
  }

  function renderTrades() {
    const all = [...S.trades, ...(S.live.trades || [])].sort((a, b) => b.ts - a.ts);
    $('#noTrades').hidden = all.length > 0;
    $('#tradeCount').textContent = all.length ? `${all.length} trade${all.length > 1 ? 's' : ''} · fees paid ${usd(all.reduce((s, t) => s + t.fee, 0), 2)}` : '';
    $('#tradeTable tbody').innerHTML = all.slice(0, 200).map((t) => `<tr>
      <td>${when(t.ts)}</td>
      <td><span class="side side-${t.side}">${t.side}</span> ${t.coin}${t.mode === 'live' ? '<span class="tag-live">LIVE</span>' : ''}</td>
      <td class="r">${qty(t.qty)}</td><td class="r">${usd(t.price)}</td><td class="r">${usd(t.usd, 2)}</td><td class="r">${usd(t.fee, 2)}</td></tr>`).join('');
    const sig = all.length + ':' + (all[0] ? all[0].ts : 0);
    if (sig !== lastTradeSig) { lastTradeSig = sig; const c = candleCache[coin + tf]; if (c) drawCandles(c.rows); }
  }

  function renderLog() {
    $('#log').innerHTML = [...S.log].reverse().map((l) => `<li class="${l.level}"><time>${when(l.ts)}</time><span>${esc(l.msg)}</span></li>`).join('');
  }

  function renderMarket() {
    const order = Object.keys(S.coins);
    $('#market').innerHTML = order.map((c) => {
      const st = S.stats[c]; const p = S.prices[c];
      if (!st) return `<div class="mcell"><span class="sym">${c}</span><span class="px">—</span><span class="nm">${esc(S.coins[c])}</span></div>`;
      const last = p ? p.price : st.last;
      const ch = last / st.open - 1;
      const pos = st.high > st.low ? Math.min(100, Math.max(0, (last - st.low) / (st.high - st.low) * 100)) : 50;
      return `<div class="mcell"><span class="sym">${c}</span><span class="px">${usd(last)}</span>
        <span class="nm">${esc(S.coins[c])}</span><span class="ch ${cls(ch)}">${pct(ch, 2)}</span>
        <div class="range" title="Where the price sits in today's low–high range"><i style="left:calc(${pos.toFixed(1)}% - 1px)"></i></div>
        <div class="range-l"><span>${usd(st.low)}</span><span>${usd(st.high)}</span></div></div>`;
    }).join('');
  }

  function stratLabel() {
    const c = S.config;
    if (c.strategy !== 'mix') return S.strategies[c.strategy];
    const m = c.mix || {};
    return `Mix: ${Math.round(m.a_pct)}% ${S.strategies[m.a]} + ${Math.round(100 - m.a_pct)}% ${S.strategies[m.b]}`;
  }

  // ------------------------------------------------------------ race + news
  function renderRace() {
    const r = S.race;
    const tbl = $('#raceTable');
    if (!r) { tbl.innerHTML = '<tbody><tr><td class="muted">Calculating…</td></tr></tbody>'; return; }
    if (r.error) { tbl.innerHTML = `<tbody><tr><td class="err-text">${esc(r.error)}</td></tr></tbody>`; return; }
    const names = Object.keys(r.since_start || {});
    const label = (k) => k === 'mix' ? stratLabel() : S.strategies[k];
    const best = (w) => names.reduce((a, k) => (r[w][k] !== null && (a === null || r[w][k] > r[w][a])) ? k : a, null);
    const b1 = best('last_year');
    tbl.innerHTML = `<thead><tr><th>Strategy</th><th class="r">Since you started</th><th class="r">Last 12 months</th><th class="r">Last 3 years</th></tr></thead><tbody>` +
      names.map((k) => `<tr class="${k === b1 && k !== 'hold' ? 'race-best' : ''}"><td><b>${esc(label(k))}</b>${k === S.config.strategy ? ' <span class="muted small">(yours)</span>' : ''}</td>` +
        ['since_start', 'last_year', 'last_3_years'].map((w) => `<td class="r ${cls(r[w][k])}">${pct(r[w][k], 1)}</td>`).join('') + '</tr>').join('') + '</tbody>';
  }
  let newsAt = 0;
  async function loadNews() {
    if (Date.now() - newsAt < 600000) return;
    newsAt = Date.now();
    try {
      const n = await api('/api/news');
      const el = $('#news');
      if (!n.items || !n.items.length) { el.innerHTML = `<li class="muted">No headlines right now${n.failed && n.failed.length ? ' (could not reach ' + esc(n.failed.join(', ')) + ')' : ''}.</li>`; return; }
      el.innerHTML = n.items.map((it) => `<li><a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title)}</a><span class="src">${esc(it.source)}${it.ts ? ' · ' + esc(when(it.ts)) : ''}</span></li>`).join('');
    } catch (e) { newsAt = 0; $('#news').innerHTML = `<li class="muted">Headlines unavailable: ${esc(e.message)}</li>`; }
  }

  // ------------------------------------------------------------ settings
  let settingsFilled = false;
  function renderSettings() {
    const cfg = S.config;
    if (CLOUD) return;
    if (!settingsFilled) {
      settingsFilled = true;
      $$('input[name=strategy]').forEach((r) => { r.checked = r.value === cfg.strategy; });
      $('#coinChecks').innerHTML = Object.entries(S.coins).map(([c, n]) =>
        `<label><input type="checkbox" value="${c}" ${cfg.coins.includes(c) ? 'checked' : ''}> ${c} <span class="muted">${esc((S.index_funds && S.index_funds[c]) || n)}</span></label>`).join('');
      $('#sFee').value = cfg.fee_pct; $('#sSlip').value = cfg.slippage_pct; $('#sFloor').value = cfg.min_invested_pct;
      $('#sBand').value = cfg.rebalance_band_pct; $('#sCash').value = cfg.starting_cash; $('#sNotify').checked = cfg.notifications !== false;
      $('#btFee').value = cfg.fee_pct;
      $('#btCoin').innerHTML = Object.entries(S.coins).map(([c, n]) => `<option value="${c}">${c} · ${esc((S.index_funds && S.index_funds[c]) || n)}</option>`).join('');
      const opts = Object.entries(S.mix_choices || {}).map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join('');
      $('#mixA').innerHTML = opts; $('#mixB').innerHTML = opts;
      const mx = cfg.mix || { a: 'auto', b: 'steady', a_pct: 50 };
      $('#mixA').value = mx.a; $('#mixB').value = mx.b; $('#mixPct').value = mx.a_pct;
    }
    renderBackground();
    renderLive();
  }

  function renderBackground() {
    const bg = S.background;
    const box = $('#bgBox');
    const key = JSON.stringify(bg);
    if (box.dataset.k === key) return;
    box.dataset.k = key;
    if (!bg.supported) { box.innerHTML = '<p class="muted small">Available on macOS only.</p>'; return; }
    box.innerHTML = bg.installed
      ? `<p class="ok-text">Background mode is on.</p><button class="btn btn-ghost" id="bgOff">Turn off background mode</button>`
      : `<button class="btn btn-ghost" id="bgOn">Turn on background mode</button>`;
    const on = $('#bgOn'), off = $('#bgOff');
    if (on) on.onclick = async () => {
      on.disabled = true; on.textContent = 'Switching… (the page reconnects in a few seconds)';
      try { await api('/api/background/install', {}); } catch (e) { /* the old process exits; that is expected */ }
    };
    if (off) off.onclick = async () => {
      try { await api('/api/background/uninstall', {}); box.innerHTML = '<p class="muted">Background mode is off. The bot stops now; start it again by opening the Trend Rider app in Documents › Trend Rider.</p>'; }
      catch (e) { alertBox(box, e.message); }
    };
  }

  function alertBox(el, msg, ok) {
    let p = el.querySelector('.msg');
    if (!p) { p = document.createElement('p'); p.className = 'msg'; el.appendChild(p); }
    p.className = 'msg ' + (ok ? 'ok-text' : 'err-text');
    p.textContent = msg;
  }

  function renderLive() {
    if (STOCKS()) return;
    const box = $('#liveBox');
    const st = S.live_on ? 'on' : S.has_keys ? 'keys' : 'none';
    if (box.dataset.k === st) return;
    box.dataset.k = st;
    if (st === 'none') {
      box.innerHTML = `<div class="stack">
        <p class="small muted">On kraken.com: Settings → API → Create API key. Turn on only <b>Query funds</b>, <b>Query open orders &amp; trades</b>, <b>Query closed orders &amp; trades</b> and <b>Create &amp; modify orders</b>. Never turn on withdrawals. Keys are saved only on this Mac.</p>
        <label>API key <input id="kKey" type="text" autocomplete="off" spellcheck="false"></label>
        <label>Private key <input id="kSec" type="password" autocomplete="off" spellcheck="false"></label>
        <div><button class="btn btn-ghost" id="kConnect">Connect Kraken</button></div></div>`;
      $('#kConnect').onclick = async () => {
        const b = $('#kConnect'); b.disabled = true; b.textContent = 'Checking…';
        try { const r = await api('/api/live/keys', { key: $('#kKey').value, secret: $('#kSec').value }); $('#kKey').value = ''; $('#kSec').value = ''; await refresh(); alertBox($('#liveBox'), 'Connected. ' + balanceText(r.balances), true); }
        catch (e) { b.disabled = false; b.textContent = 'Connect Kraken'; alertBox(box, e.message); }
      };
    } else if (st === 'keys') {
      box.innerHTML = `<div class="stack">
        <p class="ok-text">Kraken is connected. Live trading is off.</p>
        <div class="form-grid">
          <label>Bot budget ($) <input id="kBudget" type="number" min="20" step="10" value="${S.config.live.budget_usd}"></label>
          <label>Most it moves per coin per day ($) <input id="kCap" type="number" min="10" step="5" value="${S.config.live.max_trade_usd}"></label>
        </div>
        <p class="small muted">The bot only trades this budget, and only sells coins it bought itself. Anything else in your Kraken account is left alone.</p>
        <label>Type <b>REAL MONEY</b> to confirm <input id="kConfirm" type="text" autocomplete="off"></label>
        <div><button class="btn btn-danger" id="kEnable">Start live trading</button> <button class="btn btn-ghost" id="kTest">Check balance</button> <button class="btn btn-ghost" id="kForget">Remove keys</button></div></div>`;
      $('#kEnable').onclick = async () => {
        try { await api('/api/live/enable', { confirm: $('#kConfirm').value, budget_usd: +$('#kBudget').value, max_trade_usd: +$('#kCap').value }); await refresh(); }
        catch (e) { alertBox(box, e.message); }
      };
      $('#kTest').onclick = async () => { try { const r = await api('/api/live/test', {}); alertBox(box, balanceText(r.balances), true); } catch (e) { alertBox(box, e.message); } };
      $('#kForget').onclick = async () => { try { await api('/api/live/forget', {}); await refresh(); } catch (e) { alertBox(box, e.message); } };
    } else {
      box.innerHTML = `<div class="stack"><p><span class="pill pill-live">Live</span> Trading real money on Kraken with a ${usd(S.config.live.budget_usd, 0)} budget (moves at most ${usd(S.config.live.max_trade_usd, 0)} per coin per day).</p>
        <div><button class="btn btn-ghost" id="kDisable">Stop live trading</button></div>
        <p class="small muted">Stopping does not sell anything. Coins the bot bought stay in your Kraken account.</p></div>`;
      $('#kDisable').onclick = async () => { try { await api('/api/live/disable', {}); await refresh(); } catch (e) { alertBox(box, e.message); } };
    }
  }
  function balanceText(b) {
    const items = Object.entries(b || {}).filter(([, v]) => +v > 0).map(([k, v]) => `${k} ${(+v).toLocaleString(undefined, { maximumFractionDigits: 6 })}`);
    return items.length ? 'Balances: ' + items.join(', ') : 'The account is empty.';
  }

  // ------------------------------------------------------------ backtest
  let btPrepared = false;
  function prepareCloudBacktest() {
    if (!CLOUD || btPrepared || !S) return;
    btPrepared = true;
    $('#btCoin').innerHTML = S.config.coins.map((c) => `<option value="${c}">${c} · ${esc(S.coins[c])}</option>`).join('');
    $('#btYears').innerHTML = `<option value="1">1 year</option><option value="3">3 years</option><option value="5" selected>5 years</option><option value="12">${STOCKS() ? '12 years' : 'All available'}</option>`;
    $('#btFee').value = S.config.fee_pct; $('#btFee').disabled = true;
    $('#btFloor').value = S.config.min_invested_pct; $('#btFloor').disabled = true;
    $('#btForm').insertAdjacentHTML('afterend', '<p class="small muted">Cloud version: results are prepared once a week (and whenever you change settings) for your coins, with your fee and settings.</p>');
  }
  async function runBacktest(ev) {
    ev && ev.preventDefault();
    const btn = $('#btRun'); btn.disabled = true; btn.textContent = 'Downloading prices…';
    $('#btMsg').hidden = true;
    const c = $('#btCoin').value, y = $('#btYears').value, f = $('#btFee').value, fl = $('#btFloor').value;
    try {
      const r = await api(`/api/backtest?coin=${c}&years=${y}&fee=${f}&floor=${fl}`);
      drawBacktest(r.runs, c);
    } catch (e) {
      $('#btMsg').hidden = false; $('#btMsg').textContent = 'Backtest failed: ' + e.message;
    } finally { btn.disabled = false; btn.textContent = 'Run backtest'; }
  }
  const BT_COLORS = { growth: '--bot', steady: '--steady', auto: '--auto', cruise: '--live', mix: '--mix', hold: '--hold' };
  function drawBacktest(runs, c) {
    $('#btOut').hidden = false;
    const g = runs[0];
    const d0 = new Date(g.from * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    const d1 = new Date(g.to * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    $('#btTitle').textContent = `${c}: $10,000 from ${d0} to ${d1}`;
    if (charts.bt) { charts.bt.remove(); charts.bt = null; }
    charts.bt = makeChart($('#btChart'), { rightPriceScale: { mode: 1, borderColor: css('--line'), scaleMargins: { top: 0.08, bottom: 0.05 } }, localization: { priceFormatter: (p) => usd(p, 0) } });
    const uniq = (arr) => arr.filter((p, i) => i === 0 || p.time > arr[i - 1].time);
    const lines = runs.map((r) => ({ key: r.strategy, label: r.label || S.strategies[r.strategy], data: r.curve.map((p) => ({ time: p[0], value: p[1] * 10000 })) }));
    lines.push({ key: 'hold', label: 'Just hold', data: g.curve.map((p) => ({ time: p[0], value: p[2] * 10000 })) });
    charts.btSeries = lines.map((l) => { const s = charts.bt.addLineSeries({ color: css(BT_COLORS[l.key]), lineWidth: 2, title: l.key === 'mix' ? 'Mix' : l.label }); s.setData(uniq(l.data)); return s; });
    charts.btKeys = lines.map((l) => l.key);
    charts.bt.timeScale().fitContent();
    $('#btLegend').innerHTML = lines.map((l) => `<span><span class="key key-${l.key === 'growth' ? 'bot' : l.key === 'cruise' ? 'live' : l.key}"></span>${esc(l.label)}</span>`).join('');
    const row = (name, st, trades) => `<tr><td><b>${esc(name)}</b></td><td class="r">${usd(10000 * (1 + st.total), 0)}</td><td class="r ${cls(st.total)}">${pct(st.total, 0)}</td><td class="r">${pct(st.cagr, 1)}</td><td class="r neg">${pct(st.maxdd, 0)}</td><td class="r">${st.sharpe.toFixed(2)}</td><td class="r">${trades === null ? '1' : trades}</td></tr>`;
    $('#btStats').innerHTML = `<thead><tr><th></th><th class="r">Ends at</th><th class="r">Total</th><th class="r">Per year</th><th class="r">Worst drop</th><th class="r" title="Return per unit of risk. Higher is better.">Sharpe</th><th class="r">Trades</th></tr></thead><tbody>` +
      runs.map((r) => row(r.label || S.strategies[r.strategy], r.stats, r.trades)).join('') + row('Just hold', g.hold_stats, null) + '</tbody>';
    $('#btYearly').innerHTML = `<thead><tr><th>Year</th>${runs.map((r) => `<th class="r">${esc(r.strategy === 'mix' ? 'Mix' : (r.label || S.strategies[r.strategy]))}</th>`).join('')}<th class="r">Just hold</th></tr></thead><tbody>` +
      g.yearly.map((y, i) => `<tr><td>${y.year}</td>${runs.map((r) => { const v = r.yearly[i] ? r.yearly[i].bot : null; return `<td class="r ${cls(v)}">${pct(v, 0)}</td>`; }).join('')}<td class="r ${cls(y.hold)}">${pct(y.hold, 0)}</td></tr>`).join('') + '</tbody>';
  }

  // ------------------------------------------------------------ research
  let researchLoaded = false;
  async function loadResearch() {
    if (researchLoaded) return;
    try {
      const r = await api('/api/research');
      researchLoaded = true;
      $('#research').innerHTML = r.html || '<p>Research summary not found.</p>';
    } catch (e) { $('#research').innerHTML = `<p class="err-text">${esc(e.message)}</p>`; }
  }

  // ------------------------------------------------------------ events
  function switchMarket(m) {
    if (m === market) return;
    market = m;
    try { localStorage.setItem('tr-market', m); } catch (e) { /* ignore */ }
    S = null; coin = null; candleCache = {}; lastTradeSig = ''; settingsFilled = false; btPrepared = false; newsAt = 0;
    $('#coinTabs').dataset.coins = ''; $('#btOut').hidden = true;
    ['#viewSettingsKey'].forEach(() => {});
    const vs = $('#view-settings'); if (vs) vs.dataset.k = '';
    $('#votes').innerHTML = ''; $('#tradeTable tbody').innerHTML = ''; $('#log').innerHTML = ''; $('#market').innerHTML = '';
    charts.candles.setData([]); charts.candles.setMarkers([]);
    refresh().then(() => loadCandles(true));
  }

  function bind() {
    $('#marketTabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) switchMarket(b.dataset.m); });
    $('#customAdd').onclick = async () => {
      const t = ($('#customTicker').value || '').trim().toUpperCase();
      if (!t) return;
      const custom = [...new Set([...(S.config.custom || []), t])];
      const coins = [...new Set([...$$('#coinChecks input:checked').map((i) => i.value), t])];
      const msg = $('#savedMsg');
      $('#customAdd').disabled = true; $('#customAdd').textContent = 'Checking…';
      try { await api('/api/settings', { custom, coins }); $('#customTicker').value = ''; settingsFilled = false; msg.hidden = false; msg.className = 'saved'; msg.textContent = `Added ${t}.`; await refresh(); }
      catch (e) { msg.hidden = false; msg.className = 'saved err-text'; msg.textContent = e.message; }
      finally { $('#customAdd').disabled = false; $('#customAdd').textContent = 'Add'; }
    };
    $('#coinTabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; coin = b.dataset.coin; render(); loadCandles(true); });
    $('#tfTabs').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return; tf = b.dataset.tf;
      $$('#tfTabs button').forEach((x) => x.classList.toggle('on', x === b)); loadCandles(true);
    });
    $('#btnCheck').onclick = async () => { const b = $('#btnCheck'); b.disabled = true; b.textContent = 'Checking…'; try { await api('/api/decide', {}); setTimeout(refresh, 2500); } catch (e) { const bn = $('#banner'); bn.hidden = false; bn.textContent = e.message; } finally { setTimeout(() => { b.disabled = false; b.textContent = 'Check now'; }, 3000); } };
    $('#btnPause').onclick = async () => { try { await api('/api/pause', { paused: !S.paused }); refresh(); } catch (e) { /* shown by status */ } };
    $('#btForm').addEventListener('submit', runBacktest);
    $('#saveSettings').onclick = async () => {
      const body = {
        strategy: ($$('input[name=strategy]').find((r) => r.checked) || {}).value,
        coins: $$('#coinChecks input:checked').map((i) => i.value),
        fee_pct: +$('#sFee').value, slippage_pct: +$('#sSlip').value, min_invested_pct: +$('#sFloor').value,
        rebalance_band_pct: +$('#sBand').value, notifications: $('#sNotify').checked,
        mix: { a: $('#mixA').value, b: $('#mixB').value, a_pct: +$('#mixPct').value },
      };
      const msg = $('#savedMsg');
      try { await api('/api/settings', body); msg.hidden = false; msg.className = 'saved'; msg.textContent = 'Saved. The bot is re-checking its targets.'; setTimeout(refresh, 2500); }
      catch (e) { msg.hidden = false; msg.className = 'saved err-text'; msg.textContent = e.message; }
    };
    $('#resetBtn').onclick = () => { $('#resetConfirm').hidden = false; };
    $('#quitBtn').onclick = () => { $('#quitConfirm').hidden = false; };
    $('#quitNo').onclick = () => { $('#quitConfirm').hidden = true; };
    $('#quitYes').onclick = async () => {
      try { await api('/api/quit', {}); $('#quitConfirm').innerHTML = '<p>Trend Rider is stopped. Open the Trend Rider app in your Documents › Trend Rider folder to start it again.</p>'; }
      catch (e) { alertBox($('#quitConfirm'), e.message); }
    };
    $('#resetNo').onclick = () => { $('#resetConfirm').hidden = true; };
    $('#resetYes').onclick = async () => {
      try { await api('/api/reset', { starting_cash: +$('#sCash').value }); $('#resetConfirm').hidden = true; settingsFilled = false; setTimeout(refresh, 2500); refresh(); }
      catch (e) { alertBox($('#resetConfirm'), e.message); }
    };
  }

  // ------------------------------------------------------------ loop
  let failures = 0;
  async function refresh() {
    const m = market;
    try {
      const fresh = await api('/api/state');
      if (m !== market) return;   // the user switched Crypto/Stocks while this was loading
      S = fresh;
      failures = 0;
      render();
    } catch (e) {
      console.error('Trend Rider refresh failed:', e);
      failures++;
      if (failures > 1) {
        $('#statusChip').className = 'status err';
        $('#statusText').textContent = 'Not connected to the bot';
        const b = $('#banner'); b.hidden = false;
        b.textContent = CLOUD ? 'Could not load the bot\'s latest results (' + e.message + '). This page retries by itself.' : 'Trend Rider is not running (or is restarting). Open the Trend Rider app in Documents › Trend Rider; this page reconnects by itself.';
      }
    }
  }

  initCharts();
  bind();
  route();
  refresh().then(() => loadCandles(true));
  setInterval(refresh, CLOUD ? 15000 : 5000);
  setInterval(() => loadCandles(false), 60000);
})();
