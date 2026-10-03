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

Envelope `{version:1, artifact, ...}`. Capabilities: setupTokenObservation,
managedAccountMetadata, durableProbeBudget, rotationSuggestion true;
automaticRotation and rotationWriterOwnership false. Existing writers do not all
participate in CAS/ownership, therefore new automatic rotation is unavailable.

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
ignored by upstream. No legacy auto policy changed. Runtime never performs a
switch or claims an apply receipt; storage import receipts remain caller-owned.

## Desktop/Happy compatibility (2026-10-04)

`status.accounts[].number` is the integer machine-local selector ONLY, never an
identity. `roster` contains exact `{email, organizationUuid, uuid}` from sequence;
`legacyUsageOwned:false` means runtime does not replace normal OAuth/API usage.
Desktop must keep normal OAuth/API `list --json` rows, joined only when number
AND exact roster metadata match, and overlay setup-token rows from runtime.
Never attach legacy lastGood to a new generation by email/org alone. Normal
OAuth continues to use upstream profile usage; status never calls that endpoint
or retroactively asserts a source/generation for an old cache. List active state
is global stored selection, not session active binding. Runtime emits no active
account or execution receipt. Happy new-session binding must remain disabled:
upstream `cswap run` active fastpath is not proven to prepare a scrubbed profile.

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
command can mark it verified. Suggestions never apply and never emit a receipt.

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
| T11 | managed import의 기존 roster lock/CAS; auto capability false | auto/TUI/menubar/manual/외부 legacy 모든 writer의 owner/lease/CAS 미구현 |
| T12 | 실제 suggest 경로에 연결된 90/80/max/stable tie/coverage/TTL/reset/pin 정책, synthetic 테스트 | 실측 coverage 없음; switch cooldown의 지속·실제 적용 전 재검증은 writer 단계 필요 |
| T13 | unknown/stale/reset/401/403/429/교체/late/예산/POST 이전 쓰기 실패/managed rollback fixture | owner 경합/실제 switch commit 불명/receipt failure 통합 회귀 미구현 |
| T14 | dry-run, no apply/no replay, disabled/pin 후보 제외 | 실제 적용 receipt와 수동 pin controller/turn별 전환 제어 미구현 |

`cswap run` active fastpath의 profile/env scrub 보장도 미검증이다. Happy는 새
세션 binding을 열지 않아야 한다. 관리 import CAS는 같은 helper를 쓰는 writer만
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
Automatic rotation and true prepared-profile support are still pending as above.

Organization bridge validation (2026-10-04):
`uv run --frozen pytest -n 0 tests/test_org_probe.py tests/test_token_probe.py tests/test_token_runtime.py`
66 passed; compileall and selected E9/F ruff checks passed. `uv build --out-dir dist`,
lock-preserving installed-wheel reinstall and `tests/pack_token_runtime.py` passed,
including actual wheel file-backend managed import/replacement/export and organization
fake-HTTP/replay. Repeatable fixtures use a new synthetic token per generation so
retained token cooldown is never reset to make the test pass. No live traffic.
