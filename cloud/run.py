"""Trend Rider cloud run (GitHub Actions).

Runs one pass of the bot: refresh prices, make the daily decision if a new daily
candle has closed, record the portfolio value, update the strategy race, news and
backtests, and write everything the dashboard needs into data/. The workflow then
commits data/ back to the repository and GitHub Pages serves the dashboard.

Settings live in data/config.json (edit it on github.com). Real-money trading needs
KRAKEN_API_KEY and KRAKEN_API_SECRET repository secrets AND, in data/config.json,
"live": {"enabled": true, "confirm": "REAL MONEY", "budget_usd": ..., "max_trade_usd": ...}.
"""
import json, os, sys, time, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'app')
sys.path.insert(0, APP)
os.environ['TREND_RIDER_CLOUD'] = '1'

import server  # noqa: E402  (reuses the exact same bot code as the Mac app)
import strategy as S  # noqa: E402

DATA = os.path.join(ROOT, 'data')
server.SUPPORT = ROOT
server.DATA = DATA
server.CACHE = os.path.join('/tmp', 'trend-rider-cache')
DAY = server.DAY


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f, separators=(',', ':'), default=str)


def slim(run, label):
    """Smaller backtest files: ~350 points, 5 significant digits (the repo grows with every save)."""
    step = max(1, len(run['curve']) // 350)
    pts = run['curve'][::step] + ([run['curve'][-1]] if (len(run['curve']) - 1) % step else [])
    run['curve'] = [[p[0]] + [float(f'{v:.5g}') for v in p[1:]] for p in pts]
    run['label'] = label
    return run


def build_index():
    """The Pages site is the Mac app's page with paths pointing into app/static."""
    src = open(os.path.join(APP, 'static', 'index.html')).read()
    for name in ('style.css', 'app.js', 'vendor/lightweight-charts.standalone.production.js'):
        src = src.replace(f'"/{name}"', f'"app/static/{name}"')
    src = src.replace('<script src="app/static/vendor', '<script>window.TREND_RIDER_CLOUD = true;</script>\n<script src="app/static/vendor', 1)
    with open(os.path.join(ROOT, 'index.html'), 'w') as f:
        f.write(src)


def run_market(bot, t0):
    """One pass for one account (crypto or stocks). Returns its dashboard snapshot."""
    M = bot.M
    sfx = '' if bot.market == 'crypto' else '_stocks'
    if bot.market == 'crypto':
        for c in bot.cfg['coins']:
            M.refresh_price(c)
        for c in server.COINS:
            try:
                M.refresh_stats(c)
            except Exception as e:
                M.errors['stats'] = str(e)
            time.sleep(0.15)
        M.load_fng()
    else:
        for c in bot.universe():
            M.refresh_price(c)
            time.sleep(0.3)

    # first live run: the bot's real-money budget starts as cash (crypto only)
    if bot.live_on() and not bot.state['live'].get('started'):
        budget = float(bot.cfg['live'].get('budget_usd', 0))
        bal = bot.kraken.balances()
        usd = sum(bal.get(k, 0.0) for k in ('ZUSD', 'USD'))
        if usd >= budget >= 20:
            bot.state['live'] = {'cash': budget, 'holdings': {}, 'trades': bot.state['live'].get('trades', []), 'started': time.time()}
            bot.log(f'LIVE trading ON in the cloud with a ${budget:,.0f} budget.', 'warn')
        else:
            bot.log(f'LIVE trading NOT started: Kraken has ${usd:,.2f} USD, budget is ${budget:,.2f} (minimum $20).', 'error')
            bot.cfg['live']['enabled'] = False

    if bot.due() and (bot.market != 'crypto' or (time.time() % DAY) > 90):
        bot.decide('Daily decision')
    else:
        bot.signal = {c: bot.compute_signal(c) for c in bot.cfg['coins']}
    bot.ensure_benchmark()
    bot.tick_equity()
    bot.race()
    bot.save()

    write(os.path.join(DATA, f'news{sfx}.json'), M.load_news())

    if bot.market != 'crypto':  # the web page can't fetch stock prices itself, so save recent candles
        for c in bot.universe():
            try:
                write(os.path.join(DATA, 'candles', f'{c}.json'), {'daily': [[r[0]] + [float(f'{v:.6g}') for v in r[1:]] for r in M.load_daily(c)[-300:]]})
            except Exception as e:
                print('candles failed', c, e)

    # backtests for the dashboard's Backtest tab (refreshed weekly or when settings change)
    prefix = '' if bot.market == 'crypto' else 'stocks_'
    meta_path = os.path.join(DATA, 'backtest', f'{prefix}meta.json')
    meta = server.read_json(meta_path, {})
    key = [bot.cfg['coins'], bot.cfg['strategy'], bot.cfg.get('mix'), bot.cfg['fee_pct'], bot.cfg['min_invested_pct']]
    if time.time() - meta.get('ts', 0) > 7 * DAY - 3600 or meta.get('key') != key:
        names = ['growth', 'steady', 'auto', 'cruise'] + (['mix'] if bot.cfg['strategy'] == 'mix' else [])
        for coin in bot.cfg['coins']:
            for years in (1, 3, 5, 12):
                try:
                    runs = [slim(server.run_backtest(coin, s, years, float(bot.cfg['fee_pct']), float(bot.cfg['min_invested_pct']), bot.cfg.get('mix'), bot),
                                 server.strategy_label(dict(bot.cfg, strategy=s))) for s in names]
                    write(os.path.join(DATA, 'backtest', f'{prefix}{coin}_{years}.json'), {'runs': runs})
                except Exception as e:
                    print('backtest failed', bot.market, coin, years, e)
        write(meta_path, {'ts': time.time(), 'key': key})

    snap = bot.snapshot()
    snap['cloud'] = {'run_ts': time.time(), 'run_seconds': round(time.time() - t0, 1),
                     'repo': os.environ.get('GITHUB_REPOSITORY', '')}
    snap['raw'] = {'holdings': bot.state['holdings'], 'hold_qty': bot.state.get('hold_qty', {}),
                   'hold_cash': bot.state.get('hold_cash', 0.0)}
    snap['background'] = {'supported': False, 'installed': False}
    write(os.path.join(DATA, f'dashboard{sfx}.json'), snap)
    return snap


def main():
    t0 = time.time()
    server.ensure_dirs()
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(os.path.join(DATA, 'config.json')):
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        cfg['live'] = {'enabled': False, 'confirm': '', 'budget_usd': 100.0, 'max_trade_usd': 100.0}
        cfg.pop('port', None)
        write(os.path.join(DATA, 'config.json'), cfg)
    server.CFG = server.load_config()
    if 'stocks' not in server.read_json(os.path.join(DATA, 'config.json'), {}):
        full = server.read_json(os.path.join(DATA, 'config.json'), {})
        full['stocks'] = server.CFG['stocks']
        with open(os.path.join(DATA, 'config.json'), 'w') as f:
            json.dump(full, f, indent=2)
    crypto = server.Bot('crypto')
    stocks = server.Bot('stocks')
    server.BOT = crypto
    server.BOTS = {'crypto': crypto, 'stocks': stocks}
    failures = []
    for bot in (crypto, stocks):
        try:
            snap = run_market(bot, t0)
            print(f"{bot.market}: equity {snap['equity']} · hold {snap['hold_equity']} · errors {snap['errors']}")
        except Exception:
            traceback.print_exc()
            failures.append(bot.market)
    build_index()
    print(f'done in {time.time() - t0:.1f}s')
    if failures:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
