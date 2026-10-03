"""Internal trusted-Core bridge; a plain permit is never public consent."""
import hashlib
import uuid
from datetime import datetime, timezone

from claude_swap.locking import FileLock
from claude_swap.token_probe import probe
from claude_swap.token_runtime import ARTIFACT, _timestamp


def collect_org(runtime, request):
    def stopped(reason):
        return dict(version=1, artifact=ARTIFACT, reason=reason, applied=False)
    try:
        if set(request) != {'version', 'context', 'permit', 'inUse', 'online'} or request['version'] != 1:
            raise ValueError
        context, permit = request['context'], request['permit']
        if set(context) != {'companyId', 'machineId', 'managedAccountId', 'credentialGeneration', 'policyRevision', 'accountRef'}:
            raise ValueError
        if set(permit) != {'version', 'permitId', 'companyId', 'machineId', 'managedAccountId', 'credentialGeneration', 'policyRevision', 'reservedAt', 'transportDeadline', 'expiresAt', 'timeoutMs', 'accountRemaining', 'companyRemaining'}:
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
            if now < token_state.get('nextAt', 0):
                return stopped('backing_off')
            if len(state['attempts']) >= 288 or sum(a['digest'] == digest for a in state['attempts']) >= 96:
                return stopped('budget_exhausted')
            snapshot = (digest, account['generation'], account['scope'])
            permits[permit_hash] = permit['expiresAt']
            state['attempts'].append({'at': now, 'digest': digest})
            state['tokens'][digest] = {**token_state, 'nextAt': now+300}
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
            token_state.update(failures=failures, nextAt=max(completed+min(86400, 300*2**min(failures, 6)), retry_at or 0))
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
