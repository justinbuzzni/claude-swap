"""Real two-process contention over the file backend (no shared Python state).

Every worker is a separate interpreter with its own FileLock handles; the only
shared medium is the isolated store on disk, as in production.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).with_name('rotation_contention_worker.py')
SRC = Path(__file__).resolve().parents[1] / 'src'

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX flock contention')


def _env(root: Path) -> dict:
    home = root / 'home'
    (home / '.claude').mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if key not in ('CLAUDE_SECURESTORAGE_CONFIG_DIR', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN')}
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_DATA_HOME=str(home / 'data'),
               CLAUDE_CONFIG_DIR=str(home / '.claude'), PYTHONPATH=str(SRC))
    return env


def _spawn(root: Path, role: str, arg: str | None = None) -> subprocess.Popen:
    args = [sys.executable, str(WORKER), role, str(root)] + ([arg] if arg else [])
    return subprocess.Popen(args, env=_env(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _result(process: subprocess.Popen) -> dict:
    out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err[-2000:]
    return json.loads(out.strip().splitlines()[-1])


def _run(root: Path, role: str, arg: str | None = None) -> dict:
    return _result(_spawn(root, role, arg))


def _go(root: Path) -> None:
    (root / 'sync').mkdir(parents=True, exist_ok=True)
    (root / 'sync' / 'go').write_text('go')


def test_lease_is_mutually_exclusive_across_processes(tmp_path):
    workers = [_spawn(tmp_path, 'lease-loop', 'proc-x'), _spawn(tmp_path, 'lease-loop', 'legacy-auto')]
    results = [_result(worker) for worker in workers]
    assert all(result['verified'] for result in results)
    assert all(result['wins'] > 0 for result in results), results  # both really contended
    a, b = (result['intervals'] for result in results)
    overlaps = [(x, y) for x in a for y in b if x[0] < y[1] and y[0] < x[1]]
    assert overlaps == []


def test_manual_writer_in_another_process_rejects_the_delayed_commit(tmp_path):
    before = _run(tmp_path, 'setup')
    controller = _spawn(tmp_path, 'apply-waits-for-manual')
    manual = _spawn(tmp_path, 'manual-switch')
    assert _result(manual) == {'switched': True}
    applied = _result(controller)
    assert applied['applied'] is False
    assert applied['receipt']['status'] == 'rejected'
    assert applied['receipt']['reason'] == 'selection_changed'
    after = _run(tmp_path, 'inspect')
    assert after['revision'] == before['revision'] + 1  # only the manual write landed
    assert after['lastSelection']['writer'] == 'manual-cli'
    assert after['live'] == 'two@token.local' and after['pending'] is None


def test_consent_change_in_another_process_waits_for_the_inflight_commit(tmp_path):
    before = _run(tmp_path, 'setup')
    controller = _spawn(tmp_path, 'apply-holds-state')
    revoke = _spawn(tmp_path, 'revoke-consent', before['refs']['2'])
    revoked = _result(revoke)
    applied = _result(controller)
    # The commit was decided and written under the state lock; the revocation
    # was serialized after it instead of interleaving with the write.
    assert applied['applied'] is True and applied['receipt']['status'] == 'applied'
    assert revoked['finished'] >= applied['receipt']['finishedAtEpoch']
    assert revoked['finished'] - revoked['started'] >= 0.5
    assert _run(tmp_path, 'inspect')['revision'] == before['revision'] + 1


def test_two_controllers_in_separate_processes_commit_at_most_once(tmp_path):
    before = _run(tmp_path, 'setup')
    racers = [_spawn(tmp_path, 'apply-race', 'proc-a'), _spawn(tmp_path, 'apply-race', 'proc-b')]
    _go(tmp_path)
    results = [_result(racer) for racer in racers]
    assert sum(result['applied'] for result in results) <= 1, results
    assert sum(result['reachedCommit'] for result in results) == 1, results  # never two commit attempts
    after = _run(tmp_path, 'inspect')
    assert after['revision'] - before['revision'] == sum(result['applied'] for result in results)
    assert after['pending'] is None
