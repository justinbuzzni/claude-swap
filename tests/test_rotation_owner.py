"""Rotation writer ownership, CAS commit, receipt and recovery boundaries."""
import json
from unittest.mock import patch

import pytest

from claude_swap.exceptions import SwitchError
from claude_swap.models import Platform
from claude_swap.rotation_owner import (
    LEGACY_OWNER,
    LeaseStore,
    RotationJournal,
    SelectionConflict,
    writing_as,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.token_probe import iso
from claude_swap.token_runtime import TokenRuntime, capabilities

NOW = 2000000000
MODEL = 'fixture-model'


def complete_obs(ref, generation, pct, *, now=NOW, coverage=True):
    obs = {'version': 1, 'source': 'inference_probe', 'observedAt': iso(now), 'accountRef': ref,
           'credentialGeneration': generation, 'retryAt': None, 'authState': 'usable',
           'reason': 'coverage_verified' if coverage else 'coverage_unknown',
           'coverage': {'model': MODEL, 'requiredWindows': ['unified5h', 'unified7d', 'modelWeekly']} if coverage else 'unknown',
           'windows': [{'kind': 'unified5h', 'pct': pct, 'resetsAt': iso(now + 4000), 'status': None},
                       {'kind': 'unified7d', 'pct': 10, 'resetsAt': iso(now + 7000), 'status': None},
                       {'kind': 'modelWeekly', 'model': MODEL, 'pct': pct, 'resetsAt': iso(now + 5000), 'status': None}]}
    return obs


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def pool(temp_home):
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    switcher.add_account_from_token(token='sk-ant-oat01-fixture-one', email='one@token.local')
    switcher.add_account_from_token(token='sk-ant-oat01-fixture-two', email='two@token.local')
    switcher.switch_to('1', json_output=True)
    clock = Clock()
    runtime = TokenRuntime(switcher, clock=clock)
    runtime.clock_ref = clock
    return runtime


def seq(runtime):
    return json.loads(runtime.switcher.sequence_file.read_text())


def refs(runtime):
    return {row['number']: row['accountRef'] for row in runtime.status()['accounts']}


def inject(runtime, pcts, *, coverage=True):
    runtime.status()
    state = json.loads(runtime.state_file.read_text())
    for ref, account in state['accounts'].items():
        account['authState'] = 'usable'
        account['consent'] = True
        account['observation'] = complete_obs(ref, account['generation'], pcts[int(account['slot'])],
                                              now=runtime.clock(), coverage=coverage)
    runtime.state_file.write_text(json.dumps(state))


def armed(runtime, pcts=None):
    inject(runtime, pcts or {1: 95, 2: 40})
    runtime.set_auto(True)
    return runtime


def live_email(runtime):
    return runtime.switcher._get_current_account()[0]


# -- every writer participates in the selection revision ---------------------

def test_every_roster_selection_write_bumps_revision_and_stamps_writer(pool):
    before = seq(pool)
    assert before['selectionRevision'] >= 1
    with writing_as('manual-cli'):
        pool.switcher.switch_to('2', json_output=True)
    after = seq(pool)
    assert after['selectionRevision'] == before['selectionRevision'] + 1
    assert after['lastSelection']['writer'] == 'manual-cli'
    assert after['lastActiveChangeAt'] is not None
    pool.switcher.set_account_disabled('1', True)
    assert seq(pool)['selectionRevision'] == after['selectionRevision'] + 1


def test_non_selection_rewrite_keeps_revision(pool):
    data = seq(pool)
    revision = data['selectionRevision']
    data['lastUpdated'] = 'later'
    data['selectionRevision'] = 0  # stale in-memory copy must not regress the counter
    pool.switcher._write_json(pool.switcher.sequence_file, data)
    assert seq(pool)['selectionRevision'] == revision


# -- lease exclusivity ---------------------------------------------------------

def test_lease_is_exclusive_fenced_and_owner_released(pool):
    clock = Clock()
    leases = LeaseStore(pool.switcher.backup_dir, clock=clock)
    first = leases.acquire('desk-1', ttl=600)
    assert first and first['epoch'] == 1
    assert leases.acquire('desk-2', ttl=600) is None
    assert leases.claim_legacy() is False
    assert leases.release('desk-2') is False
    clock.now += 601
    second = leases.acquire('desk-2', ttl=600)
    assert second['epoch'] == 2
    assert leases.check('desk-1', 1) is False
    assert leases.release('desk-2') is True
    assert leases.claim_legacy() is True
    assert leases.acquire('desk-1', ttl=600) is None  # legacy engines now own the pool


def test_legacy_engine_switch_refused_inside_lock_while_enhanced_owner_holds_lease(pool):
    LeaseStore(pool.switcher.backup_dir).acquire('desk-1', ttl=600)
    guard = LeaseStore(pool.switcher.backup_dir).legacy_guard()
    with pytest.raises(SelectionConflict) as caught:
        pool.switcher.switch_to('2', json_output=True, guard=guard)
    assert caught.value.reason == 'rotation_owned_elsewhere'
    assert live_email(pool) == 'one@token.local'


# -- controller ------------------------------------------------------------------

def test_unknown_coverage_never_applies_even_when_armed(pool):
    inject(pool, {1: 99, 2: 1}, coverage=False)
    pool.set_auto(True)
    result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['applied'] is False and result['suggestedRef'] is None
    assert result['reason'] == 'coverage_unknown'
    assert live_email(pool) == 'one@token.local'
    assert 'receipt' not in result


def test_apply_requires_explicit_auto_opt_in(pool):
    inject(pool, {1: 95, 2: 40})
    result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['applied'] is False and result['reason'] == 'auto_disabled'
    assert result['suggestedRef'] == refs(pool)[2]
    assert live_email(pool) == 'one@token.local'


def test_verified_apply_records_receipt_then_cooldown_and_turn_guard(pool):
    armed(pool)
    result = pool.rotate(MODEL, apply=True, owner='desk-1', turn_id='turn-1')
    assert result['applied'] is True
    receipt = result['receipt']
    assert receipt['status'] == 'applied'
    assert receipt['toRef'] == refs(pool)[2] and receipt['fromRef'] == refs(pool)[1]
    assert live_email(pool) == 'two@token.local'
    assert seq(pool)['lastSelection'] == {**seq(pool)['lastSelection'], 'writer': 'setup-token-auto', 'intentId': receipt['intentId']}
    # Swing back is blocked by cooldown, then by the turn guard after cooldown.
    inject(pool, {1: 40, 2: 95})
    assert pool.rotate(MODEL, apply=True, owner='desk-1', turn_id='turn-2')['reason'] == 'cooldown'
    pool.clock_ref.now += 301
    inject(pool, {1: 40, 2: 95})
    again = pool.rotate(MODEL, apply=True, owner='desk-1', turn_id='turn-1')
    assert again['applied'] is False and again['reason'] == 'turn_already_switched'
    assert live_email(pool) == 'two@token.local'
    assert json.dumps(result).find('fixture-two') == -1


def test_manual_switch_counts_for_cooldown(pool):
    armed(pool)
    with writing_as('manual-cli'):
        pool.switcher.switch_to('2', json_output=True)
        pool.switcher.switch_to('1', json_output=True)
    pool.clock_ref.now = seq(pool)['lastActiveChangeAt'] + 10
    armed(pool)
    assert pool.rotate(MODEL, apply=True, owner='desk-1')['reason'] == 'cooldown'


def test_manual_writer_between_decision_and_commit_rejects_without_switching(pool):
    armed(pool)
    real = pool.switcher.switch_to

    def racing(identifier, **kwargs):
        with writing_as('menubar'):
            pool.switcher.set_account_disabled('2', False)  # any roster selection write
            data = seq(pool)
            data['accounts']['1']['pinned'] = False
            pool.switcher._write_json(pool.switcher.sequence_file, data)
        return real(identifier, **kwargs)

    with patch.object(pool.switcher, 'switch_to', side_effect=racing):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['applied'] is False
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['reason'] == 'selection_changed'
    assert live_email(pool) == 'one@token.local'


def test_old_binary_unstamped_write_is_detected(pool):
    armed(pool)
    real = pool.switcher.switch_to

    def legacy_binary(identifier, **kwargs):
        # An upstream build rewrites the roster without knowing the revision.
        path = pool.switcher.sequence_file
        data = json.loads(path.read_text())
        data['sequence'] = list(reversed(data['sequence']))
        path.write_text(json.dumps(data))
        return real(identifier, **kwargs)

    with patch.object(pool.switcher, 'switch_to', side_effect=legacy_binary):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['receipt']['status'] == 'rejected'
    assert live_email(pool) == 'one@token.local'


def test_external_live_drift_fails_closed(pool):
    armed(pool)
    config = pool.switcher._get_claude_config_path()
    data = json.loads(config.read_text())
    data['oauthAccount']['emailAddress'] = 'elsewhere@example.com'
    config.write_text(json.dumps(data))
    result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['applied'] is False and result['reason'] == 'live_selection_drift'


def test_enhanced_controller_yields_to_running_legacy_engine(pool):
    armed(pool)
    assert LeaseStore(pool.switcher.backup_dir, clock=pool.clock).claim_legacy() is True
    result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['applied'] is False and result['reason'] == 'rotation_owned_elsewhere'
    assert live_email(pool) == 'one@token.local'


def test_candidate_replaced_after_decision_is_rejected(pool):
    armed(pool)
    real = pool.switcher.switch_to

    def replace_candidate(identifier, **kwargs):
        pool.switcher._write_account_credentials('2', 'two@token.local', 'sk-ant-oat01-fixture-replaced')
        return real(identifier, **kwargs)

    with patch.object(pool.switcher, 'switch_to', side_effect=replace_candidate):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['reason'] == 'candidate_changed'
    assert live_email(pool) == 'one@token.local'


def test_pinned_current_stays_and_flags_are_preserved(pool):
    pool.pin(refs(pool)[1], True)
    pool.switcher.set_account_disabled('2', False)
    armed(pool)
    result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['applied'] is False
    assert live_email(pool) == 'one@token.local'
    pool.pin(refs(pool)[1], False)
    pool.switcher.set_account_disabled('1', True)  # disabled current: never rotated automatically
    armed(pool)
    assert pool.rotate(MODEL, apply=True, owner='desk-1')['applied'] is False
    pool.switcher.set_account_disabled('1', False)
    armed(pool)
    assert pool.rotate(MODEL, apply=True, owner='desk-1')['applied'] is True
    data = seq(pool)
    assert not data['accounts']['1'].get('pinned') and not data['accounts']['1'].get('disabled')


def test_rolled_back_switch_is_failed_and_does_not_block(pool):
    armed(pool)
    with patch.object(pool.switcher, '_perform_switch', side_effect=SwitchError('Switch failed and was rolled back: disk')):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['receipt']['status'] == 'failed'
    assert pool.rotate(MODEL, apply=False)['reason'] != 'unresolved_receipt'


def test_unknown_commit_outcome_is_unresolved_blocks_auto_until_ack(pool):
    armed(pool)
    with patch.object(pool.switcher, '_perform_switch', side_effect=SwitchError('Switch failed and rollback also failed: disk')):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    receipt = result['receipt']
    assert receipt['status'] == 'unresolved' and result['applied'] is False
    blocked = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert blocked['reason'] == 'unresolved_receipt' and blocked['applied'] is False
    pool.acknowledge(receipt['intentId'])
    # The unresolved commit may have switched, so it still starts the cooldown.
    assert pool.rotate(MODEL, apply=True, owner='desk-1')['reason'] == 'cooldown'
    pool.clock_ref.now += 301
    armed(pool)
    assert pool.rotate(MODEL, apply=True, owner='desk-1')['applied'] is True


def test_crash_after_intent_is_recovered_by_reading_the_roster(pool):
    armed(pool)
    journal = RotationJournal(pool.switcher.backup_dir, clock=pool.clock)
    data = seq(pool)
    journal.begin({'intentId': 'crashed', 'expectedRevision': data['selectionRevision'], 'fromSlot': '1', 'toSlot': '2',
                   'fromIdentity': ['one@token.local', ''], 'toIdentity': ['two@token.local', ''], 'turnId': None})
    result = pool.rotate(MODEL, apply=False)
    assert journal.receipt('crashed')['status'] == 'failed'
    assert journal.receipt('crashed')['reason'] == 'not_committed'
    assert result['reason'] == 'suggestion_only'
    # A pending intent whose stamp is on the roster was committed before the crash.
    journal.begin({'intentId': 'landed', 'expectedRevision': data['selectionRevision'], 'fromSlot': '1', 'toSlot': '2',
                   'fromIdentity': ['one@token.local', ''], 'toIdentity': ['two@token.local', ''], 'turnId': None})
    with writing_as('setup-token-auto', intent_id='landed'):
        pool.switcher.switch_to('2', json_output=True)
    pool.rotate(MODEL, apply=False)
    assert journal.receipt('landed')['status'] == 'applied'


def test_capabilities_report_writer_ownership_without_claiming_external_exclusion():
    caps = capabilities()
    assert caps['rotationWriterOwnership'] is True
    assert caps['rotationReceipt'] is True
    assert caps['externalWriterExclusion'] is False
    assert caps['automaticRotation'] is False  # production coverage is never verified
    assert LEGACY_OWNER == 'legacy-auto'


def test_legacy_engine_yields_to_enhanced_owner_and_keeps_default_switching_otherwise(pool):
    from claude_swap.autoswitch import AutoSwitchEngine, NoSwitchEvent, TickOutcome
    from claude_swap.settings import AutoSwitchSettings
    events = []
    engine = AutoSwitchEngine(pool.switcher, AutoSwitchSettings(), events.append,
                              state_path=pool.switcher.backup_dir / 'autoswitch-fixture.json')
    leases = LeaseStore(pool.switcher.backup_dir)
    leases.acquire('desk-1', ttl=600)
    assert engine._perform('2', 'two@token.local', 'failover', (None, 0.0)) is TickOutcome.NO_ACTION
    assert isinstance(events[-1], NoSwitchEvent) and events[-1].reason == 'rotation-owned'
    assert live_email(pool) == 'one@token.local'
    leases.release('desk-1')
    assert engine._perform('2', 'two@token.local', 'failover', (None, 0.0)) is TickOutcome.SWITCHED
    assert live_email(pool) == 'two@token.local'
    assert seq(pool)['lastSelection']['writer'] == LEGACY_OWNER
    assert leases.current()['owner'] == LEGACY_OWNER


def test_corrupt_ownership_state_fails_closed_for_legacy_and_enhanced(pool):
    from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
    from claude_swap.settings import AutoSwitchSettings
    (pool.switcher.backup_dir / 'rotation-owner.v1.json').write_text('{not json')
    events = []
    engine = AutoSwitchEngine(pool.switcher, AutoSwitchSettings(), events.append,
                              state_path=pool.switcher.backup_dir / 'autoswitch-fixture.json')
    assert engine._perform('2', 'two@token.local', 'failover', (None, 0.0)) is TickOutcome.NO_ACTION
    assert events[-1].detail == 'ownership_state_invalid'
    armed(pool)
    assert pool.rotate(MODEL, apply=True, owner='desk-1')['reason'] == 'ownership_state_invalid'
    assert live_email(pool) == 'one@token.local'


def test_transaction_rollback_write_never_carries_the_controller_intent(pool):
    from claude_swap.models import SwitchTransaction
    transaction = SwitchTransaction('creds', '{}', '1', 'one@token.local', pool.switcher._get_claude_config_path())
    with writing_as('manual-cli'):
        pool.switcher.switch_to('2', json_output=True)
    transaction.completed_steps = ['sequence_updated']
    with writing_as('setup-token-auto', intent_id='intent-x'):
        assert transaction.rollback(pool.switcher) is True
    stamp = seq(pool)['lastSelection']
    assert stamp['intentId'] is None and stamp['writer'] == 'rollback'


# -- the decision itself is revalidated at commit ------------------------------

def _no_write(pool, revision_before, result, decision_reason):
    assert result['applied'] is False
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['reason'] == 'decision_changed'
    assert result['receipt']['decisionReason'] == decision_reason
    assert live_email(pool) == 'one@token.local'
    assert seq(pool)['selectionRevision'] == revision_before


def _between_decision_and_commit(pool, action):
    """Run ``action`` after the suggestion, before the guarded commit starts."""
    real = pool.leases.acquire

    def acquire(owner, **kwargs):
        action()
        return real(owner, **kwargs)
    return patch.object(pool.leases, 'acquire', side_effect=acquire)


def _edit_state(pool, mutate):
    state = json.loads(pool.state_file.read_text())
    mutate(state)
    pool.state_file.write_text(json.dumps(state))


def test_auto_disabled_after_suggestion_is_rejected_at_commit(pool):
    armed(pool)
    revision = seq(pool)['selectionRevision']
    with _between_decision_and_commit(pool, lambda: pool.set_auto(False)):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['receipt']['reason'] == 'auto_disabled'
    assert result['receipt']['status'] == 'rejected'
    assert live_email(pool) == 'one@token.local' and seq(pool)['selectionRevision'] == revision


def test_candidate_consent_revoked_after_suggestion_is_rejected(pool):
    armed(pool)
    revision = seq(pool)['selectionRevision']
    with _between_decision_and_commit(pool, lambda: pool.consent(refs(pool)[2], enabled=False)):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    _no_write(pool, revision, result, 'no_eligible_candidate')


def test_candidate_auth_invalidated_after_suggestion_is_rejected(pool):
    armed(pool)
    revision = seq(pool)['selectionRevision']
    target = refs(pool)[2]
    with _between_decision_and_commit(pool, lambda: _edit_state(
            pool, lambda state: state['accounts'][target].update(authState='invalid'))):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    _no_write(pool, revision, result, 'no_eligible_candidate')


def test_cooldown_started_elsewhere_after_suggestion_is_rejected(pool):
    armed(pool)
    revision = seq(pool)['selectionRevision']

    def other_receipt():
        pool.journal.finish({'intentId': 'other-controller'}, 'applied', finishedAtEpoch=pool.clock())
    with _between_decision_and_commit(pool, other_receipt):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    _no_write(pool, revision, result, 'cooldown')


def _slow_preflight(pool, action):
    real = pool.switcher._prefetch_live_identity

    def slow():
        action()
        return real()
    return patch.object(pool.switcher, '_prefetch_live_identity', side_effect=slow)


def test_observation_ttl_crossed_during_switch_preflight_is_rejected(pool):
    armed(pool)
    revision = seq(pool)['selectionRevision']

    def wait():
        pool.clock_ref.now += 301
    with _slow_preflight(pool, wait):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    _no_write(pool, revision, result, 'coverage_unknown')


def test_candidate_reset_crossed_during_switch_preflight_is_rejected(pool):
    armed(pool)
    target = refs(pool)[2]
    _edit_state(pool, lambda state: state['accounts'][target]['observation']['windows'][0].update(resetsAt=iso(NOW + 100)))
    revision = seq(pool)['selectionRevision']

    def wait():
        pool.clock_ref.now += 150
    with _slow_preflight(pool, wait):
        result = pool.rotate(MODEL, apply=True, owner='desk-1')
    _no_write(pool, revision, result, 'no_eligible_candidate')


def test_consent_revoked_by_suggestion_time_never_suggests(pool):
    armed(pool)
    pool.consent(refs(pool)[2], enabled=False)
    result = pool.rotate(MODEL, apply=True, owner='desk-1')
    assert result['suggestedRef'] is None and result['applied'] is False
