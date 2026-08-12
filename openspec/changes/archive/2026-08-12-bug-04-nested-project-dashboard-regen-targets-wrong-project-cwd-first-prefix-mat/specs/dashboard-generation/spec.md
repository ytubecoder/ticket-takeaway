## ADDED Requirements

### Requirement: Dashboard regeneration names the changed project explicitly

`regenerate_dashboard()` SHALL pass the changed project's registered id to
`generate.py` via `--project`, rather than relying on cwd auto-detection, so a
write on one project can never rebuild another project's dashboard. When the
regeneration subprocess exits non-zero, the CLI SHALL print a warning to stderr
naming the project and the last line of the subprocess output; on success it
SHALL print nothing.

#### Scenario: Write on a nested project regenerates its own page

- **GIVEN** a registry where a child project's path lives inside a parent
  project's tree and the parent is listed first
- **WHEN** a ticket write happens on the child project
- **THEN** the child's `docs/sdlc-dashboard.html` is regenerated
- **AND** the parent's `docs/sdlc-dashboard.html` is untouched

#### Scenario: A failed regeneration is visible

- **WHEN** `generate.py` exits non-zero during post-write regeneration
- **THEN** a warning naming the project appears on stderr
- **AND** a successful regeneration prints nothing

#### Scenario: Legacy project dicts without an id still regenerate

- **WHEN** `regenerate_dashboard()` is called with a project dict lacking `id`
- **THEN** the regeneration runs without a `--project` flag, as before

### Requirement: cwd auto-detect resolves the most specific registered path

When `generate.py` runs with no `--project` or `--all` flag,
`detect_project_from_cwd()` SHALL select the registry entry with the longest
registered path that contains the cwd, independent of registry order. Entries
with an empty path SHALL never match, and a path SHALL only match at a
path-component boundary.

#### Scenario: Nested child wins regardless of registry order

- **GIVEN** a parent project and a child project registered inside its tree
- **WHEN** auto-detect runs from the child's directory
- **THEN** the child project is selected whether the parent is listed before
  or after it

#### Scenario: A sibling name prefix is not a match

- **GIVEN** a registered project at `/a/foo`
- **WHEN** auto-detect runs from `/a/foo-bar`
- **THEN** no project is selected

#### Scenario: Unregistered directories select nothing

- **WHEN** auto-detect runs from a directory under no registered path
- **THEN** no project is selected and generation proceeds unfiltered, as
  before
