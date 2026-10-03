# Combined token runtime artifact provenance (local, unpublished)

Branch `feat/claude-token-integration`. Current artifact built from commit
`86e6018` (Astra audit fixes); the previous `6a930c9` build is kept below as
history. This file is updated by doc-only commits, so it is never inside the
artifacts it describes. Local custom artifact only: not published, not pinned, not a release.

| Item | Value |
|---|---|
| Base | `6397291` organization bridge + personal generation CAS (Sol) |
| Cherry-picked | `29db313`→`4bbbfc8`, `eb33624`→`79aed16`, `f51bd28`→`8df9a61` (rotation ownership, guarded commit) |
| Integration | `6a930c9` secret-free personal probe budget, combined smoke, contract corrections |
| Audit fixes | `86e6018` profile-OAuth classification, org completion-time deadline, elapsed retryAt |
| Artifact marker | `saycode-setup-token-runtime-v1` (upstream base `0.27.0b1`) |
| Toolchain | uv 0.10.2, Python 3.14.5, `uv build --out-dir dist` from the clean committed tree |
| Wheel (`86e6018`, current) | `claude_swap-0.27.0b1-py3-none-any.whl` sha256 `26fd90da40b8b1bcbfd945e8bb7f7043c3494d6c88502efc1cf42991d2aee9a6` |
| sdist (`86e6018`, current) | `claude_swap-0.27.0b1.tar.gz` sha256 `d5c11cd1b5504be23bed44489c80610c6bfa94bcfcffda71e71f5eedeb38cfc1` |
| Wheel (`6a930c9`, superseded) | sha256 `b6c125f1bdbb9405bf877f8353e707937699851859e2a7960126200c1feddae4` |
| sdist (`6a930c9`, superseded) | sha256 `f2bbd669f5a2d1b0ece32614925e5cddf429214c9c1cdb32501b02096f22a384` |

Installed-wheel modules compared byte-for-byte with `git show 86e6018:src/claude_swap/<m>` (sha256):

| Module | sha256 |
|---|---|
| token_runtime.py | `d01043d45f5961a4e8bedc2b0efbb6916f3c9b17db0394b65849de88d3d2453c` |
| rotation_owner.py | `47947a0e8069289f620d26fa89bbc6ef70f331e03ab0af21de4c8bd26a47ecd4` |
| org_probe.py | `19ddc135db28f8a94cf71f15946e5a4ef4af4664605a4fb039a28c62062a0b04` |
| token_probe.py | `68b11fbb2b081cc46b11b008fa763ed36a7d53be4d801b56abd15d4c0fb10252` |
| switcher.py | `b87af4ba3ec5187c93f62f170b3bb7c9e84a7db7e42f0130ed2228853b050f41` |
| autoswitch.py | `3889fba6c73e5b8065813d1ccddc17419fdb1d51643ad3eb6822b8f0f214f446` |

Hashes are environment-specific build outputs; a rebuild elsewhere may differ in
archive metadata while module contents match the commit.

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
