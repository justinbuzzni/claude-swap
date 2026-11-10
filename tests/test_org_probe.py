import copy
import json
from unittest.mock import patch

import pytest

from claude_swap.org_probe import collect_org
from claude_swap.token_runtime import TokenRuntime, capabilities, command
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.models import Platform
from claude_swap.token_probe import iso

ID = '11111111-1111-4111-8111-111111111111'
NOW = 2000000000


@pytest.fixture
def setup(temp_home):
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    switcher.add_account_from_token(token='sk-ant-oat01-org-fixture', email='managed-'+ID+'@setup-token.local')
    data = switcher._get_sequence_data()
    data['accounts']['1'].update(managedAccountId=ID, credentialType='setup_token', credentialGeneration=4)
    switcher._write_json(switcher.sequence_file, data)
    runtime = TokenRuntime(switcher, clock=lambda: NOW)
    ref = runtime.status()['accounts'][0]['accountRef']
    permit = dict(version=1, permitId='22222222-2222-4222-8222-222222222222',companyId='company',machineId='machine',managedAccountId=ID,credentialGeneration=4,policyRevision=2,reservedAt=NOW*1000,transportDeadline=NOW*1000+10000,expiresAt=NOW*1000+30000,timeoutMs=10000,accountRemaining=95,companyRemaining=287)
    context = dict(companyId='company',machineId='machine',managedAccountId=ID,credentialGeneration=4,policyRevision=2,accountRef=ref)
    request = dict(version=1,context=context,permit=permit,inUse=True,online=True)
    return runtime, request


def observation():
    return dict(version=1,source='inference_probe',observedAt=iso(NOW),windows=[dict(kind='unified5h',pct=63,resetsAt=iso(NOW+1000),status=None)],coverage='unknown',reason='coverage_unknown',retryAt=None,authState='usable')


def test_managed_one_use_grant_calls_without_local_consent_and_replay_is_durable(setup):
    runtime, request = setup
    assert capabilities()['organizationCollectorVersion'] == 1
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        result = collect_org(runtime,request)
        assert result['observation']['credentialGeneration'] == 4
        assert result['observation']['accountRef'] == request['context']['accountRef']
        again = TokenRuntime(runtime.switcher,clock=runtime.clock)
        assert collect_org(again,request)['reason'] == 'permit_replayed'
        http.assert_called_once()
    assert runtime.status()['budget']['machineUsed24h'] == 1
    assert runtime.status()['accounts'][0]['probeEnabled'] is False
    assert 'sk-ant-oat01-' not in runtime.state_file.read_text()
    assert 'sk-ant-oat01-' not in json.dumps(result)


@pytest.mark.parametrize('field,value',[('companyId','other'),('machineId','other'),('managedAccountId','33333333-3333-4333-8333-333333333333'),('credentialGeneration',3),('policyRevision',1),('version',2),('timeoutMs',20000),('expiresAt',NOW*1000+60000),('reservedAt',NOW*1000+1000),('transportDeadline',NOW*1000-1)])
def test_invalid_scope_generation_policy_time_grant_never_calls(setup,field,value):
    runtime,request=setup
    request['permit'][field]=value
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime,request)['reason'] == 'grant_invalid'
        http.assert_not_called()


@pytest.mark.parametrize('field',['inUse','online'])
def test_receiver_unused_offline_no_fallback(setup,field):
    runtime,request=setup
    request[field]=False
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime,request)['reason'] in ('offline','not_in_use')
        http.assert_not_called()


def test_late_credential_replacement_discards_result_spending_remains(setup):
    runtime,request=setup
    def http(*args,**kwargs):
        runtime.switcher._write_account_credentials('1',runtime.switcher.account_email('1'),json.dumps({'claudeAiOauth':{'accessToken':'sk-ant-oat01-new-fixture','scopes':['user:inference']}}))
        return observation()
    with patch('claude_swap.org_probe.probe',side_effect=http):
        assert collect_org(runtime,request)['reason']=='credential_changed'
    assert runtime.status()['budget']['machineUsed24h']==1


def test_transit_reduces_timeout_and_post_deadline_discards(setup):
    runtime,request=setup
    runtime.clock=lambda:NOW+8
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        collect_org(runtime,request)
        assert http.call_args.kwargs['timeout_s']==2
    runtime,request=setup
    request=copy.deepcopy(request)
    request['permit']['permitId']='44444444-4444-4444-8444-444444444444'
    runtime.clock=lambda:NOW+1000
    request['permit'].update(reservedAt=(NOW+1000)*1000,transportDeadline=(NOW+1010)*1000,expiresAt=(NOW+1030)*1000)
    def slow(*args,**kwargs):
        runtime.clock=lambda:NOW+1011
        return observation()
    with patch('claude_swap.org_probe.probe',side_effect=slow):
        assert collect_org(runtime,request)['reason']=='permit_expired'


def test_grant_write_failure_prevents_http(setup):
    runtime,request=setup
    with patch.object(runtime,'_save',side_effect=OSError('fixture')),patch('claude_swap.org_probe.probe') as http:
        with pytest.raises(OSError):collect_org(runtime,request)
        http.assert_not_called()


def test_cli_grant_only_stdin_and_bounded(monkeypatch):
    import io
    monkeypatch.setattr('sys.stdin',io.StringIO('x'*8193))
    with pytest.raises(ValueError,match='grant_invalid'):command(['collect-org'])


def _ms_iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _slow_persistence(runtime, after_probe_now):
    """Advance the clock while the post-transport state write runs."""
    real, calls = runtime._save, []

    def save(state):
        calls.append(1)
        if len(calls) == 2:  # 1st = reservation before POST, 2nd = result bookkeeping
            runtime.clock = lambda: after_probe_now
        return real(state)
    return patch.object(runtime, '_save', side_effect=save)


def test_completion_before_deadline_survives_slow_persistence_with_completion_time(setup):
    runtime, request = setup

    def completes(*args, **kwargs):
        runtime.clock = lambda: NOW + 9.5
        return observation()
    with patch('claude_swap.org_probe.probe', side_effect=completes), _slow_persistence(runtime, NOW + 12):
        result = collect_org(runtime, request)
    assert result['observation']['observedAt'] == _ms_iso(NOW + 9.5)


def test_processing_past_expiry_is_discarded(setup):
    runtime, request = setup

    def completes(*args, **kwargs):
        runtime.clock = lambda: NOW + 9.5
        return observation()
    with patch('claude_swap.org_probe.probe', side_effect=completes), _slow_persistence(runtime, NOW + 31):
        assert collect_org(runtime, request)['reason'] == 'permit_expired'


def test_elapsed_retry_after_is_null_but_durable_backoff_retained(setup):
    runtime, request = setup
    throttled = dict(observation(), reason='throttled', retryAt=iso(NOW))  # Retry-After: 0 at request start

    def completes(*args, **kwargs):
        runtime.clock = lambda: NOW + 0.6
        return throttled
    with patch('claude_swap.org_probe.probe', side_effect=completes):
        result = collect_org(runtime, request)
    assert result['observation']['retryAt'] is None
    tokens = json.loads(runtime.state_file.read_text())['tokens']
    assert all(entry['nextAt'] >= NOW + 600 for entry in tokens.values())


def test_future_retry_after_is_kept(setup):
    runtime, request = setup
    with patch('claude_swap.org_probe.probe', return_value=dict(observation(), reason='throttled', retryAt=iso(NOW + 120))):
        assert collect_org(runtime, request)['observation']['retryAt'] == iso(NOW + 120)


def spent(runtime, request, count):
    """Recent attempts on this account's token, as earlier collections left them."""
    state = runtime._load()
    runtime._sync(state)
    digest = state['accounts'][request['context']['accountRef']]['digest']
    state['attempts'] = [{'at': NOW-60, 'digest': digest} for _ in range(count)]
    state['tokens'] = {}
    runtime._save(state)


def switch(request):
    request['permit']['purpose'] = 'switch'
    return request


def test_switch_check_may_spend_up_to_twice_the_account_budget(setup):
    runtime, request = setup
    assert capabilities()['switchProbeVersion'] == 1
    spent(runtime, request, 96)
    with patch('claude_swap.org_probe.probe', return_value=observation()) as http:
        assert collect_org(runtime, request)['reason'] == 'budget_exhausted'
        assert 'observation' in collect_org(runtime, switch(copy.deepcopy(request)))
        http.assert_called_once()
    spent(runtime, request, 192)
    request['permit']['permitId'] = '33333333-3333-4333-8333-333333333333'
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime, switch(request))['reason'] == 'budget_exhausted'
        http.assert_not_called()


def test_switch_check_may_spend_up_to_twice_the_machine_budget(setup):
    runtime, request = setup
    state = runtime._load()
    state['attempts'] = [{'at': NOW-60, 'digest': 'another-token'} for _ in range(288)]
    runtime._save(state)
    with patch('claude_swap.org_probe.probe', return_value=observation()):
        assert collect_org(runtime, copy.deepcopy(request))['reason'] == 'budget_exhausted'
        assert 'observation' in collect_org(runtime, switch(request))


def test_unknown_purpose_never_calls(setup):
    runtime, request = setup
    request['permit']['purpose'] = 'periodic'
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime, request)['reason'] == 'grant_invalid'
        http.assert_not_called()


def test_new_organization_permit_rechecks_long_provider_backoff_hourly_without_resetting_spending(setup):
    runtime, request = setup
    with patch('claude_swap.org_probe.probe', return_value=dict(observation(), reason='throttled', retryAt=iso(NOW + 4 * 86400))) as http:
        assert 'observation' in collect_org(runtime, request)
    assert runtime.status()['budget']['machineUsed24h'] == 1
    request = copy.deepcopy(request)
    request['permit']['permitId'] = '44444444-4444-4444-8444-444444444444'
    def at(seconds):
        runtime.clock = lambda: NOW + seconds
        request['permit'].update(reservedAt=(NOW+seconds)*1000, transportDeadline=(NOW+seconds)*1000+10000, expiresAt=(NOW+seconds)*1000+30000)
    at(3589)
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime, request)['reason'] == 'backing_off'
        http.assert_not_called()
    assert runtime.status()['budget']['machineUsed24h'] == 1
    at(3600)
    fresh = observation()
    fresh['windows'][0]['resetsAt'] = iso(NOW + 7200)
    with patch('claude_swap.org_probe.probe', return_value=fresh) as http:
        assert 'observation' in collect_org(runtime, request)
        assert collect_org(runtime, request)['reason'] == 'permit_replayed'
        http.assert_called_once()
    assert runtime.status()['budget']['machineUsed24h'] == 2


def test_organization_retry_with_expired_attempt_history_can_recover_old_retry_after(setup):
    runtime, request = setup
    with patch('claude_swap.org_probe.probe', return_value=dict(observation(), reason='throttled', retryAt=iso(NOW + 7 * 86400))):
        collect_org(runtime, request)
    later = NOW + 2 * 86400
    runtime.clock = lambda: later
    request['permit'].update(permitId='44444444-4444-4444-8444-444444444444',reservedAt=later*1000,transportDeadline=later*1000+10000,expiresAt=later*1000+30000)
    fresh = observation()
    fresh['windows'][0]['resetsAt'] = iso(later + 1000)
    with patch('claude_swap.org_probe.probe', return_value=fresh) as http:
        assert 'observation' in collect_org(runtime, request)
        http.assert_called_once()
    assert runtime.status()['budget']['machineUsed24h'] == 1


@pytest.mark.parametrize('limit', ['account', 'machine'])
def test_hourly_organization_recheck_never_bypasses_durable_daily_budget(setup, limit):
    runtime, request = setup
    state = runtime._load()
    runtime._sync(state)
    digest = state['accounts'][request['context']['accountRef']]['digest']
    state['tokens'][digest] = dict(nextAt=NOW+7*86400, failures=1)
    state['attempts'] = [{'at': NOW-3600, 'digest': digest if limit == 'account' else 'another-token'} for _ in range(96 if limit == 'account' else 288)]
    runtime._save(state)
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime, request)['reason'] == 'budget_exhausted'
        http.assert_not_called()
    assert len(runtime._load()['attempts']) == len(state['attempts'])


@pytest.mark.parametrize('reason', ['timeout', 'headers_missing', 'coverage_unknown'])
def test_failed_or_partial_hourly_probe_preserves_the_personal_provider_deadline(setup, reason):
    runtime, request = setup
    deadline = NOW + 4*86400
    with patch('claude_swap.org_probe.probe', return_value=dict(observation(), reason='throttled', retryAt=iso(deadline))):
        collect_org(runtime, request)
    runtime.clock = lambda: NOW + 3600
    request['permit'].update(permitId='44444444-4444-4444-8444-444444444444',reservedAt=(NOW+3600)*1000,transportDeadline=(NOW+3610)*1000,expiresAt=(NOW+3630)*1000)
    fresh = observation()
    fresh['windows'][0]['resetsAt'] = iso(NOW + 7200)
    with patch('claude_swap.org_probe.probe', return_value=dict(fresh, reason=reason)):
        assert 'observation' in collect_org(runtime, request)
    assert all(entry['nextAt'] >= deadline for entry in runtime._load()['tokens'].values())


def test_organization_hourly_exception_does_not_bypass_exponential_transport_backoff(setup):
    runtime, request = setup
    state = runtime._load()
    runtime._sync(state)
    digest = state['accounts'][request['context']['accountRef']]['digest']
    state['attempts'] = [{'at': NOW-3600, 'digest': digest}]
    state['tokens'][digest] = dict(nextAt=NOW+1200, failures=4)
    runtime._save(state)
    with patch('claude_swap.org_probe.probe') as http:
        assert collect_org(runtime, request)['reason'] == 'backing_off'
        http.assert_not_called()


@pytest.mark.parametrize('outcome', ['full', 'crash', 'legacy-timeout'])
def test_hourly_recheck_retains_provider_wait_durably_until_a_complete_success(setup, outcome):
    runtime, request = setup
    deadline = NOW + 4*86400
    with patch('claude_swap.org_probe.probe', return_value=dict(observation(), reason='throttled', retryAt=iso(deadline))):
        collect_org(runtime, request)
    if outcome == 'legacy-timeout':
        state = runtime._load()
        for entry in state['tokens'].values(): entry.pop('orgRetryAt')
        runtime._save(state)
    runtime.clock = lambda: NOW + 3600
    request['permit'].update(permitId='44444444-4444-4444-8444-444444444444',reservedAt=(NOW+3600)*1000,transportDeadline=(NOW+3610)*1000,expiresAt=(NOW+3630)*1000)
    fresh = observation()
    fresh['windows'] = [dict(kind=kind,pct=0,resetsAt=iso(NOW+7200),status=None) for kind in ['unified5h', 'unified7d']]
    if outcome == 'crash':
        with patch('claude_swap.org_probe.probe', side_effect=RuntimeError('fixture crash')):
            with pytest.raises(RuntimeError, match='fixture crash'): collect_org(runtime, request)
    else:
        with patch('claude_swap.org_probe.probe', return_value=dict(fresh, reason='timeout') if outcome == 'legacy-timeout' else fresh):
            assert 'observation' in collect_org(runtime, request)
    token_state = next(iter(runtime._load()['tokens'].values()))
    if outcome == 'full':
        assert token_state['nextAt'] == NOW+3900
        assert token_state['orgRetryAt'] == 0
    else:
        assert token_state['nextAt'] >= deadline
        assert token_state['orgRetryAt'] == deadline
    assert runtime.status()['budget']['machineUsed24h'] == 2


def test_invalid_persisted_provider_deadline_stops_before_probe(setup):
    runtime, request = setup
    state = runtime._load()
    runtime._sync(state)
    digest = state['accounts'][request['context']['accountRef']]['digest']
    state['tokens'][digest] = dict(nextAt=NOW+5000, orgRetryAt='bad', failures=1)
    runtime._save(state)
    with patch('claude_swap.org_probe.probe') as http:
        with pytest.raises(ValueError): collect_org(runtime, request)
        http.assert_not_called()


def test_personal_throttle_after_org_success_still_allows_an_hourly_org_check(setup):
    runtime, request = setup
    with patch('claude_swap.org_probe.probe', return_value=observation()): collect_org(runtime, request)
    runtime.switcher.add_account_from_token(token='sk-ant-oat01-org-fixture',email='personal@token.local')
    alias = next(a['accountRef'] for a in runtime.status()['accounts'] if a['accountRef'] != request['context']['accountRef'])
    runtime.consent(alias, enabled=True, ack_cost=True)
    runtime.clock = lambda: NOW+300
    with patch('claude_swap.token_runtime.probe', return_value=dict(observation(),reason='throttled',retryAt=iso(NOW+4*86400))):
        assert runtime.collect(alias,in_use=True)['account']['reasonCodes'][0] == 'throttled'
    runtime.clock = lambda: NOW+3900
    request['permit'].update(permitId='44444444-4444-4444-8444-444444444444',reservedAt=(NOW+3900)*1000,transportDeadline=(NOW+3910)*1000,expiresAt=(NOW+3930)*1000)
    with patch('claude_swap.org_probe.probe', return_value=observation()) as http:
        assert 'observation' in collect_org(runtime,request)
        http.assert_called_once()


def test_provider_deadline_never_bypasses_an_active_transport_failure_deadline(setup):
    runtime, request = setup
    state=runtime._load(); runtime._sync(state)
    digest=state['accounts'][request['context']['accountRef']]['digest']
    state['tokens'][digest]=dict(nextAt=NOW+4*86400,orgRetryAt=NOW+4*86400,failures=7,transportFailures=6,transportRetryAt=NOW+1000)
    state['attempts']=[dict(at=NOW-3600,digest=digest)]
    runtime._save(state)
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        assert collect_org(runtime,request).get('reason')=='backing_off'
        http.assert_not_called()


def test_hourly_server_permit_tolerates_the_previous_transit_delay(setup):
    runtime,request=setup
    runtime.clock=lambda:NOW+9
    with patch('claude_swap.org_probe.probe',return_value=dict(observation(),reason='throttled',retryAt=iso(NOW+4*86400))):collect_org(runtime,request)
    runtime.clock=lambda:NOW+3600
    request['permit'].update(permitId='44444444-4444-4444-8444-444444444444',reservedAt=(NOW+3600)*1000,transportDeadline=(NOW+3610)*1000,expiresAt=(NOW+3630)*1000)
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        assert 'observation' in collect_org(runtime,request)
        http.assert_called_once()


def test_repeated_transport_failures_under_a_provider_wait_back_off_beyond_hourly(setup):
    import uuid
    runtime,request=setup
    with patch('claude_swap.org_probe.probe',return_value=dict(observation(),reason='throttled',retryAt=iso(NOW+4*86400))):collect_org(runtime,request)
    def at(seconds):
        runtime.clock=lambda:NOW+seconds
        request['permit'].update(permitId=str(uuid.uuid4()),reservedAt=(NOW+seconds)*1000,transportDeadline=(NOW+seconds)*1000+10000,expiresAt=(NOW+seconds)*1000+30000)
    for seconds in [3600,7200,10800,14400]:
        at(seconds)
        with patch('claude_swap.org_probe.probe',return_value=dict(observation(),reason='transport_failed')) as http:
            assert 'observation' in collect_org(runtime,request)
            http.assert_called_once()
    at(18000)
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        assert collect_org(runtime,request)['reason']=='backing_off'
        http.assert_not_called()
    assert runtime.status()['budget']['machineUsed24h']==5
    at(21600)
    with patch('claude_swap.org_probe.probe',return_value=observation()) as http:
        assert 'observation' in collect_org(runtime,request)
        http.assert_called_once()
