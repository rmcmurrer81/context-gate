from __future__ import annotations

from datetime import date

from context_gate.email_connectors import MailSummary
from context_gate.source_catalog import SourceCatalog, SourceRecord


def test_fictional_catalog_counts_distinct_events_not_update_messages() -> None:
    catalog = SourceCatalog()

    summary = catalog.summary()
    eventbrite = catalog.answer_count_question("How many Eventbrite events are there?")
    nyc = catalog.answer_count_question("What is the total number of NYC events?")

    assert summary == {
        "messages_scanned": 10,
        "distinct_events": 9,
        "duplicate_updates": 1,
        "source_counts": {
            "Eventbrite": 5,
            "Meetup": 1,
            "Organizer email": 3,
        },
        "location_counts": {
            "Boston": 1,
            "New York City": 6,
            "Online": 1,
            "Philadelphia": 1,
        },
        "fictional": True,
    }
    assert eventbrite is not None
    assert eventbrite["count"] == 5
    assert eventbrite["matching_messages"] == 6
    assert len(eventbrite["evidence"]) == 5
    assert "excluded 1 duplicate/update message" in str(eventbrite["text"])
    assert nyc is not None
    assert nyc["count"] == 6
    assert nyc["matching_messages"] == 7
    assert len(nyc["evidence"]) == 6


def test_eventbright_typo_maps_to_eventbrite_in_questions_and_mail() -> None:
    fictional = SourceCatalog()
    correct = fictional.answer_count_question("How many Eventbrite events?")
    typo = fictional.answer_count_question("How many Eventbright events?")

    assert typo == correct
    assert typo is not None
    assert typo["count"] == 5

    scanned = SourceCatalog(records=[])
    added = scanned.add_mail(
        [
            MailSummary(
                provider="google",
                message_id="fictional-message-1",
                subject="Registration for: Future Forum",
                sender="tickets@eventbright.example",
                received_at="Thu, 03 Sep 2026 09:00:00 -0400",
                preview="Your New York, NY registration is confirmed.",
            ),
            MailSummary(
                provider="google",
                message_id="fictional-message-2",
                subject="Updated: Future Forum",
                sender="Eventbright Updates <updates@eventbright.example>",
                received_at="Thu, 03 Sep 2026 10:00:00 -0400",
                preview="Updated details for the NYC event.",
            ),
        ]
    )

    assert added == 2
    assert scanned.summary()["source_counts"] == {"Eventbrite": 1}
    assert scanned.summary()["location_counts"] == {"New York City": 1}
    assert scanned.summary()["duplicate_updates"] == 1


def test_inventory_questions_return_bounded_grounded_catalog_summaries() -> None:
    catalog = SourceCatalog()

    for question in (
        "What data are you collecting?",
        "What are you tracking?",
        "What sources/data do you have?",
        "Tell me what source data you have.",
    ):
        answer = catalog.answer_inventory_question(question)

        assert answer is not None
        assert answer["query_kind"] == "catalog"
        assert "10 visible source records" in str(answer["text"])
        assert "9 distinct events" in str(answer["text"])
        assert "By source: Eventbrite 5" in str(answer["text"])
        assert "By location:" in str(answer["text"])
        assert "AI Builders NYC" in str(answer["text"])
        assert len(answer["evidence"]) == 6
        assert all(item["reference"] for item in answer["evidence"])


def test_eventbrite_inventory_question_accepts_typo_and_lists_actual_items() -> None:
    catalog = SourceCatalog()

    for question in (
        "What did you get from Eventbrite?",
        "What did you get from Eventbright?",
        "What data have you collected from Eventbright?",
        "Tell me what you have from Eventbrite.",
    ):
        answer = catalog.answer_inventory_question(question)

        assert answer is not None
        assert answer["query_kind"] == "eventbrite"
        assert "5 distinct visible events" in str(answer["text"])
        assert "6 source records" in str(answer["text"])
        assert "AI Builders NYC" in str(answer["text"])
        assert "Philadelphia Product Lab" in str(answer["text"])
        assert "duplicate/update evidence" in str(answer["text"])
        assert len(answer["evidence"]) == 5


def test_inventory_intent_does_not_hijack_specific_questions_or_commands() -> None:
    catalog = SourceCatalog()

    for question in (
        "What sources support case B1?",
        "Why did Eventbrite conflict?",
        "Delete Eventbrite data.",
        "What are you tracking for Hanson Robotics?",
    ):
        assert catalog.answer_inventory_question(question) is None


def test_count_question_handles_two_targets_without_silently_dropping_one() -> None:
    catalog = SourceCatalog()

    separate = catalog.answer_count_question(
        "How many events are in New York and how many came from Eventbrite?"
    )
    intersection = catalog.answer_count_question(
        "How many Eventbrite events are in NYC?"
    )

    assert separate is not None
    assert separate["counts"] == {"New York City": 6, "Eventbrite": 5}
    assert "separate counts and may overlap" in str(separate["text"])
    assert len(separate["evidence"]) == 8

    assert intersection is not None
    assert intersection["count"] == 3
    assert intersection["matching_messages"] == 4
    assert "3 distinct Eventbrite events in New York City" in str(intersection["text"])
    assert "excluded 1 duplicate/update message" in str(intersection["text"])


def test_first_real_scan_replaces_fictional_demo_records() -> None:
    catalog = SourceCatalog()

    catalog.add_mail(
        [
            MailSummary(
                provider="microsoft",
                message_id="real-message-1",
                subject="One company event",
                sender="events@example.com",
                received_at="2026-09-03T08:00:00-04:00",
                preview="A real message for the Boston office.",
            )
        ]
    )

    assert catalog.summary() == {
        "messages_scanned": 1,
        "distinct_events": 1,
        "duplicate_updates": 0,
        "source_counts": {"events@example.com": 1},
        "location_counts": {"Boston": 1},
        "fictional": False,
    }


def test_count_question_requires_a_supported_target_and_count_intent() -> None:
    catalog = SourceCatalog()

    assert catalog.answer_count_question("Tell me about Eventbrite") is None
    assert catalog.answer_count_question("How many unsupported providers?") is None


def test_tracking_instruction_lists_grounded_organizer_event_details() -> None:
    catalog = SourceCatalog()

    answer = catalog.answer_tracking_question(
        "Keep track of events from Hanson Robotics."
    )

    assert answer is not None
    assert answer["target"] == "Hanson Robotics"
    assert answer["remember"] is True
    assert "2 distinct matching events" in str(answer["text"])
    assert "429 11th Avenue" in str(answer["text"])
    assert "September 18, 2026" in str(answer["text"])
    assert "10:00 AM ET" in str(answer["text"])
    assert len(answer["evidence"]) == 2


def test_hidden_source_is_reversible_but_deleted_source_stays_excluded() -> None:
    catalog = SourceCatalog()
    assert catalog.matching_count("Posh") == 1

    catalog.configure_visibility(hidden_sources=["Posh"], deleted_sources=[])
    assert catalog.matching_count("Posh") == 1
    assert all(item.organization != "Posh" for item in catalog.records())
    assert catalog.answer_count_question("How many NYC events?")["count"] == 5

    catalog.configure_visibility(hidden_sources=[], deleted_sources=[])
    assert any(item.organization == "Posh" for item in catalog.records())
    assert catalog.answer_count_question("How many NYC events?")["count"] == 6

    assert catalog.delete_source("Posh") == 1
    catalog.reset_fictional_demo()
    catalog.add_record(
        SourceRecord(
            record_id="later-posh",
            event_key="later-posh-event",
            title="A later Posh event",
            source_name="Email",
            organization="Posh",
            evidence_reference="email://later-posh",
        )
    )
    assert catalog.matching_count("Posh") == 0
    assert all(item.organization != "Posh" for item in catalog.records())


def test_calendar_events_are_grounded_deduplicated_and_visibility_aware() -> None:
    catalog = SourceCatalog(
        records=[
            SourceRecord(
                record_id="email-1",
                event_key="robotics-launch",
                title="Robotics Launch",
                source_name="Mailbox",
                organization="Example Robotics",
                evidence_reference="mailbox://email-1",
            ),
            SourceRecord(
                record_id="email-2",
                event_key="robotics-launch",
                title="Updated: Robotics Launch",
                source_name="Mailbox",
                organization="Example Robotics",
                event_date="September 18, 2026",
                event_time="10:00 AM ET",
                address="100 Example Avenue",
                evidence_reference="mailbox://email-2",
            ),
            SourceRecord(
                record_id="web-1",
                event_key="ai-forum",
                title="AI Forum",
                source_name="Public website",
                organization="Example AI",
                event_date="2026-09-22T13:00:00-04:00",
                event_time="1:00 PM ET",
                address="Online",
                evidence_reference="https://events.example.test/ai-forum",
            ),
        ]
    )

    events = catalog.calendar_events()

    assert len(events) == 2
    assert events[0] == {
        "record_id": "email-2",
        "event_key": "robotics-launch",
        "title": "Updated: Robotics Launch",
        "organization": "Example Robotics",
        "source_name": "Mailbox",
        "location": "Unknown",
        "date": "September 18, 2026",
        "date_iso": "2026-09-18",
        "time": "10:00 AM ET",
        "address": "100 Example Avenue",
        "evidence_reference": "mailbox://email-2",
        "fictional": False,
        "data_origin": "scanned_or_uploaded",
    }
    assert events[1]["date_iso"] == "2026-09-22"
    assert events[1]["evidence_reference"] == ("https://events.example.test/ai-forum")

    catalog.configure_visibility(
        hidden_sources=["Example Robotics"], deleted_sources=[]
    )
    assert [item["event_key"] for item in catalog.calendar_events()] == ["ai-forum"]
    assert catalog.delete_source("Example AI") == 1
    assert catalog.calendar_events() == []


def test_calendar_keeps_undated_events_unscheduled_instead_of_guessing() -> None:
    catalog = SourceCatalog(
        records=[
            SourceRecord(
                record_id="email-1",
                event_key="launch-date-pending",
                title="Launch date pending",
                source_name="Mailbox",
                evidence_reference="mailbox://email-1",
            )
        ]
    )

    assert catalog.calendar_events()[0]["date_iso"] is None
    assert catalog.calendar_events()[0]["date"] is None


def test_calendar_questions_list_grounded_dates_and_preserve_undated_items() -> None:
    catalog = SourceCatalog()

    overview = catalog.answer_calendar_question(
        "What's on my calendar?", today=date(2026, 9, 3)
    )
    upcoming = catalog.answer_calendar_question(
        "Show upcoming events", today=date(2026, 9, 3)
    )
    undated = catalog.answer_calendar_question(
        "Which events are missing dates?", today=date(2026, 9, 3)
    )

    assert overview is not None
    assert overview["query_kind"] == "overview"
    assert overview["fictional"] is True
    assert "3 scheduled distinct events" in str(overview["text"])
    assert "6 events needing a source date" in str(overview["text"])
    assert "Manhattan ML Meetup" in str(overview["text"])
    assert len(overview["evidence"]) == 3
    assert {
        item["reference"] for item in overview["evidence"] if isinstance(item, dict)
    } == {
        "fictional-email://m08",
        "fictional-email://m09",
        "fictional-email://m10",
    }

    assert upcoming is not None
    assert upcoming["query_kind"] == "upcoming"
    assert "3 scheduled upcoming events" in str(upcoming["text"])
    assert "on or after 2026-09-03" in str(upcoming["text"])
    assert "did not treat 6 undated events as upcoming" in str(upcoming["text"])
    assert len(upcoming["evidence"]) == 3

    assert undated is not None
    assert undated["query_kind"] == "undated"
    assert "6 visible events without a usable source date" in str(undated["text"])
    assert "date not found in source" in str(undated["text"])
    assert "instead of guessing" in str(undated["text"])
    assert "2 additional undated events are available" in str(undated["text"])
    assert len(undated["evidence"]) == 4
    assert all(
        item["date"] is None for item in undated["evidence"] if isinstance(item, dict)
    )


def test_calendar_questions_are_bounded_and_honor_hidden_sources() -> None:
    catalog = SourceCatalog()
    catalog.configure_visibility(hidden_sources=["Posh"], deleted_sources=[])

    upcoming = catalog.answer_calendar_question(
        "What events are coming up?", today=date(2026, 9, 3)
    )

    assert upcoming is not None
    assert "2 scheduled upcoming events" in str(upcoming["text"])
    assert "Manhattan ML Meetup" not in str(upcoming["text"])
    assert len(upcoming["evidence"]) == 2
    assert catalog.answer_calendar_question("Tell me about event security") is None
