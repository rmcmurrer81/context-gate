from __future__ import annotations

import base64
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from context_gate import web_console
from context_gate.company_profile import load_company_profile
from context_gate.operator_learning import GuidanceOrigin, OperatorGuidance
from context_gate.report_exports import ExportArtifact
from context_gate.source_catalog import SourceCatalog, SourceRecord
from context_gate.website_sources import (
    WebsiteEvent,
    WebsiteScanRecord,
    WebsiteScanResult,
    WebsiteSource,
)


@pytest.fixture
def console_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[web_console.ConsoleApplication]:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(web_console, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(
        web_console, "PROFILE_PATH", runtime_root / "company_profile.json"
    )
    monkeypatch.setattr(web_console, "MEMORY_PATH", runtime_root / "memory.sqlite3")
    monkeypatch.setattr(web_console, "LOGO_PATH", runtime_root / "company_logo.png")
    monkeypatch.setattr(
        web_console,
        "WEBSITE_SOURCES_PATH",
        runtime_root / "website_sources.json",
    )
    monkeypatch.setattr(
        web_console,
        "TRACKING_TOPICS_PATH",
        runtime_root / "tracking_topics.json",
    )
    monkeypatch.setattr(web_console, "TENANT_ID", "web-console-test")
    app = web_console.ConsoleApplication()
    try:
        yield app
    finally:
        app.close()


def test_provider_app_registrations_persist_without_mailbox_tokens(
    console_app: web_console.ConsoleApplication,
) -> None:
    google_client_id = "contextgate-demo.apps.googleusercontent.com"
    google_client_secret = "fictional-local-app-secret"
    microsoft_client_id = "12345678-1234-4234-9234-123456789012"

    google_state = console_app.configure_google(
        {
            "client_id": google_client_id,
            "client_secret": google_client_secret,
        }
    )
    microsoft_state = console_app.configure_microsoft(
        {"client_id": microsoft_client_id}
    )

    assert google_state["connectors"]["google"]["configured"] is True
    assert microsoft_state["connectors"]["microsoft"]["configured"] is True
    assert google_client_secret not in json.dumps(microsoft_state)
    saved = console_app.oauth_clients_path.read_text(encoding="utf-8")
    assert google_client_id in saved
    assert microsoft_client_id in saved
    assert "access_token" not in saved
    assert "refresh_token" not in saved

    console_app.close()
    reloaded = web_console.ConsoleApplication()
    try:
        status = reloaded.state()["connectors"]
        assert status["google"]["configured"] is True
        assert status["microsoft"]["configured"] is True
        assert status["google"]["connected"] is False
        assert status["microsoft"]["connected"] is False
        assert google_client_secret not in json.dumps(status)
    finally:
        reloaded.close()


def test_one_screen_state_and_explainable_source_counts(
    console_app: web_console.ConsoleApplication,
) -> None:
    state = console_app.state()
    assert state["service"] == "ContextGate"
    assert state["mode"] == "LOCAL-ONLY"
    assert state["totals"] == {
        "total": 9,
        "allow": 3,
        "review": 3,
        "block": 3,
        "corrections": 0,
    }
    assert state["selected_case_id"] == "R1"
    assert state["source_status"]["mailbox"] == "Not connected"
    assert state["no_external_action"] is True
    assert state["calendar"]["scheduled_count"] == 3
    assert state["calendar"]["unscheduled_count"] == 6
    assert state["calendar"]["data_mode"] == "fictional_demo"
    assert len(state["calendar"]["events"]) == 9
    assert {
        item["title"] for item in state["calendar"]["events"] if item["date_iso"]
    } == {
        "Manhattan ML Meetup",
        "Javits Future Expo",
        "Streaming Agents Webinar",
    }
    assert all(
        item["data_origin"] == "fictional_demo" for item in state["calendar"]["events"]
    )
    patterns = {item["pattern_id"]: item for item in state["patterns"]}
    assert len(patterns["distinct-events"]["items"]) == 9
    assert len(patterns["eventbrite-events"]["items"]) == 5
    assert {item["title"] for item in patterns["eventbrite-events"]["items"]} == {
        "AI Builders NYC",
        "Boston Robotics Forum",
        "Brooklyn Data Night",
        "Philadelphia Product Lab",
        "Queens Tech Social",
    }
    assert len(patterns["new-york-city-events"]["items"]) == 6
    assert all(item["reference"] for item in patterns["eventbrite-events"]["items"])

    pattern_summary = console_app.chat(
        {"message": "What patterns did you find?", "save_guidance": False}
    )["answer"]
    assert "5 active visible patterns" in pattern_summary["text"]
    assert "Eventbrite events: 5" in pattern_summary["text"]
    assert "Address recurrence: 8 vs 3" in pattern_summary["text"]
    assert pattern_summary["citations"]

    selected_explanation = console_app.chat(
        {
            "message": "Why does the selected case need review?",
            "save_guidance": False,
        }
    )["answer"]
    assert selected_explanation["text"].startswith("[case R1] Missing provenance")
    assert "event:evt-105" in selected_explanation["citations"]

    eventbrite = console_app.chat(
        {"message": "How many events came from Eventbright?", "save_guidance": False}
    )["answer"]
    assert "5 distinct Eventbrite events" in eventbrite["text"]
    assert "6 matching messages" in eventbrite["text"]
    assert eventbrite["citations"]

    new_york = console_app.chat(
        {"message": "How many events are in New York City?", "save_guidance": False}
    )["answer"]
    assert "6 distinct New York City events" in new_york["text"]
    assert "7 matching messages" in new_york["text"]

    tracked = console_app.chat(
        {
            "message": "Keep track of events from Hanson Robotics.",
            "save_guidance": False,
        }
    )
    assert tracked["state"]["chat_history"][-2]["saved"] is True
    assert "429 11th Avenue" in tracked["answer"]["text"]
    assert "September 18, 2026" in tracked["answer"]["text"]
    assert tracked["state"]["guidance_count"] == 1

    repeated_tracking = console_app.chat(
        {
            "message": "Keep track of events from Hanson Robotics.",
            "save_guidance": False,
        }
    )
    assert repeated_tracking["state"]["guidance_count"] == 1
    assert any(
        item.startswith("guidance:")
        for item in repeated_tracking["answer"]["citations"]
    )


def test_demo_reset_restores_welcome_after_chat_history_rolls_over(
    console_app: web_console.ConsoleApplication,
) -> None:
    for _ in range(web_console.MAX_HISTORY):
        console_app.chat({"message": "What can you do?", "save_guidance": False})

    assert (
        console_app.state()["chat_history"][0]["text"] != web_console.WELCOME_CHAT_TEXT
    )

    reset = console_app.reset_demo()

    assert reset["chat_history"] == [
        {
            "role": "assistant",
            "text": web_console.WELCOME_CHAT_TEXT,
            "citations": [],
            "saved": False,
        }
    ]


def test_chat_answers_calendar_upcoming_and_undated_questions_with_citations(
    console_app: web_console.ConsoleApplication,
) -> None:
    overview = console_app.chat(
        {"message": "What is on my calendar?", "save_guidance": False}
    )["answer"]
    undated = console_app.chat(
        {"message": "Which events need dates?", "save_guidance": False}
    )["answer"]

    assert "3 scheduled distinct events" in overview["text"]
    assert "6 events needing a source date" in overview["text"]
    assert set(overview["citations"]) == {
        "fictional-email://m08",
        "fictional-email://m09",
        "fictional-email://m10",
    }
    assert "6 visible events without a usable source date" in undated["text"]
    assert "instead of guessing" in undated["text"]
    assert "2 additional undated events are available" in undated["text"]
    assert len(undated["citations"]) == 4

    today = datetime.now(UTC).astimezone().date()
    tomorrow = today + timedelta(days=1)
    console_app.catalog = SourceCatalog(
        records=[
            SourceRecord(
                record_id="live-event-1",
                event_key="next-launch",
                title="Next Launch",
                source_name="Public website",
                organization="Example Robotics",
                event_date=tomorrow.isoformat(),
                event_time="10:00 AM ET",
                address="100 Example Avenue",
                evidence_reference="https://events.example.test/next-launch",
            ),
            SourceRecord(
                record_id="live-event-2",
                event_key="date-pending",
                title="Date Pending",
                source_name="Mailbox",
                organization="Example AI",
                evidence_reference="google-mail://date-pending",
            ),
        ]
    )

    upcoming = console_app.chat(
        {"message": "Show upcoming events", "save_guidance": False}
    )

    assert "1 scheduled upcoming event" in upcoming["answer"]["text"]
    assert tomorrow.isoformat() in upcoming["answer"]["text"]
    assert "did not treat 1 undated event as upcoming" in upcoming["answer"]["text"]
    assert upcoming["answer"]["citations"] == [
        "https://events.example.test/next-launch"
    ]
    assert upcoming["state"]["calendar"]["scheduled_count"] == 1
    assert upcoming["state"]["calendar"]["unscheduled_count"] == 1


def test_chat_configures_parallel_grouped_metrics_and_answers_from_uploaded_rows(
    console_app: web_console.ConsoleApplication,
) -> None:
    proposed = console_app.chat(
        {"message": "Also track office sales", "save_guidance": False}
    )
    assert "Proposed tracking topic" in proposed["answer"]["text"]
    assert proposed["state"]["tracking"]["topics"] == []
    assert proposed["state"]["tracking"]["pending_confirmation"] is True

    confirmed = console_app.chat(
        {"message": "Confirm tracking configuration", "save_guidance": False}
    )
    assert "Confirmed and saved office sales" in confirmed["answer"]["text"]
    assert confirmed["state"]["company"]["important_detail"] == "sales"
    assert confirmed["state"]["company"]["identity_fields"] == ["office"]
    assert len(confirmed["state"]["tracking"]["topics"]) == 1

    source = b"office,sales\nNew York,100\nAustin,40\nNew York,89\nAustin,33\n"
    uploaded = console_app.upload(
        {
            "filename": "fictional-office-sales.csv",
            "content_type": "text/csv",
            "data_base64": base64.b64encode(source).decode("ascii"),
        }
    )
    assert uploaded["source_status"]["last_upload"]["grouped_metric_rows"] == 4
    assert uploaded["grouped_metrics"][0]["group_totals"] == {
        "New York": 189,
        "Austin": 73,
    }
    # Structured metric rows do not become fake calendar events.
    assert all(
        item["title"] != "fictional-office-sales.csv"
        for item in uploaded["calendar"]["events"]
    )

    new_york = console_app.chat(
        {"message": "What are total sales for New York?", "save_guidance": False}
    )["answer"]
    assert "New York: 189 sales" in new_york["text"]
    assert len(new_york["citations"]) == 2
    austin = console_app.chat(
        {"message": "What are total sales for Austin?", "save_guidance": False}
    )["answer"]
    assert "Austin: 73 sales" in austin["text"]

    console_app.chat({"message": "Track revenue by region", "save_guidance": False})
    parallel = console_app.chat(
        {"message": "Confirm tracking configuration", "save_guidance": False}
    )
    assert len(parallel["state"]["tracking"]["topics"]) == 2
    listed = console_app.chat(
        {"message": "What are you tracking?", "save_guidance": False}
    )["answer"]["text"]
    assert "office sales" in listed
    assert "region revenue" in listed
    switched = console_app.chat({"message": "Go back to sales", "save_guidance": False})
    assert switched["state"]["tracking"]["active_topic"]["name"] == "office sales"
    assert len(switched["state"]["tracking"]["topics"]) == 2
    assert "does not stop source collection" in switched["answer"]["text"]


def test_chat_paraphrases_do_not_collide_with_topic_switching(
    console_app: web_console.ConsoleApplication,
) -> None:
    shown_events = console_app.chat(
        {"message": "Show all events from Hanson Robotics.", "save_guidance": False}
    )
    assert (
        "2 distinct matching events from Hanson Robotics"
        in shown_events["answer"]["text"]
    )
    assert shown_events["state"]["chat_history"][-2]["saved"] is False

    tracked_events = console_app.chat(
        {"message": "Track all events by Hanson Robotics.", "save_guidance": False}
    )
    assert (
        "I’ll keep track of events from Hanson Robotics"
        in tracked_events["answer"]["text"]
    )
    assert tracked_events["state"]["chat_history"][-2]["saved"] is True

    sourced_count = console_app.chat(
        {"message": "Show your sources for the NYC total.", "save_guidance": False}
    )
    assert "6 distinct New York City events" in sourced_count["answer"]["text"]
    assert len(sourced_count["answer"]["citations"]) == 6

    proposal = console_app.chat(
        {
            "message": "Set our important detail to sales and identify records by office.",
            "save_guidance": False,
        }
    )
    assert proposal["state"]["tracking"]["pending_confirmation"] is True
    canceled = console_app.chat({"message": "Never mind.", "save_guidance": False})
    assert "Nothing was saved" in canceled["answer"]["text"]
    assert canceled["state"]["tracking"]["topics"] == []

    console_app.chat(
        {"message": "I also want to track office sales.", "save_guidance": False}
    )
    confirmed = console_app.chat(
        {"message": "Yes, confirm that.", "save_guidance": False}
    )
    assert confirmed["state"]["tracking"]["active_topic"]["name"] == "office sales"

    source = b"office,sales\nNew York,100\nAustin,40\nNew York,89\nAustin,33\n"
    console_app.upload(
        {
            "filename": "fictional-office-sales.csv",
            "content_type": "text/csv",
            "data_base64": base64.b64encode(source).decode("ascii"),
        }
    )
    natural_total = console_app.chat(
        {
            "message": "How much did the New York office sell?",
            "save_guidance": False,
        }
    )
    assert "New York: 189 sales" in natural_total["answer"]["text"]
    event_count = console_app.chat(
        {"message": "How many events are in New York City?", "save_guidance": False}
    )
    assert "6 distinct New York City events" in event_count["answer"]["text"]

    console_app.chat({"message": "Also track robotics.", "save_guidance": False})
    console_app.chat({"message": "Confirm it.", "save_guidance": False})
    monitoring = console_app.chat(
        {"message": "What are you monitoring?", "save_guidance": False}
    )
    assert "office sales" in monitoring["answer"]["text"]
    assert "robotics" in monitoring["answer"]["text"]
    switched = console_app.chat(
        {"message": "Show me robotics.", "save_guidance": False}
    )
    assert switched["state"]["tracking"]["active_topic"]["name"] == "robotics"
    unavailable = console_app.chat(
        {"message": "Show me an unknown topic.", "save_guidance": False}
    )
    assert "not available" in unavailable["answer"]["text"]
    assert unavailable["state"]["tracking"]["active_topic"]["name"] == "robotics"


def test_chat_case_and_calendar_requests_are_not_stolen_by_topic_switching(
    console_app: web_console.ConsoleApplication,
) -> None:
    green = console_app.chat(
        {"message": "Show every green case.", "save_guidance": False}
    )
    assert "3 case(s) reach ALLOW" in green["answer"]["text"]
    assert {"case:A1", "case:A2", "case:A3"} <= set(green["answer"]["citations"])

    evidence = console_app.chat(
        {"message": "Show me the evidence for B1.", "save_guidance": False}
    )
    assert "[event evt-100]" in evidence["answer"]["text"]
    assert "[event evt-104]" in evidence["answer"]["text"]
    assert "event:evt-100" in evidence["answer"]["citations"]

    calendar = console_app.chat(
        {"message": "How many events are on my calendar?", "save_guidance": False}
    )
    assert "3 scheduled distinct events" in calendar["answer"]["text"]
    assert "6 events needing a source date" in calendar["answer"]["text"]
    assert len(calendar["answer"]["citations"]) == 3

    mixed_domains = console_app.chat(
        {"message": "How many red items came from Eventbrite?", "save_guidance": False}
    )
    assert "those records are not joined" in mixed_domains["answer"]["text"]
    assert "I will not invent an intersection" in mixed_domains["answer"]["text"]
    assert mixed_domains["answer"]["citations"] == []

    console_app.chat({"message": "Also track robotics.", "save_guidance": False})
    console_app.chat({"message": "Confirm it.", "save_guidance": False})
    evidence_with_topic = console_app.chat(
        {"message": "Show me the evidence for B1.", "save_guidance": False}
    )
    assert "[event evt-100]" in evidence_with_topic["answer"]["text"]
    assert evidence_with_topic["state"]["tracking"]["active_topic"]["name"] == (
        "robotics"
    )


def test_chat_negated_or_explanatory_delete_language_never_deletes(
    console_app: web_console.ConsoleApplication,
) -> None:
    negated = console_app.chat(
        {"message": "Don't delete data from Posh.", "save_guidance": False}
    )
    assert "No data was deleted" in negated["answer"]["text"]
    assert negated["state"]["company"]["deleted_sources"] == []
    assert console_app.catalog.matching_count("Posh") == 1

    hidden = console_app.chat(
        {"message": "Do not show me data from Posh.", "save_guidance": False}
    )
    assert hidden["state"]["company"]["hidden_sources"] == ["Posh"]

    explanatory = console_app.chat(
        {"message": "Why did you delete it?", "save_guidance": False}
    )
    assert "No data was deleted" in explanatory["answer"]["text"]
    assert explanatory["state"]["company"]["hidden_sources"] == ["Posh"]
    assert explanatory["state"]["company"]["deleted_sources"] == []


def test_chat_lists_bounded_events_and_sources_without_switching_topics(
    console_app: web_console.ConsoleApplication,
) -> None:
    show_me = console_app.chat(
        {"message": "Show me events from Hanson Robotics.", "save_guidance": False}
    )
    assert (
        "2 distinct matching events from Hanson Robotics" in show_me["answer"]["text"]
    )
    assert len(show_me["answer"]["citations"]) == 2
    assert show_me["state"]["chat_history"][-2]["saved"] is False

    all_events = console_app.chat(
        {"message": "Show all events and sources.", "save_guidance": False}
    )
    assert "9 distinct visible events" in all_events["answer"]["text"]
    assert "Showing 6 with their sources" in all_events["answer"]["text"]
    assert "Open Calendar for the remaining 3 events" in all_events["answer"]["text"]
    assert len(all_events["answer"]["citations"]) == 6
    assert all_events["state"]["tracking"]["active_topic"] is None
    assert all_events["state"]["chat_history"][-2]["saved"] is False

    eventbrite = console_app.chat(
        {"message": "What events came from Eventbrite?", "save_guidance": False}
    )
    assert "5 distinct matching events from Eventbrite" in eventbrite["answer"]["text"]
    assert len(eventbrite["answer"]["citations"]) == 5

    hanson = console_app.chat(
        {
            "message": "What Hanson Robotics events do you have?",
            "save_guidance": False,
        }
    )
    assert "2 distinct matching events from Hanson Robotics" in hanson["answer"]["text"]
    assert len(hanson["answer"]["citations"]) == 2


def test_chat_natural_inventory_questions_show_counts_items_and_citations(
    console_app: web_console.ConsoleApplication,
) -> None:
    for question in (
        "What data are you collecting?",
        "What sources/data do you have?",
        "What sources do you have?",
    ):
        answer = console_app.chat({"message": question, "save_guidance": False})[
            "answer"
        ]

        assert "10 visible source records" in answer["text"]
        assert "9 distinct events" in answer["text"]
        assert "By source: Eventbrite 5" in answer["text"]
        assert "Showing 6 items" in answer["text"]
        assert len(answer["citations"]) == 6

    tracking = console_app.chat(
        {"message": "What are you tracking?", "save_guidance": False}
    )["answer"]
    assert "No named tracking topics are configured" in tracking["text"]
    assert "10 visible source records" in tracking["text"]
    assert "By source: Eventbrite 5" in tracking["text"]
    assert len(tracking["citations"]) == 6


def test_chat_eventbrite_inventory_handles_common_misspelling(
    console_app: web_console.ConsoleApplication,
) -> None:
    for question in (
        "What did you get from Eventbrite?",
        "What did you get from Eventbright?",
    ):
        answer = console_app.chat({"message": question, "save_guidance": False})[
            "answer"
        ]

        assert "From Eventbrite, I have 5 distinct visible events" in answer["text"]
        assert "6 source records" in answer["text"]
        assert "AI Builders NYC" in answer["text"]
        assert "Philadelphia Product Lab" in answer["text"]
        assert len(answer["citations"]) == 5


def test_chat_help_setup_ingestion_and_monitoring_answers_are_truthful(
    console_app: web_console.ConsoleApplication,
) -> None:
    capabilities = console_app.chat(
        {"message": "What can you do?", "save_guidance": False}
    )["answer"]["text"]
    assert "count or list visible events with source citations" in capabilities
    assert "I do not send mail" in capabilities

    tracking_help = console_app.chat(
        {"message": "How do I change what you track?", "save_guidance": False}
    )["answer"]["text"]
    assert "show the proposed metric and identity/grouping fields" in tracking_help
    assert "confirm tracking configuration" in tracking_help

    company_setup = console_app.chat(
        {
            "message": "How do I set this up for my company?",
            "save_guidance": False,
        }
    )["answer"]["text"]
    assert company_setup.startswith("Open Company setup in the left rail")
    assert "Crowd size" in company_setup

    correction_help = console_app.chat(
        {
            "message": "How can I correct a mistaken blocked decision?",
            "save_guidance": False,
        }
    )["answer"]["text"]
    assert "Open a case in Case details" in correction_help
    assert "Chat did not change any decision" in correction_help

    crowd = console_app.chat(
        {"message": "How did you get 113 people?", "save_guidance": False}
    )["answer"]
    assert "35 + 78 = 113" in crowd["text"]
    assert len(crowd["citations"]) == 2

    uploads = console_app.chat(
        {"message": "What file formats can I upload?", "save_guidance": False}
    )["answer"]["text"]
    assert "CSV, JSON, HTML, XML" in uploads
    assert "OCR_REQUIRED" in uploads
    assert "Upload bytes are not saved" in uploads

    natural_uploads = console_app.chat(
        {
            "message": "Can I upload a screenshot, Word document, PDF, or exported email?",
            "save_guidance": False,
        }
    )["answer"]["text"]
    assert "text-layer PDF, DOCX, PNG, JPEG, GIF, and WebP" in natural_uploads
    assert "OCR_REQUIRED" in natural_uploads

    provider_export = console_app.chat(
        {
            "message": "Can I upload an Outlook email export?",
            "save_guidance": False,
        }
    )["answer"]["text"]
    assert "exported EML email" in provider_export
    assert "Google or Microsoft client ID" not in provider_export

    website = console_app.chat(
        {"message": "How do I set up a website source?", "save_guidance": False}
    )["answer"]["text"]
    assert "public HTTP or HTTPS URL and a short extraction goal" in website
    assert "then choose Scan" in website
    assert "only while the browser app remains open" in website

    website_monitor = console_app.chat(
        {"message": "How do I set up website monitoring?", "save_guidance": False}
    )["answer"]["text"]
    assert "public HTTP or HTTPS URL and a short extraction goal" in website_monitor
    assert "optional automatic check" in website_monitor

    monitor_off = console_app.chat(
        {
            "message": "Are you continuously monitoring my sources?",
            "save_guidance": False,
        }
    )["answer"]["text"]
    assert "Automatic source checks are off" in monitor_off
    assert "there is no always-on server watcher" in monitor_off

    console_app.update_profile(
        {
            "identity_fields": console_app.profile.identity_fields,
            "auto_monitor_enabled": True,
            "auto_monitor_minutes": 7,
        }
    )
    monitor_on = console_app.chat(
        {
            "message": "Are you continuously monitoring my sources?",
            "save_guidance": False,
        }
    )["answer"]["text"]
    assert "configured every 7 minutes" in monitor_on
    assert "only while the browser app remains open" in monitor_on
    assert "cannot see the browser timer’s last or next run" in monitor_on


def test_web_chat_deduplicates_guidance_and_ignores_one_token_matches(
    console_app: web_console.ConsoleApplication,
) -> None:
    repeated = "Keep track of events from Hanson Robotics."
    for index in range(2):
        console_app.learning.append_guidance(
            OperatorGuidance(
                tenant_id=web_console.TENANT_ID,
                guidance_id=f"duplicate-guidance-{index}",
                origin=GuidanceOrigin.CHAT,
                source_record_id=f"duplicate-source-{index}",
                created_at=datetime.now(UTC) + timedelta(seconds=index),
                guidance=repeated,
                case_ids=[],
            )
        )

    unrelated = console_app.chat(
        {
            "message": "How many events came from Eventbrite?",
            "save_guidance": False,
        }
    )["answer"]
    assert "Remembered company guidance" not in unrelated["text"]
    assert not any(item.startswith("guidance:") for item in unrelated["citations"])

    relevant = console_app.chat(
        {
            "message": "Show me events from Hanson Robotics.",
            "save_guidance": False,
        }
    )["answer"]
    assert relevant["text"].count("Remembered company guidance") == 1
    guidance_citations = [
        item for item in relevant["citations"] if item.startswith("guidance:")
    ]
    assert len(guidance_citations) == 1


def test_chat_source_control_and_report_paraphrases(
    console_app: web_console.ConsoleApplication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hidden = console_app.chat(
        {"message": "Please don't show any records from Posh.", "save_guidance": False}
    )
    assert hidden["state"]["company"]["hidden_sources"] == ["Posh"]
    shown = console_app.chat(
        {"message": "Include records from Posh again.", "save_guidance": False}
    )
    assert shown["state"]["company"]["hidden_sources"] == []

    generated = tmp_path / "prepared-report.pdf"

    def fake_export(state: dict[str, object], request: str) -> list[ExportArtifact]:
        assert state["service"] == "ContextGate"
        assert request == "Please prepare a report and graph."
        return [
            ExportArtifact(
                kind="pdf",
                path=str(generated),
                filename=generated.name,
                size_bytes=123,
            )
        ]

    monkeypatch.setattr(web_console, "create_exports", fake_export)
    report = console_app.chat(
        {"message": "Please prepare a report and graph.", "save_guidance": False}
    )
    assert "Saved 1 export file" in report["answer"]["text"]
    assert report["answer"]["citations"] == [f"local-export:{generated}"]


def test_company_settings_persist_and_do_not_claim_identity(
    console_app: web_console.ConsoleApplication,
) -> None:
    footer = "Questions: operations@example.test"
    updated = console_app.update_profile(
        {
            "company_name": "Example Operations",
            "operator_name": "Demo Operator",
            "important_detail": "Crowd size",
            "identity_fields": "Event name, Event date, Venue",
            "risk_posture": "safety_first",
            "source_mode": "file_upload",
            "voice_enabled": False,
            "mail_scan_limit": 15,
            "auto_monitor_enabled": True,
            "auto_monitor_minutes": 30,
            "document_company_header": False,
            "document_footer": footer,
            "company_website": "https://example.test",
        }
    )
    assert updated["company"]["company_name"] == "Example Operations"
    assert updated["company"]["operator_name"] == "Demo Operator"
    assert updated["company"]["identity_fields"] == [
        "Event name",
        "Event date",
        "Venue",
    ]
    assert updated["company"]["voice_enabled"] is False
    assert updated["company"]["auto_monitor_enabled"] is True
    assert updated["company"]["auto_monitor_minutes"] == 30
    assert updated["company"]["document_company_header"] is False
    assert updated["company"]["document_footer"] == footer
    assert updated["company"]["company_website"] == "https://example.test"
    assert web_console.PROFILE_PATH.is_file()

    reloaded = load_company_profile(web_console.PROFILE_PATH)
    assert reloaded.company_name == "Example Operations"
    assert reloaded.operator_name == "Demo Operator"
    assert reloaded.auto_monitor_enabled is True
    assert reloaded.auto_monitor_minutes == 30
    assert reloaded.document_company_header is False
    assert reloaded.document_footer == footer
    assert reloaded.company_website == "https://example.test"
    serialized = web_console.PROFILE_PATH.read_text(encoding="utf-8")
    assert "password" not in serialized.casefold()
    assert "access_token" not in serialized.casefold()


@pytest.mark.parametrize(
    ("content_type", "image_format"),
    (("image/png", "PNG"), ("image/jpeg", "JPEG")),
)
def test_company_logo_upload_normalizes_and_removes_png_and_jpeg(
    console_app: web_console.ConsoleApplication,
    content_type: str,
    image_format: str,
) -> None:
    source = Image.new("RGB", (960, 240), "#ff7a00")
    payload = io.BytesIO()
    source.save(payload, format=image_format)

    saved = console_app.save_company_logo(
        {
            "content_type": content_type,
            "data_base64": base64.b64encode(payload.getvalue()).decode("ascii"),
        }
    )

    assert saved["company"]["has_company_logo"] is True
    data_url = saved["company"]["company_logo_data_url"]
    assert data_url.startswith("data:image/png;base64,")
    assert (
        base64.b64decode(data_url.split(",", 1)[1])
        == web_console.LOGO_PATH.read_bytes()
    )
    with Image.open(web_console.LOGO_PATH) as normalized:
        assert normalized.format == "PNG"
        assert normalized.mode == "RGBA"
        assert normalized.size == (512, 128)

    removed = console_app.remove_company_logo()
    assert removed["company"]["has_company_logo"] is False
    assert removed["company"]["company_logo_data_url"] is None
    assert not web_console.LOGO_PATH.exists()


def test_company_logo_rejects_unsupported_corrupt_oversized_and_huge_images(
    console_app: web_console.ConsoleApplication,
) -> None:
    tiny = io.BytesIO()
    Image.new("RGB", (8, 8), "navy").save(tiny, format="PNG")
    tiny_encoded = base64.b64encode(tiny.getvalue()).decode("ascii")

    invalid_payloads = (
        {"content_type": "image/gif", "data_base64": tiny_encoded},
        {"content_type": "image/png", "data_base64": "not valid base64!"},
        {
            "content_type": "image/png",
            "data_base64": base64.b64encode(b"not an image").decode("ascii"),
        },
        {
            "content_type": "image/png",
            "data_base64": base64.b64encode(
                b"x" * (web_console.MAX_LOGO_BYTES + 1)
            ).decode("ascii"),
        },
    )
    for invalid in invalid_payloads:
        with pytest.raises(web_console.ConsoleError):
            console_app.save_company_logo(invalid)

    huge = io.BytesIO()
    Image.new("1", (4_001, 4_000)).save(huge, format="PNG")
    with pytest.raises(web_console.ConsoleError, match="dimensions are too large"):
        console_app.save_company_logo(
            {
                "content_type": "image/png",
                "data_base64": base64.b64encode(huge.getvalue()).decode("ascii"),
            }
        )
    assert not web_console.LOGO_PATH.exists()


def test_chat_hides_then_deletes_a_named_source_without_conflating_actions(
    console_app: web_console.ConsoleApplication,
) -> None:
    hidden = console_app.chat(
        {"message": "Do not show me data from Posh.", "save_guidance": False}
    )
    assert "underlying data was not deleted" in hidden["answer"]["text"]
    assert hidden["state"]["company"]["hidden_sources"] == ["Posh"]
    assert hidden["state"]["source_summary"]["location_counts"]["New York City"] == 5

    shown = console_app.chat(
        {"message": "Show me data from Posh again.", "save_guidance": False}
    )
    assert "visible again" in shown["answer"]["text"]
    assert shown["state"]["company"]["hidden_sources"] == []
    assert shown["state"]["source_summary"]["location_counts"]["New York City"] == 6

    console_app.chat(
        {"message": "Do not show me data from Posh.", "save_guidance": False}
    )
    deleted = console_app.chat({"message": "Delete it.", "save_guidance": False})
    assert "Deleted 1 stored record" in deleted["answer"]["text"]
    assert deleted["state"]["company"]["hidden_sources"] == []
    assert deleted["state"]["company"]["deleted_sources"] == ["Posh"]
    assert console_app.catalog.matching_count("Posh") == 0

    refused = console_app.chat(
        {"message": "Show me data from Posh again.", "save_guidance": False}
    )
    assert "it was deleted" in refused["answer"]["text"]


def test_human_correction_changes_resolved_view_then_retracts(
    console_app: web_console.ConsoleApplication,
) -> None:
    corrected = console_app.correct(
        {
            "case_id": "B1",
            "corrected_outcome": "ALLOW",
            "reviewer": "authorized-reviewer",
            "rationale": "Signed organizer evidence proves this was a false positive.",
        }
    )
    assert corrected["totals"]["allow"] == 4
    assert corrected["totals"]["block"] == 2
    b1 = next(case for case in corrected["cases"] if case["case_id"] == "B1")
    assert b1["original_outcome"] == "BLOCK"
    assert b1["outcome"] == "ALLOW"
    assert b1["corrected"] is True
    assert b1["decision"]["action_executed"] is False

    restored = console_app.retract(
        {"case_id": "B1", "reason": "Rehearsal complete; restore original."}
    )
    assert restored["totals"]["allow"] == 3
    assert restored["totals"]["block"] == 3


def test_chat_explains_and_explicitly_retracts_an_active_human_correction(
    console_app: web_console.ConsoleApplication,
) -> None:
    corrected = console_app.correct(
        {
            "case_id": "B1",
            "corrected_outcome": "ALLOW",
            "reviewer": "authorized-reviewer",
            "rationale": "Signed organizer evidence proves this was a false positive.",
        }
    )
    assert corrected["totals"]["allow"] == 4

    explained = console_app.chat(
        {
            "message": "Why was B1 red if I corrected it?",
            "save_guidance": False,
        }
    )
    assert "original deterministic BLOCK receipt" in explained["answer"]["text"]
    assert "effective outcome to ALLOW" in explained["answer"]["text"]
    assert explained["state"]["totals"]["allow"] == 4

    retracted = console_app.chat(
        {"message": "Undo the correction for B1.", "save_guidance": False}
    )
    assert "Retracted the active human correction for B1" in retracted["answer"]["text"]
    assert "no external action was executed" in retracted["answer"]["text"]
    assert retracted["state"]["totals"]["allow"] == 3
    assert retracted["state"]["totals"]["block"] == 3
    assert retracted["state"]["chat_history"][-2]["saved"] is True


def test_website_source_add_scan_and_remove_feed_grounded_event_catalog(
    console_app: web_console.ConsoleApplication,
) -> None:
    source = WebsiteSource(
        source_id=f"website-{'a' * 32}",
        label="Example Robotics",
        url="https://events.example.test/",
        extraction_goal="Find event names, dates, times, and addresses",
        created_at="2026-09-03T04:00:00+00:00",
    )
    event = WebsiteEvent(
        name="Robotics Open House",
        start_date="2026-09-18T10:00:00-04:00",
        location="New York City",
        address="100 Example Avenue, New York, NY 10000",
        url="https://events.example.test/open-house",
        organizer="Example Robotics",
    )
    record = WebsiteScanRecord(
        record_id=f"web-{'b' * 32}",
        source_id=source.source_id,
        source_url=source.url,
        final_url=source.url,
        extraction_goal=source.extraction_goal,
        kind="event",
        title=event.name,
        fields={
            "name": event.name,
            "startDate": event.start_date,
            "location": event.location,
            "address": event.address,
            "url": event.url,
            "organizer": event.organizer,
        },
        evidence_reference=event.url,
    )
    scan_result = WebsiteScanResult(
        source=source,
        final_url=source.url,
        content_type="text/html",
        bytes_read=500,
        scanned_at="2026-09-03T04:01:00+00:00",
        events=[event],
        records=[record],
    )

    class StubWebsiteRegistry:
        def __init__(self) -> None:
            self.sources: list[WebsiteSource] = []

        def list_sources(self) -> list[WebsiteSource]:
            return list(self.sources)

        def add_source(
            self, url: str, extraction_goal: str, *, label: str = ""
        ) -> WebsiteSource:
            assert url == source.url
            assert extraction_goal == source.extraction_goal
            assert label == source.label
            self.sources = [source]
            return source

        def scan_source(self, source_id: str) -> WebsiteScanResult:
            assert source_id == source.source_id
            return scan_result

        def remove_source(self, source_id: str) -> bool:
            if source_id != source.source_id or not self.sources:
                return False
            self.sources = []
            return True

    console_app.websites = StubWebsiteRegistry()  # type: ignore[assignment]
    added = console_app.add_website_source(
        {
            "url": source.url,
            "extraction_goal": source.extraction_goal,
            "label": source.label,
        }
    )
    assert added["website_sources"][0]["status"] == "Ready to scan"

    scanned = console_app.scan_website_source({"source_id": source.source_id})
    assert scanned["imported"] == 1
    assert scanned["events_found"] == 1
    assert scanned["state"]["source_summary"]["fictional"] is False
    assert scanned["state"]["source_summary"]["location_counts"] == {"New York City": 1}
    assert scanned["state"]["calendar"] == {
        "events": [
            {
                "record_id": f"website-{record.record_id}",
                "event_key": "robotics open house 2026 09 18",
                "title": "Robotics Open House",
                "organization": "Example Robotics",
                "source_name": "Example Robotics",
                "location": "New York City",
                "date": "2026-09-18",
                "date_iso": "2026-09-18",
                "time": "10:00:00-04:00",
                "address": "100 Example Avenue, New York, NY 10000",
                "evidence_reference": ("https://events.example.test/open-house"),
                "fictional": False,
                "data_origin": "scanned_or_uploaded",
            }
        ],
        "scheduled_count": 1,
        "unscheduled_count": 0,
        "data_mode": "scanned_or_uploaded",
    }
    tracked = console_app.chat(
        {
            "message": "Show events from Example Robotics.",
            "save_guidance": False,
        }
    )
    assert "Robotics Open House" in tracked["answer"]["text"]
    assert "100 Example Avenue" in tracked["answer"]["text"]

    removed = console_app.remove_website_source({"source_id": source.source_id})
    assert removed["website_sources"] == []
    assert removed["source_summary"]["distinct_events"] == 1


def test_local_http_health_state_chat_and_origin_guard(
    console_app: web_console.ConsoleApplication,
) -> None:
    server = web_console.create_server(port=0, application=console_app)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
            health = json.load(response)
        assert health == {
            "service": "ContextGate",
            "status": "ok",
            "ui": "one-screen-command-center",
        }

        request = urllib.request.Request(
            f"{base}/api/chat",
            data=json.dumps(
                {"message": "Why are these items blocked?", "save_guidance": False}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": base,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            answer = json.load(response)
        assert answer["answer"]["text"]
        assert answer["state"]["totals"]["total"] == 9

        console_app.configure_microsoft(
            {"client_id": "12345678-1234-4234-9234-123456789012"}
        )
        microsoft_start = urllib.request.Request(
            f"{base}/api/connectors/microsoft/start",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(microsoft_start, timeout=5) as response:
            authorization = json.load(response)["authorization_url"]
        parameters = urllib.parse.parse_qs(urllib.parse.urlparse(authorization).query)
        assert parameters["redirect_uri"] == [
            f"http://localhost:{port}/oauth/microsoft/callback"
        ]

        rejected = urllib.request.Request(
            f"{base}/api/select",
            data=b'{"case_id":"B1"}',
            headers={
                "Content-Type": "application/json",
                "Origin": "https://example.invalid",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(rejected, timeout=5)
        assert error.value.code == 400
    finally:
        server.shutdown()
        # server_close owns the application; avoid the fixture closing it twice.
        server.server_close()
