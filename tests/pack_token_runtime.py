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
    assert json.loads(output.getvalue())['artifact'] == 'saycode-setup-token-runtime-v1'
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
    payload['accounts'][0]['credentials']['claudeAiOauth']['accessToken'] = 'sk-ant-oat01-pack-replacement'
    fixture.write_text(json.dumps(payload))
    import_accounts(switcher, str(fixture))
    replacement = TokenRuntime(switcher).status()['accounts'][0]
    assert replacement['accountRef'] == row['accountRef']
    assert replacement['credentialGeneration'] == row['credentialGeneration'] + 1
    export_accounts(switcher, str(fixture))
    exported = json.loads(fixture.read_text())['accounts'][0]
    assert exported['displayName'] == 'Pack fixture' and exported['kind'] == 'oauth'
    with zipfile.ZipFile(root / 'dist/claude_swap-0.27.0b1-py3-none-any.whl') as pack:
        assert {'claude_swap/token_probe.py', 'claude_swap/token_runtime.py'}.issubset(pack.namelist())
        assert 'saycode-setup-token-runtime-v1' in pack.read('claude_swap/token_runtime.py').decode()
    with tarfile.open(root / 'dist/claude_swap-0.27.0b1.tar.gz') as pack:
        names = pack.getnames()
        assert not any('/.uv-cache/' in name or '/.venv/' in name or '/.ruff_cache/' in name for name in names)
        assert any(name.endswith('specs/token-runtime-contract.md') for name in names)
    print('Installed-wheel smoke passed: marker, managed import/status/replacement/export, archive contents; offline file backend')


if __name__ == '__main__':
    main()
