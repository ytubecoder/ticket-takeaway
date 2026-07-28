## ADDED Requirements

### Requirement: Screen scan results are cached per project and served on demand

The server SHALL keep screen-scan results in a per-project in-memory cache
guarded by a lock. `POST /<pid>/api/screens/scan` SHALL run the headless-browser
scan for the project, store the JSON result in the cache under the project id,
and return it. `GET /<pid>/api/screens` SHALL return the cached result without
re-scanning, or an empty list with a hint when no scan has run yet. Neither
endpoint may raise for the no-scan-yet case.

#### Scenario: Scan populates the cache

- **WHEN** a client POSTs to `/<pid>/api/screens/scan` and the scan succeeds
- **THEN** the response contains the discovered screens
- **AND** the result is stored in the cache under that project id

#### Scenario: Cached results are served without re-scanning

- **GIVEN** a completed scan for a project
- **WHEN** a client GETs `/<pid>/api/screens`
- **THEN** the cached screens are returned and no new browser scan is launched

#### Scenario: No scan has run yet

- **WHEN** a client GETs `/<pid>/api/screens` before any scan for that project
- **THEN** the response is an empty screens list plus a hint to POST
  `/api/screens/scan`, not an error
