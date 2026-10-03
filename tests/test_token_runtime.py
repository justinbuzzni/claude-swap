import json
import threading
from unittest.mock import patch

import pytest

from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.models import Platform
from claude_swap.token_runtime import TokenRuntime, capabilities, command
from claude_swap.token_probe import iso


@pytest.fixture
def runtime(temp_home):
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    switcher.add_account_from_token(token='sk-ant-oat01-fixture-one', email='one@token.local')
    return TokenRuntime(switcher, clock=lambda: 2000000000)


def observation(now=2000000000, reason='coverage_unknown', auth='usable'):
    return {'version':1, 'source':'inference_probe','observedAt':iso(now), 'windows':[
        {'kind':'unified5h','pct':63, 'resetsAt':iso(now+4000),'status':None},
        {'kind':'unified7d','pct':20, 'resetsAt':iso(now+7000),'status':None}],
        'coverage':'unknown','reason':reason,'retryAt':None,'authState':auth}


def first(runtime): return runtime.status()['accounts'][0]


def enable(runtime):
    ref = first(runtime)['accountRef']
    runtime.consent(ref, enabled=True, ack_cost=True)
    return ref


def test_default_off_no_inference_dead_or_identity_claim(runtime):
    row = first(runtime)
    assert row['authState'] == 'expiry-unknown'
    assert row['identityConfidence'] == 'unresolved'
    assert row['usageStatus'] == 'probe-disabled'
    assert row['decisionEligible'] is False
    with patch('claude_swap.token_runtime.probe') as http:
        runtime.collect(row['accountRef'], in_use=True)
        http.assert_not_called()
    assert capabilities()['automaticRotation'] is False


def test_durable_optin_status_does_not_probe_min_interval_and_disable(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe', return_value=observation()) as http:
        runtime.collect(ref, in_use=True)
        other = TokenRuntime(runtime.switcher, clock=runtime.clock)
        assert first(other)['observation']['windows'][0]['pct'] == 63
        assert first(other)['usageStatus'] == 'partial'
        other.collect(ref, in_use=True)
        http.assert_called_once()
        other.consent(ref, enabled=False)
        other.collect(ref, in_use=True)
        http.assert_called_once()
    state = runtime.state_file.read_text()
    assert 'sk-ant-oat01-' not in state
    assert 'fixture-one' not in json.dumps(runtime.status())


def test_offline_and_unused_do_not_spend_budget(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe') as http:
        runtime.collect(ref, in_use=True, offline=True)
        runtime.collect(ref, in_use=False)
        http.assert_not_called()
    assert runtime.status()['budget']['machineUsed24h'] == 0


def test_token_replacement_invalidates_consent_cache_preserves_ref(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe', return_value=observation()):
        runtime.collect(ref, in_use=True)
    row = runtime.switcher._get_sequence_data()['accounts']['1']
    runtime.switcher._write_account_credentials('1', row['email'], json.dumps({'claudeAiOauth':{'accessToken':'sk-ant-oat01-replaced','scopes':['user:inference']}}))
    current = first(runtime)
    assert current['accountRef'] == ref
    assert current['credentialGeneration'] == 2
    assert current['usageStatus'] == 'probe-disabled'
    assert current.get('observation') is None


def test_late_response_discarded_on_replacement(runtime):
    ref = enable(runtime)
    def http(*args, **kwargs):
        row = runtime.switcher._get_sequence_data()['accounts']['1']
        runtime.switcher._write_account_credentials('1', row['email'], json.dumps({'claudeAiOauth':{'accessToken':'sk-ant-oat01-replaced','scopes':['user:inference']}}))
        return observation()
    with patch('claude_swap.token_runtime.probe', side_effect=http):
        result = runtime.collect(ref, in_use=True)
    assert result['reason'] == 'credential_changed'
    assert first(runtime).get('observation') is None
    assert runtime.status()['budget']['machineUsed24h'] == 1


def test_reset_and_ttl_make_last_good_display_only(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe', return_value=observation()): runtime.collect(ref, in_use=True)
    runtime.clock = lambda: 2000000301
    assert first(runtime)['usageStatus'] == 'stale'
    assert first(runtime)['decisionEligible'] is False


def test_duplicate_token_shares_budget_and_interval(runtime):
    ref = enable(runtime)
    runtime.switcher.add_account_from_token(token='sk-ant-oat01-fixture-one', email='two@token.local')
    second = runtime.status()['accounts'][1]['accountRef']
    runtime.consent(second, enabled=True, ack_cost=True)
    with patch('claude_swap.token_runtime.probe', return_value=observation()) as http:
        runtime.collect(ref, in_use=True)
        runtime.collect(second, in_use=True)
        http.assert_called_once()
    assert runtime.status()['budget']['machineUsed24h'] == 1


def test_auth_and_scope_separated_and_backoff_durable(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe', return_value=observation(reason='scope_missing', auth='unverified')) as http:
        runtime.collect(ref, in_use=True)
        assert first(runtime)['usageStatus'] == 'scope-missing'
        assert first(runtime)['authState'] != 'invalid'
        runtime.clock = lambda: 2000000301
        runtime.collect(ref, in_use=True)
        http.assert_called_once()


def test_managed_recipient_cannot_consent(runtime):
    data = runtime.switcher._get_sequence_data()
    data['accounts']['1'].update(managedAccountId='11111111-1111-4111-8111-111111111111', credentialType='setup_token',displayName='조직 계정',credentialGeneration=4)
    runtime.switcher._write_json(runtime.switcher.sequence_file, data)
    row = first(runtime)
    assert row['label'] == '조직 계정'
    assert row['identityConfidence'] == 'user-labeled'
    with pytest.raises(ValueError, match='organization_collector_required'):
        runtime.consent(row['accountRef'], enabled=True, ack_cost=True)


def test_suggestion_unknown_keeps_current_and_never_writes(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe', return_value=observation()): runtime.collect(ref, in_use=True)
    with patch.object(runtime.switcher, 'switch_to') as writer:
        result = runtime.suggest(ref, 'claude-haiku-4-5-20251001')
        writer.assert_not_called()
    assert result['selectedRef'] == ref
    assert result['reason'] == 'coverage_unknown'
    assert result['applied'] is False


def test_cli_capabilities_does_not_construct_switcher(capsys):
    with patch('claude_swap.token_runtime.ClaudeAccountSwitcher', side_effect=AssertionError('store accessed')):
        command(['capabilities'])
    result = json.loads(capsys.readouterr().out)
    assert result['artifact'] == 'saycode-setup-token-runtime-v1'


def test_durable_account_and_machine_budgets_block_transport(runtime):
    ref = enable(runtime)
    state = json.loads(runtime.state_file.read_text())
    digest = state['accounts'][ref]['digest']
    for attempts in ([{'at':runtime.clock(), 'digest':digest}] * 96,
                     [{'at':runtime.clock(), 'digest':'other'}] * 288):
        state['attempts'] = attempts
        runtime._save(state)
        with patch('claude_swap.token_runtime.probe') as http:
            assert runtime.collect(ref, in_use=True)['reason'] == 'budget_exhausted'
            http.assert_not_called()


def test_budget_reservation_failure_prevents_post(runtime):
    ref = enable(runtime)
    with patch.object(runtime, '_save', side_effect=OSError('fixture-write-failed')), patch('claude_swap.token_runtime.probe') as http:
        with pytest.raises(OSError): runtime.collect(ref, in_use=True)
        http.assert_not_called()


def test_corrupt_state_fails_closed(runtime):
    ref = enable(runtime)
    runtime.state_file.write_text('{broken')
    with patch('claude_swap.token_runtime.probe') as http:
        with pytest.raises(ValueError, match='runtime_state_invalid'): runtime.collect(ref, in_use=True)
        http.assert_not_called()


def test_singleflight_across_runtime_instances(runtime):
    ref = enable(runtime)
    started, finish = threading.Event(), threading.Event()
    def http(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return observation()
    result = []
    with patch('claude_swap.token_runtime.probe', side_effect=http) as request:
        thread = threading.Thread(target=lambda: result.append(runtime.collect(ref, in_use=True)))
        thread.start()
        try:
            assert started.wait(5)
            other = TokenRuntime(runtime.switcher, clock=runtime.clock)
            assert other.collect(ref, in_use=True)['reason'] == 'collector_busy'
        finally:
            finish.set()
            thread.join(5)
        request.assert_called_once()
    assert not thread.is_alive()


def test_revocation_while_post_inflight_discards_result(runtime):
    ref = enable(runtime)
    def http(*args, **kwargs):
        TokenRuntime(runtime.switcher, clock=runtime.clock).consent(ref, enabled=False)
        return observation()
    with patch('claude_swap.token_runtime.probe', side_effect=http):
        assert runtime.collect(ref, in_use=True)['reason'] == 'credential_changed'
    assert first(runtime).get('observation') is None


def test_slot_deletion_reuse_new_ref_even_with_same_token(runtime):
    old = first(runtime)['accountRef']
    runtime.switcher.remove_account('1', assume_yes=True)
    runtime.switcher.add_account_from_token(token='sk-ant-oat01-fixture-one', email='one@token.local', slot=1)
    assert first(runtime)['accountRef'] != old


def test_entrypoint_runtime_dispatch_and_safe_error(monkeypatch,capsys):
    from claude_swap import cli
    monkeypatch.setattr('sys.argv', ['cswap','token-runtime','capabilities'])
    cli.main()
    assert json.loads(capsys.readouterr().out)['automaticRotation'] is False
    monkeypatch.setattr('sys.argv', ['cswap','token-runtime','status'])
    with patch('claude_swap.token_runtime.TokenRuntime.status',side_effect=OSError('fixture-secret-never-echo')):
        with pytest.raises(SystemExit): cli.main()
    assert 'fixture-secret' not in capsys.readouterr().out


def policy_row(ref, pct, *, model='fixture-model'):
    obs = observation()
    obs['reason'] = 'coverage_verified'
    obs['coverage'] = {'model':model, 'requiredWindows':['unified5h','unified7d','modelWeekly']}
    obs['windows'].append({'kind':'modelWeekly','model':model,'pct':pct,'resetsAt':iso(2000005000),'status':None})
    obs['windows'][0]['pct'] = pct
    return dict(accountRef=ref, authState='usable', disabled=False, pinned=False, usageStatus='fresh', observation=obs)


def test_suggestion_policy_90_80_max_score_stable_tie_and_cooldown():
    from claude_swap.token_runtime import choose_suggestion
    rows = [policy_row('current',90),policy_row('z',50),policy_row('b',40),policy_row('a',40)]
    assert choose_suggestion(rows, 'current', 'fixture-model', 2000000000)['suggestedRef'] == 'a'
    rows[0]['observation']['windows'][0]['pct'] = 89
    rows[0]['observation']['windows'][2]['pct'] = 89
    assert choose_suggestion(rows, 'current', 'fixture-model', 2000000000)['suggestedRef'] is None
    rows[0] = policy_row('current',95)
    assert choose_suggestion(rows, 'current', 'fixture-model', 2000000000, last_switch_at=1999999900)['reason'] == 'cooldown'


@pytest.mark.parametrize('mutation', ['unknown','stale','reset','disabled','pinned','invalid','missing','boundary80'])
def test_suggestion_candidates_fail_closed(mutation):
    from claude_swap.token_runtime import choose_suggestion
    candidate = policy_row('candidate',40)
    if mutation == 'unknown': candidate['observation']['coverage'] = 'unknown'
    if mutation == 'stale': candidate['observation']['observedAt'] = iso(1999999000)
    if mutation == 'reset': candidate['observation']['windows'][0]['resetsAt'] = iso(2000000000)
    if mutation in ('disabled','pinned'): candidate[mutation] = True
    if mutation == 'invalid': candidate['authState'] = 'invalid'
    if mutation == 'missing': candidate['observation']['windows'].pop()
    if mutation == 'boundary80': candidate['observation']['windows'][0]['pct'] = 80
    result = choose_suggestion([policy_row('current',95),candidate], 'current','fixture-model',2000000000)
    assert result['suggestedRef'] is None
    assert result['selectedRef'] == 'current'


def test_local_selector_and_exact_roster_join_without_active_claim(runtime):
    row = first(runtime)
    assert row['number'] == 1
    assert row['roster'] == {'email':'one@token.local','organizationUuid':'','uuid':''}
    assert 'isActive' not in row and 'activeAccountNumber' not in runtime.status()
    assert row['legacyUsageOwned'] is False


def test_replacement_does_not_reset_machine_or_old_digest_spending(runtime):
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe', return_value=observation()): runtime.collect(ref,in_use=True)
    runtime.switcher.add_account_from_token(token='sk-ant-oat01-new-generation',email='one@token.local')
    runtime.consent(ref,enabled=True,ack_cost=True)
    with patch('claude_swap.token_runtime.probe', return_value=observation()): runtime.collect(ref,in_use=True)
    assert runtime.status()['budget']['machineUsed24h'] == 2
    assert len({a['digest'] for a in json.loads(runtime.state_file.read_text())['attempts']}) == 2


def test_personal_generation_cas_rejects_before_consent_and_transport(runtime):
    row = first(runtime)
    with pytest.raises(ValueError, match='credential_changed'):
        runtime.consent(row['accountRef'], enabled=True, ack_cost=True, expected_generation=2)
    assert first(runtime)['probeEnabled'] is False
    ref = enable(runtime)
    with patch('claude_swap.token_runtime.probe') as http:
        assert runtime.collect(ref, in_use=True, expected_generation=2)['reason'] == 'credential_changed'
        http.assert_not_called()
    assert runtime.status()['budget']['machineUsed24h'] == 0


def test_status_exposes_secret_free_durable_personal_budget_for_scheduler(runtime):
    from claude_swap.token_runtime import _timestamp
    fresh = first(runtime)['probeBudget']
    assert fresh == {'scope': 'local-per-token', 'accountUsed24h': 0, 'accountLimit24h': 96, 'accountRemaining24h': 96,
                     'nextProbeAt': None, 'failureStreak': 0, 'minIntervalSeconds': 300, 'recommendedIntervalSeconds': 900}
    ref = enable(runtime)
    runtime.switcher.add_account_from_token(token='sk-ant-oat01-fixture-one', email='two@token.local')
    with patch('claude_swap.token_runtime.probe', return_value=observation(reason='throttled')):
        runtime.collect(ref, in_use=True)
    status = runtime.status()
    one, alias = status['accounts']
    assert one['probeBudget']['accountUsed24h'] == 1 and one['probeBudget']['accountRemaining24h'] == 95
    assert one['probeBudget']['failureStreak'] == 1
    assert _timestamp(one['probeBudget']['nextProbeAt']) >= 2000000000 + 1800  # durable backoff, not the minimum
    assert alias['probeBudget'] == one['probeBudget']  # aliases of one token share one bucket
    assert status['budget']['machineRemaining24h'] == 287
    assert _timestamp(status['budget']['machineSlotFreesAt']) == 2000000000 + 86400
    state = json.loads(runtime.state_file.read_text())
    exposed = json.dumps(status)
    assert all(digest not in exposed for digest in state['tokens'])
    assert 'fixture-one' not in exposed
