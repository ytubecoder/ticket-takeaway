"""E2E: real tmux session → link → capture → API surfaces → unlink.

Skipped if tmux isn't on PATH.
"""
from __future__ import annotations
import os, subprocess, time, shutil, pytest, requests

if not shutil.which("tmux"):
    pytestmark = pytest.mark.skip(reason="tmux not installed")

SESSION = "tt-e2e-pane"


@pytest.fixture
def tmux_session():
    subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "-x", "80", "-y", "24"], check=True)
    pane = subprocess.run(
        ["tmux", "list-panes", "-t", SESSION, "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    yield pane
    subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)


def test_capture_after_link(dashboard_server, tmux_session):
    base = dashboard_server
    pane = tmux_session
    # Get first ticket ID
    r = requests.get(f"{base}/api/tickets", timeout=5)
    data = r.json()
    tickets = data.get("tickets", data) if isinstance(data, dict) else data
    tid = tickets[0]["id"]
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": pane, "host": os.uname()[1], "pane_descriptor": f"{SESSION}:0.0"},
        timeout=5,
    )
    # Send some output into the pane
    subprocess.run(["tmux", "send-keys", "-t", pane, "echo 'hello from pane'", "Enter"], check=True)
    # Wait for capture worker to pick it up (interval 2s + buffer)
    time.sleep(4)
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    links = r.json()["pane_links"]
    match = next((l for l in links if l["pane_address"] == pane), None)
    assert match is not None
    assert "hello from pane" in (match["tail_text"] or "")


def test_send_keys_writes_to_pane(dashboard_server, tmux_session):
    base = dashboard_server
    pane = tmux_session
    # Get first ticket ID
    r = requests.get(f"{base}/api/tickets", timeout=5)
    data = r.json()
    tickets = data.get("tickets", data) if isinstance(data, dict) else data
    tid = tickets[0]["id"]
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": pane, "host": os.uname()[1], "pane_descriptor": f"{SESSION}:0.0"},
        timeout=5,
    )
    requests.post(
        f"{base}/api/pane-links/{pane}/send-keys",
        json={"text": "echo 'from gui'", "press_enter": True},
        timeout=5,
    )
    time.sleep(4)
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    match = next((l for l in r.json()["pane_links"] if l["pane_address"] == pane), None)
    assert "from gui" in (match["tail_text"] or "")
