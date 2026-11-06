"""Minimal Kraken spot client (Python standard library only).
Used only when you turn on live trading. Every real order is first sent with
validate=true (Kraken checks it without trading); only if that passes is the
real order sent.

Orders go "limit first": a post-only limit order rests at the best price on the
book and pays Kraken's lower maker fee (0.40% instead of 0.80% at the entry tier
since July 2026). Whatever has not filled after the wait is cancelled and sent
as a market order. If the bot cannot confirm what happened to a limit order, it
does NOT send the market order (it never risks buying or selling twice)."""
import base64, hashlib, hmac, json, math, os, time, urllib.parse

from net import http_request

API = 'https://api.kraken.com'


def sign(path, data, secret_b64):
    """API-Sign = base64(HMAC-SHA512(path + SHA256(nonce + postdata), base64decode(secret)))."""
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data['nonce']) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret_b64), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenError(Exception):
    pass


class Kraken:
    def __init__(self, key, secret):
        self.key, self.secret = key.strip(), secret.strip()
        self._last_nonce = 0

    def _nonce(self):
        n = int(time.time() * 1000)
        if n <= self._last_nonce:
            n = self._last_nonce + 1
        self._last_nonce = n
        return n

    def public(self, method, params=None):
        q = ('?' + urllib.parse.urlencode(params)) if params else ''
        body = http_request(f'{API}/0/public/{method}{q}')
        return self._unwrap(body)

    def private(self, method, params=None):
        path = f'/0/private/{method}'
        data = dict(params or {})
        data['nonce'] = self._nonce()
        headers = {
            'API-Key': self.key,
            'API-Sign': sign(path, data, self.secret),
            'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
        }
        body = http_request(API + path, data=urllib.parse.urlencode(data).encode(), headers=headers)
        return self._unwrap(body)

    @staticmethod
    def _unwrap(body):
        j = json.loads(body)
        if j.get('error'):
            raise KrakenError(', '.join(j['error']))
        return j.get('result', {})

    # -- helpers
    def balances(self):
        return {k: float(v) for k, v in self.private('Balance').items()}

    def pair_info(self, pair):
        res = self.public('AssetPairs', {'pair': pair})
        info = next(iter(res.values()))
        return {
            'lot_decimals': int(info.get('lot_decimals', 8)),
            'pair_decimals': int(info.get('pair_decimals', 1)),
            'ordermin': float(info.get('ordermin', 0) or 0),
            'costmin': float(info.get('costmin', 0) or 0),
        }

    def ticker(self, pair):
        """(best bid, best ask)."""
        t = next(iter(self.public('Ticker', {'pair': pair}).values()))
        return float(t['b'][0]), float(t['a'][0])

    def query(self, txid):
        return self.private('QueryOrders', {'txid': txid}).get(txid, {})

    def _final_state(self, txid):
        """Cancel the order if it is still open and return its final state. Raises if it cannot be confirmed."""
        last_err = None
        for attempt in range(6):
            try:
                st = self.query(txid)
                if st.get('status') in ('open', 'pending'):
                    try:
                        self.private('CancelOrder', {'txid': txid})
                    except KrakenError as e:
                        if 'Unknown order' not in str(e):
                            raise
                    self._sleep(1.5)
                    continue
                if st.get('status') in ('closed', 'canceled', 'expired'):
                    return st
            except Exception as e:  # network trouble: try again
                last_err = e
            self._sleep(2)
        raise KrakenError(f'could not confirm the state of order {txid} ({last_err or "still open"})')

    def _find_by_ref(self, ref):
        """After an unclear AddOrder failure: was the order placed anyway? Returns its txid or None."""
        self._sleep(3)
        for method, key in (('OpenOrders', 'open'), ('ClosedOrders', 'closed')):
            found = self.private(method, {'userref': str(ref)}).get(key, {})
            if found:
                return next(iter(found))
        return None

    @staticmethod
    def _sleep(s):
        time.sleep(s)

    def limit_first(self, orders, wait_s=1800, poll_s=15, log=None):
        """Each order rests first as a post-only limit at the best bid (buy) or best ask (sell); after `wait_s`
        seconds whatever has not filled is cancelled and sent as a market order.
        orders: [{'pair', 'side', 'volume', 'info', 'price', 'est_fee', ...}] (extra keys are passed through).
        Returns the same dicts with vol, cost, fee, price (average), maker_vol, txids, confirmed and abort."""
        log = log or (lambda *a, **k: None)
        out = []
        for o in orders:
            o = dict(o, txids=[], vol=0.0, cost=0.0, fee=0.0, maker_vol=0.0, txid=None, abort=False, confirmed=True)
            info, lot = o['info'], o['info']['lot_decimals']
            if wait_s > 0:
                try:
                    bid, ask = self.ticker(o['pair'])
                    px = bid if o['side'] == 'buy' else ask
                    o['ref'] = int.from_bytes(os.urandom(4), 'big') & 0x7fffffff
                    params = {'pair': o['pair'], 'type': o['side'], 'ordertype': 'limit',
                              'price': f"{px:.{info['pair_decimals']}f}", 'volume': f"{o['volume']:.{lot}f}",
                              'oflags': 'post,fciq', 'userref': str(o['ref'])}
                    self.private('AddOrder', dict(params, validate='true'))  # dry run first
                except Exception as e:
                    log(f"LIVE: limit order for {o['pair']} not possible ({e}); using an instant order.", 'warn')
                else:
                    try:
                        res = self.private('AddOrder', params)
                        o['txid'] = (res.get('txid') or [None])[0]
                    except Exception as e:
                        try:
                            o['txid'] = self._find_by_ref(o['ref'])
                        except Exception as e2:
                            o['abort'] = True
                            log(f"LIVE: could not tell whether the {o['pair']} order was placed ({e}; {e2}). "
                                'No further order sent for it this round; check Kraken.', 'error')
                        if not o['txid'] and not o['abort']:
                            log(f"LIVE: limit order for {o['pair']} failed ({e}); using an instant order.", 'warn')
            out.append(o)
        deadline = time.time() + wait_s
        waiting = [o for o in out if o['txid']]
        while waiting and time.time() < deadline:
            self._sleep(poll_s)
            for o in list(waiting):
                try:
                    if self.query(o['txid']).get('status') in ('closed', 'canceled', 'expired'):
                        waiting.remove(o)
                except Exception:
                    pass  # checked again on the next round
        for o in out:
            info, lot = o['info'], o['info']['lot_decimals']
            if o['txid']:
                o['txids'].append(o['txid'])
                try:
                    st = self._final_state(o['txid'])
                except Exception as e:
                    o['abort'] = True
                    log(f"LIVE: {e}. No instant order sent for {o['pair']} this round; check Kraken.", 'error')
                    continue
                o['maker_vol'] = o['vol'] = float(st.get('vol_exec') or 0)
                o['cost'] = float(st.get('cost') or 0)
                o['fee'] = float(st.get('fee') or 0)
            if o['abort']:
                continue
            rest = math.floor(max(0.0, o['volume'] - o['vol']) * 10 ** lot + 1e-9) / 10 ** lot
            if rest > 0 and rest >= info['ordermin'] and rest * o['price'] >= max(info['costmin'], 5):
                m = self.market_order(o['pair'], o['side'], rest, lot)
                o['txids'] += m['txid']
                f = m['fill'] or {'vol': rest, 'cost': rest * o['price'], 'fee': rest * o['price'] * o.get('est_fee', 0.008)}
                o['confirmed'] = bool(m['fill'])
                o['vol'] += f['vol']; o['cost'] += f['cost']; o['fee'] += f['fee']
            o['price'] = o['cost'] / o['vol'] if o['vol'] else o['price']
        return out

    def market_order(self, pair, side, volume, lot_decimals):
        vol = f'{volume:.{lot_decimals}f}'
        params = {'pair': pair, 'type': side, 'ordertype': 'market', 'volume': vol, 'oflags': 'fciq'}
        self.private('AddOrder', dict(params, validate='true'))  # dry run first
        res = self.private('AddOrder', params)
        txids = res.get('txid', [])
        fill = None
        if txids:
            for _ in range(5):
                self._sleep(1.5)
                try:
                    q = self.private('QueryOrders', {'txid': txids[0]})
                    o = q.get(txids[0], {})
                    if o.get('status') == 'closed':
                        fill = {'vol': float(o.get('vol_exec', 0)), 'cost': float(o.get('cost', 0)),
                                'fee': float(o.get('fee', 0)), 'price': float(o.get('price', 0))}
                        break
                except Exception:
                    pass
        return {'txid': txids, 'descr': res.get('descr', {}).get('order', ''), 'volume': float(vol), 'fill': fill}
