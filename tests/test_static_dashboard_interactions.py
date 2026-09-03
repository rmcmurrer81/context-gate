from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC_ROOT = Path(__file__).resolve().parents[1] / "context_gate" / "web" / "static"


class _StartTagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append((tag, dict(attrs)))


def test_kpi_cards_are_accessible_queue_filter_controls() -> None:
    parser = _StartTagCollector()
    parser.feed((STATIC_ROOT / "index.html").read_text(encoding="utf-8"))

    cards = {
        attrs["data-kpi-filter"]: attrs
        for tag, attrs in parser.tags
        if tag == "button" and "data-kpi-filter" in attrs
    }

    assert set(cards) == {"ALL", "ALLOW", "REVIEW", "BLOCK"}
    assert all(attrs["type"] == "button" for attrs in cards.values())
    assert all(attrs["aria-controls"] == "case-queue" for attrs in cards.values())
    assert cards["REVIEW"]["aria-pressed"] == "true"
    assert sum(attrs["aria-pressed"] == "true" for attrs in cards.values()) == 1


def test_dashboard_script_wires_drill_down_interactions() -> None:
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert "setQueueFilter(button.dataset.kpiFilter" in script
    assert 'button.setAttribute("aria-current", "true")' in script
    assert 'showDialog("case-dialog")' in script
    assert '$("#case-dialog").addEventListener("close"' in script
    assert "row.dataset.caseId === returnId" in script
    assert "item?.items" in script
    assert 'trigger.setAttribute("aria-expanded", String(expanded))' in script
    assert "renderPatternEvidence(evidence, item, items, label)" in script
    assert "No supporting items will be invented" in script


def test_dashboard_styles_show_active_and_expanded_state() -> None:
    styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")

    assert '.kpi-card[aria-pressed="true"]' in styles
    assert ".case-row.selected .case-row-cue" in styles
    assert ".pattern-item.expanded" in styles
    assert ".pattern-evidence-list" in styles


def test_unconfigured_oauth_is_guided_before_any_popup() -> None:
    parser = _StartTagCollector()
    parser.feed((STATIC_ROOT / "index.html").read_text(encoding="utf-8"))
    controls = {attrs["id"]: attrs for _, attrs in parser.tags if attrs.get("id")}

    assert "required" not in controls["google-client-json"]
    assert "required" not in controls["google-client-id"]
    assert "required" not in controls["google-client-secret"]
    assert "required" not in controls["microsoft-client-id"]
    assert controls["google-client-id"]["aria-describedby"] == (
        "google-configure-status"
    )
    assert controls["google-client-secret"]["aria-describedby"] == (
        "google-configure-status"
    )
    assert controls["google-client-secret"]["type"] == "password"
    assert controls["google-client-json"]["aria-describedby"] == (
        "google-configure-status"
    )
    assert controls["microsoft-client-id"]["aria-describedby"] == (
        "microsoft-configure-status"
    )

    links = {
        attrs.get("href"): attrs
        for tag, attrs in parser.tags
        if tag == "a" and attrs.get("href")
    }
    oauth_links = {
        "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
        "https://console.cloud.google.com/auth/audience",
        "https://console.cloud.google.com/auth/clients/create",
        "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    }
    assert oauth_links <= set(links)
    assert all(links[url]["target"] == "_blank" for url in oauth_links)
    assert all("noopener" in (links[url]["rel"] or "") for url in oauth_links)

    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    precondition = script.index("if (!connectorIsConfigured(provider))")
    popup = script.index('window.open("about:blank"', precondition)
    assert precondition < popup
    assert 'showOAuthSetupRequired("google")' in script
    assert 'showOAuthSetupRequired("microsoft")' in script
    assert "button.textContent = `Connect ${providerLabel}`" in script
    assert "Every program that opens" in script
    assert "No blank sign-in window was opened" in script
    assert "client_id: clientId" in script
    assert "client_secret: clientSecret" in script
    assert "configureGoogleJson" in script
    assert "closeAuthorizationWindow(authWindow)" in script

    styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
    assert ".connector-needs-setup" in styles
    assert ".oauth-setup-guidance" in styles
    assert ".oauth-action-links" in styles


def test_connectivity_control_is_truthful_and_event_driven() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="connectivity-chip"' in html
    assert 'id="network-status">NETWORK UNKNOWN' in html
    assert 'id="monitor-cadence">MANUAL' in html
    assert "it is not a verified website or provider probe" in html
    assert "proven reachable only when its actual scan succeeds" in html
    assert 'window.addEventListener("online", renderConnectivity)' in script
    assert 'window.addEventListener("offline", renderConnectivity)' in script
    assert "navigator.onLine" in script
    assert "auto_monitor_enabled" in script
    assert "auto_monitor_minutes" in script
    assert "automatic checks run only while this page" in script


def test_connectivity_labels_cover_online_offline_unknown_and_cadence() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the browser-helper behavior test")

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const source = fs.readFileSync(process.argv[1], "utf8");
const handlers = new Map();
const window = { addEventListener: (name, callback) => handlers.set(name, callback) };
vm.runInNewContext(source, { window });
const helpers = window.ContextGateConnectivity;
assert.strictEqual(helpers.networkLabel(true), "NETWORK ONLINE");
assert.strictEqual(helpers.networkLabel(false), "NETWORK OFFLINE");
assert.strictEqual(helpers.networkLabel(null), "NETWORK UNKNOWN");
assert.strictEqual(helpers.cadenceLabel(false, 15), "MANUAL");
assert.strictEqual(helpers.cadenceLabel(true, 7), "AUTO · 7 MIN");
assert.strictEqual(helpers.cadenceLabel("true", 0), "AUTO · 1 MIN");
assert.strictEqual(helpers.cadenceLabel(true, 5000), "AUTO · 1440 MIN");
assert.ok(handlers.has("online"));
assert.ok(handlers.has("offline"));
"""
    completed = subprocess.run(
        [node, "-e", harness, str(STATIC_ROOT / "app.js")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
