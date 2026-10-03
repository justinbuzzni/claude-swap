# Combined token runtime artifact provenance (local, unpublished)

Branch `feat/claude-token-integration`, built from commit `6a930c9` (this file is
added by the following doc-only commit, so it is not inside the artifacts it
describes). Local custom artifact only: not published, not pinned, not a release.

| Item | Value |
|---|---|
| Base | `6397291` organization bridge + personal generation CAS (Sol) |
| Cherry-picked | `29db313`→`4bbbfc8`, `eb33624`→`79aed16`, `f51bd28`→`8df9a61` (rotation ownership, guarded commit) |
| Integration | `6a930c9` secret-free personal probe budget, combined smoke, contract corrections |
| Artifact marker | `saycode-setup-token-runtime-v1` (upstream base `0.27.0b1`) |
| Toolchain | uv 0.10.2, Python 3.14.5, `uv build --out-dir dist` from the clean committed tree |
| Wheel | `claude_swap-0.27.0b1-py3-none-any.whl` sha256 `b6c125f1bdbb9405bf877f8353e707937699851859e2a7960126200c1feddae4` |
| sdist | `claude_swap-0.27.0b1.tar.gz` sha256 `f2bbd669f5a2d1b0ece32614925e5cddf429214c9c1cdb32501b02096f22a384` |

Installed-wheel modules compared byte-for-byte with `git show 6a930c9:src/claude_swap/<m>` (sha256):

| Module | sha256 |
|---|---|
| token_runtime.py | `2921137387b3d56930c47d9f0de8827eaf49dd080c386d1ca59b4334323dd93b` |
| rotation_owner.py | `47947a0e8069289f620d26fa89bbc6ef70f331e03ab0af21de4c8bd26a47ecd4` |
| org_probe.py | `2c94ca85084215fa6af52c0eb17615f9474f71cd97ea46df7bdbd18ccb9e6fe4` |
| token_probe.py | `68b11fbb2b081cc46b11b008fa763ed36a7d53be4d801b56abd15d4c0fb10252` |
| switcher.py | `b87af4ba3ec5187c93f62f170b3bb7c9e84a7db7e42f0130ed2228853b050f41` |
| autoswitch.py | `3889fba6c73e5b8065813d1ccddc17419fdb1d51643ad3eb6822b8f0f214f446` |

Hashes are environment-specific build outputs; a rebuild elsewhere may differ in
archive metadata while module contents match the commit.

## Validation at `6a930c9`

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
