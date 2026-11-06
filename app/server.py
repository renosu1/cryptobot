"""Trend Rider - local crypto dashboard and trading bot.

Run:  python3 app/server.py --open
Then use the dashboard at http://127.0.0.1:8765

Paper trading by default (real prices, pretend money). Everything is stored in
the data/ folder next to this app. Standard library only.
"""
import json, math, os, re, shutil, sys, threading, xml.etree.ElementTree as ET, email.utils, time, traceback, urllib.parse, webbrowser, argparse, subprocess, plistlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if sys.platform == 'darwin':
    SUPPORT = os.path.expanduser('~/Library/Application Support/TrendRider')
else:
    SUPPORT = ROOT
DATA = os.path.join(SUPPORT, 'data')
CACHE = os.path.join(DATA, 'cache')
STATIC = os.path.join(HERE, 'static')
sys.path.insert(0, HERE)

import strategy as S
from net import get_json, http_request, NetError
from kraken import Kraken, KrakenError
from binanceus import BinanceUS, BinanceUSError
EXCHANGES = {'kraken': Kraken, 'binanceus': BinanceUS}

VERSION = '1.1.0'
COINS = {
    'BTC': {'name': 'Bitcoin', 'cb': 'BTC-USD', 'kr': 'XBTUSD', 'kr_bal': ['XXBT', 'XBT']},
    'ETH': {'name': 'Ethereum', 'cb': 'ETH-USD', 'kr': 'ETHUSD', 'kr_bal': ['XETH', 'ETH']},
    'SOL': {'name': 'Solana', 'cb': 'SOL-USD', 'kr': 'SOLUSD', 'kr_bal': ['SOL']},
    'XRP': {'name': 'XRP', 'cb': 'XRP-USD', 'kr': 'XRPUSD', 'kr_bal': ['XXRP', 'XRP']},
    'DOGE': {'name': 'Dogecoin', 'cb': 'DOGE-USD', 'kr': 'XDGUSD', 'kr_bal': ['XXDG', 'XDG']},
    'ADA': {'name': 'Cardano', 'cb': 'ADA-USD', 'kr': 'ADAUSD', 'kr_bal': ['ADA']},
    'LINK': {'name': 'Chainlink', 'cb': 'LINK-USD', 'kr': 'LINKUSD', 'kr_bal': ['LINK']},
    'AVAX': {'name': 'Avalanche', 'cb': 'AVAX-USD', 'kr': 'AVAXUSD', 'kr_bal': ['AVAX']},
    'LTC': {'name': 'Litecoin', 'cb': 'LTC-USD', 'kr': 'LTCUSD', 'kr_bal': ['XLTC', 'LTC']},
}
CB = 'https://api.exchange.coinbase.com'
DAY = 86400

DEFAULT_CONFIG = {
    'coins': ['BTC', 'ETH'],
    'strategy': 'growth',
    'mix': {'a': 'auto', 'b': 'steady', 'a_pct': 50},
    'notifications': True,   # macOS notification on every trade
    'starting_cash': 10000.0,
    'fee_pct': 0.40,          # per trade, % (Kraken limit-order fee at the entry tier; Binance.US: 0% limit / 0.02% instant)
    'slippage_pct': 0.05,     # extra price you lose on a market order, %
    'min_invested_pct': 0,    # never go below this much invested (0 = let the bot decide)
    'rebalance_band_pct': 5,  # ignore drifts smaller than this share of a coin's slot
    'live': {'enabled': False, 'budget_usd': 100.0, 'max_trade_usd': 100.0, 'exchange': 'kraken',
             'limit_orders': True, 'limit_wait_min': 30},   # limit first (maker fee), instant order after 30 min
    'port': 8765,
}
STOCK_INDEX_FUNDS = {'SPY': 'S&P 500 index fund', 'QQQ': 'Nasdaq 100 index fund', 'IWM': 'Russell 2000 small companies', 'DIA': 'Dow Jones 30 index fund', 'VT': 'Whole world stock market'}
STOCK_DEFAULTS = {
    'coins': ['SPY', 'QQQ'],      # 'coins' = the assets this account trades
    'custom': [],                 # tickers you added yourself
    'strategy': 'cruise',         # best on stocks in testing (see How it was tested)
    'version': 2,
    'mix': {'a': 'auto', 'b': 'hold', 'a_pct': 50},
    'starting_cash': 10000.0,
    'fee_pct': 0.0,               # most US brokers charge no commission
    'slippage_pct': 0.05,
    'min_invested_pct': 0,
    'rebalance_band_pct': 5,
    'park': 'GLD',                # idle money waits in a gold fund ('cash' = plain cash); see How it was tested, round seven
    'live': {'enabled': False},
}
PARK = 'GLD'   # SPDR Gold Shares: where the stocks account parks money the strategy keeps out of the market
CFG = None

LOCK = threading.RLock()
LIVE_LOCK = threading.Lock()   # one set of real orders at a time


# ================================================================ storage
def ensure_dirs():
    os.makedirs(CACHE, exist_ok=True)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj, private=False):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, separators=(',', ':'))
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = read_json(os.path.join(DATA, 'config.json'), {})
    for k, v in user.items():
        if k == 'live' and isinstance(v, dict):
            cfg['live'].update(v)
        else:
            cfg[k] = v
    cfg['coins'] = [c for c in cfg.get('coins', []) if c in COINS] or ['BTC']
    if cfg.get('strategy') not in S.STRATEGIES:
        cfg['strategy'] = 'growth'
    st = json.loads(json.dumps(STOCK_DEFAULTS))
    saved = cfg.get('stocks') or {}
    if saved and saved.get('version', 1) < 2:   # older default was Growth: move to Cruise once
        saved = dict(saved, strategy='cruise', version=2)
    st.update(saved)
    st['live'] = {'enabled': False}
    if st.get('strategy') not in S.STRATEGIES:
        st['strategy'] = 'growth'
    st['coins'] = [c for c in st.get('coins', []) if isinstance(c, str)][:8] or ['SPY']
    if st.get('park') not in (PARK, 'cash'):
        st['park'] = PARK
    cfg['stocks'] = st
    return cfg


def save_config(cfg):
    write_json(os.path.join(DATA, 'config.json'), cfg)


# ================================================================ market data
class Market:
    def __init__(self):
        self.prices = {}       # coin -> {'price', 'ts', 'src'}
        self.stats = {}        # coin -> {'last','open','high','low','volume','ts'}
        self.spark = {}        # coin -> [closes of last 7 days, hourly]
        self.daily = {}        # coin -> list of [t,o,h,l,c,v] (completed + current)
        self.daily_ts = {}
        self.hourly = {}
        self.hourly_ts = {}
        self.errors = {}       # source -> message
        self.full_history = {}  # coin -> bool (backtest history fetched)

    # ---------- raw fetch
    def _candles(self, coin, gran, start=None, end=None):
        p = {'granularity': gran}
        if start is not None:
            p['start'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(start))
            p['end'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end))
        url = f"{CB}/products/{COINS[coin]['cb']}/candles?" + urllib.parse.urlencode(p)
        rows = get_json(url)
        if not isinstance(rows, list):
            raise NetError(f'Unexpected candle data: {str(rows)[:120]}')
        # Coinbase order: [time, low, high, open, close, volume], newest first
        out = [[int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])] for r in rows]
        out.sort(key=lambda r: r[0])
        return out

    def _merge(self, old, new):
        m = {r[0]: r for r in old}
        for r in new:
            m[r[0]] = r
        return [m[k] for k in sorted(m)]

    def load_daily(self, coin, history_days=900, force=False):
        """Daily candles, cached on disk. Refreshes the recent part every 5 minutes."""
        path = os.path.join(CACHE, f'{coin}_1d.json')
        if coin not in self.daily:
            self.daily[coin] = read_json(path, [])
        rows = self.daily[coin]
        now = time.time()
        have_from = rows[0][0] if rows else now
        need_from = now - history_days * DAY
        changed = False
        if force or now - self.daily_ts.get(coin, 0) > 300 or not rows:
            new = self._candles(coin, DAY)  # latest 300 days
            rows = self._merge(rows, new)
            changed = True
            self.daily_ts[coin] = now
        # extend backwards if more history is needed
        guard = 0
        while rows and rows[0][0] > need_from + DAY and guard < 20:
            end = rows[0][0] - 1
            start = max(need_from, end - 299 * DAY)
            older = self._candles(coin, DAY, start, end)
            guard += 1
            if not older:
                break
            before = rows[0][0]
            rows = self._merge(rows, older)
            changed = True
            if rows[0][0] >= before:
                break
            time.sleep(0.35)
        if changed:
            self.daily[coin] = rows
            write_json(path, rows)
        return rows

    def load_hourly(self, coin):
        now = time.time()
        if now - self.hourly_ts.get(coin, 0) > 120 or coin not in self.hourly:
            rows = self._candles(coin, 3600)
            self.hourly[coin] = rows
            self.hourly_ts[coin] = now
        return self.hourly[coin]

    def refresh_price(self, coin):
        try:
            j = get_json(f"{CB}/products/{COINS[coin]['cb']}/ticker", timeout=10)
            self.prices[coin] = {'price': float(j['price']), 'ts': time.time(), 'src': 'Coinbase'}
            self.errors.pop('price', None)
            return
        except Exception as e:
            err = str(e)
        try:  # fallback: Kraken public ticker
            j = get_json(f"https://api.kraken.com/0/public/Ticker?pair={COINS[coin]['kr']}", timeout=10)
            res = next(iter(j['result'].values()))
            self.prices[coin] = {'price': float(res['c'][0]), 'ts': time.time(), 'src': 'Kraken'}
            self.errors.pop('price', None)
        except Exception as e2:
            self.errors['price'] = f'Live prices unavailable ({err}; backup: {e2})'

    def refresh_stats(self, coin):
        j = get_json(f"{CB}/products/{COINS[coin]['cb']}/stats", timeout=10)
        self.stats[coin] = {k: float(j[k]) for k in ('open', 'high', 'low', 'last', 'volume') if k in j}
        self.stats[coin]['ts'] = time.time()
        if coin not in self.prices or time.time() - self.prices[coin]['ts'] > 60:
            self.prices[coin] = {'price': self.stats[coin]['last'], 'ts': time.time(), 'src': 'Coinbase'}

    def price(self, coin):
        p = self.prices.get(coin)
        return p['price'] if p else None

    def completed(self, rows):
        return completed_daily(rows)

    def name(self, coin):
        return COINS[coin]['name'] if coin in COINS else coin

    # ---------- Crypto Fear & Greed index (alternative.me), daily since 2018
    def load_fng(self):
        path = os.path.join(CACHE, 'fear_greed.json')
        if not getattr(self, 'fng', None):
            self.fng = {int(k): v for k, v in read_json(path, {}).items()}
        if time.time() - getattr(self, 'fng_ts', 0) > 3600:
            self.fng_ts = time.time()
            try:
                j = get_json('https://api.alternative.me/fng/?limit=0&format=json', timeout=20)
                self.fng = {int(x['timestamp']) // DAY * DAY: int(x['value']) for x in j.get('data', [])}
                write_json(path, {str(k): v for k, v in self.fng.items()})
                self.errors.pop('fng', None)
            except Exception as e:
                if not self.fng:
                    self.errors['fng'] = f'Fear & Greed index unavailable ({e}). Auto runs without it.'
        return self.fng

    def fng_for(self, times):
        """Index value for each daily candle (value stamped day t is published at the start of day t)."""
        f = self.load_fng()
        out, last = [], None
        for t in times:
            v = f.get(t // DAY * DAY)
            if v is not None:
                last = v
            out.append(last)
        return out if any(v is not None for v in out) else None

    def fng_latest(self):
        f = self.load_fng()
        if not f:
            return None
        k = max(f)
        return {'ts': k, 'value': f[k], 'label': fng_label(f[k])}

    # ---------- news headlines (shown to you; the bot does not trade on them)
    FEEDS = [('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/'),
             ('Cointelegraph', 'https://cointelegraph.com/rss'),
             ('Decrypt', 'https://decrypt.co/feed')]

    def load_news(self):
        if time.time() - getattr(self, 'news_ts', 0) < 900 and getattr(self, 'news', None) is not None:
            return self.news
        self.news_ts = time.time()
        items, failed = [], []
        for src, url in self.FEEDS:
            try:
                xml = http_request(url, headers={'Accept': 'application/rss+xml, application/xml, text/xml'}, timeout=12)
                root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
                for it in root.iter('item'):
                    title = (it.findtext('title') or '').strip()
                    link = (it.findtext('link') or '').strip()
                    pub = it.findtext('pubDate') or ''
                    try:
                        ts = email.utils.parsedate_to_datetime(pub).timestamp()
                    except Exception:
                        ts = 0
                    if title and link.startswith('http'):
                        items.append({'title': re.sub(r'\s+', ' ', title)[:220], 'link': link, 'ts': ts, 'source': src})
            except Exception as e:
                failed.append(src)
        items.sort(key=lambda x: -x['ts'])
        seen, out = set(), []
        for it in items:
            k = it['title'].lower()[:60]
            if k not in seen:
                seen.add(k); out.append(it)
        self.news = {'items': out[:18], 'failed': failed, 'ts': time.time()}
        return self.news


class StockMarket:
    """US stocks and funds from Yahoo Finance (daily, adjusted for dividends and splits)."""
    YF = 'https://query1.finance.yahoo.com/v8/finance/chart/'
    FEEDS = [('CNBC', 'https://www.cnbc.com/id/100003114/device/rss/rss.html'),
             ('MarketWatch', 'https://feeds.content.dowjones.io/public/rss/mw_topstories'),
             ('Yahoo Finance', 'https://finance.yahoo.com/news/rssindex')]

    def __init__(self):
        self.prices, self.stats, self.daily, self.daily_ts, self.errors, self.names = {}, {}, {}, {}, {}, {}
        self.hourly, self.hourly_ts = {}, {}

    def _chart(self, sym, days=None, interval='1d', range_=None):
        q = {'interval': interval, 'events': 'div,split', 'includePrePost': 'false'}
        if range_:
            q['range'] = range_
        else:
            q['period1'] = str(max(0, int(time.time() - (days or 900) * DAY)))
            q['period2'] = str(int(time.time()) + DAY)
        j = get_json(self.YF + urllib.parse.quote(sym) + '?' + urllib.parse.urlencode(q))
        res = (j.get('chart') or {}).get('result')
        if not res:
            raise NetError(f'{sym}: no data from Yahoo Finance (check the ticker).')
        r = res[0]
        meta = r.get('meta', {})
        self.names[sym] = meta.get('shortName') or meta.get('longName') or sym
        qd = r['indicators']['quote'][0]
        adj = ((r['indicators'].get('adjclose') or [{}])[0].get('adjclose')) or qd['close']
        rows = []
        for i, t in enumerate(r.get('timestamp') or []):
            o, h, lo, c, a = qd['open'][i], qd['high'][i], qd['low'][i], qd['close'][i], adj[i]
            if None in (o, h, lo, c, a) or c <= 0:
                continue
            f = a / c
            rows.append([int(t), o * f, h * f, lo * f, a, float(qd['volume'][i] or 0)])
        return meta, rows

    def load_daily(self, sym, history_days=900, force=False):
        """Fetched fresh (adjusted prices change after every dividend), cached for 10 minutes."""
        have = self.daily.get(sym)
        need = history_days + 10
        if force or not have or time.time() - self.daily_ts.get(sym, 0) > 600 or getattr(self, '_days_' + sym, 0) < need:
            meta, rows = self._chart(sym, days=need)
            self.daily[sym] = rows
            self.daily_ts[sym] = time.time()
            setattr(self, '_days_' + sym, need)
            self._update_from(sym, meta, rows)
        return self.daily[sym]

    def load_hourly(self, sym):
        if time.time() - self.hourly_ts.get(sym, 0) > 300 or sym not in self.hourly:
            meta, rows = self._chart(sym, interval='60m', range_='1mo')
            self.hourly[sym] = rows
            self.hourly_ts[sym] = time.time()
        return self.hourly[sym]

    def _update_from(self, sym, meta, rows):
        p = meta.get('regularMarketPrice')
        if p:
            self.prices[sym] = {'price': float(p), 'ts': time.time(), 'src': 'Yahoo Finance'}
        done = self.completed(rows)
        prev = done[-1][4] if done and (not rows or done[-1][0] != rows[-1][0]) else (done[-2][4] if len(done) > 1 else None)
        if rows:
            last = rows[-1]
            self.stats[sym] = {'open': prev or last[1], 'high': float(meta.get('regularMarketDayHigh') or last[2]),
                               'low': float(meta.get('regularMarketDayLow') or last[3]), 'last': float(p or last[4]), 'ts': time.time()}

    def refresh_price(self, sym):
        try:
            meta, rows = self._chart(sym, range_='5d')
            self._update_from(sym, meta, rows)
            self.errors.pop('price', None)
        except Exception as e:
            self.errors['price'] = f'Stock prices unavailable ({e})'

    def refresh_stats(self, sym):
        self.refresh_price(sym)

    def price(self, sym):
        p = self.prices.get(sym)
        return p['price'] if p else None

    @staticmethod
    def completed(rows, now=None):
        """A daily bar counts once the US market has closed (bar time = 9:30 New York; close 16:00, +10 min)."""
        now = now or time.time()
        return [r for r in rows if r[0] + 6.5 * 3600 + 600 <= now]

    def name(self, sym):
        return self.names.get(sym) or STOCK_INDEX_FUNDS.get(sym) or sym

    def load_fng(self):
        return {}

    def fng_for(self, times):
        return None

    def fng_latest(self):
        return None

    load_news = Market.load_news


def ny_now():
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        return _dt.datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        import datetime as _dt
        return _dt.datetime.utcnow() - _dt.timedelta(hours=5)


def next_stock_close():
    """Unix time of the next weekday 16:10 New York time (holidays not counted)."""
    import datetime as _dt
    n = ny_now()
    d = n.replace(hour=16, minute=10, second=0, microsecond=0)
    if n >= d:
        d += _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += _dt.timedelta(days=1)
    return d.timestamp()


def us_market_open():
    n = ny_now()
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 30) and n.hour < 16


def fng_label(v):
    return ('Extreme fear' if v <= 20 else 'Fear' if v <= 40 else 'Neutral' if v < 60 else 'Greed' if v < 80 else 'Extreme greed')


MARKET = Market()
STOCKS = StockMarket()


def completed_daily(rows, now=None):
    now = now or time.time()
    return [r for r in rows if r[0] + DAY <= now]


def notify(title, text):
    """Show a macOS notification (best effort, never blocks the bot)."""
    if sys.platform != 'darwin' or not load_config().get('notifications', True):
        return
    def esc(x):
        return x.replace('\\', '').replace('"', "'")
    try:
        subprocess.Popen(['osascript', '-e', f'display notification "{esc(text)}" with title "Trend Rider" subtitle "{esc(title)}"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ================================================================ bot
class Bot:
    def __init__(self, market='crypto'):
        self.market = market
        self.M = MARKET if market == 'crypto' else STOCKS
        self.ann = 365 if market == 'crypto' else 252
        self.path = os.path.join(DATA, 'state.json' if market == 'crypto' else 'state_stocks.json')
        self.cfg = CFG if market == 'crypto' else CFG['stocks']
        self.state = read_json(self.path, None)
        self.paused = False
        self.last_attempt = 0
        self.last_signal = 0
        self.signal = {}   # coin -> latest decision info
        self.next_decision = None
        self.ex = None   # the exchange for real-money trading (Kraken or Binance.US), if connected
        sec = {} if market != 'crypto' else read_json(os.path.join(DATA, 'secrets.json'), {})
        if market == 'crypto' and os.environ.get('TREND_RIDER_CLOUD'):   # cloud: keys come from GitHub Secrets
            name = self.cfg['live'].get('exchange', 'kraken')
            env = 'BINANCEUS' if name == 'binanceus' else 'KRAKEN'
            if os.environ.get(f'{env}_API_KEY') and os.environ.get(f'{env}_API_SECRET'):
                sec = {'exchange': name, 'key': os.environ[f'{env}_API_KEY'], 'secret': os.environ[f'{env}_API_SECRET']}
        if sec.get('key') and sec.get('secret'):
            self.ex = EXCHANGES.get(sec.get('exchange', 'kraken'), Kraken)(sec['key'], sec['secret'])
        if not self.state:
            self.state = self.fresh_state()
            self.save()
        self.paused = self.state.get('paused', False)

    def universe(self):
        """Assets shown in the market list."""
        if self.market == 'crypto':
            return list(COINS)
        out = list(STOCK_INDEX_FUNDS)
        for c in list(self.cfg.get('custom', [])) + list(self.cfg['coins']):
            if c not in out:
                out.append(c)
        return out

    def price_list(self):
        """Everything whose live price the bot needs (the market list plus the gold fund when used)."""
        out = self.universe()
        if self.market != 'crypto' and PARK not in out and (self.parking() or self.state['holdings'].get(PARK)):
            out.append(PARK)
        return out

    # ---------- parking idle money (stocks account)
    def parking(self):
        """'GLD' when idle money should wait in the gold fund, else None (plain cash).
        If you trade GLD yourself, idle money stays cash so the two never get mixed up."""
        if self.market == 'crypto' or PARK in self.cfg['coins']:
            return None
        return PARK if self.cfg.get('park', PARK) == PARK else None

    def park_returns(self, times):
        """Daily return of the gold fund lined up with `times` (None where unknown), or None if not parking."""
        if not self.parking() or not times:
            return None
        try:
            rows = self.M.completed(self.M.load_daily(PARK, history_days=int((time.time() - times[0]) / DAY) + 10))
            self.M.errors.pop('park', None)
        except Exception as e:
            self.M.errors['park'] = f'Gold fund prices unavailable, idle money counted as cash for now ({e})'
            return None
        day = lambda t: time.strftime('%Y-%m-%d', time.gmtime(t))
        px = {day(r[0]): r[4] for r in rows}
        live = self.M.price(PARK)
        out, prev = [], None
        for i, t in enumerate(times):
            p = px.get(day(t))
            if p is None and live and i == len(times) - 1:
                p = live  # today's move so far
            out.append(p / prev - 1 if (p and prev) else None)
            prev = p if p else prev
        return out

    def park_share(self, sigs):
        """Share of the account the strategy keeps out of the market (parked in gold), or None."""
        coins = self.cfg['coins']
        if not self.parking() or not all((sigs.get(c) or {}).get('ok') for c in coins):
            return None
        return sum((1 - sigs[c]['target']) / len(coins) for c in coins)

    # ---------- state
    def fresh_state(self):
        cash = float(self.cfg['starting_cash'])
        return {'version': 1, 'created': time.time(), 'cash': cash, 'start_cash': cash,
                'holdings': {}, 'hold_qty': {}, 'hold_start_prices': {}, 'trades': [], 'equity': [],
                'log': [], 'last_decision_day': None, 'paused': False,
                'live': {'cash': 0.0, 'holdings': {}, 'trades': [], 'started': None}}

    def save(self):
        with LOCK:
            write_json(self.path, self.state)

    def log(self, msg, level='info'):
        with LOCK:
            self.state['log'].append({'ts': time.time(), 'msg': msg, 'level': level})
            self.state['log'] = self.state['log'][-400:]
        print(time.strftime('%H:%M:%S'), msg, flush=True)

    @property
    def fee(self):
        return float(self.cfg['fee_pct']) / 100

    @property
    def slip(self):
        return float(self.cfg['slippage_pct']) / 100

    def live_on(self):
        if self.market != 'crypto':
            return False  # stocks are paper-only for now
        if os.environ.get('TREND_RIDER_CLOUD') and (self.cfg['live'].get('confirm') or '').strip().upper() != 'REAL MONEY':
            return False  # in the cloud, config.json must also contain "confirm": "REAL MONEY"
        return bool(self.cfg['live'].get('enabled')) and self.ex is not None

    # ---------- valuation
    def equity(self):
        st = self.state
        v = st['cash']
        for c, q in st['holdings'].items():
            p = self.M.price(c)
            if p is None:
                return None
            v += q * p
        return v

    def hold_equity(self):
        st = self.state
        if not st['hold_qty']:
            return None
        v = st.get('hold_cash', 0.0)
        for c, q in st['hold_qty'].items():
            p = self.M.price(c)
            if p is None:
                return None
            v += q * p
        return v

    def ensure_benchmark(self):
        """'Just hold' benchmark: the same starting cash, split evenly and bought once."""
        st = self.state
        coins = self.cfg['coins']
        if st['hold_qty'] and set(st['hold_qty']) == set(coins):
            return
        if any(self.M.price(c) is None for c in coins):
            return
        base = st['start_cash'] if not st['trades'] else (self.equity() or st['start_cash'])
        w = 1 / len(coins)
        st['hold_qty'] = {c: base * w * (1 - self.fee - self.slip) / self.M.price(c) for c in coins}
        st['hold_start_prices'] = {c: self.M.price(c) for c in coins}
        st['hold_cash'] = 0.0
        st['hold_started'] = time.time()

    # ---------- signals
    def compute_signal(self, coin):
        rows = self.M.completed(self.M.load_daily(coin))
        if len(rows) < S.MIN_BARS:
            return {'coin': coin, 'ok': False, 'why': f'{coin}: only {len(rows)} days of history (needs {S.MIN_BARS}).'}
        t = [r[0] for r in rows]
        h = [r[2] for r in rows]; lo = [r[3] for r in rows]; c = [r[4] for r in rows]
        fng = self.M.fng_for(t)
        strat, mix = self.cfg['strategy'], self.cfg.get('mix')
        expo, votes = S.exposure_series(strat, h, lo, c, fng, mix, self.ann)
        floor = float(self.cfg['min_invested_pct']) / 100
        target = S.apply_floor(expo[-1], floor)
        regime = S.regime_series(c)[-1]
        # preview: what the signal would be if today closed at the live price
        preview = None
        p = self.M.price(coin)
        if p:
            ex2, _ = S.exposure_series(strat, h + [max(h[-1], p)], lo + [min(lo[-1], p)], c + [p],
                                       (fng + [fng[-1]]) if fng else None, mix, self.ann)
            preview = S.apply_floor(ex2[-1], floor)
        # history of exposure for the chart (last 400 days)
        hist = [[t[i], S.apply_floor(expo[i], floor)] for i in range(max(0, len(t) - 400), len(t))]
        return {'coin': coin, 'ok': True, 'day': t[-1], 'close': c[-1], 'target': target,
                'raw': expo[-1], 'votes': votes, 'preview': preview, 'history': hist, 'regime': regime}

    # ---------- trading
    def decide(self, reason='Daily decision', force=True):
        """Compute today's target exposure for each coin and trade toward it."""
        self.last_attempt = time.time()
        go_live = False
        coins = list(self.cfg['coins'])
        sigs = {}
        try:
            for coin in coins:
                self.M.refresh_price(coin)
                if force:
                    self.M.load_daily(coin, force=True)
                sigs[coin] = self.compute_signal(coin)
            if self.market != 'crypto' and PARK in self.price_list() and PARK not in coins:
                self.M.refresh_price(PARK)
        except Exception as e:
            self.log(f'Could not get price history, will retry in 5 minutes: {e}', 'error')
            return
        with LOCK:
            self.signal = sigs
            if any(not s['ok'] for s in sigs.values()):
                for s in sigs.values():
                    if not s['ok']:
                        self.log(f"{s['coin']}: {s['why']}", 'warn')
                return
            if any(self.M.price(c) is None for c in coins):
                self.log('Skipped the decision: no live price yet.', 'warn')
                return
            self.ensure_benchmark()
            day = min(s['day'] for s in sigs.values())
            if reason == 'Daily decision' and self.state.get('last_decision_day') == day:
                return  # exchange has not published the new daily candle yet; try again later
            if self.paused:
                self.log('Bot is paused: signals updated, no trades placed.')
                self.state['last_decision_day'] = day
                self.save()
                return
            summary = ', '.join(f"{c} {sigs[c]['target'] * 100:.0f}%" for c in coins)
            pk = self.park_share(sigs)
            if pk and pk > 0.005:
                summary += f'; idle money in gold {pk * 100:.0f}% of the account'
            self.log(f'{reason}: target invested {summary}.')
            self.rebalance_paper(sigs, reason)
            self.state['last_decision_day'] = day
            self.state['last_decision_ts'] = time.time()
            self.save()
            go_live = self.live_on()
        if go_live:   # outside the lock: the dashboard keeps working while limit orders wait for fills
            try:
                self.rebalance_live(sigs, reason)
            except Exception as e:
                self.log(f'LIVE trading error, no further live orders this round: {e}', 'error')
            with LOCK:
                self.save()

    def _targets(self, sigs, equity):
        coins = self.cfg['coins']
        w = 1 / len(coins)
        band = float(self.cfg['rebalance_band_pct']) / 100
        out = []
        for c in coins:
            out.append((c, equity * w * sigs[c]['target'], equity * w * band))
        return out

    def rebalance_paper(self, sigs, reason):
        st = self.state
        eq = self.equity()
        if eq is None:
            return
        orders = []
        targets = [(c, tv, band, sigs[c]['target']) for c, tv, band in self._targets(sigs, eq)]
        if self.market != 'crypto' and PARK not in self.cfg['coins'] and self.M.price(PARK) and (self.parking() or st['holdings'].get(PARK)):
            share = self.park_share(sigs) or 0.0   # parking switched off: sell any gold that is left
            band = float(self.cfg['rebalance_band_pct']) / 100 / len(self.cfg['coins'])
            targets.append((PARK, eq * share, eq * band if share > 0 else 0.0, share))
        tgt = {c: pct for c, _, _, pct in targets}
        for c, target_val, band, _ in targets:
            p = self.M.price(c)
            cur = st['holdings'].get(c, 0.0) * p
            diff = target_val - cur
            if abs(diff) < max(band, 1.0):
                continue
            orders.append((c, diff, p))
        for c, diff, p in sorted(orders, key=lambda o: o[1]):  # sells first
            if diff < 0:
                fill = p * (1 - self.slip)
                qty = min(st['holdings'].get(c, 0.0), -diff / p)
                gross = qty * fill
                fee = gross * self.fee
                st['holdings'][c] = st['holdings'].get(c, 0.0) - qty
                if st['holdings'][c] < 1e-12:
                    st['holdings'].pop(c)
                st['cash'] += gross - fee
                self._record('sell', c, qty, fill, gross, fee, reason, tgt[c])
            else:
                fill = p * (1 + self.slip)
                spend = min(diff, st['cash'])
                if spend < 1.0:
                    continue
                fee = spend * self.fee
                qty = (spend - fee) / fill
                st['cash'] -= spend
                st['holdings'][c] = st['holdings'].get(c, 0.0) + qty
                self._record('buy', c, qty, fill, spend, fee, reason, tgt[c])
        if not orders:
            self.log('No trade needed: already at the target.')

    def _record(self, side, coin, qty, price, usd, fee, reason, target, mode='paper', extra=None):
        t = {'ts': time.time(), 'side': side, 'coin': coin, 'qty': qty, 'price': price, 'usd': usd,
             'fee': fee, 'reason': reason, 'target': target, 'mode': mode}
        if extra:
            t.update(extra)
        book = self.state['trades'] if mode == 'paper' else self.state['live']['trades']
        book.append(t)
        del book[:-2000]
        verb = 'Bought' if side == 'buy' else 'Sold'
        tag = '' if mode == 'paper' else 'LIVE '
        aim = (f'{target * 100:.0f}% of the account waiting in gold' if coin == PARK and self.market != 'crypto' and coin not in self.cfg['coins']
               else f'{target * 100:.0f}% invested')
        notify(f'{tag}{verb} {coin}', f'${usd:,.2f} at ${price:,.2f}. Now targeting {aim}.')
        self.log(f'{tag}{verb} {qty:.6f} {coin} at ${price:,.2f} (${usd:,.2f}, fee ${fee:,.2f}). Target {aim}.',
                 'trade')

    # ---------- live (Kraken or Binance.US)
    def live_equity(self):
        lv = self.state['live']
        v = lv['cash']
        for c, q in lv['holdings'].items():
            p = self.M.price(c)
            if p is None:
                return None
            v += q * p
        return v

    def rebalance_live(self, sigs, reason):
        """Trades only the bot's own budget. Never sells coins the bot did not buy.
        Sells first, then buys with the freed cash. Each order is a limit order first (the exchange's lower maker
        fee); whatever has not filled after `limit_wait_min` minutes goes out as an instant (market) order."""
        if not LIVE_LOCK.acquire(blocking=False):
            self.log('LIVE: the previous orders are still working; nothing new sent this round.', 'warn')
            return
        try:
            lv = self.state['live']
            lcfg = self.cfg['live']
            cap = float(lcfg['max_trade_usd'])
            wait = float(lcfg.get('limit_wait_min', 30)) * 60 if lcfg.get('limit_orders', True) else 0.0
            eq = self.live_equity()
            if eq is None:
                return
            plan = self._targets(sigs, eq)
            ex = self.ex
            bal = ex.balances()
            sells = []
            for c, target_val, band in plan:
                p = self.M.price(c)
                diff = target_val - lv['holdings'].get(c, 0.0) * p
                if diff >= 0 or -diff < max(band, 10.0):
                    continue
                info = ex.pair_info(ex.pair(c))
                coin_bal = ex.coin_balance(bal, c)
                qty = min(lv['holdings'].get(c, 0.0), coin_bal, min(-diff, cap) / p)
                qty = math.floor(qty * 10 ** info['lot_decimals']) / 10 ** info['lot_decimals']
                if qty < info['ordermin'] or qty * p < max(info['costmin'], 5):
                    self.log(f'LIVE: sell of {c} too small for {ex.NAME}, skipped.', 'warn')
                    continue
                sells.append({'coin': c, 'pair': ex.pair(c), 'side': 'sell', 'volume': qty, 'info': info,
                              'price': p, 'est_fee': self.fee})
            self._live_execute(sells, wait, sigs, reason)
            usd_bal = ex.usd_balance(ex.balances())
            cash = min(lv['cash'], usd_bal * 0.995)
            buys = []
            for c, target_val, band in plan:
                p = self.M.price(c)
                diff = target_val - lv['holdings'].get(c, 0.0) * p
                if diff <= 0 or diff < max(band, 10.0):
                    continue
                info = ex.pair_info(ex.pair(c))
                spend = min(diff, cap, cash)
                qty = spend * (1 - self.fee) / (p * (1 + self.slip))
                qty = math.floor(qty * 10 ** info['lot_decimals']) / 10 ** info['lot_decimals']
                if qty < info['ordermin'] or qty * p < max(info['costmin'], 5):
                    self.log(f'LIVE: buy of {c} too small for {ex.NAME} (or budget used up), skipped.', 'warn')
                    continue
                cash -= spend
                buys.append({'coin': c, 'pair': ex.pair(c), 'side': 'buy', 'volume': qty, 'info': info,
                             'price': p, 'est_fee': self.fee})
            self._live_execute(buys, wait, sigs, reason)
        finally:
            LIVE_LOCK.release()

    def _live_execute(self, orders, wait, sigs, reason):
        if not orders:
            return
        if wait:
            self.log(f"LIVE: {len(orders)} limit order(s) placed at the best price on {self.ex.NAME}'s book; "
                     f'anything not filled within {wait / 60:.0f} minutes becomes an instant order.')
        for f in self.ex.limit_first(orders, wait_s=wait, log=self.log):
            if f['abort'] or f['vol'] <= 0:
                continue
            c = f['coin']
            with LOCK:
                lv = self.state['live']
                lv['holdings'][c] = max(0.0, lv['holdings'].get(c, 0.0) + f['coin_delta'])   # fees included
                if lv['holdings'][c] < 10 ** -f['info']['lot_decimals']:
                    lv['holdings'].pop(c)
                lv['cash'] += f['cash_delta']
                self._record(f['side'], c, f['vol'], f['price'], f['cost'], f['fee'], reason, sigs[c]['target'], 'live',
                             {'txid': f['txids'], 'confirmed': f['confirmed'],
                              'limit_share': round(f['maker_vol'] / f['vol'], 3) if f['vol'] else 0.0})
                self.save()

    # ---------- loop
    def tick_equity(self):
        eq, he = self.equity(), self.hold_equity()
        if eq is None:
            return
        with LOCK:
            pts = self.state['equity']
            now = time.time()
            if not pts or now - pts[-1][0] >= 600:
                pts.append([now, round(eq, 2), round(he, 2) if he else None,
                            round(self.live_equity(), 2) if self.live_on() and self.live_equity() is not None else None])
                if len(pts) > 6000:
                    old, new = pts[:3000], pts[3000:]
                    self.state['equity'] = old[::3] + new
                self.save()

    def due(self):
        """A new daily candle has closed since the last decision."""
        last = self.state.get('last_decision_day')
        if self.market != 'crypto':
            try:
                done = self.M.completed(self.M.load_daily(self.cfg['coins'][0]))
            except Exception:
                return False
            return bool(done) and (last is None or done[-1][0] > last)
        today_open = (int(time.time()) // DAY) * DAY
        latest_closed = today_open - DAY
        return last is None or last < latest_closed

    def code_changed(self):
        """True when the app's code was updated on disk since this copy started."""
        try:
            m = max(os.path.getmtime(os.path.join(HERE, f)) for f in os.listdir(HERE) if f.endswith('.py'))
        except Exception:
            return False
        if not hasattr(self, '_code_mtime'):
            self._code_mtime = m
        return m > self._code_mtime + 1

    def run(self):
        if self.market != 'crypto':
            self.log(f'Stocks account started (paper). Strategy: {strategy_label(self.cfg)}. Stocks: {", ".join(self.cfg["coins"])}.')
        else:
          self.log(f'Trend Rider {VERSION} started in {"LIVE + paper" if self.live_on() else "paper"} mode. '
                 f'Strategy: {strategy_label(self.cfg)}. Coins: {", ".join(self.cfg["coins"])}.')
        last_stats = 0
        while True:
            try:
                if self.market != 'crypto':
                    every = 60 if us_market_open() else 1800
                    if time.time() - last_stats > every:
                        last_stats = time.time()
                        for c in self.price_list():
                            self.M.refresh_price(c)
                            time.sleep(0.4)
                    if self.due() and time.time() - self.last_attempt > 300:
                        self.decide()
                    elif time.time() - self.last_signal > 600:
                        self.last_signal = time.time()
                        sig = {c: self.compute_signal(c) for c in self.cfg['coins']}
                        with LOCK:
                            self.signal = sig
                    self.ensure_benchmark()
                    self.tick_equity()
                    if time.time() - getattr(self, 'race_ts', 0) > 1800:
                        self.race()
                    self.M.errors.pop('bot', None)
                    time.sleep(30)
                    continue
                for c in self.cfg['coins']:
                    self.M.refresh_price(c)
                if time.time() - last_stats > 60:
                    last_stats = time.time()
                    for c in COINS:
                        try:
                            self.M.refresh_stats(c)
                            self.M.errors.pop('stats', None)
                        except Exception as e:
                            self.M.errors['stats'] = str(e)
                        time.sleep(0.15)
                if self.due() and (time.time() % DAY) > 90 and time.time() - self.last_attempt > 300:
                    self.decide()
                elif time.time() - self.last_signal > 300:
                    self.last_signal = time.time()
                    sig = {c: self.compute_signal(c) for c in self.cfg['coins']}
                    with LOCK:
                        self.signal = sig
                self.ensure_benchmark()
                self.tick_equity()
                if time.time() - getattr(self, 'race_ts', 0) > 600:
                    self.race()
                if self.market == 'crypto' and self.code_changed():
                    self.log('Trend Rider was updated. Restarting with the new version…')
                    self.save()
                    time.sleep(1)
                    os.execv(sys.executable, [sys.executable, os.path.join(HERE, 'server.py')])
                self.M.errors.pop('bot', None)
            except Exception as e:
                self.M.errors['bot'] = str(e)
                self.log(f'Problem in the bot loop (will retry): {e}', 'error')
                traceback.print_exc()
            time.sleep(15)

    # ---------- strategy race: every strategy on the same coins, same fees
    def race(self):
        if time.time() - getattr(self, 'race_ts', 0) < 600 and getattr(self, 'race_cache', None):
            return self.race_cache
        self.race_ts = time.time()
        coins = self.cfg['coins']
        cost = self.fee + self.slip
        names = list(S.BASE_STRATEGIES) + (['mix'] if self.cfg['strategy'] == 'mix' else [])
        windows = {'since_start': self.state['created'], 'last_year': time.time() - 365 * DAY, 'last_3_years': time.time() - 3 * 365 * DAY}
        out = {k: {} for k in windows}
        try:
            per_coin = {}
            for coin in coins:
                rows = self.M.completed(self.M.load_daily(coin, history_days=3 * 365 + (S.MIN_BARS + 60) * (1 if self.market == 'crypto' else 2)))
                p = self.M.price(coin)
                t = [r[0] for r in rows]; h = [r[2] for r in rows]; lo = [r[3] for r in rows]; c = [r[4] for r in rows]
                if p:  # count today's move so far at the live price
                    t, h, lo, c = t + [int(time.time())], h + [max(h[-1], p)], lo + [min(lo[-1], p)], c + [p]
                per_coin[coin] = (t, h, lo, c, self.M.fng_for(t), self.park_returns(t))
            for wname, wstart in windows.items():
                for st in names:
                    rets = []
                    for coin, (t, h, lo, c, fng, park) in per_coin.items():
                        si = next((i for i, x in enumerate(t) if x >= wstart - DAY), len(t) - 1)
                        si = min(max(si, S.MIN_BARS), len(t) - 1)
                        r = S.backtest(t, h, lo, c, st, cost=cost, floor=float(self.cfg['min_invested_pct']) / 100 if st != 'hold' else 0,
                                       start_index=si, fng=fng, mix=self.cfg.get('mix'), ann=self.ann, idle=park if st != 'hold' else None)
                        rets.append(r['equity'][-1] - 1 if st != 'hold' else r['hold'][-1] - 1)
                    out[wname][st] = sum(rets) / len(rets) if rets else None
        except Exception as e:
            out['error'] = str(e)
        self.race_cache = out
        return out

    # ---------- views
    def snapshot(self):
        with LOCK:
            st = self.state
            eq, he = self.equity(), self.hold_equity()
            positions = []
            for c in self.cfg['coins']:
                p = self.M.price(c)
                q = st['holdings'].get(c, 0.0)
                positions.append({'coin': c, 'name': self.M.name(c), 'qty': q, 'price': p,
                                  'value': q * p if p else None, 'target': (self.signal.get(c) or {}).get('target')})
            if self.market != 'crypto' and PARK not in self.cfg['coins'] and (self.parking() or st['holdings'].get(PARK)):
                p = self.M.price(PARK)
                q = st['holdings'].get(PARK, 0.0)
                positions.append({'coin': PARK, 'name': 'Gold fund: idle money waits here', 'qty': q, 'price': p,
                                  'value': q * p if p else None, 'target': self.park_share(self.signal) if self.parking() else 0.0, 'parked': True})
            now = time.time()
            next_close = (int(now) // DAY + 1) * DAY + 90 if self.market == 'crypto' else next_stock_close()
            return {
                'version': VERSION, 'now': now, 'config': self.public_config(),
                'market': self.market,
                'coins': {k: self.M.name(k) for k in self.universe()},
                'index_funds': STOCK_INDEX_FUNDS if self.market != 'crypto' else None,
                'strategies': S.STRATEGIES,
                'paused': self.paused, 'live_on': self.live_on(), 'has_keys': self.ex is not None,
                'exchange': self.ex.NAME if self.ex else None,
                'equity': eq, 'hold_equity': he, 'cash': st['cash'], 'start_cash': st['start_cash'],
                'created': st['created'], 'positions': positions,
                'signals': self.signal, 'next_decision': next_close,
                'last_decision_ts': st.get('last_decision_ts'),
                'trades': st['trades'][-200:], 'log': st['log'][-150:],
                'equity_curve': st['equity'],
                'prices': self.M.prices, 'stats': self.M.stats, 'errors': self.M.errors,
                'live': {'cash': st['live']['cash'], 'holdings': st['live']['holdings'],
                         'trades': st['live']['trades'][-100:], 'equity': self.live_equity() if self.live_on() else None,
                         'started': st['live'].get('started')},
                'background': background_status(),
                'fng': self.M.fng_latest() if (self.market == 'crypto' and getattr(self.M, 'fng', None)) else None,
                'us_market_open': us_market_open() if self.market != 'crypto' else None,
                'park': self.parking(),
                'race': getattr(self, 'race_cache', None),
                'mix_choices': {k: S.STRATEGIES[k] for k in S.BASE_STRATEGIES},
                'portfolio': portfolio_summary(),
            }

    def public_config(self):
        c = json.loads(json.dumps(self.cfg))
        return c


BOT = None
BOTS = {}


def portfolio_summary():
    """Both paper accounts together (crypto + stocks)."""
    out = {}
    for name, b in BOTS.items():
        try:
            out[name] = {'equity': b.equity(), 'hold_equity': b.hold_equity(), 'start': b.state['start_cash']}
        except Exception:
            pass
    return out


def strategy_label(cfg):
    if cfg['strategy'] == 'mix':
        m = cfg.get('mix', {})
        return f"Mix ({m.get('a_pct', 50):.0f}% {S.STRATEGIES[m.get('a', 'auto')]} + {100 - m.get('a_pct', 50):.0f}% {S.STRATEGIES[m.get('b', 'steady')]})"
    return S.STRATEGIES[cfg['strategy']]


# ================================================================ backtest API
def run_backtest(coin, strategy, years, fee_pct, floor_pct, mix=None, bot=None):
    bot = bot or BOT
    M = bot.M
    days = int(years * 365) + (S.MIN_BARS + 60) * (1 if bot.market == 'crypto' else 2)
    rows = M.completed(M.load_daily(coin, history_days=days))
    if len(rows) < S.MIN_BARS + 30:
        raise ValueError(f'Not enough {coin} history yet ({len(rows)} days).')
    t = [r[0] for r in rows]; h = [r[2] for r in rows]; lo = [r[3] for r in rows]; c = [r[4] for r in rows]
    start_t = time.time() - years * 365 * DAY
    si = next((i for i, x in enumerate(t) if x >= start_t), S.MIN_BARS)
    si = max(si, S.MIN_BARS)
    cost = (fee_pct + 0.05) / 100
    out = {'coin': coin, 'strategy': strategy, 'from': t[si - 1], 'to': t[-1], 'cost_pct': cost * 100}
    park = bot.park_returns(t) if bot.market != 'crypto' and strategy != 'hold' else None
    out['parked_in'] = PARK if park else None
    res = S.backtest(t, h, lo, c, strategy, cost=cost, floor=floor_pct / 100, start_index=si, fng=M.fng_for(t), mix=mix, ann=bot.ann, idle=park)
    step = max(1, len(res['times']) // 700)
    out['curve'] = [[res['times'][i], res['equity'][i], res['hold'][i], res['exposure'][i]]
                    for i in range(0, len(res['times']), step)] + [[res['times'][-1], res['equity'][-1], res['hold'][-1], res['exposure'][-1]]]
    out['stats'] = res['stats']; out['hold_stats'] = res['hold_stats']; out['trades'] = res['trades']
    yb, yh = S.yearly(res['times'], res['equity']), S.yearly(res['times'], res['hold'])
    out['yearly'] = [{'year': a['year'], 'bot': a['ret'], 'hold': b['ret']} for a, b in zip(yb, yh)]
    out['first_day_available'] = t[0]
    return out


# ================================================================ background service (launchd)
LABEL = 'com.trendrider.bot'
PLIST = os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')
BG_APP = os.path.join(SUPPORT, 'app')  # macOS blocks background apps from Documents, so run a copy from here


def background_status():
    running_copy = os.path.abspath(HERE) == os.path.abspath(BG_APP)
    return {'supported': sys.platform == 'darwin', 'installed': os.path.exists(PLIST), 'this_is_background': running_copy}


def _launchctl(args_new, args_old):
    uid = str(os.getuid())
    r = subprocess.run(['launchctl'] + [a.replace('UID', uid) for a in args_new], capture_output=True)
    if r.returncode != 0:
        subprocess.run(['launchctl'] + args_old, capture_output=True)


def background_install():
    if sys.platform != 'darwin':
        raise ValueError('Background mode is only set up for macOS.')
    if os.path.abspath(HERE) != os.path.abspath(BG_APP):
        shutil.copytree(HERE, BG_APP, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    pl = {'Label': LABEL, 'ProgramArguments': [sys.executable, os.path.join(BG_APP, 'server.py')],
          'RunAtLoad': True, 'KeepAlive': True, 'WorkingDirectory': SUPPORT, 'ProcessType': 'Background',
          'StandardOutPath': os.path.join(DATA, 'bot.log'), 'StandardErrorPath': os.path.join(DATA, 'bot.log')}
    with open(PLIST, 'wb') as f:
        plistlib.dump(pl, f)
    # stop this copy, then let launchd start the background copy
    subprocess.Popen(['/bin/sh', '-c',
                      f'sleep 2; launchctl bootstrap gui/$(id -u) "{PLIST}" 2>/dev/null || launchctl load -w "{PLIST}"'],
                     start_new_session=True)
    threading.Timer(1.0, lambda: os._exit(0)).start()


def background_uninstall():
    if os.path.exists(PLIST):
        subprocess.Popen(['/bin/sh', '-c',
                          f'sleep 1; launchctl bootout gui/$(id -u) "{PLIST}" 2>/dev/null || launchctl unload -w "{PLIST}"; rm -f "{PLIST}"'],
                         start_new_session=True)


# ================================================================ HTTP
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
        '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json', '.txt': 'text/plain; charset=utf-8',
        '.ico': 'image/x-icon'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'TrendRider'

    def log_message(self, *a):
        pass

    def _allowed_host(self):
        host = (self.headers.get('Host') or '').lower()
        port = BOT.cfg['port']
        return host in (f'127.0.0.1:{port}', f'localhost:{port}')

    def _send(self, code, body, ctype='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, {'error': 'Open the app at http://127.0.0.1:%d' % BOT.cfg['port']})
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        bot = BOTS.get(q.get('market', 'crypto'), BOT)
        try:
            if u.path == '/api/state':
                return self._send(200, bot.snapshot())
            if u.path == '/api/candles':
                coin = q.get('coin', 'BTC')
                if coin not in bot.universe():
                    return self._send(400, {'error': 'Unknown asset'})
                if q.get('tf') == '1h':
                    rows = bot.M.load_hourly(coin)
                else:
                    rows = bot.M.load_daily(coin)[-500:]
                return self._send(200, {'coin': coin, 'rows': rows})
            if u.path == '/api/backtest':
                coin = q.get('coin', 'BTC')
                if coin not in bot.universe():
                    return self._send(400, {'error': 'Unknown asset'})
                years = max(1.0, min(30.0, float(q.get('years', 5))))
                fee = max(0.0, min(2.0, float(q.get('fee', bot.cfg['fee_pct']))))
                floor = max(0.0, min(100.0, float(q.get('floor', 0))))
                names = ['growth', 'steady', 'auto', 'cruise'] + (['mix'] if bot.cfg['strategy'] == 'mix' else [])
                res = {'runs': [dict(run_backtest(coin, s, years, fee, floor, bot.cfg.get('mix'), bot), label=strategy_label(dict(bot.cfg, strategy=s))) for s in names]}
                return self._send(200, res)
            if u.path == '/api/news':
                return self._send(200, bot.M.load_news())
            if u.path == '/api/research':
                return self._send(200, read_json(os.path.join(HERE, 'research.json'), {}))
            path = u.path if u.path != '/' else '/index.html'
            full = os.path.normpath(os.path.join(STATIC, path.lstrip('/')))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                return self._send(404, {'error': 'Not found'})
            with open(full, 'rb') as f:
                return self._send(200, f.read(), MIME.get(os.path.splitext(full)[1], 'application/octet-stream'))
        except (NetError, ValueError, KrakenError, BinanceUSError) as e:
            return self._send(502, {'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {'error': str(e)})

    def do_POST(self):
        # Only the app's own page may change things: custom header + same-origin checks.
        origin = self.headers.get('Origin')
        port = BOT.cfg['port']
        if (not self._allowed_host() or self.headers.get('X-TrendRider') != '1'
                or (origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'))):
            return self._send(403, {'error': 'Blocked: requests must come from the Trend Rider page.'})
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        except Exception:
            return self._send(400, {'error': 'Bad JSON'})
        p = urllib.parse.urlparse(self.path).path
        try:
            return self._send(200, self.route_post(p, body))
        except (NetError, ValueError, KrakenError, BinanceUSError) as e:
            return self._send(400, {'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {'error': str(e)})

    def route_post(self, p, b):
        bot = BOTS.get(b.get('market', 'crypto'), BOT)
        if bot.market != 'crypto' and p.startswith('/api/live/'):
            raise ValueError('Live trading is only available for crypto (Kraken or Binance.US) right now.')
        if p == '/api/decide':
            threading.Thread(target=bot.decide, args=('Manual check',), daemon=True).start()
            return {'ok': True}
        if p == '/api/pause':
            bot.paused = bool(b.get('paused', True))
            bot.state['paused'] = bot.paused
            bot.save()
            bot.log('Bot paused: it will not place trades.' if bot.paused else 'Bot resumed.')
            return {'ok': True}
        if p == '/api/settings':
            cfg = bot.cfg
            reset_needed = False
            if 'custom' in b and bot.market != 'crypto':
                custom = []
                for t in b['custom'][:8]:
                    t = re.sub(r'[^A-Z0-9.\-^=]', '', str(t).upper())[:10]
                    if t and t not in custom and t not in STOCK_INDEX_FUNDS:
                        try:  # check the ticker exists (without touching the bot's error banner)
                            meta, rows = bot.M._chart(t, range_='5d')
                            bot.M._update_from(t, meta, rows)
                        except Exception:
                            rows = []
                        if not rows or bot.M.price(t) is None:
                            raise ValueError(f'Could not find {t} on Yahoo Finance. Check the ticker.')
                        custom.append(t)
                cfg['custom'] = custom
            if 'coins' in b:
                allowed = set(COINS) if bot.market == 'crypto' else set(bot.universe())
                coins = [c for c in b['coins'] if c in allowed][:8]
                if not coins:
                    raise ValueError('Pick at least one coin.' if bot.market == 'crypto' else 'Pick at least one stock or fund.')
                if coins != cfg['coins']:
                    cfg['coins'] = coins
                    reset_needed = True
            if 'strategy' in b:
                if b['strategy'] not in S.STRATEGIES:
                    raise ValueError('Unknown strategy.')
                cfg['strategy'] = b['strategy']
            if 'mix' in b and isinstance(b['mix'], dict):
                m = b['mix']
                a, bb = m.get('a', 'auto'), m.get('b', 'steady')
                if a not in S.BASE_STRATEGIES or bb not in S.BASE_STRATEGIES or a == bb:
                    raise ValueError('Pick two different strategies for the mix.')
                pct = float(m.get('a_pct', 50))
                if not 10 <= pct <= 90:
                    raise ValueError('The mix split must be between 10% and 90%.')
                cfg['mix'] = {'a': a, 'b': bb, 'a_pct': pct}
            if 'notifications' in b:
                cfg['notifications'] = bool(b['notifications'])
            if 'park' in b and bot.market != 'crypto':
                if b['park'] not in (PARK, 'cash'):
                    raise ValueError('Idle money can wait in gold (GLD) or in cash.')
                cfg['park'] = b['park']
            for k, lo, hi in (('fee_pct', 0, 2), ('slippage_pct', 0, 1), ('min_invested_pct', 0, 100),
                              ('rebalance_band_pct', 1, 25), ('starting_cash', 100, 10_000_000)):
                if k in b:
                    v = float(b[k])
                    if not lo <= v <= hi:
                        raise ValueError(f'{k} must be between {lo} and {hi}.')
                    cfg[k] = v
            save_config(CFG)
            bot.race_ts = 0
            bot.log(f"Settings saved: {strategy_label(cfg)}, {'coins' if bot.market == 'crypto' else 'stocks'} {', '.join(cfg['coins'])}, "
                    f"fee {cfg['fee_pct']}%, minimum invested {cfg['min_invested_pct']:.0f}%"
                    + (f", idle money in {'gold (GLD)' if bot.parking() else 'cash'}." if bot.market != 'crypto' else '.'))
            if reset_needed:
                bot.state['hold_qty'] = {}
            threading.Thread(target=bot.decide, args=('Settings changed',), daemon=True).start()
            return {'ok': True}
        if p == '/api/reset':
            with LOCK:
                if 'starting_cash' in b:
                    bot.cfg['starting_cash'] = max(100.0, float(b['starting_cash']))
                    save_config(CFG)
                live = bot.state.get('live')
                bot.state = bot.fresh_state()
                bot.state['live'] = live or bot.state['live']
                bot.save()
            bot.log(f"Paper account reset to ${bot.cfg['starting_cash']:,.0f}.")
            threading.Thread(target=bot.decide, args=('Fresh start',), daemon=True).start()
            return {'ok': True}
        if p == '/api/live/keys':
            key, secret = (b.get('key') or '').strip(), (b.get('secret') or '').strip()
            if not key or not secret:
                raise ValueError('Paste both the API key and the private key.')
            name = b.get('exchange') or 'kraken'
            if name not in EXCHANGES:
                raise ValueError('Choose Kraken or Binance.US.')
            if bot.live_on():
                raise ValueError('Stop live trading before changing exchange keys.')
            k = EXCHANGES[name](key, secret)
            bal = k.balances()  # fails loudly if wrong
            write_json(os.path.join(DATA, 'secrets.json'), {'exchange': name, 'key': key, 'secret': secret}, private=True)
            bot.ex = k
            bot.cfg['live']['exchange'] = name
            save_config(CFG)
            bot.log(f'{k.NAME} connected (keys saved on this Mac only).')
            return {'ok': True, 'balances': bal}
        if p == '/api/live/test':
            if not bot.ex:
                raise ValueError('Connect an exchange first.')
            return {'ok': True, 'balances': bot.ex.balances()}
        if p == '/api/live/forget':
            try:
                os.remove(os.path.join(DATA, 'secrets.json'))
            except FileNotFoundError:
                pass
            name = bot.ex.NAME if bot.ex else 'Exchange'
            bot.ex = None
            bot.cfg['live']['enabled'] = False
            save_config(CFG)
            bot.log(f'{name} keys removed. Live trading is off.')
            return {'ok': True}
        if p == '/api/live/enable':
            if not bot.ex:
                raise ValueError('Connect an exchange first.')
            if (b.get('confirm') or '').strip().upper() != 'REAL MONEY':
                raise ValueError('Type REAL MONEY to confirm.')
            budget = float(b.get('budget_usd', 0)); cap = float(b.get('max_trade_usd', 0) or budget)
            if budget < 20 or cap < 10:
                raise ValueError('Budget must be at least $20 and the daily limit at least $10.')
            usd = bot.ex.usd_balance(bot.ex.balances())
            if usd < budget:
                raise ValueError(f'Your {bot.ex.NAME} account has ${usd:,.2f} USD, less than the ${budget:,.2f} budget.')
            with LOCK:
                bot.cfg['live'].update({'enabled': True, 'budget_usd': budget, 'max_trade_usd': cap})
                save_config(CFG)
                bot.state['live'] = {'cash': budget, 'holdings': {}, 'trades': bot.state['live'].get('trades', []),
                                     'started': time.time()}
                bot.save()
            bot.log(f'LIVE trading ON with a ${budget:,.0f} budget (moves at most ${cap:,.0f} per coin per day).', 'warn')
            threading.Thread(target=bot.decide, args=('Live trading switched on',), daemon=True).start()
            return {'ok': True}
        if p == '/api/live/disable':
            bot.cfg['live']['enabled'] = False
            save_config(CFG)
            bot.log(f"Live trading OFF. Coins already bought stay in your {bot.ex.NAME if bot.ex else 'exchange'} account.", 'warn')
            return {'ok': True}
        if p == '/api/quit':
            if background_status()['installed']:
                raise ValueError('Background mode is on, so the bot would restart itself. Turn off background mode instead.')
            bot.log('Trend Rider was stopped from the Settings page.')
            bot.save()
            threading.Timer(0.8, lambda: os._exit(0)).start()
            return {'ok': True}
        if p == '/api/background/install':
            bot.log('Background mode on: Trend Rider now runs without a Terminal window and starts when you log in.')
            bot.save()
            background_install()
            return {'ok': True, 'restarting': True}
        if p == '/api/background/uninstall':
            background_uninstall()
            bot.log('Background mode off. Start the app with "Start Trend Rider" from now on.')
            return {'ok': True}
        raise ValueError('Unknown action')


def main():
    global BOT
    ap = argparse.ArgumentParser()
    ap.add_argument('--open', action='store_true', help='open the dashboard in your browser')
    args = ap.parse_args()
    ensure_dirs()
    global CFG, BOTS
    CFG = load_config()
    port = CFG['port']
    url = f'http://127.0.0.1:{port}'
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError:
        print(f'Trend Rider is already running. Opening {url}', flush=True)
        if args.open:
            webbrowser.open(url)
        return
    BOT = Bot('crypto')
    BOTS = {'crypto': BOT, 'stocks': Bot('stocks')}
    for b_ in BOTS.values():
        threading.Thread(target=b_.run, daemon=True).start()
    print(f'\n  Trend Rider is running: {url}\n  Keep this window open (or turn on background mode in Settings).\n', flush=True)
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('Stopped.')


if __name__ == '__main__':
    main()
