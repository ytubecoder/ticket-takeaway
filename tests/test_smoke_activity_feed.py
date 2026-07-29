"""Smoke: /api/activity/feed shape. Hits the REAL tickets.db via the
dashboard_server fixture — run deliberately, never as a gate."""

import json
import urllib.request


def _get(base, path):
    # dashboard_server is project-scoped (http://host:port/ticket-takeaway);
    # the activity feed is a global (no-project) endpoint.
    root = base.rsplit("/", 1)[0]
    with urllib.request.urlopen(root + path) as r:
        return json.loads(r.read())


def test_cursor_init_shape(dashboard_server):
    d = _get(dashboard_server, "/api/activity/feed")
    assert set(d) == {"latest_id", "events"}
    assert d["events"] == []
    assert isinstance(d["latest_id"], int)


def test_since_and_bad_params(dashboard_server):
    d = _get(dashboard_server, "/api/activity/feed?since_id=0&limit=5")
    assert isinstance(d["events"], list)
    assert len(d["events"]) <= 5
    if d["events"]:
        e = d["events"][0]
        assert {"id", "project_id", "project_name", "subject_id",
                "event_kind", "actor_type"} <= set(e)
    # junk params must not 500
    d2 = _get(dashboard_server, "/api/activity/feed?since_id=abc&limit=zz")
    assert set(d2) == {"latest_id", "events"}
