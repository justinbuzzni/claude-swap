"""Subprocess worker for real multi-process rotation contention tests.

Never collected by pytest. Usage: python rotation_contention_worker.py ROLE ROOT [ARG]
The parent sets HOME/XDG_DATA_HOME/CLAUDE_CONFIG_DIR under ROOT. This worker
forces the file backend, forbids network/profile/Keychain access and refuses to
run unless every store path resolves inside ROOT. Coordination uses marker files
in ROOT/sync. Output: one JSON line on stdout.
"""
import json
import os
import sys
import time
from pathlib import Path

ROLE, ROOT = sys.argv[1], Path(sys.argv[2]).resolve()
ARG = sys.argv[3] if len(sys.argv) > 3 else None
SYNC = ROOT / 'sync'
TIMEOUT = 30

for name in ('CLAUDE_SECURESTORAGE_CONFIG_DIR', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
    os.environ.pop(name, None)
if not all(str(Path(os.environ[key]).resolve()).startswith(str(ROOT))
           for key in ('HOME', 'XDG_DATA_HOME', 'CLAUDE_CONFIG_DIR')):
    sys.exit(3)
sys.platform = 'linux'  # file backend only, never the system Keychain

import urllib.request  # noqa: E402


def _forbidden(*_args, **_kwargs):
    raise AssertionError('network forbidden in contention worker')


urllib.request.urlopen = _forbidden
urllib.request.build_opener = _forbidden

from claude_swap import macos_keychain, oauth  # noqa: E402

oauth.fetch_oauth_profile = lambda _token: None
for _name in ('get_password', 'item_exists', 'set_password', 'delete_password'):
    setattr(macos_keychain, _name, _forbidden)

from claude_swap.models import Platform  # noqa: E402
from claude_swap.rotation_owner import LEGACY_OWNER, LeaseStore, writing_as  # noqa: E402
from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: E402
from claude_swap.token_probe import iso  # noqa: E402
from claude_swap.token_runtime import TokenRuntime  # noqa: E402

MODEL = 'fixture-model'


def mark(name):
    (SYNC / name).write_text(str(time.time()))


def wait(name):
    deadline = time.time() + TIMEOUT
    while not (SYNC / name).exists():
        if time.time() > deadline:
            raise TimeoutError(name)
        time.sleep(0.005)


def switcher():
    instance = ClaudeAccountSwitcher()
    instance.platform = Platform.LINUX
    for path in (instance.backup_dir, instance._get_claude_config_path()):
        if not str(Path(path).resolve()).startswith(str(ROOT)):
            sys.exit(3)
    return instance


def obs(ref, generation, pct, now):
    return {'version': 1, 'source': 'inference_probe', 'observedAt': iso(now), 'accountRef': ref,
            'credentialGeneration': generation, 'retryAt': None, 'authState': 'usable',
            'reason': 'coverage_verified',
            'coverage': {'model': MODEL, 'requiredWindows': ['unified5h', 'unified7d', 'modelWeekly']},
            'windows': [{'kind': 'unified5h', 'pct': pct, 'resetsAt': iso(now + 4000), 'status': None},
                        {'kind': 'unified7d', 'pct': 10, 'resetsAt': iso(now + 7000), 'status': None},
                        {'kind': 'modelWeekly', 'model': MODEL, 'pct': pct, 'resetsAt': iso(now + 5000), 'status': None}]}


def setup():
    s = switcher()
    s.add_account_from_token(token='sk-ant-oat01-contention-one', email='one@token.local')
    s.add_account_from_token(token='sk-ant-oat01-contention-two', email='two@token.local')
    s.switch_to('1', json_output=True)
    runtime = TokenRuntime(s)
    runtime.status()
    state = json.loads(runtime.state_file.read_text())
    now = time.time()
    # Push the roster's last active change out of the cooldown window.
    data = json.loads(s.sequence_file.read_text())
    data['lastActiveChangeAt'] = now - 3600
    s.sequence_file.write_text(json.dumps(data))
    refs = {}
    for ref, account in state['accounts'].items():
        account.update(authState='usable', consent=True,
                       observation=obs(ref, account['generation'], 95 if account['slot'] == '1' else 40, now))
        refs[account['slot']] = ref
    runtime.state_file.write_text(json.dumps(state))
    runtime.set_auto(True)
    return {'refs': refs, 'revision': json.loads(s.sequence_file.read_text())['selectionRevision']}


def inspect():
    s = switcher()
    runtime = TokenRuntime(s)
    data = json.loads(s.sequence_file.read_text())
    return {'revision': data['selectionRevision'], 'lastSelection': data.get('lastSelection'),
            'active': data.get('activeAccountNumber'), 'live': s._get_current_account()[0],
            'receipts': runtime.journal.receipts(), 'pending': runtime.journal.pending()}


def delayed_apply(owner, before_lock):
    """Controller apply whose switch preflight blocks on the peer's markers."""
    s = switcher()
    runtime = TokenRuntime(s)
    real = s._prefetch_live_identity

    def slow():
        mark('a-preflight')
        before_lock()
        return real()
    s._prefetch_live_identity = slow
    result = runtime.rotate(MODEL, apply=True, owner=owner)
    return {'applied': result['applied'], 'reason': result['reason'], 'receipt': result.get('receipt'),
            'returnedAt': time.time()}


def lease_loop(owner):
    store = LeaseStore(ROOT / 'lease')
    holder_file = ROOT / 'lease' / 'rotation-owner.v1.json'
    peer = 'legacy-auto' if owner != LEGACY_OWNER else 'proc-x'
    mark(f'lease-ready-{owner}')
    wait(f'lease-ready-{peer}')  # both interpreters are up before contending
    wins, intervals, verified = 0, [], True
    stop = time.time() + 1.5
    while time.time() < stop:
        held = store.claim_legacy(ttl=60) if owner == LEGACY_OWNER else store.acquire(owner, ttl=60)
        if held:
            start = time.time_ns()
            for _check in range(3):
                verified &= json.loads(holder_file.read_text())['holder']['owner'] == owner
                time.sleep(0.002)
            end = time.time_ns()
            assert store.release(owner)
            wins += 1
            intervals.append([start, end])
        time.sleep(0.001)
    return {'owner': owner, 'wins': wins, 'intervals': intervals, 'verified': verified}


def main():
    SYNC.mkdir(parents=True, exist_ok=True)
    (ROOT / 'lease').mkdir(exist_ok=True)
    if ROLE == 'setup':
        out = setup()
    elif ROLE == 'inspect':
        out = inspect()
    elif ROLE == 'lease-loop':
        out = lease_loop(ARG)
    elif ROLE == 'apply-waits-for-manual':
        out = delayed_apply('proc-a', lambda: wait('b-done'))
    elif ROLE == 'manual-switch':
        wait('a-preflight')
        with writing_as('manual-cli'):
            switcher().switch_to('2', json_output=True)
        mark('b-done')
        out = {'switched': True}
    elif ROLE == 'apply-holds-state':
        out = delayed_apply('proc-a', lambda: (wait('b-attempting'), time.sleep(0.8)))
    elif ROLE == 'revoke-consent':
        wait('a-preflight')
        runtime = TokenRuntime(switcher())
        mark('b-attempting')
        started = time.time()
        runtime.consent(ARG, enabled=False)
        out = {'started': started, 'finished': time.time()}
    elif ROLE == 'apply-race':
        s = switcher()
        real = s._prefetch_live_identity
        peer = 'proc-b' if ARG == 'proc-a' else 'proc-a'

        def barrier():
            # Both racers decided: give the peer up to 3s to reach its commit too,
            # so an exclusion bug lets two decisions commit back to back.
            mark(f'ready-{ARG}')
            deadline = time.time() + 3
            while not (SYNC / f'ready-{peer}').exists() and time.time() < deadline:
                time.sleep(0.005)
            return real()
        s._prefetch_live_identity = barrier
        wait('go')
        result = TokenRuntime(s).rotate(MODEL, apply=True, owner=ARG)
        out = {'owner': ARG, 'applied': result['applied'], 'reason': result['reason'],
               'reachedCommit': (SYNC / f'ready-{ARG}').exists()}
    else:
        raise SystemExit(f'unknown role {ROLE}')
    print(json.dumps(out))


if __name__ == '__main__':
    main()
