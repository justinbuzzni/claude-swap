"""Source-only OAuth rotation and access-only machine transfers."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import math
import time

from claude_swap import oauth
from claude_swap.locking import FileLock
from claude_swap.claude_locks import claude_credentials_lock
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts

OWNER_FIELD = 'saycodeRefreshOwnerVersion'


def access_only(payload: dict) -> dict:
    result = deepcopy(payload)
    for account in result['accounts']:
        credentials = account.get('credentials')
        data = credentials.get('claudeAiOauth') if isinstance(credentials, dict) else None
        if not isinstance(data, dict):
            continue
        if OWNER_FIELD in data and (type(data[OWNER_FIELD]) is not int or data[OWNER_FIELD] != 1):
            raise ValueError('owner_contract_unsupported')
        token = data.get('accessToken')
        if not isinstance(token, str) or not token:
            raise ValueError('owner_credential_invalid')
        if token.startswith('sk-ant-oat01-') and not data.get('refreshToken') and OWNER_FIELD not in data:
            continue
        expires = data.get('expiresAt')
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= 0:
            raise ValueError('owner_expiry_invalid')
        data.pop('refreshToken', None)
        data.pop('refreshTokenExpiresAt', None)
        data[OWNER_FIELD] = 1
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in ('refreshToken', 'refresh_token') and child not in (None, ''):
                    raise ValueError('owner_refresh_secret_remaining')
                pending.append(child)
    return result


def _export(switcher):
    output = io.StringIO()
    with redirect_stdout(output):
        export_accounts(switcher, '-')
    payload = json.loads(output.getvalue())
    records = (switcher._get_sequence_data_migrated() or {}).get('accounts', {})
    active = switcher._get_current_account()
    payload['activeAccountNumber'] = None
    for account in payload['accounts']:
        record = records.get(str(account['number']), {})
        account['disabled'] = record.get('disabled') is True
        if active == (account['email'], account.get('organizationUuid') or ''):
            payload['activeAccountNumber'] = account['number']
    return payload


def _mark_sources(switcher, captured):
    sources = []
    for account in captured['accounts']:
        credentials = account.get('credentials')
        data = credentials.get('claudeAiOauth') if isinstance(credentials, dict) else None
        if not isinstance(data, dict):
            continue
        if OWNER_FIELD in data:
            raise ValueError('owner_source_required')
        if data.get('refreshToken'):
            sources.append(account)
        elif not str(data.get('accessToken', '')).startswith('sk-ant-oat01-'):
            raise ValueError('owner_source_required')
    with FileLock(switcher.lock_file):
        records = switcher._get_sequence_data_migrated() or {}
        for account in sources:
            record = records.get('accounts', {}).get(str(account['number']))
            if not record or (record.get('email'), record.get('organizationUuid') or '') != (account['email'], account.get('organizationUuid') or ''):
                raise ValueError('owner_roster_changed')
            role = record.get('oauthRefreshOwnerVersion')
            if role is not None and (type(role) is not int or role != 1):
                raise ValueError('owner_source_contract_unsupported')
            # Local custody only: transfer.export_accounts deliberately does not export this field.
            record['oauthRefreshOwnerVersion'] = 1
        if sources:
            switcher._write_json(switcher.sequence_file, records)


def source_export(switcher):
    captured = _export(switcher)
    result = access_only(captured)
    _mark_sources(switcher, captured)
    return result


def refresh_owner(switcher, *, threshold_ms=3600000):
    if type(threshold_ms) not in (int, float) or not 0 <= threshold_ms <= 86400000:
        raise ValueError('owner_threshold_invalid')
    captured = _export(switcher)
    _mark_sources(switcher, captured)
    active = switcher._get_current_account()
    for account in captured['accounts']:
        credentials = account.get('credentials')
        data = credentials.get('claudeAiOauth') if isinstance(credentials, dict) else None
        if not isinstance(data, dict) or account.get('disabled') or account.get('managedAccountId'):
            continue
        if OWNER_FIELD in data:
            raise ValueError('owner_source_required')
        if not data.get('refreshToken'):
            if str(data.get('accessToken', '')).startswith('sk-ant-oat01-'):
                continue
            raise ValueError('owner_source_required')
        expires = data.get('expiresAt')
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError('owner_expiry_invalid')
        if expires > time.time() * 1000 + threshold_ms:
            continue
        number, email = str(account['number']), account['email']
        org = account.get('organizationUuid') or ''
        if switcher._live_session_pids(number, email):
            # A separate cswap-run profile owns its grant. Never consume its backup.
            raise ValueError('owner_session_busy')
        secret = json.dumps(credentials)
        if active == (email, org):
            switcher._fetch_active_usage(number, email, secret, org, refresh_before_ms=threshold_ms)
        else:
            outcome = switcher.consume_backup_grant(number, email, secret)
            if outcome.error or not outcome.credentials:
                raise ValueError('owner_refresh_failed')
    renewed = _export(switcher)
    # Success is determined by the persisted credential, never a subprocess exit alone.
    expected = {(a['email'], a.get('organizationUuid') or '') for a in captured['accounts']}
    if expected != {(a['email'], a.get('organizationUuid') or '') for a in renewed['accounts']}:
        raise ValueError('owner_roster_changed')
    for account in renewed['accounts']:
        data = (account.get('credentials') or {}) if isinstance(account.get('credentials'), dict) else {}
        data = data.get('claudeAiOauth') or {}
        if data.get('refreshToken') and not account.get('disabled') and not account.get('managedAccountId'):
            if data.get('expiresAt', 0) <= time.time() * 1000 + threshold_ms:
                raise ValueError('owner_refresh_failed')
    return access_only(renewed)


def _stored_oauth(text):
    try:
        return oauth.extract_oauth_data(text) or {}
    except (ValueError, TypeError, AttributeError):
        return {}


def active_snapshot(switcher):
    """Read only the actual selected target; unrelated personal credentials never leave this command."""
    empty = {'version': 1, 'encrypted': False, 'activeAccountNumber': None, 'accounts': []}
    with FileLock(switcher.lock_file), claude_credentials_lock():
        active = switcher._get_current_account()
        records = (switcher._get_sequence_data_migrated() or {}).get('accounts', {})
        matches = [(number, record) for number, record in records.items()
                   if active == (record.get('email'), record.get('organizationUuid') or '')]
        if not matches:
            return empty
        if len(matches) != 1:
            raise ValueError('owner_identity_conflict')
        number, record = matches[0]
        live_text = switcher._read_credentials() or '{}'
        live = _stored_oauth(live_text)
        live = live or {}
        backup_text = switcher._read_account_credentials(number, record['email']) or '{}'
        backup = _stored_oauth(backup_text)
        backup = backup or {}
        if backup.get(OWNER_FIELD) == 1:
            if live.get('refreshToken') or live.get('accessToken') != backup.get('accessToken') or live.get('expiresAt') != backup.get('expiresAt'):
                raise ValueError('owner_activation_required')
            live = backup
        elif OWNER_FIELD not in live:
            return empty
        if switcher._get_current_account() != active:
            raise ValueError('owner_active_identity_changed')
        return access_only({'version': 1, 'encrypted': False, 'activeAccountNumber': int(number), 'accounts': [{
            'number': int(number), 'email': record['email'], 'organizationUuid': record.get('organizationUuid') or '',
            'credentials': {'claudeAiOauth': live}}]})


def command(argv):
    parser = argparse.ArgumentParser(prog='cswap oauth-owner')
    parser.add_argument('action', choices=['capabilities', 'export', 'refresh', 'import', 'active'])
    parser.add_argument('--threshold-ms', type=int, default=3600000)
    parser.add_argument('--file')
    args = parser.parse_args(argv)
    if args.action == 'capabilities':
        result = {'version': 1, 'claudeRefreshOwnerVersion': 1}
    elif args.action == 'active':
        result = active_snapshot(ClaudeAccountSwitcher())
    elif args.action == 'import':
        if not args.file:
            raise ValueError('owner_file_required')
        switcher = ClaudeAccountSwitcher()
        import_accounts(switcher, args.file, force=True, owner_target=True)
        result = {'version': 1, 'imported': True}
    else:
        switcher = ClaudeAccountSwitcher()
        result = refresh_owner(switcher, threshold_ms=args.threshold_ms) if args.action == 'refresh' else source_export(switcher)
    print(json.dumps(result))
