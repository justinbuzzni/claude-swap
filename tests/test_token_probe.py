import json
import urllib.error
import urllib.request

import pytest

from claude_swap.token_probe import parse_headers, probe, select_source


def test_scope_selection_never_escalates_unknown_or_api_key():
    assert select_source('sk-ant-api01-test') == 'none'
    assert select_source(json.dumps({'claudeAiOauth': {'accessToken': 'ordinary'}})) == 'none'
    assert select_source(json.dumps({'claudeAiOauth': {'accessToken': 'sk-ant-oat01-test', 'scopes': ['user:inference']}})) == 'inference_probe'
    assert select_source(json.dumps({'claudeAiOauth': {'accessToken': 'ordinary', 'scopes': ['user:profile']}})) == 'profile_oauth'


def test_headers_case_insensitive_fraction_seconds_and_unknown():
    windows = parse_headers({'ANTHROPIC-RATELIMIT-UNIFIED-5H-UTILIZATION': '.63', 'anthropic-ratelimit-unified-5h-reset': '2000000000'})
    assert windows[0]['pct'] == 63
    assert windows[0]['resetsAt'] == '2033-05-18T03:33:20Z'
    assert windows[1]['pct'] is None


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '1.1', 'invalid', ''])
def test_bad_utilization_remains_unknown(value):
    assert parse_headers({'anthropic-ratelimit-unified-5h-utilization': value})[0]['pct'] is None


class Response:
    status = 200
    headers = {'anthropic-ratelimit-unified-5h-utilization': '.5'}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return b'{}'


def test_fixed_transport_no_proxy_redirect_tiny_prompt(monkeypatch):
    captured = {}
    def build(*handlers):
        captured['handlers'] = handlers
        class Opener:
            def open(self, request, timeout):
                captured.update(request=request, timeout=timeout)
                return Response()
        return Opener()
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    result = probe('fixture-token', now=1000)
    req = captured['request']
    assert req.full_url == 'https://api.anthropic.com/v1/messages'
    assert captured['timeout'] == 10
    assert captured['handlers'][0].proxies == {}
    with pytest.raises(urllib.error.HTTPError):
        captured['handlers'][1].redirect_request(req, None, 302, '', {}, 'https://evil.invalid')
    assert json.loads(req.data) == {'model': 'claude-haiku-4-5-20251001', 'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'Hi'}]}
    assert result['coverage'] == 'unknown'


@pytest.mark.parametrize('status,reason,auth', [(401,'authentication_failed','invalid'), (403,'scope_missing','unverified'), (429,'throttled','unverified'), (400,'bad_request','unverified'), (404,'not_found','unverified'), (503,'provider_unavailable','unverified'), (302,'redirect_refused','unverified')])
def test_http_status_auth_scope_and_429_headers(monkeypatch, status, reason, auth):
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://api.anthropic.com/v1/messages',status,'ignored',{'Retry-After':'120','anthropic-ratelimit-unified-5h-utilization':'.8'},None)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    result = probe('fixture-token', now=1000)
    assert result['reason'] == reason
    assert result['authState'] == auth
    if status == 429:
        assert result['windows'][0]['pct'] == 80
        assert result['retryAt'] == '1970-01-01T00:18:40Z'
    else:
        assert result['windows'][0]['pct'] is None


def test_oversized_body_does_not_return_headers(monkeypatch):
    class Large(Response):
        def read(self, size):
            assert size <= 65537
            return b'x' * 65537
    class Opener:
        def open(self, *args, **kwargs): return Large()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    assert probe('fixture-token', now=1000)['reason'] == 'response_too_large'


def test_unknown_status_does_not_make_decision_number():
    assert parse_headers({'anthropic-ratelimit-unified-5h-utilization':'.4','anthropic-ratelimit-unified-status':'unrecognized'})[0]['pct'] is None


def test_429_without_headers_is_unknown_and_network_error_is_not_auth(monkeypatch):
    class Opener:
        def open(self,*args,**kwargs):
            raise urllib.error.HTTPError('https://api.anthropic.com/v1/messages',429,'ignored',{},None)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args: Opener())
    assert all(w['pct'] is None for w in probe('fixture-token',now=1000)['windows'])
    class Offline:
        def open(self,*args,**kwargs): raise TimeoutError('fixture-secret')
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args: Offline())
    result = probe('fixture-token',now=1000)
    assert result['authState'] == 'unverified'
    assert 'fixture-secret' not in str(result)


def test_stream_read_deadline_size_and_no_fallback(monkeypatch):
    calls = []
    class Stream(Response):
        def read1(self,size):
            calls.append(size)
            return b'x' * size
    class Opener:
        def open(self,*args,**kwargs): return Stream()
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args: Opener())
    assert probe('fixture-token',now=1000)['reason'] == 'response_too_large'
    assert sum(calls) == 65537


@pytest.mark.parametrize('reader', ['read', 'read1'])
def test_truncated_http_response_is_transport_failure(monkeypatch, reader):
    from http.client import IncompleteRead

    class Truncated(Response):
        pass

    def truncated(self, size):
        raise IncompleteRead(b'provider-body-not-retained', 100)

    setattr(Truncated, reader, truncated)

    class Opener:
        def open(self, *args, **kwargs):
            return Truncated()

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    result = probe('fixture-token', now=1000)
    assert result['reason'] == 'transport_failed'
    assert result['authState'] == 'unverified'
    assert all(window['pct'] is None for window in result['windows'])
    assert 'provider-body' not in str(result)
