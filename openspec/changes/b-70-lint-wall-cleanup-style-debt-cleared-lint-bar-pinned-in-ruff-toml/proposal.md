## Why

The recurring "179-finding lint wall" on serve.py was ruff 0.16's expanded default
ruleset moving under the repo, and it pushed style churn into every bugfix diff.
Clearing it in one scoped pass also surfaced two latent runtime failures: the
screen-discovery API raised NameError on every call because its module-level
cache state had been lost from src/.

## What Changes

- Repo-wide `ruff format` + safe/reviewed autofixes; the lint bar is pinned in
  `ruff.toml` with a rationale comment per ignored rule.
- The verify gate is pinned in `WORKFLOW.toml` `[verify]` to the TDD suite
  (smoke/e2e suites write to the real tickets.db and can spawn billed agents,
  so they must never run as a gate).
- Restored page-scan module state: `GET /<pid>/api/screens` and
  `POST /<pid>/api/screens/scan` function again instead of raising NameError.
- Dead code removed: unused locals, unreferenced renderer fragments (see I-45
  for the possibly-lost workflows-view UI), a no-op `global`, stale noqa.

## Capabilities

### New Capabilities

- `screen-discovery`: screen scanning for the journey path builder —
  `POST /<pid>/api/screens/scan` discovers a project's pages with a headless
  browser; `GET /<pid>/api/screens` serves the cached results.

### Modified Capabilities

### Impact

- 90+ files reformatted (no behavior intended); `ruff.toml` and `WORKFLOW.toml`
  added at the repo root; exec bits added to the 8 shebanged entry points.
- serve.py: screens API restored; feedbacks callback no longer calls an
  undefined helper.
- Two stale trigger-evaluation tests aligned with the shipped master-switch
  design (`automation_mode='auto'` required by every default mutating
  workflow's trigger); the TDD suite is green end-to-end for the first time.
