"""Run after uv build + uv pip install --target dist/pack-smoke/install.

Uses only the installed wheel, an isolated worktree home and file backend.
Run: uv run --frozen python3 tests/pack_token_runtime.py
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import urllib.request
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    install = root / 'dist/pack-smoke/install'
    home = root / 'dist/pack-smoke/home'
    home.mkdir(parents=True, exist_ok=True)
    for name in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'XDG_DATA_HOME',
                 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(name, None)
    os.environ.update(HOME=str(home), XDG_DATA_HOME=str(home / 'data'),
                      CLAUDE_CONFIG_DIR=str(home / 'claude'))
    # Pack validation is for the file backend, never the system Keychain.
    sys.platform = 'linux'
    sys.path.insert(0, str(install))

    def network_forbidden(*args, **kwargs):
        raise AssertionError('network forbidden in artifact smoke')

    urllib.request.urlopen = network_forbidden
    urllib.request.build_opener = network_forbidden
    import claude_swap
    from claude_swap.cli import main as cli_main
    from claude_swap.token_runtime import TokenRuntime
    from claude_swap.transfer import export_accounts, import_accounts

    assert str(claude_swap.__file__).startswith(str(install))
    output = io.StringIO()
    sys.argv = ['cswap', 'token-runtime', 'capabilities']
    with contextlib.redirect_stdout(output):
        cli_main()
    caps = json.loads(output.getvalue())
    assert caps['artifact'] == 'saycode-setup-token-runtime-v1'
    assert caps['rotationWriterOwnership'] is True and caps['rotationReceipt'] is True
    assert caps['automaticRotation'] is False and caps['externalWriterExclusion'] is False
    switcher = claude_swap.ClaudeAccountSwitcher()
    identity = '11111111-1111-4111-8111-111111111111'
    payload = {'version': 1, 'encrypted': False, 'accounts': [{
        'number': 1, 'email': identity + '@token.local', 'kind': 'oauth',
        'credentialType': 'setup_token', 'managedAccountId': identity,
        'credentialGeneration': 1, 'displayName': 'Pack fixture',
        'credentials': {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-pack-fixture',
                                        'scopes': ['user:inference']}},
        'config': {'oauthAccount': {'emailAddress': identity + '@token.local'}}}]}
    # Repeatable: import a fixture above this smoke home's previous generation.
    existing = switcher._get_sequence_data() or {}
    previous = [a.get('credentialGeneration', 0) for a in existing.get('accounts', {}).values()
                if a.get('managedAccountId') == identity]
    payload['accounts'][0]['credentialGeneration'] = max(previous, default=0) + 1
    fixture = home / 'fixture.json'
    fixture.write_text(json.dumps(payload))
    fixture.chmod(0o600)
    import_accounts(switcher, str(fixture))
    row = TokenRuntime(switcher).status()['accounts'][0]
    assert row['number'] == 1 and row['managedAccountId'] == identity and not row['probeEnabled']
    payload['accounts'][0]['credentialGeneration'] += 1
    payload['accounts'][0]['credentials']['claudeAiOauth']['accessToken'] = 'sk-ant-oat01-pack-replacement-' + str(payload['accounts'][0]['credentialGeneration'])
    fixture.write_text(json.dumps(payload))
    import_accounts(switcher, str(fixture))
    replacement = TokenRuntime(switcher).status()['accounts'][0]
    assert replacement['accountRef'] == row['accountRef']
    assert replacement['credentialGeneration'] == row['credentialGeneration'] + 1
    # Real installed-artifact org bridge with a fake HTTP opener; all other network stays blocked.
    import time
    import uuid
    from unittest.mock import patch
    from claude_swap.org_probe import collect_org
    now = time.time()
    generation = replacement['credentialGeneration']
    permit = dict(version=1, permitId=str(uuid.uuid4()), companyId='pack-company', machineId='pack-machine',
                  managedAccountId=identity, credentialGeneration=generation, policyRevision=1,
                  reservedAt=int(now*1000), transportDeadline=int(now*1000)+10000,
                  expiresAt=int(now*1000)+30000, timeoutMs=10000, accountRemaining=95, companyRemaining=287)
    context = {k:permit[k] for k in ('companyId','machineId','managedAccountId','credentialGeneration','policyRevision')}
    context['accountRef'] = replacement['accountRef']
    class Response:
        status = 200
        headers = {'anthropic-ratelimit-unified-5h-utilization':'0.4','anthropic-ratelimit-unified-5h-reset':str(now+3600)}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{}'
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.anthropic.com/v1/messages'
            assert 0 < timeout <= 10
            assert json.loads(request.data)['messages'] == [{'role':'user','content':'Hi'}]
            return Response()
    grant = dict(version=1, context=context, permit=permit, online=True, inUse=True)
    with patch('urllib.request.build_opener', return_value=Opener()):
        result = collect_org(TokenRuntime(switcher), grant)
    assert result['observation']['windows'][0]['pct'] == 40
    assert collect_org(TokenRuntime(switcher), grant)['reason'] == 'permit_replayed'
    assert 'sk-ant-oat01-' not in json.dumps(result)
    # Personal leg: generation-fenced consent, one fake-HTTP probe, durable budget metadata.
    personal_token = 'sk-ant-oat01-pack-personal-' + uuid.uuid4().hex  # new token per run: no cooldown reset
    switcher.add_account_from_token(token=personal_token, email='personal-pack@token.local')
    personal = TokenRuntime(switcher)
    prow = next(r for r in personal.status()['accounts'] if r['roster']['email'] == 'personal-pack@token.local')
    assert 'managedAccountId' not in prow and prow['probeBudget']['accountUsed24h'] == 0
    try:
        personal.consent(prow['accountRef'], enabled=True, ack_cost=True,
                         expected_generation=prow['credentialGeneration'] + 1)
        raise AssertionError('stale generation consent accepted')
    except ValueError as error:
        assert str(error) == 'credential_changed'
    personal.consent(prow['accountRef'], enabled=True, ack_cost=True, expected_generation=prow['credentialGeneration'])
    with patch('urllib.request.build_opener', return_value=Opener()):
        collected = personal.collect(prow['accountRef'], in_use=True, expected_generation=prow['credentialGeneration'])
    assert collected['account']['observation']['windows'][0]['pct'] == 40
    budget = next(r for r in personal.status()['accounts'] if r['accountRef'] == prow['accountRef'])['probeBudget']
    assert budget['accountUsed24h'] == 1 and budget['nextProbeAt'] is not None
    assert personal_token not in json.dumps(personal.status())
    # Rotation controller in the installed wheel: roster revision stamped,
    # lease fencing, and production (unknown) coverage never applies.
    from claude_swap.rotation_owner import LeaseStore
    switcher.switch_to('1', json_output=True)
    assert (switcher._get_sequence_data() or {}).get('selectionRevision', 0) >= 1
    runtime = TokenRuntime(switcher)
    runtime.set_auto(True)
    decision = runtime.rotate('claude-haiku-4-5-20251001', apply=True, owner='pack-smoke')
    assert decision['applied'] is False and decision['reason'] == 'coverage_unknown'
    assert 'receipt' not in decision and runtime.receipts()['pending'] is None
    runtime.set_auto(False)
    leases = LeaseStore(switcher.backup_dir)
    assert leases.acquire('pack-smoke') and not leases.claim_legacy()
    assert leases.release('pack-smoke') and leases.claim_legacy()
    leases_state = json.loads((switcher.backup_dir / 'rotation-owner.v1.json').read_text())
    leases_state['holder'] = None
    (switcher.backup_dir / 'rotation-owner.v1.json').write_text(json.dumps(leases_state))
    export_accounts(switcher, str(fixture))
    exported = json.loads(fixture.read_text())['accounts'][0]
    assert exported['displayName'] == 'Pack fixture' and exported['kind'] == 'oauth'
    with zipfile.ZipFile(root / 'dist/claude_swap-0.27.0b1-py3-none-any.whl') as pack:
        assert {'claude_swap/token_probe.py', 'claude_swap/token_runtime.py', 'claude_swap/org_probe.py',
                'claude_swap/rotation_owner.py'}.issubset(pack.namelist())
        assert 'saycode-setup-token-runtime-v1' in pack.read('claude_swap/token_runtime.py').decode()
    with tarfile.open(root / 'dist/claude_swap-0.27.0b1.tar.gz') as pack:
        names = pack.getnames()
        assert not any('/.uv-cache/' in name or '/.venv/' in name or '/.ruff_cache/' in name for name in names)
        assert any(name.endswith('specs/token-runtime-contract.md') for name in names)
    print('Installed-wheel smoke passed: marker, managed import/status/replacement/export, '
          'org fake-HTTP/replay, personal generation CAS/probe/budget, '
          'rotation revision/lease/fail-closed controller, archive contents; '
          'offline file backend')


if __name__ == '__main__':
    main()
