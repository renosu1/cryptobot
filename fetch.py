"""Download price history for strategy research (Yahoo Finance daily, Coinbase daily and hourly) into data/."""
import gzip, json, os, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) research-data/1.0'}

def get(url, tries=6):
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 + 3 * a)
        except Exception:
            time.sleep(2 + 2 * a)
    return None

def save(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, 'wt') as f:
        f.write('t,o,h,l,c,v\n')
        for r in rows:
            f.write(','.join(str(x) if i == 0 else f'{x:.8g}' for i, x in enumerate(r)) + '\n')

def yahoo(sym):
    j = get(f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?period1=-2208988800&period2={int(time.time())}&interval=1d&events=div%7Csplit')
    try:
        res = j['chart']['result'][0]
        q = res['indicators']['quote'][0]
        adj = (res['indicators'].get('adjclose') or [{}])[0].get('adjclose') or q['close']
        rows = []
        for i, t in enumerate(res['timestamp']):
            o, h, l, c, a, v = q['open'][i], q['high'][i], q['low'][i], q['close'][i], adj[i], q['volume'][i]
            if None in (o, h, l, c, a) or c <= 0 or a <= 0:
                continue
            f = a / c
            rows.append((t, o * f, h * f, l * f, a, v or 0))
        if len(rows) > 250:
            save(f'data/yahoo/{sym}.csv.gz', rows)
            return sym, len(rows)
    except Exception as e:
        return sym, f'error {e}'
    return sym, 0

def coinbase(coin, gran, start):
    step = 300 * gran
    out = {}
    end = int(time.time()) // gran * gran
    t = start
    misses = 0
    while t < end:
        s = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t)); e = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(min(t + step - gran, end)))
        j = get(f'https://api.exchange.coinbase.com/products/{coin}-USD/candles?granularity={gran}&start={s}&end={e}')
        if j is None:
            misses += 1
            if misses > 3 and not out:
                return coin, 0
        elif isinstance(j, list):
            for k in j:   # [time, low, high, open, close, volume]
                out[k[0]] = (k[0], k[3], k[2], k[1], k[4], k[5])
        t += step
        time.sleep(0.12)
    rows = [out[k] for k in sorted(out)]
    if len(rows) > 200:
        save(f"data/coinbase_{'daily' if gran == 86400 else 'hourly'}/{coin}.csv.gz", rows)
    return coin, len(rows)

def main():
    sec, lists = None, {}
    for line in open('symbols.txt'):
        line = line.strip()
        if line.startswith('['):
            sec = line[1:-1]; lists[sec] = []
        elif line:
            lists[sec] += line.split()
    log = {}
    with ThreadPoolExecutor(4) as ex:
        for sym, n in ex.map(yahoo, lists['yahoo']):
            log['yahoo/' + sym] = n
    with ThreadPoolExecutor(3) as ex:
        for coin, n in ex.map(lambda c: coinbase(c, 86400, 1420070400), lists['coinbase_daily']):
            log['daily/' + coin] = n
    with ThreadPoolExecutor(3) as ex:
        for coin, n in ex.map(lambda c: coinbase(c, 3600, 1514764800), lists['coinbase_hourly']):
            log['hourly/' + coin] = n
    os.makedirs('data', exist_ok=True)
    json.dump(log, open('data/fetch_log.json', 'w'), indent=1)
    print(json.dumps(log))

if __name__ == '__main__':
    main()
