"""Opt-in machine collector. Legacy writers remain separate; no auto apply."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
import uuid
from datetime import datetime

from claude_swap.locking import FileLock
from claude_swap.oauth import extract_oauth_data
from claude_swap.settings import atomic_write_json
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.token_probe import probe, select_source

ARTIFACT = 'saycode-setup-token-runtime-v1'


def capabilities():
    return dict(version=1, artifact=ARTIFACT, setupTokenObservation=True,
                managedAccountMetadata=True, durableProbeBudget=True, organizationCollectorVersion=1, personalProbeVersion=1,
                rotationSuggestion=True, automaticRotation=False,
                rotationWriterOwnership=False)


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _decision_score(row, model, now):
    if row.get('disabled') or row.get('pinned') or row.get('authState') != 'usable':
        return None
    obs = row.get('observation') or {}
    coverage = obs.get('coverage')
    if not isinstance(coverage, dict) or coverage.get('model') != model:
        return None
    required = coverage.get('requiredWindows')
    if not isinstance(required, list) or not {'unified5h', 'unified7d', 'modelWeekly'}.issubset(required):
        return None
    observed = _timestamp(obs.get('observedAt'))
    if observed is None or not 0 <= now - observed <= 300:
        return None
    if obs.get('reason') not in (None, 'coverage_verified'):
        return None
    windows = {}
    for window in obs.get('windows', []):
        if window.get('kind') == 'modelWeekly' and window.get('model') != model:
            continue
        if window.get('kind') in windows:
            return None
        windows[window.get('kind')] = window
    values = []
    for kind in required:
        window = windows.get(kind, {})
        pct, reset = window.get('pct'), _timestamp(window.get('resetsAt'))
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct) or not 0 <= pct <= 100 or reset is None or reset <= now:
            return None
        values.append(pct)
    return max(values) if values else None


def choose_suggestion(rows, current, model, now, *, last_switch_at=None):
    """Dry-run policy; inference transport cannot establish coverage today."""
    result = dict(selectedRef=current, suggestedRef=None, applied=False,
                  automaticRotation=False, reason='coverage_unknown')
    active = next((row for row in rows if row['accountRef'] == current), None)
    if active is None:
        raise ValueError('account_not_found')
    score = _decision_score(active, model, now)
    if score is None:
        return result
    if last_switch_at is not None and now - last_switch_at < 300:
        result['reason'] = 'cooldown'
        return result
    if score < 90:
        result['reason'] = 'below_trigger'
        return result
    candidates = []
    for row in rows:
        if row['accountRef'] == current:
            continue
        candidate_score = _decision_score(row, model, now)
        if candidate_score is not None and candidate_score < 80:
            candidates.append((candidate_score, row['accountRef']))
    result['reason'] = 'no_eligible_candidate'
    if candidates:
        result.update(suggestedRef=min(candidates)[1], reason='suggestion_only')
    return result


class TokenRuntime:
    def __init__(self, switcher, *, clock=time.time):
        self.switcher = switcher
        self.clock = clock
        self.state_file = switcher.backup_dir / 'token-runtime.v1.json'
        self.state_lock = switcher.backup_dir / '.token-runtime-state.lock'
        self.collector_lock = switcher.backup_dir / '.token-runtime-collector.lock'

    def _load(self):
        if not self.state_file.exists():
            return {'version': 1, 'accounts': {}, 'attempts': [], 'tokens': {}}
        try:
            state = json.loads(self.state_file.read_text())
            if state['version'] != 1 or not isinstance(state['accounts'], dict) or not isinstance(state['attempts'], list) or not isinstance(state['tokens'], dict):
                raise ValueError
            return state
        except (ValueError, KeyError, TypeError):
            # Never reset a broken durable budget to zero.
            raise ValueError('runtime_state_invalid') from None

    def _save(self, state):
        atomic_write_json(self.state_file, state)
        # A reservation must reach stable storage before credential transport.
        with self.state_file.open('rb') as saved:
            os.fsync(saved.fileno())
        if os.name != 'nt':
            directory = os.open(self.state_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _sync(self, state):
        # No network under the credential/roster lock. Assign identity to rows,
        # not slot numbers, so slot swaps preserve refs and new rows get new IDs.
        credentials = {}
        with FileLock(self.switcher.lock_file):
            data = self.switcher._get_sequence_data() or {}
            records = data.get('accounts', {})
            changed = False
            seen = set()
            for slot, record in records.items():
                ref = record.get('runtimeAccountRef')
                if not isinstance(ref, str) or ref in seen:
                    ref = str(uuid.uuid4())
                    record['runtimeAccountRef'] = ref
                    changed = True
                seen.add(ref)
                secret = self.switcher._read_account_credentials(slot, record['email'])
                oauth = extract_oauth_data(secret) or {}
                token = oauth.get('accessToken')
                digest = hashlib.sha256((token if isinstance(token, str) else secret).encode()).hexdigest()
                scope = [record.get('organizationUuid') or '', record.get('managedAccountId') or '']
                old = state['accounts'].get(ref)
                managed_gen = record.get('credentialGeneration', 1)
                if isinstance(managed_gen, bool) or not isinstance(managed_gen, int) or managed_gen < 1:
                    raise ValueError('credential_generation_invalid')
                if old is None or old['digest'] != digest or old['scope'] != scope or old.get('managedGeneration') != managed_gen:
                    generation = managed_gen if record.get('managedAccountId') else max(managed_gen, (old['generation'] + 1) if old else 1)
                    old = {'digest': digest, 'scope': scope, 'generation': generation,
                           'managedGeneration': managed_gen, 'consent': False, 'authState': 'expiry-unknown',
                           'consentRevision': (old.get('consentRevision', 0) + 1) if old else 0}
                    state['accounts'][ref] = old
                    if isinstance(token, str) and token.startswith('sk-ant-oat01-'):
                        self.switcher._usage_store.invalidate_credentials([slot], {slot:(record['email'], scope[0])})
                source = select_source(secret)
                is_setup = isinstance(token, str) and token.startswith('sk-ant-oat01-')
                label = record.get('alias') or record.get('displayName') or record.get('email') or 'Unresolved'
                confidence = 'user-labeled' if record.get('alias') or record.get('displayName') else ('unresolved' if record.get('email', '').endswith('@token.local') else 'saved-metadata')
                old.update(slot=slot, roster={'email':record['email'], 'organizationUuid':record.get('organizationUuid') or '', 'uuid':record.get('uuid') or ''}, label=label, identityConfidence=confidence,
                           source=source, credentialType='setup_token' if is_setup else ('oauth' if oauth else 'api_key'),
                           disabled=bool(record.get('disabled')), pinned=bool(record.get('pinned')))
                credentials[ref] = token
            for ref in list(state['accounts']):
                if ref not in seen:
                    del state['accounts'][ref]
            if changed:
                self.switcher._write_json(self.switcher.sequence_file, data)
        now = self.clock()
        state['attempts'] = [a for a in state['attempts'] if a['at'] > now - 86400]
        return credentials

    def _row(self, ref, account):
        now = self.clock()
        obs = account.get('observation')
        status = 'probe-disabled' if not account['consent'] else 'unavailable'
        reason = 'probe_disabled' if not account['consent'] else 'not_observed'
        if account['source'] != 'inference_probe':
            status, reason = ('scope-missing', 'scope_diagnosis_required') if account['credentialType'] == 'setup_token' else ('unavailable', 'legacy_source')
        elif account['consent'] and obs:
            reason = obs['reason']
            status = {'scope_missing':'scope-missing','throttled':'backing-off'}.get(reason, 'partial' if reason == 'coverage_unknown' else 'unavailable')
            observed = _timestamp(obs['observedAt'])
            resets = [_timestamp(w.get('resetsAt')) for w in obs['windows']]
            if observed is None or not 0 <= now - observed <= 300 or any(t is not None and t <= now for t in resets):
                status = 'stale'
        row = dict(accountRef=ref, number=int(account['slot']), roster=account['roster'], legacyUsageOwned=False,
                   credentialGeneration=account['generation'], label=account['label'],
                   identityConfidence=account['identityConfidence'], credentialType=account['credentialType'],
                   authState=account['authState'], usageStatus=status, decisionEligible=False,
                   reasonCodes=[reason, 'coverage_unknown', 'writer_ownership_unavailable'],
                   probeEnabled=account['consent'], disabled=account['disabled'], pinned=account['pinned'])
        if account['scope'][1]:
            row['managedAccountId'] = account['scope'][1]
        if obs:
            row['observation'] = obs
        if account.get('lastGood'):
            row['lastGood'] = account['lastGood']
        return row

    def status(self):
        with FileLock(self.state_lock):
            state = self._load()
            self._sync(state)
            self._save(state)
            return dict(version=1, artifact=ARTIFACT, accounts=[self._row(ref, a) for ref, a in sorted(state['accounts'].items(), key=lambda pair: int(pair[1]['slot']))],
                        budget={'machineUsed24h':len(state['attempts']), 'machineLimit24h':288, 'accountLimit24h':96})

    def consent(self, ref, *, enabled, ack_cost=False, expected_generation=None):
        with FileLock(self.state_lock):
            state = self._load()
            self._sync(state)
            account = state['accounts'].get(ref)
            if account is None:
                raise ValueError('account_not_found')
            if expected_generation is not None and account['generation'] != expected_generation:
                raise ValueError('credential_changed')
            if enabled:
                if account['scope'][1] or account['scope'][0]:
                    raise ValueError('organization_collector_required')
                if not ack_cost:
                    raise ValueError('inference_cost_ack_required')
                if account['source'] != 'inference_probe':
                    raise ValueError('inference_scope_required')
            account['consent'] = enabled
            account['consentRevision'] += 1
            self._save(state)
            return dict(version=1, artifact=ARTIFACT, accountRef=ref, probeEnabled=enabled)

    def collect(self, ref, *, in_use=False, offline=False, expected_generation=None):
        def stopped(reason):
            return dict(version=1, artifact=ARTIFACT, accountRef=ref, reason=reason, applied=False)
        if offline or not in_use:
            return stopped('offline' if offline else 'not_in_use')
        lock = FileLock(self.collector_lock, timeout=0)
        if not lock.acquire():
            return stopped('collector_busy')
        try:
            with FileLock(self.state_lock):
                state = self._load()
                credentials = self._sync(state)
                account = state['accounts'].get(ref)
                if account is None:
                    return stopped('account_not_found')
                if expected_generation is not None and account['generation'] != expected_generation:
                    return stopped('credential_changed')
                if not account['consent'] or account['source'] != 'inference_probe' or any(account['scope']):
                    self._save(state)
                    return stopped('probe_disabled')
                if account['disabled']:
                    return stopped('account_disabled')
                now, digest = self.clock(), account['digest']
                token_state = state['tokens'].get(digest, {})
                if now < token_state.get('nextAt', 0):
                    self._save(state)
                    return stopped('backing_off')
                attempts = state['attempts']
                if len(attempts) >= 288 or sum(a['digest'] == digest for a in attempts) >= 96:
                    self._save(state)
                    return stopped('budget_exhausted')
                snapshot = (digest, account['generation'], account['scope'], account['consentRevision'])
                attempts.append({'at':now,'digest':digest})
                state['tokens'][digest] = {**token_state, 'nextAt':now+900}
                # Reserve durably BEFORE POST; timeout/crash also consumes quota.
                self._save(state)
                token = credentials[ref]
            obs = probe(token, now=now)
            with FileLock(self.state_lock):
                state = self._load()
                self._sync(state)
                account = state['accounts'].get(ref)
                if account is None or snapshot != (account['digest'], account['generation'], account['scope'], account['consentRevision']) or not account['consent']:
                    self._save(state)
                    return stopped('credential_changed')
                obs.update(accountRef=ref, credentialGeneration=account['generation'])
                account['observation'] = obs
                if obs['authState'] in ('invalid', 'usable'):
                    account['authState'] = obs['authState']
                token_state = state['tokens'][digest]
                failed = obs['reason'] != 'coverage_unknown'
                failures = token_state.get('failures', 0) + 1 if failed else 0
                retry = _timestamp(obs.get('retryAt')) or 0
                # Jitter only lengthens the durable backoff; never below 5min.
                delay = min(86400, 900 * 2 ** min(failures, 6)) * (1 + random.random() * .1)
                token_state.update(nextAt=max(self.clock()+delay, retry), failures=failures)
                if obs['authState'] == 'usable' and any(w['pct'] is not None for w in obs['windows']):
                    account['lastGood'] = obs
                self._save(state)
                return dict(version=1, artifact=ARTIFACT, account=self._row(ref, account), applied=False)
        finally:
            lock.release()

    def suggest(self, current, model):
        rows = self.status()['accounts']
        if not any(row['accountRef'] == current for row in rows):
            raise ValueError('account_not_found')
        # Production probes always have unknown coverage. This shared policy
        # is tested with synthetic complete coverage, not a live claim.
        result = choose_suggestion(rows, current, model, self.clock())
        return dict(version=1, artifact=ARTIFACT, model=model, accounts=rows, **result)


def command(argv):
    parser = argparse.ArgumentParser(prog='cswap token-runtime')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('capabilities')
    sub.add_parser('status')
    sub.add_parser('collect-org')
    consent = sub.add_parser('consent')
    consent.add_argument('account_ref')
    consent.add_argument('--generation', type=int)
    mode = consent.add_mutually_exclusive_group(required=True)
    mode.add_argument('--enable', action='store_true')
    mode.add_argument('--disable', action='store_true')
    consent.add_argument('--ack-cost', action='store_true', help='Acknowledge actual inference, quota use and possible extra charges')
    collect = sub.add_parser('collect')
    collect.add_argument('account_ref')
    collect.add_argument('--generation', type=int)
    collect.add_argument('--in-use', action='store_true')
    collect.add_argument('--offline', action='store_true')
    suggest = sub.add_parser('suggest')
    suggest.add_argument('--current', required=True)
    suggest.add_argument('--model', required=True)
    args = parser.parse_args(argv)
    if args.action == 'capabilities':
        result = capabilities()
    else:
        grant = None
        if args.action == 'collect-org':
            import sys
            raw = sys.stdin.read(8193)
            if len(raw.encode()) > 8192:
                raise ValueError('grant_invalid')
            try:
                grant = json.loads(raw)
            except ValueError:
                raise ValueError('grant_invalid') from None
        runtime = TokenRuntime(ClaudeAccountSwitcher())
        if args.action == 'collect-org':
            from claude_swap.org_probe import collect_org
            result = collect_org(runtime, grant)
        elif args.action == 'status':
            result = runtime.status()
        elif args.action == 'consent':
            result = runtime.consent(args.account_ref, enabled=args.enable, ack_cost=args.ack_cost, expected_generation=args.generation)
        elif args.action == 'collect':
            result = runtime.collect(args.account_ref, in_use=args.in_use, offline=args.offline, expected_generation=args.generation)
        else:
            result = runtime.suggest(args.current, args.model)
    print(json.dumps(result, ensure_ascii=False))
