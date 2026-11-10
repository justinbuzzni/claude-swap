"""Internal trusted-Core bridge; a plain permit is never public consent."""
import hashlib
import uuid
from datetime import datetime, timezone

from claude_swap.locking import FileLock
from claude_swap.token_probe import probe
from claude_swap.token_runtime import ARTIFACT, _timestamp

PERMIT_FIELDS = {'version', 'permitId', 'companyId', 'machineId', 'managedAccountId', 'credentialGeneration', 'policyRevision', 'reservedAt', 'transportDeadline', 'expiresAt', 'timeoutMs', 'accountRemaining', 'companyRemaining'}
ACCOUNT_LIMIT, MACHINE_LIMIT = 96, 288
# A check of a candidate before a session switches onto it may spend up to twice the daily budget.
SWITCH_BUDGET_FACTOR = 2
ORG_RECHECK_SECONDS = 3600
# Signed transport starts within ten seconds; tolerate delivery variation, never transport failure backoff.
ORG_TRANSIT_SLACK_SECONDS = 10


def collect_org(runtime, request):
    def stopped(reason):
        return dict(version=1, artifact=ARTIFACT, reason=reason, applied=False)
    try:
        if set(request) != {'version', 'context', 'permit', 'inUse', 'online'} or request['version'] != 1:
            raise ValueError
        context, permit = request['context'], request['permit']
        if set(context) != {'companyId', 'machineId', 'managedAccountId', 'credentialGeneration', 'policyRevision', 'accountRef'}:
            raise ValueError
        if set(permit) - {'purpose'} != PERMIT_FIELDS or permit.get('purpose', 'switch') != 'switch':
            raise ValueError
        uuid.UUID(permit['permitId'])
        uuid.UUID(context['managedAccountId'])
        uuid.UUID(context['accountRef'])
        for field in ('companyId', 'machineId', 'managedAccountId', 'credentialGeneration', 'policyRevision'):
            if context[field] != permit[field]:
                raise ValueError
        for field in ('credentialGeneration', 'policyRevision', 'reservedAt', 'transportDeadline', 'expiresAt', 'timeoutMs', 'accountRemaining', 'companyRemaining', 'version'):
            if type(permit[field]) is not int or not 0 <= permit[field] <= 9007199254740991:
                raise ValueError
        if permit['version'] != 1 or permit['credentialGeneration'] < 1 or permit['policyRevision'] < 1 or permit['timeoutMs'] != 10000:
            raise ValueError
        if permit['transportDeadline'] != permit['reservedAt'] + 10000 or permit['expiresAt'] != permit['reservedAt'] + 30000:
            raise ValueError
        if not permit['reservedAt'] <= runtime.clock()*1000 < permit['transportDeadline']:
            raise ValueError
        if any(not isinstance(context[f], str) or not 0 < len(context[f]) <= 128 for f in ('companyId', 'machineId')):
            raise ValueError
        if type(request['online']) is not bool or type(request['inUse']) is not bool:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        return stopped('grant_invalid')
    if not request['online'] or not request['inUse']:
        return stopped('offline' if not request['online'] else 'not_in_use')
    lock = FileLock(runtime.collector_lock, timeout=0)
    if not lock.acquire():
        return stopped('collector_busy')
    try:
        ref = context['accountRef']
        permit_hash = hashlib.sha256((context['companyId'] + ':' + permit['permitId']).encode()).hexdigest()
        with FileLock(runtime.state_lock):
            state = runtime._load()
            credentials = runtime._sync(state)
            account = state['accounts'].get(ref)
            if account is None or account['scope'][1] != context['managedAccountId'] or account['generation'] != context['credentialGeneration']:
                return stopped('credential_changed')
            if account['source'] != 'inference_probe' or account['disabled']:
                return stopped('probe_disabled')
            now, digest = runtime.clock(), account['digest']
            permits = state.setdefault('orgPermits', {})
            if not isinstance(permits, dict):
                raise ValueError('runtime_state_invalid')
            # Keep expired receipts for a day; all older grants fail time validation.
            permits = {k:v for k,v in permits.items() if v > now*1000-86400000}
            state['orgPermits'] = permits
            if permit_hash in permits:
                return stopped('permit_replayed')
            token_state = state['tokens'].get(digest, {})
            # A newly authorized organization grant may check an early provider reset hourly.
            # Retain the original backoff for personal collection and all durable spending.
            attempts = [a['at'] for a in state['attempts'] if a['digest'] == digest]
            last_attempt = max(attempts, default=None)
            prior_next = token_state.get('nextAt', 0)
            provider_retry = token_state.get('orgRetryAt', 0)
            # Older states did not record the provider deadline separately. Infer it only
            # beyond the largest ordinary personal cooldown (including jitter/transport).
            legacy_delay = 900 * 2**min(token_state.get('failures', 0), 6) * 1.1 + 10
            if 'orgRetryAt' not in token_state and prior_next > now and (last_attempt is None or prior_next > last_attempt + legacy_delay):
                provider_retry = prior_next
            hourly_retry = provider_retry > now and now >= token_state.get('transportRetryAt', 0) and (last_attempt is None or now - last_attempt >= ORG_RECHECK_SECONDS - ORG_TRANSIT_SLACK_SECONDS)
            if now < prior_next and not hourly_retry:
                return stopped('backing_off')
            factor = SWITCH_BUDGET_FACTOR if permit.get('purpose') == 'switch' else 1
            if len(state['attempts']) >= MACHINE_LIMIT*factor or sum(a['digest'] == digest for a in state['attempts']) >= ACCOUNT_LIMIT*factor:
                return stopped('budget_exhausted')
            snapshot = (digest, account['generation'], account['scope'])
            permits[permit_hash] = permit['expiresAt']
            state['attempts'].append({'at': now, 'digest': digest})
            state['tokens'][digest] = {**token_state, 'nextAt': max(prior_next, now+300), 'orgRetryAt': provider_retry}
            runtime._save(state)
            token = credentials[ref]
        remaining = (permit['transportDeadline'] - runtime.clock()*1000)/1000
        if remaining <= 0:
            return stopped('permit_expired')
        obs = probe(token, now=now, timeout_s=min(10, remaining))
        # Judge the transport by when it completed, not by how long bookkeeping took.
        completed = runtime.clock()
        retry_at = _timestamp(obs.get('retryAt'))
        with FileLock(runtime.state_lock):
            state = runtime._load()
            runtime._sync(state)
            account = state['accounts'].get(ref)
            token_state = state['tokens'].get(digest, {})
            failures = token_state.get('failures', 0) + 1 if obs['reason'] != 'coverage_unknown' else 0
            transport_failures = token_state.get('transportFailures', 0) + 1 if obs['reason'] not in ('coverage_unknown', 'throttled') else 0
            measured = {w['kind'] for w in obs['windows'] if w.get('pct') is not None}
            full_reading = obs['reason'] == 'coverage_unknown' and obs.get('authState') == 'usable' and {'unified5h', 'unified7d'} <= measured
            # A failed or partial early check must not shorten the personal provider wait.
            retained = provider_retry if not full_reading else 0
            token_state.update(failures=failures, transportFailures=transport_failures,
                               transportRetryAt=completed+300*2**min(transport_failures,6) if transport_failures else 0,
                               orgRetryAt=max(retained, retry_at or 0),
                               nextAt=max(completed+min(86400, 300*2**min(failures, 6)), retry_at or 0,
                                          prior_next if retained > completed else 0))
            state['tokens'][digest] = token_state
            runtime._save(state)
            if account is None or snapshot != (account['digest'], account['generation'], account['scope']) or account['disabled']:
                return stopped('credential_changed')
            if completed*1000 > permit['transportDeadline']:
                return stopped('permit_expired')
            # Processing/publish grace ends with the permit itself.
            if runtime.clock()*1000 > permit['expiresAt']:
                return stopped('permit_expired')
            if retry_at is not None and retry_at <= completed:
                # Second-precision Retry-After from request start (e.g. 0) has already
                # elapsed; the durable backoff above still holds the next attempt.
                obs['retryAt'] = None
            obs.update(accountRef=ref, credentialGeneration=account['generation'],
                       observedAt=datetime.fromtimestamp(completed, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'))
            return dict(version=1, artifact=ARTIFACT, observation=obs, applied=False)
    finally:
        lock.release()
