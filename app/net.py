"""HTTP helper. Tries Python's own HTTPS first; if this Mac's Python has no
certificate bundle (common with the python.org installer), it switches to the
built-in macOS `curl`, which uses the system certificates."""
import json, ssl, subprocess, urllib.request, urllib.error

UA = 'TrendRider/1.0 (personal paper-trading app)'
_use_curl = False


class NetError(Exception):
    pass


def http_request(url, data=None, headers=None, timeout=20):
    global _use_curl
    headers = dict(headers or {})
    headers.setdefault('User-Agent', UA)
    headers.setdefault('Accept', 'application/json')
    if not _use_curl:
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='replace')[:300]
            raise NetError(f'HTTP {e.code} from {url.split("?")[0]}: {body}')
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError) or 'CERTIFICATE' in str(e.reason).upper():
                _use_curl = True
            else:
                raise NetError(f'Could not reach {url.split("/")[2]}: {e.reason}')
        except ssl.SSLError:
            _use_curl = True
    cmd = ['curl', '-sS', '-m', str(timeout), '-w', '\n%{http_code}']
    for k, v in headers.items():
        cmd += ['-H', f'{k}: {v}']
    if data is not None:
        cmd += ['--data-binary', '@-']
    cmd.append(url)
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 5)
    except Exception as e:
        raise NetError(f'Could not reach {url.split("/")[2]}: {e}')
    if p.returncode != 0:
        raise NetError(f'Could not reach {url.split("/")[2]}: {p.stderr.decode(errors="replace").strip()[:200]}')
    out = p.stdout.decode(errors='replace')
    body, _, code = out.rpartition('\n')
    if not code.startswith('2'):
        raise NetError(f'HTTP {code} from {url.split("?")[0]}: {body[:300]}')
    return body


def get_json(url, timeout=20):
    return json.loads(http_request(url, timeout=timeout))
