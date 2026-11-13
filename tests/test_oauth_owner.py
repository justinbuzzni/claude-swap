"""The transfer boundary must never export a refresh grant."""
import json
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.oauth_owner import access_only, refresh_owner
from tests.test_transfer import _linux_switcher, _seed_account


def test_access_only_removes_refresh_secrets_and_preserves_other_kinds():
    source = {'version': 1, 'accounts': [
        {'email': 'a@example.com', 'credentials': {'claudeAiOauth': {
            'accessToken': 'fake-access', 'refreshToken': 'fake-secret',
            'refreshTokenExpiresAt': 123, 'expiresAt': 9999999999999}}},
        {'email': 'setup@example.com', 'credentials': {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-fake'}}},
        {'email': 'api@example.com', 'credentials': 'sk-ant-api-fake'}]}
    result = access_only(source)
    assert 'fake-secret' not in json.dumps(result)
    assert result['accounts'][0]['credentials']['claudeAiOauth']['saycodeRefreshOwnerVersion'] == 1
    assert 'refreshTokenExpiresAt' not in result['accounts'][0]['credentials']['claudeAiOauth']
    assert result['accounts'][1:] == source['accounts'][1:]
    assert source['accounts'][0]['credentials']['claudeAiOauth']['refreshToken'] == 'fake-secret'


def test_target_never_posts_even_if_a_refresh_secret_was_reintroduced():
    credentials = json.dumps({'claudeAiOauth': {'accessToken': 'fake', 'refreshToken': 'must-not-post',
                                              'saycodeRefreshOwnerVersion': 1}})
    with patch('urllib.request.urlopen') as request:
        outcome = oauth.try_refresh_oauth_credentials(credentials)
    request.assert_not_called()
    assert outcome.error == 'owner_refresh_required'


def test_unknown_owner_contract_is_rejected():
    with pytest.raises(ValueError):
        access_only({'accounts': [{'credentials': {'claudeAiOauth': {
            'accessToken': 'fake', 'saycodeRefreshOwnerVersion': 2}}}]})


def test_refresh_does_not_consume_a_target_or_a_fresh_source(temp_home):
    switcher = _linux_switcher(temp_home)
    _seed_account(switcher, 1, 'a@example.com', creds={'claudeAiOauth': {
        'accessToken': 'fake-access', 'refreshToken': 'fake-secret', 'expiresAt': 9999999999999}})
    with patch.object(switcher, '_get_current_account', return_value=None), patch.object(switcher, 'consume_backup_grant') as consume:
        result = refresh_owner(switcher, threshold_ms=3600000)
    consume.assert_not_called()
    assert 'fake-secret' not in json.dumps(result)


def test_import_cannot_roll_back_an_owner_account(temp_home):
    from claude_swap.transfer import import_accounts
    switcher = _linux_switcher(temp_home)
    original = {'claudeAiOauth': {'accessToken': 'new', 'expiresAt': 9999999999999,
                                 'saycodeRefreshOwnerVersion': 1}}
    _seed_account(switcher, 1, 'a@example.com', creds=original)
    incoming = {'version': 1, 'encrypted': False, 'accounts': [{
        'number': 1, 'email': 'a@example.com', 'credentials': {'claudeAiOauth': {
            'accessToken': 'old', 'expiresAt': 100, 'saycodeRefreshOwnerVersion': 1}},
        'config': {'oauthAccount': {'emailAddress': 'a@example.com'}}}]}
    file = temp_home / 'incoming.json'
    file.write_text(json.dumps(incoming))
    with pytest.raises(Exception, match='CLAUDE_OWNER_GENERATION_STALE'):
        import_accounts(switcher, str(file), force=True)
    assert json.loads(switcher._read_account_credentials('1', 'a@example.com')) == original


def test_proactive_source_refresh_holds_claude_locks_and_persists_both_stores(temp_home, mock_claude_config):
    import time
    from types import SimpleNamespace
    from claude_swap.claude_locks import oauth_refresh_lock_dir, credentials_lock_dir
    switcher = _linux_switcher(temp_home)
    secret = json.dumps({'claudeAiOauth': {'accessToken': 'old', 'refreshToken': 'old-refresh',
                                         'expiresAt': int(time.time() * 1000) + 60000}})
    _seed_account(switcher, 1, 'test@example.com', creds=json.loads(secret))
    switcher._write_credentials(secret)
    mock_claude_config.write_text(json.dumps({'oauthAccount': {'emailAddress': 'test@example.com'}}))

    def refresh(request, **kwargs):
        assert oauth_refresh_lock_dir().exists()
        assert credentials_lock_dir().exists()
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'access_token': 'new', 'refresh_token': 'new-refresh', 'expires_in': 28800}).encode()
        return response
    with patch('urllib.request.urlopen', side_effect=refresh) as request, patch(
        'claude_swap.oauth.try_fetch_usage_for_account', return_value=SimpleNamespace(usage=None, error=None, retry_after_s=None)):
        switcher._fetch_active_usage('1', 'test@example.com', secret, refresh_before_ms=3600000)
    request.assert_called_once()
    live = json.loads(switcher._read_credentials())
    backup = json.loads(switcher._read_account_credentials('1', 'test@example.com'))
    assert live['claudeAiOauth']['refreshToken'] == 'new-refresh'
    assert backup == live


def test_explicit_target_migration_strips_an_equal_generation_but_default_import_protects_source(temp_home):
    from claude_swap.transfer import import_accounts
    switcher = _linux_switcher(temp_home)
    credentials = {'claudeAiOauth': {'accessToken': 'same', 'refreshToken': 'source-secret', 'expiresAt': 9999999999999}}
    _seed_account(switcher, 1, 'a@example.com', creds=credentials)
    data = switcher._get_sequence_data()
    data['accounts']['1']['disabled'] = True
    switcher._write_json(switcher.sequence_file, data)
    payload = {'version': 1, 'encrypted': False, 'accounts': [{
        'number': 1, 'email': 'a@example.com', 'credentials': credentials,
        'config': {'oauthAccount': {'emailAddress': 'a@example.com'}}}]}
    file = temp_home / 'incoming.json'
    file.write_text(json.dumps(access_only(payload)))
    with pytest.raises(Exception, match='CLAUDE_OWNER_SOURCE_PROTECTED'):
        import_accounts(switcher, str(file), force=True)
    import_accounts(switcher, str(file), force=True, owner_target=True)
    stored = json.loads(switcher._read_account_credentials('1', 'a@example.com'))
    assert stored['claudeAiOauth']['saycodeRefreshOwnerVersion'] == 1
    assert 'refreshToken' not in stored['claudeAiOauth']
    assert switcher._get_sequence_data()['accounts']['1']['disabled'] is True


def test_active_snapshot_does_not_bind_an_unmanaged_personal_login(temp_home):
    from claude_swap.oauth_owner import active_snapshot
    switcher = _linux_switcher(temp_home)
    with patch.object(switcher, '_get_current_account', return_value=('personal@example.com', '')):
        assert active_snapshot(switcher)['accounts'] == []


@pytest.mark.parametrize('kind', ['personal', 'owner', 'partial-import'])
def test_active_snapshot_uses_the_live_identity_and_requires_completed_activation(temp_home, kind):
    from claude_swap.oauth_owner import active_snapshot
    switcher = _linux_switcher(temp_home)
    credentials = {'claudeAiOauth': {'accessToken': 'same', 'expiresAt': 9999999999999}}
    if kind == 'personal':
        credentials['claudeAiOauth']['refreshToken'] = 'personal-refresh'
    else:
        credentials['claudeAiOauth']['saycodeRefreshOwnerVersion'] = 1
    _seed_account(switcher, 1, 'a@example.com', creds=credentials)
    live = credentials if kind != 'partial-import' else {'claudeAiOauth': {'accessToken': 'old', 'refreshToken': 'old-refresh', 'expiresAt': 100}}
    with patch.object(switcher, '_get_current_account', return_value=('a@example.com', '')), patch.object(switcher, '_read_credentials', return_value=json.dumps(live)):
        if kind == 'partial-import':
            with pytest.raises(ValueError, match='owner_activation_required'):
                active_snapshot(switcher)
        else:
            snapshot = active_snapshot(switcher)
            assert 'personal-refresh' not in json.dumps(snapshot)
            assert len(snapshot['accounts']) == (1 if kind == 'owner' else 0)


def test_source_custody_survives_a_newer_target_import_and_is_not_exported(temp_home):
    from claude_swap.oauth_owner import source_export
    from claude_swap.transfer import import_accounts
    switcher = _linux_switcher(temp_home)
    source = {'claudeAiOauth': {'accessToken': 'original', 'refreshToken': 'source-only', 'expiresAt': 9999999999998}}
    _seed_account(switcher, 1, 'a@example.com', creds=source)
    with patch.object(switcher, '_get_current_account', return_value=None):
        payload = source_export(switcher)
    assert 'source-only' not in json.dumps(payload)
    assert 'oauthRefreshOwnerVersion' not in json.dumps(payload)
    assert switcher._get_sequence_data()['accounts']['1']['oauthRefreshOwnerVersion'] == 1
    data = payload['accounts'][0]['credentials']['claudeAiOauth']
    data.update(accessToken='newer-copy', expiresAt=9999999999999)
    file = temp_home / 'incoming.json'; file.write_text(json.dumps(payload))
    with pytest.raises(Exception, match='CLAUDE_OWNER_SOURCE_PROTECTED'):
        import_accounts(switcher, str(file), force=True, owner_target=True)
    assert json.loads(switcher._read_account_credentials('1', 'a@example.com')) == source


def test_a_target_copy_cannot_be_promoted_to_a_source(temp_home):
    from claude_swap.oauth_owner import source_export
    switcher = _linux_switcher(temp_home)
    _seed_account(switcher, 1, 'a@example.com', creds={'claudeAiOauth': {
        'accessToken': 'target-only', 'expiresAt': 9999999999999, 'saycodeRefreshOwnerVersion': 1}})
    with patch.object(switcher, '_get_current_account', return_value=None):
        with pytest.raises(ValueError, match='owner_source_required'):
            source_export(switcher)


def test_restoring_a_normal_source_backup_keeps_its_local_custody(temp_home):
    from claude_swap.oauth_owner import source_export
    from claude_swap.transfer import import_accounts
    switcher = _linux_switcher(temp_home)
    source = {'claudeAiOauth': {'accessToken': 'original', 'refreshToken': 'source-only', 'expiresAt': 9999999999998}}
    _seed_account(switcher, 1, 'a@example.com', creds=source)
    with patch.object(switcher, '_get_current_account', return_value=None):
        source_export(switcher)
    payload = {'version': 1, 'encrypted': False, 'accounts': [{
        'number': 1, 'email': 'a@example.com', 'credentials': source,
        'config': {'oauthAccount': {'emailAddress': 'a@example.com'}}}]}
    file = temp_home / 'backup.json'; file.write_text(json.dumps(payload))
    import_accounts(switcher, str(file), force=True)
    assert switcher._get_sequence_data()['accounts']['1']['oauthRefreshOwnerVersion'] == 1


def _mixed_switcher(temp_home):
    switcher = _linux_switcher(temp_home)
    _seed_account(switcher, 1, 'a@example.com', creds={'claudeAiOauth': {
        'accessToken': 'source-access', 'refreshToken': 'source-only', 'expiresAt': 1}})
    _seed_account(switcher, 2, 'b@example.com', creds={'claudeAiOauth': {
        'accessToken': 'target-only', 'expiresAt': 9999999999999, 'saycodeRefreshOwnerVersion': 1}})
    return switcher


def test_a_received_target_copy_does_not_block_exporting_the_own_source(temp_home):
    from claude_swap.oauth_owner import source_export
    switcher = _mixed_switcher(temp_home)
    with patch.object(switcher, '_get_current_account', return_value=None):
        payload = source_export(switcher)
    assert [account['email'] for account in payload['accounts']] == ['a@example.com']
    assert 'source-only' not in json.dumps(payload)
    assert 'oauthRefreshOwnerVersion' not in switcher._get_sequence_data()['accounts']['2']


def test_a_received_target_copy_does_not_block_refreshing_the_own_source(temp_home):
    switcher = _mixed_switcher(temp_home)
    refreshed = json.dumps({'claudeAiOauth': {
        'accessToken': 'new-access', 'refreshToken': 'new-secret', 'expiresAt': 9999999999999}})

    def consume(number, email, secret):
        switcher._write_account_credentials(number, email, refreshed)
        return oauth.RefreshOutcome(refreshed, None)

    with patch.object(switcher, '_get_current_account', return_value=None), \
            patch.object(switcher, '_live_session_pids', return_value=[]), \
            patch.object(switcher, 'consume_backup_grant', side_effect=consume) as grant:
        result = refresh_owner(switcher, threshold_ms=3600000)
    assert [call.args[1] for call in grant.call_args_list] == ['a@example.com']
    assert [account['email'] for account in result['accounts']] == ['a@example.com']
    assert 'new-secret' not in json.dumps(result)
