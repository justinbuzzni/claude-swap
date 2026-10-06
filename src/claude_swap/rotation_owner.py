"""Machine-wide rotation writer ownership.

Three cooperating pieces keep automatic rotation from racing any other writer:

* Selection revision: every ``sequence.json`` write whose selection projection
  (active slot, order, per-slot identity/disabled/pinned/managed generation)
  changes is stamped with a monotonic ``selectionRevision`` and the writer
  label. A guarded commit compares the revision AND the projection digest, so
  an unstamped write by an older binary is still detected.
* Lease: one automatic owner per pool. Legacy auto engines share the single
  ``legacy-auto`` owner (they already serialize among themselves); an enhanced
  owner holds an exclusive, epoch-fenced lease. Manual writers never take the
  lease: user intent wins and surfaces as a CAS conflict to the automatic owner.
* Journal: a durable intent written before the commit and a receipt after it.
  An intent with no receipt is recovered by reading the roster, never assumed.

Lock order (outermost first): rotation controller lock → token-runtime state
lock → switch locks (cswap FileLock, then Claude Code's credential and config
locks) → lease lock / journal lock. ``TokenRuntime._sync`` already takes the
state lock before the cswap FileLock, so a commit guard running under the switch
locks must never take the state lock; the guarded apply holds it from before the
switch instead. Nothing holding the lease or journal lock takes any other lock.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

LEGACY_OWNER = 'legacy-auto'
LEASE_FILE = 'rotation-owner.v1.json'
JOURNAL_FILE = 'rotation-journal.v1.json'
MAX_RECEIPTS = 50
_SELECTION_ACCOUNT_FIELDS = ('email', 'organizationUuid', 'uuid', 'disabled', 'pinned',
                             'managedAccountId', 'credentialGeneration')

_writer: contextvars.ContextVar[tuple[str, str | None]] = contextvars.ContextVar(
    'claude_swap_selection_writer', default=('cswap', None))


class SelectionConflict(Exception):
    """A guarded commit found the selection changed; nothing was written."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@contextmanager
def writing_as(label: str, intent_id: str | None = None):
    """Label roster writes made in this context (and thread) for receipts/audit."""
    token = _writer.set((label, intent_id))
    try:
        yield
    finally:
        _writer.reset(token)


def run_as_writer(label, fn, *args, **kwargs):
    """Call ``fn`` labelled as ``label`` — for worker threads that lose context."""
    with writing_as(label):
        return fn(*args, **kwargs)


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z')


def selection_projection(data: dict) -> dict:
    accounts = data.get('accounts') if isinstance(data.get('accounts'), dict) else {}
    return {
        'active': data.get('activeAccountNumber'),
        'sequence': data.get('sequence'),
        'accounts': {slot: {field: record.get(field) for field in _SELECTION_ACCOUNT_FIELDS}
                     for slot, record in sorted(accounts.items()) if isinstance(record, dict)},
    }


def projection_digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(selection_projection(data), sort_keys=True).encode()).hexdigest()


def stamp_selection(new: dict, old: dict, *, now: float | None = None) -> None:
    """Carry or advance the revision on a roster write (mutates ``new``)."""
    now = time.time() if now is None else now
    old_revision = old.get('selectionRevision')
    old_revision = old_revision if isinstance(old_revision, int) and not isinstance(old_revision, bool) else 0
    if selection_projection(new) == selection_projection(old):
        # A stale in-memory copy must never move the durable counter backwards.
        for key in ('selectionRevision', 'lastSelection', 'lastActiveChangeAt'):
            if key in old:
                new[key] = old[key]
            else:
                new.pop(key, None)
        return
    label, intent_id = _writer.get()
    new['selectionRevision'] = old_revision + 1
    new['lastSelection'] = {'revision': old_revision + 1, 'writer': label, 'intentId': intent_id, 'at': _iso(now)}
    if new.get('activeAccountNumber') != old.get('activeAccountNumber'):
        new['lastActiveChangeAt'] = now
    elif 'lastActiveChangeAt' in old:
        new['lastActiveChangeAt'] = old['lastActiveChangeAt']


def fingerprint(data: dict, live: tuple[str, str] | None) -> dict:
    return {'revision': data.get('selectionRevision', 0), 'projection': projection_digest(data),
            'active': None if data.get('activeAccountNumber') is None else str(data['activeAccountNumber']),
            'live': None if live is None else [live[0], live[1] or '']}


def slot_identity(data: dict, slot: str | None) -> list[str] | None:
    record = (data.get('accounts') or {}).get(str(slot)) if slot is not None else None
    if not isinstance(record, dict):
        return None
    return [record.get('email', ''), record.get('organizationUuid') or '']


def durable_write_json(path: Path, value: dict) -> None:
    atomic_write_json(path, value)
    # Windows rejects fsync on a read-only handle; use a writable handle
    # for the same durable-file barrier on every platform.
    with path.open('r+b') as saved:
        os.fsync(saved.fileno())
    if os.name != 'nt':
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _read(path: Path, empty: dict) -> dict:
    if not path.exists():
        return dict(empty)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'{path.name}_invalid') from error
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        # Unknown ownership state is never treated as "free".
        raise ValueError(f'{path.name}_invalid')
    return value


class LeaseStore:
    def __init__(self, directory: Path, *, clock=time.time):
        self.path = Path(directory) / LEASE_FILE
        self.lock = Path(directory) / '.rotation-owner.lock'
        self.clock = clock

    def _state(self) -> dict:
        state = _read(self.path, {'version': 1, 'epoch': 0, 'holder': None})
        epoch, holder = state.get('epoch'), state.get('holder')
        valid = type(epoch) is int and epoch >= 0 and 'holder' in state
        if holder is not None:
            valid = valid and isinstance(holder, dict)
            if valid:
                expires = holder.get('expiresAt')
                valid = (isinstance(holder.get('owner'), str) and bool(holder['owner'])
                         and type(holder.get('epoch')) is int and holder['epoch'] == epoch and epoch > 0
                         and type(expires) in (int, float) and math.isfinite(expires) and expires > 0)
        if not valid:
            raise ValueError(f'{self.path.name}_invalid')
        return state

    def _live_holder(self, state: dict) -> dict | None:
        holder = state.get('holder')
        if isinstance(holder, dict) and holder.get('expiresAt', 0) > self.clock():
            return holder
        return None

    def current(self) -> dict | None:
        with FileLock(self.lock):
            return self._live_holder(self._state())

    def acquire(self, owner: str, *, ttl: float = 600) -> dict | None:
        """Take or renew the lease for ``owner``; None when someone else holds it."""
        if not isinstance(owner, str) or not owner or owner == LEGACY_OWNER:
            raise ValueError('owner_invalid')
        return self._take(owner, ttl)

    def claim_legacy(self, *, ttl: float = 900) -> bool:
        return self._take(LEGACY_OWNER, ttl) is not None

    def _take(self, owner: str, ttl: float) -> dict | None:
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('ttl_invalid')
        with FileLock(self.lock):
            state = self._state()
            holder = self._live_holder(state)
            if holder is not None and holder['owner'] != owner:
                return None
            if holder is None:
                state['epoch'] = int(state.get('epoch', 0)) + 1
            holder = {'owner': owner, 'epoch': state['epoch'], 'expiresAt': self.clock() + ttl}
            state['holder'] = holder
            durable_write_json(self.path, state)
            return dict(holder)

    def release(self, owner: str) -> bool:
        with FileLock(self.lock):
            state = self._state()
            holder = self._live_holder(state)
            if holder is None or holder['owner'] != owner:
                return False
            state['holder'] = None
            durable_write_json(self.path, state)
            return True

    def check(self, owner: str, epoch: int) -> bool:
        try:
            holder = self.current()
        except ValueError:
            return False
        return holder is not None and holder['owner'] == owner and holder['epoch'] == epoch

    def legacy_guard(self):
        """Commit guard for legacy auto engines, evaluated under the switch lock."""
        def guard(_data, _live):
            try:
                claimed = self.claim_legacy()
            except ValueError:
                raise SelectionConflict('ownership_state_invalid') from None
            if not claimed:
                raise SelectionConflict('rotation_owned_elsewhere')
        return guard


class RotationJournal:
    def __init__(self, directory: Path, *, clock=time.time):
        self.path = Path(directory) / JOURNAL_FILE
        self.lock = Path(directory) / '.rotation-journal.lock'
        self.clock = clock

    def _state(self) -> dict:
        state = _read(self.path, {'version': 1, 'pending': None, 'receipts': []})
        pending, receipts = state.get('pending'), state.get('receipts')
        valid = 'pending' in state and isinstance(receipts, list)
        if pending is not None:
            valid = valid and isinstance(pending, dict)
            if valid:
                revision = pending.get('expectedRevision')
                valid = (isinstance(pending.get('intentId'), str) and bool(pending['intentId'])
                         and type(revision) is int and revision >= 0
                         and all(isinstance(pending.get(key), list) and len(pending[key]) == 2
                                 and all(isinstance(value, str) for value in pending[key])
                                 for key in ('fromIdentity', 'toIdentity')))
        if valid:
            valid = all(isinstance(receipt, dict)
                        and isinstance(receipt.get('intentId'), str) and bool(receipt['intentId'])
                        and receipt.get('status') in ('applied', 'rejected', 'failed', 'unresolved')
                        and type(receipt.get('acknowledged')) is bool
                        for receipt in receipts)
        if valid:
            for receipt in receipts:
                if 'finishedAtEpoch' in receipt:
                    finished = receipt['finishedAtEpoch']
                    valid = type(finished) in (int, float) and math.isfinite(finished) and finished >= 0
                else:
                    # COMPAT: original recovered v1 receipts have only ISO time.
                    # Reconsider after those receipts age out; never repair on read.
                    try:
                        finished = datetime.fromisoformat(receipt['finishedAt'].replace('Z', '+00:00'))
                        valid = finished.tzinfo is not None and math.isfinite(finished.timestamp()) and finished.timestamp() >= 0
                    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
                        valid = False
                if not valid:
                    break
        if not valid:
            raise ValueError(f'{self.path.name}_invalid')
        return state

    def begin(self, intent: dict) -> dict:
        with FileLock(self.lock):
            state = self._state()
            if state.get('pending'):
                raise ValueError('intent_pending')
            state['pending'] = {**intent, 'startedAt': _iso(self.clock())}
            # The intent must be durable before any credential or roster write.
            durable_write_json(self.path, state)
            return state['pending']

    def pending(self) -> dict | None:
        with FileLock(self.lock):
            return self._state().get('pending')

    def finish(self, intent: dict, status: str, **fields) -> dict:
        with FileLock(self.lock):
            state = self._state()
            pending = state.get('pending') or {}
            if pending.get('intentId') == intent['intentId']:
                state['pending'] = None
            receipt = {key: intent.get(key) for key in ('intentId', 'fromRef', 'toRef', 'fromSlot', 'toSlot',
                                                        'expectedRevision', 'toGeneration', 'owner', 'epoch', 'turnId')}
            fields.setdefault('finishedAtEpoch', self.clock())
            receipt.update(status=status, finishedAt=_iso(self.clock()), acknowledged=False, **fields)
            state['receipts'] = ([r for r in state.get('receipts', []) if r.get('intentId') != intent['intentId']]
                                 + [receipt])[-MAX_RECEIPTS:]
            durable_write_json(self.path, state)
            return receipt

    def receipt(self, intent_id: str) -> dict | None:
        with FileLock(self.lock):
            return next((r for r in self._state().get('receipts', []) if r.get('intentId') == intent_id), None)

    def receipts(self) -> list[dict]:
        with FileLock(self.lock):
            return list(self._state().get('receipts', []))

    def blocking(self) -> dict | None:
        """An unacknowledged unresolved commit stops further automatic writes."""
        return next((r for r in reversed(self.receipts())
                     if r.get('status') == 'unresolved' and not r.get('acknowledged')), None)

    def turn_used(self, turn_id: str | None) -> bool:
        return bool(turn_id) and any(r.get('turnId') == turn_id and r.get('status') in ('applied', 'unresolved')
                                     for r in self.receipts())

    def acknowledge(self, intent_id: str) -> bool:
        with FileLock(self.lock):
            state = self._state()
            found = False
            for receipt in state.get('receipts', []):
                if receipt.get('intentId') == intent_id and receipt.get('status') == 'unresolved':
                    receipt['acknowledged'] = True
                    found = True
            if found:
                durable_write_json(self.path, state)
            return found


def classify_commit(intent: dict, data: dict, live: tuple[str, str] | None) -> tuple[str, str]:
    """Decide what happened to ``intent`` from the roster and live login alone."""
    live_identity = None if live is None else [live[0], live[1] or '']
    stamp = data.get('lastSelection') if isinstance(data.get('lastSelection'), dict) else {}
    if stamp.get('intentId') == intent.get('intentId'):
        if str(data.get('activeAccountNumber')) == str(intent.get('toSlot')) and live_identity == intent.get('toIdentity'):
            return 'applied', 'receipt_verified'
        return 'unresolved', 'commit_stamped_without_live_match'
    if data.get('selectionRevision', 0) == intent.get('expectedRevision') and live_identity == intent.get('fromIdentity'):
        return 'failed', 'not_committed'
    return 'unresolved', 'commit_outcome_unknown'
