"""Minimal Kraken spot client (Python standard library only).
Used only when you turn on live trading. Every real order is first sent with
validate=true (Kraken checks it without trading); only if that passes is the
real order sent."""
import base64, hashlib, hmac, json, time, urllib.parse

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
            'ordermin': float(info.get('ordermin', 0) or 0),
            'costmin': float(info.get('costmin', 0) or 0),
        }

    def market_order(self, pair, side, volume, lot_decimals):
        vol = f'{volume:.{lot_decimals}f}'
        params = {'pair': pair, 'type': side, 'ordertype': 'market', 'volume': vol, 'oflags': 'fciq'}
        self.private('AddOrder', dict(params, validate='true'))  # dry run first
        res = self.private('AddOrder', params)
        txids = res.get('txid', [])
        fill = None
        if txids:
            for _ in range(5):
                time.sleep(1.5)
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
