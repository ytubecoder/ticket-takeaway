# OpenSpec Surfacing in Ticket Takeaway

Implementation spec. This document is self-contained: implement exactly what is
written here, in this repository, and nothing else. Where a file:line is given
it was verified against the current `main`; if lines have drifted slightly,
locate the quoted anchor code instead.

## Context

Tickets can carry an OpenSpec change proposal on disk
(`openspec/changes/<change-name>/` in the *target project's* repo), linked via
the `spec` readiness flag (wire format `A:b-44-knowledge-ingestion-pipeline`,
parsed by `actions.parse_spec_link`, src/actions.py:444). Today TT renders none
of it: a ticket with a full proposal/design/tasks/spec-delta on disk shows
nothing in the UI. Worse, changes authored directly via OpenSpec skills (not
through `tickets-cli.py spec`) never get a `spec` flag at all, so TT doesn't
even know they exist — e.g. the loops project's ticket B-13 has
`openspec/changes/b-13-dashboard-run-now-2026-08-02/` on disk and an empty
`spec` flag.

## Product requirements

1. **Spec tab on the ticket page.** A ticket's page gets a "Spec" tab that
   shows whether an OpenSpec change exists for it and renders the change's
   documents (proposal.md, design.md, tasks.md, specs/**/*.md).
2. **Inline editing.** The documents are editable in place while reading:
   monospace `<textarea>` per document, debounced autosave (1s), same pattern
   as the existing description editor. No markdown renderer exists in TT and
   none is added.
3. **Derived spec status as an automation filter.** A new derived status enum
   (below) is computable per ticket without subprocesses, and workflow
   triggers can filter on it (`spec_status_in` predicate, surfaced in the
   rules editor UI).
4. **Unrecorded-change discovery + backfill.** Change dirs on disk whose name
   maps to the ticket (naming convention, `ticket_id_from_change_name`) but
   which are not recorded on the `spec` flag are discovered and listed, with a
   one-click "Record link on ticket" button that writes the flag (the B-13
   case).

## Design: SPEC_STATUSES + derivation

### Enum

Add to `src/constants.py`, directly next to `SPEC_LANES` (:176):

```python
# Derived spec status — computed from the `spec` readiness flag + the target
# project's openspec/changes/ directory. Filesystem-only: deriving this NEVER
# shells out to the openspec CLI (validation is separately covered by the
# subprocess-priced `spec_validates` predicate).
SPEC_STATUSES: tuple[str, ...] = (
    "undeclared",        # no spec flag, no matching change dir on disk
    "unrecorded_change", # no/empty spec flag, but >=1 matching live change dir exists
    "declared_invalid",  # spec flag set but unparseable (or lane with empty change name)
    "no_delta",          # lane C sentinel: change == "none" with a reason
    "linked",            # flag names a change and its live dir exists
    "linked_missing",    # flag names a change but no live dir and no archive copy
    "archived",          # flag names a change; no live dir, but an archive copy exists
    "forced",            # ticket was accepted with --force (override recorded on the flag)
)
```

Keep the tuple order exactly as above (tests assert it).

### Derivation (pure function, precedence top-down, first match wins)

`derive_spec_status` in `src/actions.py` (place near `SpecLink`, :413). Pure —
takes values, touches nothing:

```python
def derive_spec_status(
    flag_content: str,        # raw `spec` readiness flag ("" when unset)
    set_by: str,              # readiness_flags.set_by ("" when unset)
    change_exists_live: bool, # link.change names a live dir openspec/changes/<change>/
    change_has_archive: bool, # >=1 archive copy openspec/changes/archive/<date>-<change>/
    discovered: list[str],    # live change-dir names matching the ticket id (naming convention)
) -> str
```

1. **forced** — `set_by == "accept:--force"` OR the parsed link's note starts
   with `"accepted with --force:"` (that is exactly what
   `_forced_spec_flag_content`, src/actions.py:816, writes; check both because
   the markdown round-trip can rewrite `set_by`).
2. Flag empty (`flag_content.strip()` falsy):
   - `discovered` non-empty → **unrecorded_change**
   - else → **undeclared**
3. Flag non-empty but `parse_spec_link` yields an undeclared link (no lane),
   OR lane declared with an empty change name → **declared_invalid**
4. `link.claims_no_change` (change == `"none"`) → **no_delta**
5. `change_exists_live` → **linked**
6. `change_has_archive` → **archived** (archived beats linked_missing)
7. otherwise → **linked_missing**

Drift rule: when a lane/change IS declared but *other* matching dirs also
exist on disk, status follows the flag; the extra dirs are still reported in
`unrecorded` (below).

### Status accessor

`spec_status(conn, project_id, ticket_id)` in `src/actions.py`:

- Reads the flag row (content **and** `set_by`) from `readiness_flags`.
- Resolves the project path via `project_path_for(project_id)` (:368).
- Disk probes via `openspec_adapter` only (all subprocess-free, see below).
- Returns:

```python
{
  "status": str,                      # one of SPEC_STATUSES
  "link": {"lane": .., "change": .., "note": ..} or None,  # None when not declared
  "set_by": str,
  "unrecorded": [str, ...],           # discovered live dir names NOT equal to link.change
  "detail": str,                      # one human-readable line explaining the status
}
```

If `project_path_for` returns "" (unknown project), skip disk probes
(`discovered=[]`, both bools False) and derive from the flag alone.

## Implementation steps

### 1. `src/openspec_adapter.py` — filesystem probes (subprocess-free)

Add below the existing probe section (:208). These functions must never call
`_run` / spawn a subprocess.

- `matching_change_dirs(project_path, ticket_id) -> list[str]` — names of
  live dirs under `openspec/changes/` (excluding the `archive` subdir) where
  `ticket_id_from_change_name(name) == ticket_id.upper()` (:202 — the regex
  captures the full `[a-z]+-\d+` prefix, so B-1 / B-13 / B-130 can never
  cross-match). Sorted. Empty list when the changes root doesn't exist.
- `resolve_change_dir(project_path, name) -> tuple[Path, bool] | None` — the
  live dir `(path, False)` if it exists, else the **newest** archive copy
  `(path, True)` (last element of `archived_change_dirs(...)`, :226 — they
  sort by date prefix), else `None`.
- `change_docs(change_path: Path) -> list[str]` — relative doc paths, ordered:
  `proposal.md`, `design.md`, `tasks.md` (each only if present), then all
  `specs/**/*.md` sorted. Paths POSIX-style relative to the change dir.
- `class ArchivedChangeError(Exception)` — module-level.
- `read_change_doc(project_path, name, rel_path) -> str` and
  `write_change_doc(project_path, name, rel_path, content) -> None`.
  **Containment**, modeled on the `/api/browse` guard (src/serve.py:9158-86):
  resolve the change dir via `resolve_change_dir` (raise `FileNotFoundError`
  if None); target = `(change_dir / rel_path)`; then
  `os.path.realpath(target)` must satisfy
  `Path(...).relative_to(Path(os.path.realpath(change_dir)))` — a `ValueError`
  from `relative_to` means escape → raise `ValueError`. Additionally reject:
  suffix != `.md` (ValueError), file does not already exist
  (FileNotFoundError — no create-through-write), absolute `rel_path`
  (ValueError). `write_change_doc` raises `ArchivedChangeError` when
  `resolve_change_dir` returned an archived copy. Reads of archived copies are
  allowed.

### 2. `src/actions.py` — `derive_spec_status` + `spec_status`

As designed above. Near `SpecLink` (:413). Import `SPEC_STATUSES` from
constants.

### 3. Trigger predicate `spec_status_in` — five touch points

1. **`src/conditions.py` CONDITION_CATALOG** — add after `spec_validates`
   (:549):
   ```python
   "spec_status_in": {
       "label": "Spec status is one of",
       "params": [{"name": "values", "type": "status_list"}],
       "evaluator": _eval_spec_status_in,
   },
   ```
   The evaluator does **NOT** use `_delegate` (:130 — wrong signature: it
   calls `fn(db, subject)` returning `(ok, reasons)`, while `spec_status`
   takes ids and returns a dict). Instead:
   ```python
   def _eval_spec_status_in(ctx: dict, p: dict) -> tuple[bool, str]:
       import actions
       t = ctx.get("ticket_row") or ctx["ticket"]
       project_id, ticket_id = t["project_id"], t["id"]
       status = actions.spec_status(ctx["db"], project_id, ticket_id)["status"]
       values = p.get("values") or []
       ok = status in values
       return (ok, f"spec status is {status}" if ok
               else f"spec status {status} not in {values}")
   ```
   (Match the surrounding evaluators' style for how ticket fields are read —
   `sqlite3.Row` supports `t["project_id"]`.)
2. **`ui_catalog()`** — in the options block add
   `"spec_statuses": list(SPEC_STATUSES)`; on the existing `spec` attribute
   (:1099-1118) append a filter op:
   ```python
   {"key": "status_is_one_of", "label": "status is one of",
    "predicate_kind": "spec_status_in",
    "value_control": "spec_status_multi_select"},
   ```
   and in `predicate_to_attribute` (:1244) add
   `"spec_status_in": "spec"`.
3. **`src/serve.py` rules-editor JS** — in `buildValueControl` (:7453) add a
   `spec_status_multi_select` branch: clone the `status_multi_select` branch
   (:7480-87) but iterate `opt.spec_statuses`. In `readValueControl` (:7539)
   add `spec_status_multi_select` to the multi-select condition. In the
   serialise mapping (:7766-77) add `spec_status_in` to the kinds that emit
   `pred.values = Array.isArray(val) ? val : (val ? [val] : [])`.
4. **`src/trigger_describe.py`** — add `"spec_status_in": "Spec status is one
   of"` to the labels dict (:191 area) and a describe branch so
   `describe_trigger({"kind": "spec_status_in", "values": ["linked",
   "archived"]})` renders English like `Spec status is “linked” or
   “archived”` (use `_join_quoted`, :49) — it must NOT fall through to the
   generic `(spec_status_in)` rendering.
5. No new attribute — reuse the existing `spec` attribute.

### 4. API endpoints (`src/serve.py`)

Regex routes beside the existing readiness route (:10229 for PUT; mirror the
GET route placement conventions nearby). All client-facing URLs are
origin-relative (`/{pid}/api/...`) — never bake host:port. Implement the
bodies as **module-level helpers** (testable without a server), with thin
handler glue:

- `GET  /{pid}/api/tickets/{tid}/spec` → helper
  `_spec_tab_payload(proj, ticket_id) -> dict`:
  ```json
  {
    "status": "...", "link": {...}|null, "set_by": "...", "detail": "...",
    "unrecorded": [{"name": "b-13-dashboard-...", "suggested_content": "B:b-13-dashboard-..."}],
    "change": {"name": "...", "archived": false, "docs": ["proposal.md", ...]} | null
  }
  ```
  `change` is populated when the link names a change and
  `resolve_change_dir` finds it (live or archive). `suggested_content` is
  `"B:<dir-name>"` (lane B default).
- `GET  /{pid}/api/tickets/{tid}/spec/doc?path=<rel>` → `{path, content,
  readonly}` — `readonly: true` when the resolved change is an archive copy.
  400 on containment/extension errors, 404-style 400 on missing file
  (match the existing `_send_json({"error": ...}, 400)` idiom).
- `PUT  /{pid}/api/tickets/{tid}/spec/doc` body `{"path": ..., "content":
  ...}` → `{ok: true}`. `ArchivedChangeError` → **409**
  `{"error": "archived change is read-only"}`; containment/extension →
  **400**. On success, emit a `spec_doc_edited` activity event
  (payload `{"change": <name>, "path": <rel>}`, `ActorContext.human()`) —
  open the DB, `emit_event(...)`, `conn.commit()` in one transaction,
  mirroring the readiness emission at :3600-12. The file write happens
  first; no markdown sync or dashboard regen needed (no ticket fields
  changed).

**Backfill uses the existing endpoint** — the "Record link on ticket" button
PUTs `{"content": "<lane>:<dir-name>"}` to the existing
`/{pid}/api/tickets/{tid}/readiness/spec` route (:10229). Do NOT add a new
mutation endpoint for backfill.

### 5. Spec tab (`src/serve.py` ticket page)

In `_render_ticket_page`: add `"spec"` to `valid_tabs` (:5483), a dispatch
branch (:5488-97) to a new `_render_ticket_tab_spec(ticket, proj, port)`, and
a `_tab_link("spec", "Spec")` placed immediately after Overview (:5507-13).

Tab body, top to bottom:

1. **Status strip** — the derived status as a badge, plus lane, change name,
   and the `detail` line.
2. **Unrecorded banner** (when `unrecorded` non-empty) — list each discovered
   dir with a lane `<select>` (options A/B/C, default **B**) and a "Record
   link on ticket" button that PUTs the readiness flag (step 4) and reloads.
3. **Documents** (when `change` is present) — one collapsible section per doc
   (`<details>`/`<summary>` is fine). The textarea content is **lazy-loaded**:
   first expand fetches `GET .../spec/doc?path=...`, fills a monospace
   `<textarea>`, and wires a 1s-debounced `PUT` on `input` — clone the
   description auto-save pattern (`tp-desc-editor`, :5931-44, including
   `TP_API_BASE`). When `readonly`/archived: `disabled` textarea + a "read-only
   (archived)" note, no PUT wiring.
4. **Edge states**: `undeclared` → hint text pointing at
   `tickets-cli.py spec <project> <id> --lane A|B|C`; `linked_missing` →
   warning that the flag names a change with no directory on disk;
   `no_delta` → show the recorded reason (link note); `declared_invalid` →
   show the raw flag content.

JS gotcha (documented in CLAUDE.md, it has broken this codebase twice):
inside Python triple-quoted f-strings that emit inline `onclick="..."`
handlers, nested quotes must be HTML entities (`&apos;` / `&quot;`), never
backslash escapes. Prefer `addEventListener` wiring over inline handlers.

### 6. `src/generate.py` — kanban surfacing (minimal)

- `_render_readiness_row` (:13197): add an "S" indicator —
  `("S", "Spec", "spec" in t.readiness_flags)` with `flag_map` entry
  `"S": "spec"` — following the existing D/C/L pattern exactly. For the icon,
  reuse an icon name that already exists in `_svg_icon`'s registry (inspect
  it; `file-text` is known-present) — do not add new SVG assets.
- Ticket detail overlay (`#ticket-detail-overlay`, :6191): add ONE line/link
  ("Open spec →", element id `detail-spec-link`) that navigates to
  `/{pid}/tickets/{tid}?tab=spec`; set its `href` in the same JS that
  populates the overlay's other detail fields when a card is opened. Nothing
  more — no status rendering in the overlay.

### 7. `src/tickets-cli.py` — `cmd_spec` (:1371)

- New `--status` flag: read-only — print the `actions.spec_status(...)` result
  (status, lane, change, detail, any unrecorded dirs) and exit **without
  writing anything**.
- Record-only backfill: currently `cmd_spec` always calls `osa.new_change`
  (:1431), which fails or scaffolds when the dir already exists. Change: when
  the change dir already exists on disk (`osa.change_exists(project_path,
  change)`), **skip `new_change`** and just write the readiness flag
  (`set_by="cli:spec"`), printing that an existing change was recorded. The
  existing `--change` arg (:1430) disambiguates when multiple dirs match.

### 8. Skill docs

- `src/skills/ticket-takeaway/SKILL.md`: document the Spec tab, the
  `spec_status_in` trigger filter, the `--status`/backfill CLI behavior, and
  the two new API endpoints (follow the file's existing structure/tone).
- `src/skills/spec/SKILL.md`: ONE line noting that existing on-disk changes
  can be recorded onto a ticket (backfill) instead of scaffolded.

## Mandated tests — new file `tests/test_tdd_spec_status.py`

Pure TDD tests (no server, no subprocess, tmp_path fixtures). Follow the
conventions of the existing `tests/test_tdd_*.py` files (imports via the
`src/` path conftest arrangement). Required coverage — write these as real
assertions, not smoke-stubs:

1. **`derive_spec_status` matrix** — every one of the 8 statuses is produced
   by at least one input combination, plus precedence checks:
   `forced` beats a linked live dir; `archived` beats `linked_missing`
   (archive exists, live doesn't); `set_by="accept:--force"` alone triggers
   forced; note-prefix `accepted with --force:` alone triggers forced.
2. **`matching_change_dirs`** — fixture with dirs `b-1-x`, `b-13-y`,
   `b-130-z`, a date-suffixed `b-13-dashboard-run-now-2026-08-02`, and an
   `archive/2026-01-01-b-13-old` copy: B-13 matches exactly the two live
   b-13 dirs (both returned), never B-1/B-130, and never the archive copy.
3. **Containment** — `read_change_doc`/`write_change_doc` reject: `../x.md`,
   an absolute path, a symlink escaping the change dir, a non-`.md` path, a
   non-existent file; `write_change_doc` on an archived-only change raises
   `ArchivedChangeError`; a legitimate relative doc (`specs/foo/spec.md`)
   round-trips content.
4. **`spec_status` integration** — tmp project + registry/monkeypatched
   `project_path_for` + real sqlite (in-memory via `db.init_db`): flag × disk
   combinations produce the right `status`, `unrecorded` excludes the linked
   change but includes drift dirs, `link`/`set_by`/`detail` populated.
5. **`spec_status_in` evaluator** — true and false cases, **with
   `openspec_adapter._run` monkeypatched to raise AssertionError** — proving
   the whole path is subprocess-free.
6. **Catalog + describe** —
   `ui_catalog()["options"]["spec_statuses"] == list(SPEC_STATUSES)`; the
   `spec` attribute contains the `status_is_one_of` filter op with
   `value_control == "spec_status_multi_select"`;
   `predicate_to_attribute["spec_status_in"] == "spec"`;
   `describe_trigger({"kind": "spec_status_in", "values": ["linked",
   "archived"]})` returns English containing both values and NOT the literal
   `(spec_status_in)`.
7. **Endpoint helpers** (call the module-level serve.py helpers directly, no
   HTTP): spec payload shape includes `unrecorded[].suggested_content ==
   "B:<dir>"`; a successful doc write lands a `spec_doc_edited` row in
   `activity_events` (same transaction); the archived-write path maps to the
   409 outcome.

Minimal extensions to existing TDD test files are allowed where a list of
catalog kinds is asserted exhaustively and now needs the new kind.

## Hard constraints (contract — violations mean the work is rejected)

- **Verify = `python3 -m pytest tests/test_tdd_*.py -q` ONLY.** NEVER run
  `tests/test_smoke_*` or `tests/test_e2e_*` — they write the real production
  database and can spawn real, billed agent sessions.
- **No DB migration. Do not touch `src/db.py`.** No schema changes of any
  kind. (`activity_events` and `readiness_flags` already exist.)
- **No writes outside this worktree.** Never touch
  `~/.claude/ticket-takeaway/` (runtime deploy is done by the foreman after
  merge). Do not start `serve.py`.
- **Allowed files** — touching anything else is a scope violation:
  - `src/constants.py`, `src/openspec_adapter.py`, `src/actions.py`,
    `src/conditions.py`, `src/trigger_describe.py`, `src/serve.py`,
    `src/generate.py`, `src/tickets-cli.py`
  - `src/skills/ticket-takeaway/SKILL.md`, `src/skills/spec/SKILL.md`
  - `tests/test_tdd_spec_status.py` (new) + minimal extensions to existing
    `tests/test_tdd_*.py` files
- **`uvx ruff check` must stay clean.** The ruleset is pinned in `ruff.toml`;
  every ignored rule there has a WHY comment — never "fix" an ignored
  category. SIM118 stays ignored (membership tests on `sqlite3.Row`).
- **PEON_REPORT.md** must paste (a) the full tail of the TDD suite run and
  (b) the `uvx ruff check` output, plus a file-by-file summary of what
  changed.

## Definition of Done

1. `python3 -m pytest tests/test_tdd_*.py -q` — entire suite green (new tests
   included, pre-existing tests unbroken).
2. `uvx ruff check` — clean.
3. All 8 implementation steps present; all mandated tests present and
   genuinely asserting.
4. Diff confined to the allowed files.
5. PEON_REPORT.md contains the evidence above.

## Edge decisions (already made — do not re-litigate)

- **Archived changes**: readable (newest archive copy), never writable.
- **Multiple discovered dirs**: all listed; the user picks one to record;
  status stays `unrecorded_change` until recorded.
- **Flag/disk drift** (flag → X, dir Y also matches): status follows the
  flag; Y is still listed under `unrecorded`.
- **Editor concurrency**: debounced last-write-wins, same as the description
  editor; no polling on the tab, so no dirty-guard is needed.
- **`validated`/`invalid` are deliberately NOT statuses** — validation shells
  out to the openspec CLI and is already covered by the `spec_validates`
  predicate. Everything here stays filesystem-only.
