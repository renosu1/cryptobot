"""Minimal Binance.US spot client (Python standard library only).
Used only when you turn on live trading with Binance.US. Same safety rules as kraken.py:

- every order is first checked with Binance.US's test endpoint (nothing is traded),
- orders go "limit first": a post-only LIMIT_MAKER order rests at the best bid (buy) or best ask
  (sell) and pays the maker fee (0% on Binance.US since April 2026). Whatever has not filled after
  the wait is cancelled and sent as a market order (0.02% taker fee),
- every order carries the bot's own id (newClientOrderId). After a network error the bot asks
  Binance.US whether the order went through; if it cannot tell, it sends nothing more, so it never
  buys or sells twice.
"""
import hashlib, hmac, json, math, os, time, urllib.parse

from net import http_request

API = 'https://api.binance.us'
PAIRS = {'BTC': 'BTCUSD', 'ETH': 'ETHUSD', 'SOL': 'SOLUSD', 'XRP': 'XRPUSD', 'DOGE': 'DOGEUSD',
         'ADA': 'ADAUSD', 'LINK': 'LINKUSD', 'AVAX': 'AVAXUSD', 'LTC': 'LTCUSD'}
DONE = ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH')


class BinanceUSError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def _decimals(step):
    s = ('%.12f' % float(step)).rstrip('0')
    return len(s.split('.')[1]) if '.' in s else 0


class BinanceUS:
    NAME = 'Binance.US'

    def __init__(self, key, secret):
        self.key, self.secret = key.strip(), secret.strip()
        self._offset = None

    # ---------- plumbing
    @staticmethod
    def _sleep(s):
        time.sleep(s)

    def _now_ms(self):
        if self._offset is None:   # keep in step with Binance.US's clock (it rejects requests >10 s off)
            try:
                self._offset = int(self.public('/api/v3/time')['serverTime']) - int(time.time() * 1000)
            except Exception:
                self._offset = 0
        return int(time.time() * 1000) + self._offset

    def public(self, path, params=None):
        q = ('?' + urllib.parse.urlencode(params)) if params else ''
        return self._unwrap(http_request(API + path + q, allow_error=True))

    def private(self, method, path, params=None):
        p = dict(params or {})
        p['timestamp'] = self._now_ms()
        p['recvWindow'] = 10000
        q = urllib.parse.urlencode(p)
        q += '&signature=' + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        headers = {'X-MBX-APIKEY': self.key}
        if method == 'POST':
            body = http_request(API + path, data=q.encode(), allow_error=True,
                                headers=dict(headers, **{'Content-Type': 'application/x-www-form-urlencoded'}))
        else:
            body = http_request(f'{API}{path}?{q}', headers=headers, method=method, allow_error=True)
        return self._unwrap(body)

    @staticmethod
    def _unwrap(body):
        j = json.loads(body) if body else {}
        if isinstance(j, dict) and isinstance(j.get('code'), int) and j['code'] < 0:
            raise BinanceUSError(f"Binance.US {j['code']}: {j.get('msg', '')}", j['code'])
        return j

    # ---------- account and market info (same interface as kraken.Kraken)
    def pair(self, coin):
        return PAIRS[coin]

    def balances(self):
        acct = self.private('GET', '/api/v3/account')
        return {b['asset']: float(b['free']) for b in acct.get('balances', []) if float(b['free']) or float(b['locked'])}

    @staticmethod
    def usd_balance(bal):
        return bal.get('USD', 0.0)

    @staticmethod
    def coin_balance(bal, coin):
        return bal.get(coin, 0.0)

    def pair_info(self, pair):
        s = self.public('/api/v3/exchangeInfo', {'symbol': pair})['symbols'][0]
        f = {x['filterType']: x for x in s['filters']}
        notional = f.get('MIN_NOTIONAL') or f.get('NOTIONAL') or {}
        return {'lot_decimals': _decimals(f['LOT_SIZE']['stepSize']), 'pair_decimals': _decimals(f['PRICE_FILTER']['tickSize']),
                'ordermin': float(f['LOT_SIZE']['minQty']), 'costmin': float(notional.get('minNotional', 0) or 0)}

    def ticker(self, pair):
        t = self.public('/api/v3/ticker/bookTicker', {'symbol': pair})
        return float(t['bidPrice']), float(t['askPrice'])

    # ---------- orders
    @staticmethod
    def _params(pair, side, otype, qty, info, price=None, cid=None):
        p = {'symbol': pair, 'side': side.upper(), 'type': otype, 'quantity': f"{qty:.{info['lot_decimals']}f}",
             'newOrderRespType': 'FULL'}
        if price is not None:
            p['price'] = f"{price:.{info['pair_decimals']}f}"
        if cid:
            p['newClientOrderId'] = cid
        return p

    def query(self, pair, oid=None, cid=None):
        p = {'symbol': pair}
        p.update({'origClientOrderId': cid} if cid else {'orderId': oid})
        return self.private('GET', '/api/v3/order', p)

    def _final_state(self, pair, oid):
        """Cancel the order if it is still open and return its final state. Raises if it cannot be confirmed."""
        last = None
        for _ in range(6):
            try:
                st = self.query(pair, oid=oid)
                if st.get('status') in DONE:
                    return st
                try:
                    self.private('DELETE', '/api/v3/order', {'symbol': pair, 'orderId': oid})
                except BinanceUSError as e:
                    if e.code != -2011:   # -2011: unknown order (already filled or cancelled)
                        raise
                self._sleep(1.5)
                continue
            except Exception as e:
                last = e
            self._sleep(2)
        raise BinanceUSError(f'could not confirm the state of order {oid} ({last or "still open"})')

    def _commission(self, pair, oid, coin, fills=None):
        """(fee taken from the coin, fee taken from USD, fee paid in anything else valued in USD)."""
        try:
            fills = fills if fills is not None else self.private('GET', '/api/v3/myTrades', {'symbol': pair, 'orderId': oid})
        except Exception:
            return None
        fb = fq = fo = 0.0
        for t in fills:
            c, a = float(t.get('commission', 0) or 0), t.get('commissionAsset')
            if a == coin:
                fb += c
            elif a == 'USD':
                fq += c
            elif c:
                try:
                    fo += c * float(self.public('/api/v3/ticker/price', {'symbol': f'{a}USD'})['price'])
                except Exception:
                    pass
        return fb, fq, fo

    def _apply(self, o, side, qty, quote, fees):
        """Add an execution to the running totals: what the bot's coin and cash balances change by."""
        price = quote / qty if qty else o['price']
        if fees is None:   # commission unknown: assume it came out of cash at the estimated rate
            fees = (0.0, quote * o.get('est_fee', 0.0002), 0.0)
        fb, fq, fo = fees
        if side == 'buy':
            o['coin_delta'] += qty - fb
            o['cash_delta'] -= quote + fq
        else:
            o['coin_delta'] -= qty + fb
            o['cash_delta'] += quote - fq
        o['vol'] += qty
        o['cost'] += quote
        o['fee'] += fb * price + fq + fo

    def market_order(self, pair, side, volume, info, coin, est_fee=0.0002):
        """Market order with a dry run first. Returns {'txid', 'qty', 'quote', 'fees', 'confirmed'}."""
        cid = 'tr' + os.urandom(8).hex()
        params = self._params(pair, side, 'MARKET', volume, info, cid=cid)
        self.private('POST', '/api/v3/order/test', params)
        try:
            res = self.private('POST', '/api/v3/order', params)
        except BinanceUSError:
            raise                      # a clear rejection: nothing was traded
        except Exception:              # network trouble: did it go through?
            self._sleep(2)
            try:
                res = self.query(pair, cid=cid)
            except BinanceUSError as e:
                if e.code == -2013:    # order does not exist
                    return {'txid': [], 'qty': 0.0, 'quote': 0.0, 'fees': None, 'confirmed': True}
                raise
        oid = res.get('orderId')
        if res.get('status') not in DONE:
            res = self._final_state(pair, oid)
        fills = res.get('fills')
        return {'txid': [str(oid)], 'qty': float(res.get('executedQty', 0) or 0), 'quote': float(res.get('cummulativeQuoteQty', 0) or 0),
                'fees': self._commission(pair, oid, coin, fills), 'confirmed': True}

    def limit_first(self, orders, wait_s=1800, poll_s=15, log=None):
        """Same contract as kraken.Kraken.limit_first. Each returned order also carries coin_delta and
        cash_delta: how much the bot's coin and USD balances changed, fees included."""
        log = log or (lambda *a, **k: None)
        out = []
        for o in orders:
            o = dict(o, txids=[], vol=0.0, cost=0.0, fee=0.0, maker_vol=0.0, coin_delta=0.0, cash_delta=0.0,
                     oid=None, abort=False, confirmed=True)
            if wait_s > 0:
                cid = 'tr' + os.urandom(8).hex()
                try:
                    bid, ask = self.ticker(o['pair'])
                    params = self._params(o['pair'], o['side'], 'LIMIT_MAKER', o['volume'], o['info'],
                                          price=bid if o['side'] == 'buy' else ask, cid=cid)
                    self.private('POST', '/api/v3/order/test', params)
                except Exception as e:
                    log(f"LIVE: limit order for {o['pair']} not possible ({e}); using an instant order.", 'warn')
                else:
                    try:
                        o['oid'] = self.private('POST', '/api/v3/order', params).get('orderId')
                    except BinanceUSError as e:
                        if e.code != -2010:   # -2010: would trade immediately (post-only), so go instant
                            log(f"LIVE: limit order for {o['pair']} rejected ({e}); using an instant order.", 'warn')
                    except Exception as e:
                        try:
                            o['oid'] = self.query(o['pair'], cid=cid).get('orderId')
                        except BinanceUSError as e2:
                            if e2.code != -2013:
                                o['abort'] = True
                        except Exception:
                            o['abort'] = True
                        if o['abort']:
                            log(f"LIVE: could not tell whether the {o['pair']} order was placed ({e}). "
                                'No further order sent for it this round; check Binance.US.', 'error')
            out.append(o)
        deadline = time.time() + wait_s
        waiting = [o for o in out if o['oid']]
        while waiting and time.time() < deadline:
            self._sleep(poll_s)
            for o in list(waiting):
                try:
                    if self.query(o['pair'], oid=o['oid']).get('status') in DONE:
                        waiting.remove(o)
                except Exception:
                    pass
        for o in out:
            info, lot = o['info'], o['info']['lot_decimals']
            if o['oid']:
                o['txids'].append(str(o['oid']))
                try:
                    st = self._final_state(o['pair'], o['oid'])
                except Exception as e:
                    o['abort'] = True
                    log(f"LIVE: {e}. No instant order sent for {o['pair']} this round; check Binance.US.", 'error')
                    continue
                qty, quote = float(st.get('executedQty', 0) or 0), float(st.get('cummulativeQuoteQty', 0) or 0)
                if qty > 0:
                    self._apply(o, o['side'], qty, quote, self._commission(o['pair'], o['oid'], o['coin']))
                    o['maker_vol'] = qty
            if o['abort']:
                continue
            rest = math.floor(max(0.0, o['volume'] - o['vol']) * 10 ** lot + 1e-9) / 10 ** lot
            if rest > 0 and rest >= info['ordermin'] and rest * o['price'] >= max(info['costmin'], 5):
                try:
                    m = self.market_order(o['pair'], o['side'], rest, info, o['coin'], o.get('est_fee', 0.0002))
                except Exception as e:
                    log(f"LIVE: instant order for {o['pair']} failed ({e}).", 'error')
                    continue
                o['txids'] += m['txid']
                if m['qty'] > 0:
                    self._apply(o, o['side'], m['qty'], m['quote'], m['fees'])
            o['price'] = o['cost'] / o['vol'] if o['vol'] else o['price']
        return out
