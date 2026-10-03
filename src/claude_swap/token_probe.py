"""Conservative inference header observation; no live-provider claims."""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from claude_swap.oauth import extract_oauth_data

ORIGIN = 'https://api.anthropic.com/v1/messages'
MODEL = 'claude-haiku-4-5-20251001'
MAX_BYTES = 65536


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def select_source(credentials: str) -> str:
    data = extract_oauth_data(credentials)
    if not data:
        return 'none'
    scopes = data.get('scopes')
    if not isinstance(scopes, list):
        return 'none'
    if 'user:profile' in scopes:
        return 'profile_oauth'
    token = data.get('accessToken')
    if 'user:inference' in scopes and isinstance(token, str) and token.startswith('sk-ant-oat01-'):
        return 'inference_probe'
    return 'none'


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_headers(headers) -> list[dict]:
    h = {str(k).lower(): v for k, v in headers.items()}
    windows = []
    for suffix, kind in [('5h', 'unified5h'), ('7d', 'unified7d')]:
        prefix = 'anthropic-ratelimit-unified-' + suffix
        fraction = _number(h.get(prefix + '-utilization'))
        status = h.get(prefix + '-status', h.get('anthropic-ratelimit-unified-status'))
        if status is not None and status not in ('allowed', 'allowed_warning', 'rejected'):
            fraction = None
        if fraction is not None and fraction > 1:
            fraction = None
        reset = _number(h.get(prefix + '-reset'))
        try:
            reset_iso = iso(reset) if reset is not None and reset > 0 else None
        except (OverflowError, ValueError, OSError):
            reset_iso = None
        windows.append({'kind': kind, 'pct': fraction * 100 if fraction is not None else None,
                        'resetsAt': reset_iso, 'status': status})
    return windows


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect refused', headers, fp)


def _retry_after(headers, now):
    value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
    seconds = _number(value)
    try:
        if seconds is not None:
            return iso(now + seconds)
        parsed = parsedate_to_datetime(value)
        return iso(parsed.timestamp()) if parsed.tzinfo and parsed.timestamp() >= now else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _read_bounded(response, deadline):
    # read1 permits a deadline check between socket reads (read(n) may keep
    # waiting on a peer trickling bytes). Never retain provider body content.
    if not hasattr(response, 'read1'):
        return len(response.read(MAX_BYTES + 1)) <= MAX_BYTES
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = response.read1(min(4096, MAX_BYTES + 1 - total))
        total += len(chunk)
        if total > MAX_BYTES:
            return False
        if not chunk:
            return True


def probe(token: str, *, now: float | None = None, timeout_s: float = 10) -> dict:
    now = time.time() if now is None else now
    result = {'version': 1, 'source': 'inference_probe', 'observedAt': iso(now),
              'windows': parse_headers({}), 'coverage': 'unknown', 'reason': None,
              'retryAt': None, 'authState': 'unverified'}
    request = urllib.request.Request(ORIGIN, method='POST', data=json.dumps({
        'model': MODEL, 'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'Hi'}]
    }).encode(), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                         'anthropic-version': '2023-06-01', 'anthropic-beta': 'oauth-2025-04-20'})
    # Ignore HTTP(S)_PROXY and all base-url overrides. Redirects never carry auth.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        deadline = time.monotonic() + min(10, timeout_s)
        with opener.open(request, timeout=min(10, timeout_s)) as response:
            status, headers = response.status, response.headers
            if not _read_bounded(response, deadline):
                result['reason'] = 'response_too_large'
                return result
    except urllib.error.HTTPError as error:
        status, headers = error.code, error.headers or {}
        error.close()
    except (TimeoutError, urllib.error.URLError, OSError):
        result['reason'] = 'transport_failed'
        return result
    if status in (200, 429):
        result['windows'] = parse_headers(headers)
    if status == 200:
        result['authState'] = 'usable'
        result['reason'] = 'coverage_unknown'
    elif status == 401:
        result.update(authState='invalid', reason='authentication_failed')
    elif status == 403:
        result['reason'] = 'scope_missing'
    elif status == 429:
        result.update(reason='throttled', retryAt=_retry_after(headers, now))
    elif 300 <= status < 400:
        result['reason'] = 'redirect_refused'
    else:
        result['reason'] = {400: 'bad_request', 404: 'not_found'}.get(status, 'provider_unavailable' if status >= 500 else 'unknown_status')
    return result
