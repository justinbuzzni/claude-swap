# Combined token runtime artifact provenance (local, unpublished)

Branch `feat/claude-token-integration`. Current artifact built from commit
`df1a2e3` (repeated self-review round 1/7); previous `e83608a`, `86e6018`
and `6a930c9` builds are kept below as history. This file is updated by doc-only commits, so it is never inside the
artifacts it describes. Local custom artifact only: not published, not pinned, not a release.

| Item | Value |
|---|---|
| Base | `6397291` organization bridge + personal generation CAS (Sol) |
| Cherry-picked | `29db313`→`4bbbfc8`, `eb33624`→`79aed16`, `f51bd28`→`8df9a61` (rotation ownership, guarded commit) |
| Integration | `6a930c9` secret-free personal probe budget, combined smoke, contract corrections |
| Review round 1/7 | `df1a2e3` recovered-receipt cooldown (including historical receipts), HTTP framing failure mapping |
| Fresh review fixes | `e83608a` consumed-token backoff, durable budget validation, fail-closed lease/journal state |
| Audit fixes | `86e6018` profile-OAuth classification, org completion-time deadline, elapsed retryAt |
| Artifact marker | `saycode-setup-token-runtime-v1` (upstream base `0.27.0b1`) |
| Toolchain | uv 0.10.2, Python 3.14.5, `uv build --out-dir dist` from the clean committed tree |
| Wheel (`df1a2e3`, current) | `claude_swap-0.27.0b1-py3-none-any.whl` sha256 `13c50d077998b60d3bbf4b325d3450de911b2515a3d9bec7c0b31c3fc32bce8a` |
| sdist (`df1a2e3`, current) | `claude_swap-0.27.0b1.tar.gz` sha256 `f82ea133e18939f5563cd2c976df8db5687bbcb8e70065d52ac2985111691e28` |
| Wheel (`e83608a`, superseded) | `claude_swap-0.27.0b1-py3-none-any.whl` sha256 `4591ce89e07fe647e7801b9a9c23a3542dbe7acface8d2ad240ec54a9f2ed12e` |
| sdist (`e83608a`, superseded) | `claude_swap-0.27.0b1.tar.gz` sha256 `c33d5a407dbe8142c46a78087c69e53896ab1ab4a80784c326fcf5f8ce66baa7` |
| Wheel (`86e6018`, superseded) | `claude_swap-0.27.0b1-py3-none-any.whl` sha256 `26fd90da40b8b1bcbfd945e8bb7f7043c3494d6c88502efc1cf42991d2aee9a6` |
| sdist (`86e6018`, superseded) | `claude_swap-0.27.0b1.tar.gz` sha256 `d5c11cd1b5504be23bed44489c80610c6bfa94bcfcffda71e71f5eedeb38cfc1` |
| Wheel (`6a930c9`, superseded) | sha256 `b6c125f1bdbb9405bf877f8353e707937699851859e2a7960126200c1feddae4` |
| sdist (`6a930c9`, superseded) | sha256 `f2bbd669f5a2d1b0ece32614925e5cddf429214c9c1cdb32501b02096f22a384` |

Installed-wheel modules compared byte-for-byte with `git show df1a2e3:src/claude_swap/<m>` (sha256):

| Module | sha256 |
|---|---|
| token_runtime.py | `c1c942be19eca9a8a3c2ec2d82178addc6183acddbb6a356aca65738b448f352` |
| rotation_owner.py | `0429b118ee66f68cb83c5793b339adadc58cd33f9fe732522acc1d042a1f8c4b` |
| org_probe.py | `19ddc135db28f8a94cf71f15946e5a4ef4af4664605a4fb039a28c62062a0b04` |
| token_probe.py | `13877bec995c195e8e583890d62ddf7102e5501887ad07f7340da4c6856a839a` |
| switcher.py | `b87af4ba3ec5187c93f62f170b3bb7c9e84a7db7e42f0130ed2228853b050f41` |
| autoswitch.py | `3889fba6c73e5b8065813d1ccddc17419fdb1d51643ad3eb6822b8f0f214f446` |

Hashes are environment-specific build outputs; a rebuild elsewhere may differ in
archive metadata while module contents match the commit.

## Validation at `df1a2e3` (round 1/7)

- Four red-first cases: acknowledged recovered unresolved receipt and historical
  receipt without epoch incorrectly allowed immediate synthetic apply; both
  bounded body reader paths let IncompleteRead escape structured error mapping.
- Final focused suites (token_probe/runtime/org/rotation_owner/contention/auto):
  395 passed. Changed-file E9/F ruff and git diff --check passed. Full suite not run.
- Clean committed-source build and isolated --no-deps wheel reinstall into
  dist/pack-smoke/install using the same commands as e83608a below; existing
  offline installed-wheel smoke passed. All six installed modules above match
  df1a2e3 committed source byte-for-byte.
- Current fixed locations: rotation_owner.py:288 (receipt epoch),
  token_runtime.py:426 (historical finishedAt fallback, COMPAT reconsideration
  comment at :424), token_probe.py:127 (HTTP framing error mapping).
- Local artifact only, no operating install/daemon, live credentials/Keychain/
  inference/profile, push, merge or deployment. automaticRotation and
  externalWriterExclusion remain false.

## Validation at `e83608a`

- 26 red-first regressions: consent-revoked in-flight 429 still fences another
  token alias after restart; malformed durable attempts/token backoff cannot
  erase spending; malformed ownership/TTL/journal cannot create a free writer
  or skip the unresolved-receipt gate. Historical reservation rows may omit
  failures; historical receipt optional fields remain optional.
- Final targeted suites: `UV_CACHE_DIR=.uv-cache uv run --frozen pytest -n 0
  tests/test_token_runtime.py tests/test_org_probe.py tests/test_rotation_owner.py
  tests/test_rotation_contention.py tests/test_autoswitch.py` → 371 passed.
  Broader switcher/usage-store/OAuth/cache run before journal addition: 1132
  passed; unchanged token-probe suite also passed. Full suite not run.
- Changed-file `ruff check --select E9,F` and `git diff --check` clean.
- Build from clean committed `e83608a`: `UV_CACHE_DIR=.uv-cache uv build --out-dir dist`.
  `uv pip install --python .venv/bin/python --target dist/pack-smoke/install
  --reinstall --no-deps dist/claude_swap-0.27.0b1-py3-none-any.whl` changes only
  the isolated target directory. Existing installed-wheel offline smoke passed:
  `UV_CACHE_DIR=.uv-cache uv run --frozen python3 tests/pack_token_runtime.py`.
  Installed six modules above match committed source byte-for-byte.
- Automatic rotation/external writer exclusion stay false. No live credentials,
  Keychain/inference/profile, operating daemon change, deployment, push or merge.

## Validation at `86e6018`

- Red-first: profile-OAuth classification/usage-cache test, org completion-before-deadline
  with slow persistence, elapsed Retry-After normalization (each failed before the fix);
  guards for processing past `expiresAt` and a future Retry-After kept.
- Related regressions: `uv run --frozen pytest -n 0 tests/test_org_probe.py tests/test_token_probe.py
  tests/test_token_runtime.py tests/test_rotation_owner.py tests/test_rotation_contention.py
  tests/test_autoswitch.py tests/test_switcher.py tests/test_transfer.py tests/test_usage_store.py
  tests/test_json_output.py tests/test_oauth.py tests/test_cli.py` → 1410 passed; contention file
  repeated twice more (4 passed each). Changed-file E9/F ruff and `git diff --check` clean.
- Installed-wheel offline smoke passed on the rebuilt `86e6018` wheel (same coverage as below).
- Not executed: live Keychain/token/inference/profile, daemon replacement, publish, full suite.

## Validation at `6a930c9` (superseded build)

- Focused suites: `uv run --frozen pytest -n 0 tests/test_org_probe.py tests/test_token_probe.py
  tests/test_token_runtime.py tests/test_rotation_owner.py tests/test_rotation_contention.py
  tests/test_autoswitch.py tests/test_switcher.py tests/test_transfer.py tests/test_menubar.py
  tests/test_tui.py tests/test_cli.py` → 1263 passed. Full suite intentionally not rerun.
- Real two-process contention (`tests/test_rotation_contention.py`, separate interpreters,
  isolated file backend): 4 passed in each of 3 additional repeats.
- `ruff check --select E9,F` on changed files: clean; `compileall` and `git diff --check`: clean.
- Installed-wheel offline smoke (`tests/pack_token_runtime.py`): isolated HOME/XDG/
  CLAUDE_CONFIG_DIR under `dist/pack-smoke/home`, forced file backend, every urllib entry
  blocked except an in-process fake opener that asserts the fixed origin/body. Covers
  managed import/replacement/export, organization permit collect + replay refusal, personal
  generation-fenced consent + one probe + `probeBudget`, rotation revision/lease and
  fail-closed `rotate --apply` (`coverage_unknown`), archive contents.
- Not executed: live Keychain, live token or inference, profile endpoint, daemon
  replacement, publish/push/merge, full suite.
