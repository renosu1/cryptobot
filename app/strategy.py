"""Trend Rider strategies. Pure Python (no packages needed).

Both strategies use DAILY candles and decide once per day after the daily close
(00:00 UTC). They only ever hold coins or cash: no borrowing, no shorting.

GROWTH  - five exponential-moving-average (EMA) crossovers vote. Each pair says
          "uptrend" when the faster average is above the slower one. The share
          of votes that say "uptrend" is how much of the money is invested
          (0%, 20%, 40%, 60%, 80% or 100%).
STEADY  - sixteen trend checks vote (price above its average, average crossover,
          momentum and breakout, each over 25/50/100/200 days). The result is then
          scaled down when the market is extra jumpy, so swings stay near 80% a year.
AUTO    - reads the market regime. Uses Growth normally, switches to Steady while
          the coin is in a confirmed downtrend (price below its 200-day average and
          that average falling over 20 days). When the Crypto Fear & Greed index shows
          extreme fear (20 or lower) outside a downtrend, it invests at least 60%.
CRUISE  - built for stocks. Stays invested, but holds less when the market is jumpier
          than usual (compared with its own last 3 years), and goes all in for 5 days
          after a sharp 2-day dip while the price is above its 200-day average.
          In testing it made about the same as holding on stocks with smaller drops;
          on crypto it was worse than Growth.
MIX     - splits the money between two strategies (for example 50% Auto, 50% Steady).
          The two targets are combined into one, so opposite trades cancel out and
          cost no fees.

These settings were chosen on 2013-2022 Bitcoin data and then tested, unchanged,
on 2023-2026 data the research never saw. Do not tune them to recent results;
that is how backtests lie.
"""
import math

GROWTH_PAIRS = ((10, 30), (10, 50), (20, 50), (20, 100), (30, 100))
STEADY_LOOKBACKS = (25, 50, 100, 200)
STRATEGIES = {
    'growth': 'Growth',
    'steady': 'Steady',
    'auto': 'Auto',
    'cruise': 'Cruise',
    'hold': 'Just hold',
    'mix': 'Mix',
}
BASE_STRATEGIES = ('growth', 'steady', 'auto', 'cruise', 'hold')  # what a Mix can combine
REGIME_SMA, REGIME_SLOPE_DAYS = 200, 20
FEAR_LEVEL, FEAR_FLOOR = 20, 0.6
MIN_BARS = 220  # history needed before signals are meaningful


# ---------------------------------------------------------------- indicators
def ema(xs, span):
    a = 2.0 / (span + 1.0)
    out, e = [], None
    for x in xs:
        e = x if e is None else a * x + (1 - a) * e
        out.append(e)
    return out


def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def momentum_up(xs, n):
    return [None if i < n else (1.0 if xs[i] / xs[i - n] - 1 > 0 else 0.0) for i in range(len(xs))]


def donchian(highs, lows, closes, n_in, n_out):
    """1 after a close above the prior n_in-day high, 0 after a close below the
    prior n_out-day low, otherwise keep the previous state."""
    out, state = [], 0.0
    for i in range(len(closes)):
        if i >= n_in:
            hi = max(highs[i - n_in:i])
            if closes[i] > hi:
                state = 1.0
        if i >= n_out:
            lo = min(lows[i - n_out:i])
            if closes[i] < lo:
                state = 0.0
        out.append(state)
    return out


def realized_vol(closes, n=30, ann=365):
    rets = [None] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    out = []
    for i in range(len(closes)):
        w = [r for r in rets[max(1, i - n + 1):i + 1] if r is not None]
        if i < n or len(w) < n:
            out.append(None)
            continue
        m = sum(w) / n
        var = sum((r - m) ** 2 for r in w) / (n - 1)
        out.append(math.sqrt(var) * math.sqrt(ann))
    # back-fill the start like the research did
    first = next((v for v in out if v is not None), None)
    return [first if v is None else v for v in out]


def quantize(x, step):
    return round(x / step) * step


# ---------------------------------------------------------------- strategies
def growth_series(closes):
    """Exposure (0..1) for every day, plus the per-pair votes on the last day."""
    emas = {}
    for f, s in GROWTH_PAIRS:
        for n in (f, s):
            if n not in emas:
                emas[n] = ema(closes, n)
    expo = []
    for i in range(len(closes)):
        up = sum(1.0 for f, s in GROWTH_PAIRS if emas[f][i] > emas[s][i])
        expo.append(quantize(up / len(GROWTH_PAIRS), 0.2))
    last = len(closes) - 1
    votes = [{
        'name': f'EMA {f} vs {s} days',
        'up': emas[f][last] > emas[s][last],
        'detail': f'{emas[f][last]:,.2f} vs {emas[s][last]:,.2f}',
    } for f, s in GROWTH_PAIRS]
    return expo, votes


def steady_series(highs, lows, closes, target_vol=0.8, ann=365):
    parts, names = [], []
    for n in STEADY_LOOKBACKS:
        m = sma(closes, n)
        parts.append([0.0 if m[i] is None else (1.0 if closes[i] > m[i] else 0.0) for i in range(len(closes))])
        names.append(f'Price above {n}-day average')
        ef, es = ema(closes, max(2, int(n / 2.5))), ema(closes, n)
        parts.append([1.0 if ef[i] > es[i] else 0.0 for i in range(len(closes))])
        names.append(f'EMA {max(2, int(n / 2.5))} vs {n} days')
        mo = momentum_up(closes, n)
        parts.append([0.0 if v is None else v for v in mo])
        names.append(f'Up over the last {n} days')
        parts.append(donchian(highs, lows, closes, n, n // 2))
        names.append(f'{n}-day breakout')
    rv = realized_vol(closes, ann=ann)
    expo = []
    for i in range(len(closes)):
        raw = sum(p[i] for p in parts) / len(parts)
        scale = min(1.0, target_vol / rv[i]) if rv[i] else 1.0
        expo.append(quantize(max(0.0, min(1.0, raw * scale)), 0.25))
    last = len(closes) - 1
    votes = [{'name': nm, 'up': p[last] > 0.5, 'detail': ''} for nm, p in zip(names, parts)]
    votes.append({'name': 'Market jumpiness (yearly)', 'up': None,
                  'detail': f'{rv[last] * 100:.0f}% vs 80% target' if rv[last] else 'n/a'})
    return expo, votes


def regime_series(closes):
    """'bear' = price below its 200-day average AND that average lower than 20 days ago,
    'bull' = the mirror image, 'side' = everything else."""
    m = sma(closes, REGIME_SMA)
    out = []
    for i, c in enumerate(closes):
        if m[i] is None or i < REGIME_SLOPE_DAYS or m[i - REGIME_SLOPE_DAYS] is None:
            out.append('side')
        elif c < m[i] and m[i] < m[i - REGIME_SLOPE_DAYS]:
            out.append('bear')
        elif c > m[i] and m[i] > m[i - REGIME_SLOPE_DAYS]:
            out.append('bull')
        else:
            out.append('side')
    return out


def auto_series(highs, lows, closes, fng=None, ann=365):
    g, gv = growth_series(closes)
    st, sv = steady_series(highs, lows, closes, ann=ann)
    reg = regime_series(closes)
    expo = []
    for i in range(len(closes)):
        e = st[i] if reg[i] == 'bear' else g[i]
        f = fng[i] if fng else None
        if f is not None and f <= FEAR_LEVEL and reg[i] != 'bear':
            e = max(e, FEAR_FLOOR)
        expo.append(e)
    last = len(closes) - 1
    using = 'Steady' if reg[last] == 'bear' else 'Growth'
    f = fng[last] if fng else None
    votes = (sv if using == 'Steady' else gv)[:]
    votes.insert(0, {'name': 'Market regime', 'up': None,
                     'detail': {'bear': 'downtrend → using Steady', 'bull': 'uptrend → using Growth',
                                'side': 'sideways → using Growth'}[reg[last]]})
    votes.insert(1, {'name': 'Fear & Greed', 'up': None,
                     'detail': 'n/a' if f is None else (f'{f} at last close: extreme fear → at least 60% in' if f <= FEAR_LEVEL and reg[last] != 'bear' else f'{f} at last close')})
    return expo, votes


def rsi_wilder(closes, n=2):
    up = dn = None
    a = 1.0 / n
    out = [50.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        u, w = max(d, 0.0), max(-d, 0.0)
        up = u if up is None else a * u + (1 - a) * up
        dn = w if dn is None else a * w + (1 - a) * dn
        out.append(100.0 if dn == 0 else 100 - 100 / (1 + up / dn))
    return out


def cruise_series(closes, ann=252):
    n = 21
    logr = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    vol, s1, s2 = [], 0.0, 0.0
    for i in range(len(closes)):
        if i >= 1:
            s1 += logr[i]; s2 += logr[i] ** 2
        if i > n:
            s1 -= logr[i - n]; s2 -= logr[i - n] ** 2
        if i >= n:
            m = s1 / n
            vol.append(math.sqrt(max(0.0, s2 / n - m * m)))
        else:
            vol.append(None)
    seen, target = [], []
    for v in vol:
        if v is not None:
            seen.append(v)
        win = seen[-3 * ann:]
        target.append(sum(win) / len(win) if len(win) > 60 else None)
    r2, m200 = rsi_wilder(closes, 2), sma(closes, 200)
    expo, left = [], 0
    for i in range(len(closes)):
        base = 1.0
        if vol[i] and target[i]:
            base = quantize(min(1.0, target[i] / vol[i]), 0.1)
        if m200[i] is not None and closes[i] > m200[i] and r2[i] < 10:
            left = 5
        expo.append(1.0 if left > 0 else base)
        if left > 0:
            left -= 1
    last = len(closes) - 1
    a = ann ** 0.5
    votes = [{'name': 'Market jumpiness (last month)', 'up': None,
              'detail': 'n/a' if not vol[last] else f'{vol[last] * a * 100:.0f}% a year vs {target[last] * a * 100:.0f}% normal' if target[last] else f'{vol[last] * a * 100:.0f}% a year'},
             {'name': 'Price above its 200-day average', 'up': bool(m200[last] is not None and closes[last] > m200[last]), 'detail': ''},
             {'name': 'Short dip to buy (2-day RSI under 10)', 'up': None,
              'detail': f'RSI {r2[last]:.0f}' + (' → all in for a few days' if expo[last] == 1.0 and left >= 0 and r2[last] < 10 else '')}]
    return expo, votes


def exposure_series(strategy, highs, lows, closes, fng=None, mix=None, ann=365):
    """ann = trading days per year: 365 for crypto, 252 for stocks (only used for volatility)."""
    if strategy == 'growth':
        return growth_series(closes)
    if strategy == 'steady':
        return steady_series(highs, lows, closes, ann=ann)
    if strategy == 'auto':
        return auto_series(highs, lows, closes, fng, ann)
    if strategy == 'cruise':
        return cruise_series(closes, ann)
    if strategy == 'mix':
        mix = mix or {}
        a, b = mix.get('a', 'auto'), mix.get('b', 'steady')
        w = max(0.0, min(1.0, float(mix.get('a_pct', 50)) / 100))
        ea, va = exposure_series(a, highs, lows, closes, fng, ann=ann)
        eb, vb = exposure_series(b, highs, lows, closes, fng, ann=ann)
        expo = [quantize(w * x + (1 - w) * y, 0.05) for x, y in zip(ea, eb)]
        votes = [{'name': f'{w * 100:.0f}% {STRATEGIES[a]}', 'up': None, 'detail': f'wants {ea[-1] * 100:.0f}% in'},
                 {'name': f'{(1 - w) * 100:.0f}% {STRATEGIES[b]}', 'up': None, 'detail': f'wants {eb[-1] * 100:.0f}% in'}]
        return expo, votes + [dict(v, name=f'{STRATEGIES[a]}: ' + v['name']) for v in va if v['up'] is not None]
    return [1.0] * len(closes), [{'name': 'Always fully invested', 'up': True, 'detail': ''}]


def apply_floor(x, floor):
    """Keep at least `floor` (0..1) invested no matter what the signals say."""
    return floor + (1 - floor) * x


# ---------------------------------------------------------------- backtest
def backtest(times, highs, lows, closes, strategy, cost=0.0045, floor=0.0, start_index=None, fng=None, mix=None, ann=365, idle=None):
    """Same accounting as the research: decide at the close, hold over the next
    day, pay `cost` per unit of position change. Returns curves and stats.

    `idle` (optional): per-bar returns earned by the money that is NOT invested,
    e.g. gold when the stocks account parks idle money in GLD (None = cash, 0%).
    Moving money between the asset and the parking place pays `cost` on both sides."""
    expo, _ = exposure_series(strategy, highs, lows, closes, fng, mix, ann)
    expo = [apply_floor(e, floor) for e in expo]
    s = MIN_BARS if start_index is None else start_index
    s = min(max(1, s), len(closes) - 1)
    eq, hold = [1.0], [1.0]
    prev_pos, trades, fees = 0.0, 0, 0.0
    pos_hist = []
    for i in range(s, len(closes)):
        pos_today = expo[i - 1] if i - 1 >= s - 1 else 0.0
        # position decided at close i-1 is held from i-1 to i; trade cost when it changed
        change = abs(pos_today - prev_pos)
        r = closes[i] / closes[i - 1] - 1
        park = idle[i] if idle is not None and i < len(idle) else None
        legs = 2 if park is not None else 1
        e = eq[-1] * (1 + pos_today * r + (1 - pos_today) * (park or 0.0) - change * cost * legs)
        if change > 1e-9:
            trades += 1
            fees += eq[-1] * change * cost * legs
        prev_pos = pos_today
        eq.append(e)
        hold.append(hold[-1] * (1 + r) if i > s else hold[-1] * (1 + r) * (1 - cost))
        pos_hist.append(pos_today)
    t = times[s - 1:]
    return {'times': t, 'equity': eq, 'hold': hold, 'exposure': [expo[s - 1]] + pos_hist,
            'stats': stats(t, eq, ann), 'hold_stats': stats(t, hold, ann), 'trades': trades}


def stats(times, eq, ann=365):
    if len(eq) < 3:
        return {}
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    years = (times[-1] - times[0]) / (365 * 86400) or 1e-9
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
    sharpe = m / sd * math.sqrt(ann) if sd > 0 else 0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {'total': eq[-1] / eq[0] - 1, 'cagr': cagr, 'sharpe': sharpe, 'maxdd': mdd, 'years': years}


def yearly(times, eq):
    """Return per calendar year (UTC)."""
    import time as _t
    out, cur, start_v = [], None, None
    for t, v in zip(times, eq):
        y = _t.gmtime(t).tm_year
        if cur is None:
            cur, start_v = y, v
        elif y != cur:
            out.append({'year': cur, 'ret': prev_v / start_v - 1})
            cur, start_v = y, prev_v
        prev_v = v
    if cur is not None:
        out.append({'year': cur, 'ret': prev_v / start_v - 1})
    return out
