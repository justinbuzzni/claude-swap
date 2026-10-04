# Saycode token runtime v1 (local custom artifact)

Artifact marker: `saycode-setup-token-runtime-v1`; upstream base `0.27.0b1`.
Consumers MUST check `cswap token-runtime capabilities` and this marker before
using the API. Do not replace a marked custom build with an upstream version
based on numeric version equality. No released/live artifact is verified.

Commands (JSON stdout; no raw credential arguments):
- `cswap token-runtime capabilities`: no account access, version/artifact/features.
- `cswap token-runtime status`: local secret-free accounts and cached observations;
  no network, no active binding claim.
- `cswap token-runtime consent ACCOUNT_REF --enable --ack-cost`: explicit machine
  opt-in acknowledging inference consumes quota and may incur charges; `--disable`
  revokes. Managed organization recipients cannot enable local probes.
- `cswap token-runtime collect ACCOUNT_REF --in-use [--offline]`: at most one
  synchronous probe; default stopped without `--in-use`; offline never calls.
  Caller schedules at 15 minutes, minimum 5 minutes enforced durably. This command
  is also the manual refresh path; status never schedules requests.
- `cswap token-runtime suggest --current ACCOUNT_REF --model MODEL`: dry run only;
  no switching, process restart, or turn replay.
- `cswap token-runtime rotate --model MODEL [--apply --owner ID] [--turn-id T]`:
  controller (see "Rotation ownership"). Without `--apply` it is a dry run.
- `cswap token-runtime auto --enable|--disable`: machine opt-in for `--apply`
  (default off). `pin ACCOUNT_REF --on|--off`, `receipts [--ack INTENT_ID]`,
  `lease status|acquire|release --owner ID [--ttl S]`.

Envelope `{version:1, artifact, ...}`. Capabilities: setupTokenObservation,
managedAccountMetadata, durableProbeBudget, rotationSuggestion,
rotationWriterOwnership, rotationReceipt, rotationController true;
automaticRotation and externalWriterExclusion false (see below).

## Rotation ownership (T11–T14, 2026-10-04)

Writers inside this artifact all participate; writers outside it cannot:

- Selection revision: every `sequence.json` write goes through `_write_json`,
  which advances `selectionRevision` when the selection projection changes
  (activeAccountNumber, sequence order, per-slot email/org/uuid/disabled/pinned/
  managedAccountId/credentialGeneration) and stamps `lastSelection {revision,
  writer, intentId, at}` plus `lastActiveChangeAt` on an active change. Covered
  without per-site edits: CLI switch/switch-to, TUI, menubar, legacy auto,
  add/remove/swap/move/disable, import, transaction rollback, pin. Labels:
  `manual-cli|tui|menubar|legacy-auto|setup-token-auto|manual-pin|rollback|cswap`.
- Lease (`rotation-owner.v1.json`, epoch fenced, TTL 600s enhanced / 900s legacy):
  one automatic owner per pool. All legacy auto engines share the owner
  `legacy-auto` (their existing state-lock/cooldown serialization is unchanged),
  claimed inside the switch lock on each legacy switch. While an enhanced owner
  holds it, the legacy engine emits `no-switch: rotation-owned` and does not
  switch; while legacy holds it the controller returns `rotation_owned_elsewhere`.
  Corrupt ownership state fails closed for both (`ownership_state_invalid`).
  Manual writers never take the lease: user intent wins and surfaces to the
  automatic owner as a CAS conflict. With no enhanced owner, legacy behaviour and
  defaults are unchanged.
- Lock order (outermost first): controller lock → token-runtime state lock →
  cswap FileLock → Claude Code credential/config locks → lease or journal lock.
  `_sync` already takes state then FileLock, so the guarded apply takes the state
  lock BEFORE the switch and holds it through re-sync, intent, switch and receipt;
  the guard (under the switch locks) never locks state, it reads that held copy.
  Consequence: consent/auto/collect/status calls wait for an in-flight apply
  (FileLock default timeout 10s), including a slow switch preflight.
- Commit guard: evaluated under the switch locks before the first write. Rejects
  (nothing written, receipt `rejected`) on lease lost, revision OR projection
  digest change (detects unstamped writes by older binaries), live login drift,
  target identity/credential digest change, auto opt-in off (`auto_disabled`),
  or when the exact policy re-run at write time — consent, auth, pin/disabled,
  model coverage, every required window, TTL, reset, cooldown, with the clock
  read at that moment — no longer picks the same current→candidate
  (`decision_changed`, receipt `decisionReason` = the new policy reason).
  A revoked probe consent withdraws that account's observation from decisions.
- Journal (`rotation-journal.v1.json`, fsync): intent written before the commit;
  receipt after: `applied` only when the roster carries this intent's stamp, the
  active slot is the target and the live login is the target; `rejected` (guard),
  `failed` (nothing changed: revision and live login as before), `unresolved`
  (anything else, including "rollback also failed"). An intent without a receipt
  is recovered under the controller lock from the roster alone. An unacknowledged
  `unresolved` receipt blocks further `--apply` (`unresolved_receipt`) until
  `receipts --ack`; it still counts toward the cooldown.
- Policy: existing `choose_suggestion` (current >=90, every required window of
  each candidate <80, min max-utilization, stable-ref tie, TTL 5 min, reset
  boundary, auth usable, disabled/pinned excluded, pinned or disabled current
  never rotated). Cooldown 5 min from the latest active change by ANY writer or
  applied/unresolved receipt. `--turn-id` already used by an applied/unresolved
  receipt is refused. The controller never edits disabled/pinned, never restarts
  processes, never replays turns, never probes.
- `automaticRotation:false`: no production observation carries verified model
  coverage, so `--apply` can only succeed with synthetic fixtures. It flips only
  after approved live coverage fixtures exist.
- Evidence: in-process boundary tests plus real two-process tests
  (`tests/test_rotation_contention.py`, separate interpreters, isolated file
  backend, network/profile/Keychain stubbed to fail): lease mutual exclusion,
  a manual switch in another process rejecting a delayed commit, a consent
  revocation in another process waiting for the in-flight commit, and two
  controllers committing at most once. Each was checked to fail when its
  exclusion is removed (lease lock; state-lock hold; revision CAS; all of
  controller lock + lease + CAS + state-lock hold for the controller race).
- Remaining exclusions, not provided by this artifact: (1) writers outside it —
  Desktop inline switch, upstream/older cswap binaries, manual edits — are only
  detected (projection/live drift) at the guard; one that writes Claude Code's
  credentials/config without taking Claude Code's locks inside the commit window
  is not excluded; (2) any process that reads/writes `sequence.json` without
  cswap's FileLock; (3) crash between the credential write and the roster write
  is classified `unresolved` (not repaired); (4) NFS/remote filesystems where
  flock is not exclusive; (5) model coverage is never verified in production.
- `externalWriterExclusion:false`: Desktop's inline Claude switch and older
  cswap binaries are not excluded. They are detected at commit (projection/live
  drift) but a non-locking external write racing the commit window is possible.
  Consumers MUST stop their own Claude automatic writers before enabling
  `auto --enable`, and may hold the lease via `lease acquire` to keep legacy
  engines out.

Account DTO: accountRef (opaque local UUID), credentialGeneration, label,
identityConfidence (`user-labeled|saved-metadata|unresolved`), credentialType,
managedAccountId optional, authState, usageStatus, observation optional,
decisionEligible false, reasonCodes. Setup tokens are not server-verified.
Observation: version, source (`inference_probe`), accountRef,
credentialGeneration, observedAt (UTC), windows ({kind:unified5h|unified7d,
pct:number|null, resetsAt:UTC|null, status:string|null}), coverage `unknown`,
reason, retryAt. Unified headers NEVER establish model-specific coverage.
401 changes authState to invalid; 403 scope-missing does not invalidate auth.
429 preserves validated headers and is throttled, never synthesized as 100%.
No header status is treated as proven quota exhaustion without live fixtures.
Stale (TTL 5 minutes) or crossed reset observations cannot inform suggestions.
Last-good is display-only. All unknown candidates keep the current selection.

Transport: fixed HTTPS api.anthropic.com /v1/messages, proxy and redirect disabled,
10-second socket timeout plus total response deadline, max 65536 response bytes,
no server body retained, no project content/tools, prompt `Hi`, max_tokens 1.
Pinned request model `claude-haiku-4-5-20251001` is a synthetic-test contract only;
provider acceptance and setup-token authorization remain unverified. No fallback
is enabled before validated unsupported-model fixtures exist.
Budget is reserved before transport (including timeout/error); rolling 24h
account/token digest max96, machine max288. Fingerprints are private state only.
One cross-process collector lock; durable backoff and minimum interval apply
across aliases sharing a token. Replacement invalidates consent/cache and late
results. Organization metadata blocks local consent; centralized organization
collector lease is outside this artifact.

Export/import remains version1/encrypted:false/kind:oauth; preserves
credentialType:setup_token, managedAccountId UUID, displayName (1..120),
credentialGeneration positive integer without inventing refreshToken or identity.
Imports do not imply inference verification, probe opt-in or global activation.
Rollback: disable consent and use legacy commands; additive runtime state is
ignored by upstream. No legacy auto policy changed. `status`, `consent`,
`collect`, `collect-org`, `suggest` and `rotate` without `--apply` never switch.
Only `rotate --apply` (machine opt-in, lease, guarded CAS; see "Rotation
ownership") changes the global stored selection through cswap's own switch path,
and it emits a rotation receipt for exactly that. Storage import receipts remain
caller-owned.

## Desktop/Happy compatibility (2026-10-04)

`status.accounts[].number` is the integer machine-local selector ONLY, never an
identity. `roster` contains exact `{email, organizationUuid, uuid}` from sequence;
`legacyUsageOwned:false` means runtime does not replace normal OAuth/API usage.
Desktop must keep normal OAuth/API `list --json` rows, joined only when number
AND exact roster metadata match, and overlay setup-token rows from runtime.
Never attach legacy lastGood to a new generation by email/org alone. Normal
OAuth continues to use upstream profile usage; status never calls that endpoint
or retroactively asserts a source/generation for an old cache. List active state
is global stored selection, not session active binding. Runtime `status` emits no
active account. A rotation receipt proves only that the global stored selection
and live login moved; it is never an execution receipt for any session. This
runtime does not prepare per-session profiles and does not rely on the upstream
`cswap run` active fastpath (unproven to scrub a profile). Per-session binding is
a separate Core (Happy) strategy that injects the selected setup token directly
into the spawned session's environment; it must not claim this runtime prepared
a profile, and a runtime rotation never retargets an already running session.

Managed import serializes under existing roster lock and validates same managed
ID, exact identity, generation and token before writes: higher generation replaces
without `--force`; lower generation and same-generation differing token conflict.
Observed write failure on replacement restores previous credentials/config/roster
under lock (cache remains invalid). This is local cooperating-writer CAS with
rollback, NOT a crash-durable multi-file transaction or whole-bundle transaction.
Legacy external/unowned writers and unguarded readers remain a second-phase gap.
Metadata survives AccountInfo, AccountSnapshot, list JSON and export v1.

Local spending basis: account limit is per token digest, shared by aliases and
retained for 24h even after deletion/reassignment; new token generation has a new
per-token account bucket. Machine spending NEVER resets on replacement, import,
consent revocation/re-enable or account deletion. Organization recipients cannot
probe locally; R30's authoritative managed-account/fleet budget must be supplied
by the org lease/server collector, not inferred from this local per-token budget.

Synthetic complete-coverage policy fixtures exercise >=90 trigger, every required
window <80, minimum max-utilization/stable-ref tie-break and 5min cooldown. No
production probe establishes complete coverage; no public observation import
command can mark it verified. `suggest` and `rotate` without `--apply` never apply
and never emit a receipt.

## 구현/검증 인수인계 — 2026-10-04

부모 저장소 T6–T14를 전체 완료로 표시하지 않는다. 이 worktree는 첫 번째
검증 가능한 provider 구현이며 아래 미완료 작업을 남긴다.

| Task | 구현된 범위 | 남은 범위 |
|---|---|---|
| T6 | scope 기반 source, case-insensitive fraction/seconds/unknown parser | 승인된 live 헤더/model coverage fixture 없음 |
| T7 | 고정 origin, no proxy/redirect, 10s socket/response deadline, 64KiB, 401/403/429/error 분리 | 검증된 unsupported-model fallback 없음; DNS/connect timeout은 OS/urllib 경계이며 강제 취소 API 없음 |
| T8 | opaque roster ref, generation/late/reset/TTL 폐기, 기존 setup lastGood/claim 무효화, additive runtime JSON/metadata | TUI/menubar 공통 관측 UI 연결 미구현; 일반 OAuth cache를 source/generation 관측으로 승격하지 않음 |
| T9 | process간 single-flight, fsync 예산 예약, digest 중복, backoff/jitter, offline/in-use, 공용 refresh 명령 | caller가 주기 호출; 상주 scheduler/다중 머신 collector lease/조직 계정 총예산 미구현; 진행 중 POST는 disable로 강제 중단되지 않고 결과만 폐기 |
| T10 | setup refresh 부재가 permanent-dead가 되지 않는 회귀, 기존 OAuth/API 회귀 | live 실행/설치 버전 검증 없음 |
| T11 | 모든 artifact 내부 writer의 selection revision 스탬프(중앙 `_write_json`), legacy/enhanced 단일 lease(epoch), switch lock 안의 guard CAS, 외부/구버전 writer 감지, 2-process 경합 테스트 | Desktop inline·구 binary 등 외부 writer 배제 불가(감지만); 2개 초과 process·장시간 부하는 미측정 |
| T12 | `rotate` controller: 90/80/max/stable tie/coverage/TTL/reset, 모든 writer 기준 지속 cooldown, 적용 직전 lease·revision·projection·live·후보 digest 재검증 | 실측 model coverage 없음 → production에서 적용 불가(의도) |
| T13 | 수동/구 binary 경합, live drift, 후보 교체, legacy 소유, 손상 소유 상태, rollback 실패, crash 후 intent 복구, commit 직전 auto/consent/auth/cooldown/TTL/reset 재검증 회귀, 실제 2-process lease·CAS·state lock·controller 경합 | 실제 Keychain/macOS 백엔드에서의 commit 불명 시나리오 미실측 |
| T14 | 적용 receipt(applied/rejected/failed/unresolved), unresolved 차단·ack, pin 명령, disabled/pin 보존, turn 재전환 금지 | Happy/Desktop의 turn id 공급·receipt 표시 미연결; runtime은 session profile을 준비하지 않음(session binding은 Core direct token env 전략, 별도) |

`cswap run` active fastpath의 profile/env scrub 보장은 미검증이며 이 runtime은 그
경로를 쓰지 않는다. 새 세션 binding은 이 runtime의 profile 준비가 아니라 Core가
선택된 setup token을 spawn 환경에 직접 주입하는 별도 전략으로만 연다. 관리 import CAS는 같은 helper를 쓰는 writer만
직렬화하며, crash-durable multi-file 원자성이나 bundle 전체 rollback을 주장하지
않는다. 이 제한을 해소할 ownership/prepared-profile 후속 작업이 필요하다.

검증 명령 (모두 worktree에서 `UV_CACHE_DIR="$PWD/.uv-cache"` 사용):

```sh
uv sync --frozen --group dev
uv run --frozen pytest -n 0 tests/test_token_probe.py tests/test_token_runtime.py tests/test_transfer.py tests/test_oauth.py tests/test_usage_store.py tests/test_json_output.py tests/test_autoswitch.py tests/test_session.py tests/test_switcher.py tests/test_cli.py
uv run --frozen pytest -n 0 tests/test_transfer.py tests/test_json_output.py
uv run --frozen python3 -m compileall -q src/claude_swap
uv build --out-dir dist
uv pip install --reinstall --no-deps --target dist/pack-smoke/install dist/claude_swap-0.27.0b1-py3-none-any.whl
uv run --frozen python3 tests/pack_token_runtime.py
git diff --check
```

첫 관련 suite 1540 passed. 마지막 managed export 수정은 해당 transfer+JSON
167 passed로 재검증했다. E9/F changed-file lint에는 새 진단이 없으며 기존
7개 미사용 import/변수 진단은 유지했다. 신규 source/tests 및 그 외 변경 파일의
E9/F는 통과했다. upstream에 별도 타입검사/린트 gate는 없다.

wheel/sdist 로컬 생성 후 설치 대상 package 경로를 직접 확인한 offline smoke:
capability marker, managed import → status → higher generation replacement →
export, stable ref/metadata/기본 probe OFF, archive 포함 파일/cache 제외 확인.
HOME/config/XDG는 worktree `dist/pack-smoke/home`으로 격리하고 file backend로
고정했으며 모든 urllib network 진입을 실패 stub으로 차단했다. 실제 home,
Keychain, live token, inference, daemon, DB, publish/push/merge를 실행하지 않았다.
이 로컬 custom artifact 결과는 Happy adapter에 유용한 호환성 근거이며 공개
릴리스/pin 지원이나 실제 provider 실행 성공의 증거는 아니다.

## Internal organization reservation bridge v1

`organizationCollectorVersion:1` and `cswap token-runtime collect-org` are additive.
Input is a <=8192-byte stdin JSON trusted-Core DTO, never credential arguments:
`{version:1,context:{companyId,machineId,managedAccountId,credentialGeneration,
policyRevision,accountRef},permit:{version:1,permitId,companyId,machineId,
managedAccountId,credentialGeneration,policyRevision,reservedAt,transportDeadline,
expiresAt,timeoutMs:10000,accountRemaining,companyRemaining},inUse:true,online:true}`.
Times are epoch milliseconds. Core must verify Studio's signed envelope and local
ownership before constructing this internal DTO. This local CLI is NOT a public
signature verifier or admin-consent API; local filesystem/process authority is its
trust boundary. Desktop never invokes it directly with a renderer's plain permit.

Strict scope/generation/time checks, durable permit digest consumption and shared
collector singleflight precede exactly one fixed inference request. Transit reduces
the 10s transport deadline. Local machine/token spending survives consent/import/
replacement and failed/late requests. Repeated token shares cooldown/backoff, including
validated Retry-After. Server managed-account/company debit is an additional authoritative
bound; local per-token bucket alone can change on new token generation. No personal
consent is changed, no receiver fallback, no local org observation cache is claimed.
Current roster digest/ref/generation is compared before and after transport, late
results discarded. Output `{version:1,artifact,observation,applied:false}` or
`{version:1,artifact,reason,applied:false}`. Core converts/whitelists observation
before publish. Missing signer/config/assignment MUST stop before this bridge.
Automatic rotation stays capability-gated (`automaticRotation:false`, see
"Rotation ownership"); this runtime provides no prepared profiles.

Organization bridge validation (2026-10-04):
`uv run --frozen pytest -n 0 tests/test_org_probe.py tests/test_token_probe.py tests/test_token_runtime.py`
66 passed; compileall and selected E9/F ruff checks passed. `uv build --out-dir dist`,
lock-preserving installed-wheel reinstall and `tests/pack_token_runtime.py` passed,
including actual wheel file-backend managed import/replacement/export and organization
fake-HTTP/replay. Repeatable fixtures use a new synthetic token per generation so
retained token cooldown is never reset to make the test pass. No live traffic.

Personal Core CAS: capability `personalProbeVersion:1`; consent/collect accept optional
`--generation N`. Under the runtime state/roster check, a differing generation
rejects before consent mutation or budget/transport. Legacy callers omitting it
keep prior behavior. Happy requires this capability and always supplies generation.
Personal generation CAS validation: 67 passed across token_runtime/org_probe/token_probe,
selected E9/F lint passed, rebuilt wheel/sdist and isolated installed-wheel smoke passed.

## Integration (provider branch feat/claude-token-integration, 2026-10-04)

Base `6397291` (organization bridge + personal generation CAS) with rotation
ownership cherry-picked (`29db313`, `eb33624`, `f51bd28`). Capabilities carry
both sets: `organizationCollectorVersion:1`, `personalProbeVersion:1`,
`rotationWriterOwnership`, `rotationReceipt`, `rotationController` true;
`automaticRotation` and `externalWriterExclusion` false.

Interplay: `collect-org` takes collector lock → state lock → cswap FileLock;
`rotate` takes controller lock → state lock → cswap FileLock → Claude Code locks
→ lease/journal. Both share the state→FileLock order, so a guarded apply and an
organization or personal collection serialize on the state lock and never invert.
Organization-managed rows have no local consent and no local org observation
cache, so they are never locally eligible for `rotate`; Core supplies managed
observations from the server collector separately.

Durable personal budget metadata (additive, secret-free, for Core schedulers/UI):
each `status.accounts[]` row carries `probeBudget {scope:'local-per-token',
accountUsed24h, accountLimit24h:96, accountRemaining24h, nextProbeAt (UTC|null),
failureStreak, minIntervalSeconds:300, recommendedIntervalSeconds:900}`; aliases
of one token share it. `status.budget` adds `machineRemaining24h` and
`machineSlotFreesAt` (UTC|null, when the oldest reservation leaves the rolling
24h window). The token digest itself is never emitted. Organization budget
authority remains the server permit (`accountRemaining`/`companyRemaining`); the
local bucket is an additional bound only. Consumers whitelist unknown fields
(Happy `parseTokenRuntimeStatus` ignores them until it opts in).

Audit fixes (2026-10-04):
- Classification: a row is `setup_token` only from explicit roster metadata
  (`credentialType:setup_token`, set by add-token and managed import) or an
  inference-only credential (`select_source == inference_probe`). Normal profile
  OAuth (`user:profile` scope) keeps `credentialType:oauth` and its legacy usage
  path even though its access token also starts with `sk-ant-oat01-`; the runtime
  no longer invalidates its usage cache on refresh.
- Organization bridge timing: completion time is captured immediately after the
  single HTTP request. The result is kept when completion <= `transportDeadline`
  and bookkeeping finishes before `expiresAt` (processing/publish grace); it is
  discarded otherwise. `observedAt` is the captured completion time (ms).
- Organization `retryAt`: Retry-After is second-precision from request start; a
  value already elapsed at completion (e.g. `Retry-After: 0`) is returned as
  null. The durable per-token backoff still includes it (never shortened).

Validation and artifact provenance: `specs/integration-provenance.md`.


## Fresh provider review (2026-10-04)

Personal inference outcomes now persist the consumed token's failure streak and
Retry-After before discarding an observation whose consent/credential changed
in flight. Another opted-in alias, including after process restart, therefore
obeys the provider backoff without receiving the revoked account observation.
Durable attempt timestamps/digests and token nextAt/failure values are validated
before rolling-window pruning; boolean/nonfinite/missing spending state fails
closed rather than being pruned or interpreted as zero. Reservation-only token
rows may omit failures, as in the original v1 writer.

Ownership records require a nonnegative integer epoch and a structurally valid,
epoch-matching holder with finite positive expiry. Invalid/nonpositive lease TTLs
are rejected before writing. Journal state requires pending/receipts containers,
valid pending identity/revision and receipt intent/status/boolean acknowledgment;
corrupt state blocks the controller without rewriting or erasing an unresolved
receipt. Optional historical receipt fields remain optional.

All 26 new regression cases failed before their corresponding fixes. Final
focused runtime/org/ownership/two-process-contention/autoswitch validation:
371 passed. Broader switcher/usage-store/OAuth/cache coverage before journal
validation: 1132 passed; unchanged token transport parser coverage also passed.
Changed-file E9/F ruff and git diff --check passed. No live credentials, Keychain,
inference, operating daemon, push, merge or deployment. Capabilities remain
`automaticRotation:false` and `externalWriterExclusion:false`.


### Repeated self-review round 1/7 (2026-10-04)

Two additional medium-severity defects were reproduced and fixed. Crash recovery
previously wrote applied/unresolved receipts without finishedAtEpoch, which made
the cooldown ignore an acknowledged unresolved commit when no recent roster
active-change timestamp existed. All newly finished receipts now include epoch
time; historical recovered receipts use their existing finishedAt ISO value for
cooldown without rewriting the journal. Synthetic policy fixtures reproduced
actual immediate apply before both the new-receipt and historical-receipt fixes.
Production coverage remains unknown and automaticRotation remains false.

Truncated HTTP framing (http.client.IncompleteRead) previously escaped both
bounded body readers instead of yielding the existing transport_failed result.
HTTPException is now mapped to that result, keeping auth unverified, utilization
unknown and provider partial body content out of the returned observation.

All four new regression cases failed before fixes. Final targeted command:
`UV_CACHE_DIR=.uv-cache uv run --frozen pytest -n 0 tests/test_token_probe.py
 tests/test_token_runtime.py tests/test_org_probe.py tests/test_rotation_owner.py
 tests/test_rotation_contention.py tests/test_autoswitch.py` → 395 passed.
Changed-file E9/F ruff and git diff --check passed. No full suite, live credentials,
Keychain/inference/profile, operating daemon/install, push, merge or deployment.


### Final cooldown timestamp audit (2026-10-04)

A separate medium-severity persisted-state defect affected both cooldown sources:
nonfinite receipt finishedAtEpoch or roster lastActiveChangeAt could bypass a
recent cooldown (NaN) or block indefinitely (infinity). NaN in the first roster
moment reproduced actual synthetic apply despite a valid applied receipt from
10 seconds earlier. Receipt epoch now requires a finite nonnegative number,
excluding boolean values; an absent epoch keeps historical v1 compatibility via
valid timezone-aware finishedAt ISO. A present roster lastActiveChangeAt requires
a finite nonnegative number; absent historical fields remain valid. Invalid
state rejects without rewriting either persisted source. This is distinct from
the earlier omission of recovered receipt timestamps, and remains synthetic
rotation coverage while automaticRotation stays false.

Sixteen invalid-state regressions failed before fixes; the missing historical
roster field compatibility test already passed. Final related runtime/ownership/
two-process-contention/autoswitch suites: 369 passed. Changed-file E9/F ruff and
git diff --check passed. No full suite, live credentials, Keychain/inference,
operating install/daemon, push, merge or deployment.


### Bounded live setup-token probe (2026-10-04)

At the user's request, two distinct setup-token values from the designated
Desktop worktree .env were read in memory, with no token/body output or storage.
The installed wheel's probe sent exactly one fixed Haiku4.5/Hi/max_tokens1 request
per token (two total), timeout10s/body bound64KiB, no proxy override, redirect,
retry or fallback. Both returned the200/usable path and unified5h/7d headers:
first label1%/2% (allowed), second14%/87% (7d allowed_warning), observed at
2026-10-04 04:48:41/42 UTC. Full reset details are in the Desktop context.

This verifies minimal inference authentication and real header feasibility only.
Provider account identity, full model coverage, actual Claude CLI env auth,
long-term login, exact billing and extra-usage settings remain unverified.
Coverage stays unknown and automaticRotation/externalWriterExclusion stay false.
No operating store/daemon/pin was changed. Earlier no-live statements describe
prior test runs; current cumulative live request count is2. Runtime source and
installed wheel bytes are unchanged; no additional live calls were made.


### 실제 CLI 및 scoped credential 복원 수락 후속 — 2026-10-04

부모의 실제 Claude Code2.1.283 print 검증은 지정 token2개별 격리 HOME/env에서 고정 Haiku4.5·1turn·출력상한16·tools 없음·retry0·세션 미저장으로 둘 다 OK/exit0였다. token 제거 시 같은 HOME에서 auth status exit1/loggedIn false, token 있으면 oauth_token/loggedIn true다. 생성 파일12개 token 미저장, CLI 추정 비용 각$0.000274/합계$0.000548. 누적 live4회(이전 wheel 최소probe2+실제CLI2)이며 실제 청구액은 확인하지 않았다. 증거 `/tmp/claude-cli-acceptance-rkj05uv1/result.json`.

동일 설치 wheel(source2fef57e)의 합성 file-backend 경로에서 실제 Studio prepare→installed daemon group-sync→HTTP complete applied, 교체/회수·정상 daemon restart 보존과 원래 token의 forward generation3 복구가 통과했다. scoped slot/export는 복구된 token, 전역 활성 login은 교체 token을 유지했다. 이는 import/backup과 switch 활성화를 분리하는 기존 transfer 계약이다. fresh signed generation3 Happy/Claude 대역 spawn에서 정확 scoped token 전달을 확인했으므로 전역 login을 덮어쓰지 않았다. 직접 terminal binding은 보장하지 않는다. 부모 source/result/provenance/cleanup 검토·assertions 통과, 근거 `/tmp/claude/studio-deploy-recovery.b2r665ab/`.

실제 Happy/SDK+실계정 결합·Keychain 접근 차단·장기 유지·identity·full coverage·managed-data artifact downgrade는 미수락이다. Runtime source/wheel·운영 store/daemon/pin 변경 없음, automaticRotation/externalWriterExclusion false 유지.


## Offline review fix — 2026-10-04

Status: implemented, verified and independently reviewed in the isolated provider worktree;
source accepted for the existing feature PR. Current source evidence only; historical acceptance above is not a substitute.

- [x] Reproduced G1 active -> G2 managed import without activation -> ordinary switch
  to another account -> export/status. RED: exported credentials regressed to synthetic
  G1 while metadata still claimed G2. Regression:
  `tests/test_transfer.py::TestManagedSetupMetadata::test_managed_upgrade_survives_ordinary_switch`.
- [x] Minimal fix: `_classify_outgoing_credential` treats a managed imported slot's
  stored credentials as authoritative (`managed-import`). Ordinary switch backs up
  config only and cannot replace the imported generation with stale active bytes.
  Personal OAuth lineage backup behavior is unchanged.
- [x] GREEN: the same regression exports exact G2 credentials and status generation2.
  Happy's offline source integration additionally performs the ordinary switch and
  binds exact G2 through the actual provider CLI adapter. That integration also verifies
  personal timeout spending/backoff after a synthetic 10 s transport failure with
  Happy's corrected 30 s process deadline.
- [x] Related verification: `.venv/bin/python -m pytest -n 0 tests/test_transfer.py
  tests/test_switcher.py tests/test_token_runtime.py --tb=short`: 654 passed (final follow-up). Both changed
  Python files passed in-memory `compile`; `git diff --check` passed. Existing environment
  has no Ruff executable/module, so lint was unavailable; nothing was installed.

Fixtures use temporary HOME/config, synthetic credentials and a forced Linux file
backend/offline transport. No real credentials/.env/Keychain, live inference, operating
DB/daemon/services/signers/pins, dependency installs, full suites, commit or push.
No native Keychain or packaged artifact acceptance is claimed. External writer exclusion
and automatic rotation remain false; unknown coverage and published .279 downgrade
remain intentional gates. The fix protects stored managed generation bytes; it does
not update a running process's already-delivered credential or the old active login.


### Parent follow-up: bounded live-to-backup audit — 2026-10-04

- [x] The previous writes at switcher.py:3585/3728 were `add_account` refresh and
  explicit-slot capture. RED: three G1-live/G2-import cases (no slot, same slot,
  moving to another slot) accepted capture, overwriting credentials or replacing the
  managed row. GREEN: capture refuses an import-owned source or target with ConfigError;
  metadata and exact imported credential bytes remain unchanged.
- [x] Entry-only checking was insufficient: two additional RED tests perform an actual
  cooperating import after capture selected a free slot/read the live bytes, immediately
  before commit. GREEN: both commit branches acquire the same FileLock as import/switch,
  re-read managed metadata and retain the lock through cleanup and credential/config/index
  writes. Network verification and prompts remain outside the lock. This deterministically
  exercises the interleaving; it is not an operating multi-process stress run.
- [x] The previous write at switcher.py:4782 was `_resync_rotated_backup`. Normal
  setup-token live bytes have no refresh token and return early, but that was not a
  managed-slot fence. RED: a synthetic full pair with a prior positive lineage verdict
  overwrote the imported generation. GREEN: the resync path checks managed metadata
  under its existing FileLock before writing, regardless of that identity verdict.
- [x] Final related checks: six new regressions passed; full selected transfer/switcher/
  token-runtime files total **654 passed**. Python in-memory syntax validation and
  diff whitespace check passed. Happy final counts: **472 related + 2 offline source
  integration tests**, with typecheck passed. Lint remains unavailable as recorded above.

Source evidence: `add_account` local `refuse_managed_capture`, both FileLock commit
blocks, and `_resync_rotated_backup`'s under-lock metadata check. Regressions are
`test_managed_upgrade_refuses_live_capture`,
`test_capture_rechecks_managed_import_after_preparation`, and
`test_managed_slot_refuses_live_rotation_resync` in `tests/test_transfer.py`.
This audit does **not** establish that every other credential-backup writer is fenced.
`externalWriterExclusion:false` and `automaticRotation:false` remain unchanged. All reads
were synthetic/file-backed; no operating credential store, service, DB or pin was used.
Parent review/commit/push remains pending; no commit or push performed here.


### Parent final source review — 2026-10-04

Parent reviewed the final managed-import classifier, both FileLock capture commit
branches and resync guard, and independently reran all seven new regressions: passed.
Happy final adapter checks also passed (239 related tests; counts overlap child checks),
and the two source integration tests were independently confirmed. The fork has no
configured CI; local focused/syntax/whitespace checks are the current source evidence.
The earlier installed wheel predates this correction and must be rebuilt/reaccepted.
No all-writer exclusion, native Keychain, operating recovery or release acceptance is claimed.


### Additional adjacent-writer follow-up on b6c7fce — 2026-10-04

Status: source/tests complete; this additional diff awaits parent review/commit/push.
The preceding parent acceptance record is preserved and applies to its earlier HEAD.
No broader audit was continued after the parent's stop-scope instruction.

Newly confirmed gaps (three groups; six initial RED cases):

- **P1 — add-token can overwrite or remove managed generation ownership.** The bare,
  same-slot and move-slot commands accepted synthetic old bytes against an imported
  managed account. All three RED cases now reject before mutation. Both add-token
  commits hold FileLock and repeat the shared source/target check; an additional test
  inserts a real import after free-slot selection and confirms the target survives.
- **P2 — session-profile adoption lacked a managed metadata fence.** Given a quiescent
  profile considered ahead by the existing profile resolver, the direct store write
  replaced managed bytes. The conditional writer regression supplies those two
  eligibility decisions synthetically; it does not reproduce a full profile lifecycle
  or claim that an ordinary stale-marked G1 profile passes the existing checks.
  Adoption now returns false for managed slots under its existing lock.
- **P2 — legacy persistence entry points lacked a managed metadata fence.** Direct
  invocations of `write_account_credentials` and `persist_backup_credentials` replaced
  imported bytes. Both now reject with ConfigError. No current production caller of
  either public wrapper was found in repository source; this is a latent API-boundary
  gap, not evidence of an exercised CLI route. The first wrapper retains its documented
  caller-held-lock requirement; the latter owns its FileLock.

The existing add-account and resync checks now share `_managed_backup_conflict` /
`_refuse_managed_capture` with these confirmed paths. The lower-level import writer and
transfer validation were not changed. Forward-generation import/rollback and ordinary
config-only switch backup remain intact and pass existing transfer/integration tests.
Other refresh paths were not changed or certified by this follow-up; this is still not
an all-writer or external-writer exclusion claim.

Verification: **661 passed** in selected transfer/switcher/token-runtime files, including
seven new tests (six RED -> GREEN writer cases plus the import-interleaving regression).
The initial shared predicate exposed eight existing uninitialized-store tests; handling
`_get_sequence_data() is None` restored compatibility and the final 661 all passed.
Happy's explicit fallback regressions and related files: **473 passed**, source
integration: **2 passed**, TypeScript check passed. Both changed Python files pass
in-memory compile, and diff whitespace checks pass. Ruff remains unavailable; no lint
pass is claimed and no dependency was installed. These counts replace the prior
472/2/654 for this additional diff, not add to them.

Only synthetic/file-backed credentials and mocked/offline transports were used.
No real .env/Keychain, inference, operating daemon/DB/services/signers/pins, release,
commit or push. `automaticRotation:false`, `externalWriterExclusion:false`, unknown
coverage and unsupported published .279 downgrade remain unchanged.


Parent follow-up acceptance (2026-10-04): Parent independently reviewed the shared guard, both add-token commit locks and profile/public-wrapper guards; the seven new adjacent-writer regressions passed. Current PR head/checks own the final CI result. Previous installed artifacts still require rebuild/reacceptance.
