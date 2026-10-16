"""HTTP helpers. Every request is checked against scope.txt before it is sent."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import ENV_FILE, SCOPE_FILE

UA = "pjtrack/0.1 (personal research)"


def _scope():
    lines = SCOPE_FILE.read_text().splitlines() if SCOPE_FILE.exists() else []
    return {l.strip().lower() for l in lines if l.strip() and not l.startswith("#")}


def check_scope(url):
    host = urllib.parse.urlparse(url).hostname.lower()
    if host not in _scope():
        raise PermissionError(f"{host} is not listed in scope.txt")


def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class HTTPError(Exception):
    def __init__(self, status, headers, body=b""):
        super().__init__(f"HTTP {status}")
        self.status, self.headers, self.body = status, headers, body


def request(url, *, data=None, headers=None, timeout=60, retries=3):
    """Return (status, headers, body bytes). Raises HTTPError for non-2xx except 404."""
    check_scope(url)
    hdrs = {"User-Agent": UA, **(headers or {})}
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, dict(e.headers), b""
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(2 ** attempt * 5)
                continue
            raise HTTPError(e.code, dict(e.headers), e.read()) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt * 5)


def get_json(url, **kw):
    status, headers, body = request(url, **kw)
    return (json.loads(body) if body else None), headers
