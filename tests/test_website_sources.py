from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest

from context_gate.website_sources import (
    MAX_RESPONSE_BYTES,
    WebsiteSourceError,
    WebsiteSourceRegistry,
    _SafeRedirectHandler,
)

PUBLIC_IP = "93.184.216.34"


def public_resolver(host: str, port: int, **_: object) -> list[tuple[Any, ...]]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (PUBLIC_IP, port),
        )
    ]


def private_resolver(host: str, port: int, **_: object) -> list[tuple[Any, ...]]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("10.0.0.8", port),
        )
    ]


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://events.example.org/",
        content_type: str = "text/html; charset=utf-8",
        content_length: int | None = None,
    ) -> None:
        self.body = body
        self.url = url
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_called = False

    def read(self, size: int = -1) -> bytes:
        self.read_called = True
        return self.body if size < 0 else self.body[:size]

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeOpener:
    def __init__(
        self,
        response: FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.request: urllib.request.Request | None = None
        self.timeout: float | None = None

    def open(self, request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def opener_factory(
    response: FakeResponse | None = None,
    *,
    error: Exception | None = None,
    captured: list[object] | None = None,
):
    opener = FakeOpener(response, error)

    def build(handler: urllib.request.HTTPRedirectHandler) -> FakeOpener:
        if captured is not None:
            captured.extend([handler, opener])
        return opener

    return build


def registry_with_source(
    path: Path,
    response: FakeResponse,
) -> tuple[WebsiteSourceRegistry, str]:
    registry = WebsiteSourceRegistry(
        path,
        resolver=public_resolver,
        opener_factory=opener_factory(response),
    )
    source = registry.add_source(
        "https://events.example.org/calendar",
        "Find event names, dates, times, organizers, and addresses",
        label="Company events",
    )
    return registry, source.source_id


def test_registry_persists_lists_deduplicates_and_removes_sources(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "website_sources.json"
    registry = WebsiteSourceRegistry(path, resolver=public_resolver)

    source = registry.add_source(
        "http://events.example.org",
        "Find upcoming event details",
        label="Events",
    )
    duplicate = registry.add_source(
        "http://events.example.org/",
        "find upcoming event details",
        label="Ignored duplicate label",
    )

    assert duplicate.source_id == source.source_id
    assert source.url == "http://events.example.org/"
    assert registry.list_sources() == [source]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["sources"][0]["extraction_goal"] == ("Find upcoming event details")

    reopened = WebsiteSourceRegistry(path, resolver=private_resolver)
    assert reopened.list_sources() == [source]
    assert reopened.remove_source(source.source_id) is True
    assert reopened.remove_source(source.source_id) is False
    assert reopened.list_sources() == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://events.example.org/data",
        "https://user:secret@events.example.org/",
        "http://localhost/events",
        "http://service.internal/events",
        "http://127.0.0.1/events",
        "http://10.0.0.7/events",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/events",
        "http://[fe80::1]/events",
    ],
)
def test_add_rejects_unsafe_url_forms(tmp_path: Path, url: str) -> None:
    registry = WebsiteSourceRegistry(
        tmp_path / "sources.json", resolver=public_resolver
    )

    with pytest.raises(WebsiteSourceError):
        registry.add_source(url, "Find events")


def test_add_rejects_dns_private_and_mixed_answers(tmp_path: Path) -> None:
    def mixed_resolver(host: str, port: int, **_: object) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, port),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.168.1.10", port),
            ),
        ]

    for resolver in (private_resolver, mixed_resolver):
        registry = WebsiteSourceRegistry(
            tmp_path / f"sources-{resolver.__name__}.json", resolver=resolver
        )
        with pytest.raises(WebsiteSourceError, match="non-public"):
            registry.add_source("https://events.example.org", "Find events")


def test_redirect_handler_blocks_redirect_to_private_destination() -> None:
    handler = _SafeRedirectHandler(public_resolver)
    request = urllib.request.Request("https://events.example.org/")

    with pytest.raises(WebsiteSourceError, match="non-public"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://127.0.0.1/admin",
        )


def test_scan_revalidates_final_url_to_defend_redirect_bypass(tmp_path: Path) -> None:
    response = FakeResponse(
        b"<html><title>Moved</title></html>",
        url="http://169.254.169.254/latest/meta-data",
    )
    registry, source_id = registry_with_source(tmp_path / "sources.json", response)

    with pytest.raises(WebsiteSourceError, match="non-public"):
        registry.scan_source(source_id)


def test_scan_extracts_schema_event_and_generic_evidence_fields(
    tmp_path: Path,
) -> None:
    body = b"""<!doctype html>
    <html><head>
      <title>Example Company Events</title>
      <meta name="description" content="The public events calendar.">
      <script>window.shouldNeverRun = true;</script>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@graph": [{
          "@type": ["Thing", "BusinessEvent"],
          "name": "Robotics Open House",
          "startDate": "2026-09-18T10:00:00-04:00",
          "endDate": "2026-09-18T12:00:00-04:00",
          "location": {
            "@type": "Place",
            "name": "Innovation Hall",
            "address": {
              "streetAddress": "100 Example Avenue",
              "addressLocality": "Sample City",
              "addressRegion": "NY",
              "postalCode": "10000"
            }
          },
          "url": "/events/robotics-open-house",
          "organizer": {"@type": "Organization", "name": "Example Company"}
        }]
      }
      </script>
    </head><body><h1>Upcoming programs</h1></body></html>"""
    captured: list[object] = []
    response = FakeResponse(body)
    registry = WebsiteSourceRegistry(
        tmp_path / "sources.json",
        resolver=public_resolver,
        opener_factory=opener_factory(response, captured=captured),
    )
    source = registry.add_source(
        "https://events.example.org/",
        "Extract public event dates and addresses",
    )

    result = registry.scan_source(source.source_id)

    assert result.content_type == "text/html"
    assert result.bytes_read == len(body)
    assert len(result.events) == 1
    event = result.events[0]
    assert event.name == "Robotics Open House"
    assert event.start_date == "2026-09-18T10:00:00-04:00"
    assert event.end_date == "2026-09-18T12:00:00-04:00"
    assert event.location == "Innovation Hall"
    assert event.address == "100 Example Avenue, Sample City, NY, 10000"
    assert event.url == "https://events.example.org/events/robotics-open-house"
    assert event.organizer == "Example Company"
    assert result.records[0].kind == "event"
    assert result.records[0].extraction_goal == (
        "Extract public event dates and addresses"
    )
    assert result.records[0].fields["organizer"] == "Example Company"
    assert "window.shouldNeverRun" not in result.records[0].summary
    assert isinstance(captured[0], _SafeRedirectHandler)
    opener = captured[1]
    assert isinstance(opener, FakeOpener)
    assert opener.request is not None
    assert opener.request.get_header("Accept-encoding") == "identity"
    assert opener.timeout == 8.0


def test_scan_returns_goal_grounded_page_fallback_without_running_scripts(
    tmp_path: Path,
) -> None:
    body = b"""
    <html><head><title>Community Calendar</title>
    <meta property="og:description" content="Events and workshops"></head>
    <body><script>alert('do not include me')</script>
    <h1>September gatherings</h1><p>Doors open at 6 PM.</p></body></html>
    """
    registry, source_id = registry_with_source(
        tmp_path / "sources.json", FakeResponse(body)
    )

    result = registry.scan_source(source_id)

    assert result.events == []
    assert len(result.records) == 1
    record = result.records[0]
    assert record.kind == "page"
    assert record.title == "Community Calendar"
    assert record.summary == "Events and workshops"
    assert record.fields["requested_goal"].startswith("Find event names")
    assert "September gatherings" in record.fields["text_snippet"]
    assert "alert" not in record.fields["text_snippet"]


def test_scan_parses_direct_json_ld_and_icalendar(tmp_path: Path) -> None:
    json_response = FakeResponse(
        json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Event",
                "name": "AI Day",
                "startDate": "2026-09-03",
                "location": "New York City",
            }
        ).encode(),
        content_type="application/ld+json",
    )
    registry, source_id = registry_with_source(
        tmp_path / "json-sources.json", json_response
    )
    assert registry.scan_source(source_id).events[0].name == "AI Day"

    ical_response = FakeResponse(
        b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Developer Meetup\r\n"
        b"DTSTART:20260912T183000\r\nLOCATION:35 Main St\r\n"
        b"URL:https://events.example.org/meetup\r\nEND:VEVENT\r\nEND:VCALENDAR",
        content_type="text/calendar",
    )
    registry, source_id = registry_with_source(
        tmp_path / "ical-sources.json", ical_response
    )
    event = registry.scan_source(source_id).events[0]
    assert event.name == "Developer Meetup"
    assert event.start_date == "20260912T183000"
    assert event.location == "35 Main St"


@pytest.mark.parametrize("content_type", ["image/png", "text/plain", "application/pdf"])
def test_scan_rejects_unapproved_content_types(
    tmp_path: Path, content_type: str
) -> None:
    response = FakeResponse(b"not allowed", content_type=content_type)
    registry, source_id = registry_with_source(
        tmp_path / f"sources-{content_type.replace('/', '-')}.json", response
    )

    with pytest.raises(WebsiteSourceError, match="unsupported content type"):
        registry.scan_source(source_id)


def test_scan_enforces_announced_and_streamed_response_size(tmp_path: Path) -> None:
    announced = FakeResponse(
        b"not read",
        content_length=MAX_RESPONSE_BYTES + 1,
    )
    registry, source_id = registry_with_source(tmp_path / "announced.json", announced)
    with pytest.raises(WebsiteSourceError, match="2 MB"):
        registry.scan_source(source_id)
    assert announced.read_called is False

    streamed = FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1))
    registry, source_id = registry_with_source(tmp_path / "streamed.json", streamed)
    with pytest.raises(WebsiteSourceError, match="2 MB"):
        registry.scan_source(source_id)
    assert streamed.read_called is True


def test_scan_maps_timeout_to_safe_error(tmp_path: Path) -> None:
    registry = WebsiteSourceRegistry(
        tmp_path / "sources.json",
        resolver=public_resolver,
        opener_factory=opener_factory(error=TimeoutError()),
    )
    source = registry.add_source("https://events.example.org", "Find events")

    with pytest.raises(WebsiteSourceError, match="timed out safely"):
        registry.scan_source(source.source_id)


def test_registry_refuses_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"version": 1, "sources": []}', encoding="utf-8")
    link = tmp_path / "sources.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")

    with pytest.raises(WebsiteSourceError, match="symbolic link"):
        WebsiteSourceRegistry(link, resolver=public_resolver)


def test_goal_and_source_limits_are_validated(tmp_path: Path) -> None:
    registry = WebsiteSourceRegistry(
        tmp_path / "sources.json", resolver=public_resolver
    )

    with pytest.raises(WebsiteSourceError, match="3 to 240"):
        registry.add_source("https://events.example.org", "x")
    with pytest.raises(WebsiteSourceError, match="3 to 240"):
        registry.add_source("https://events.example.org", "x" * 241)
    with pytest.raises(WebsiteSourceError, match="1 to 80"):
        registry.add_source("https://events.example.org", "Find events", label="x" * 81)
