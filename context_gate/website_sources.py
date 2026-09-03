"""Safe, bounded website-source definitions and on-demand extraction.

The registry stores only public website URLs and a human-written extraction
goal.  Scans are deliberately simple HTTP GET requests: no JavaScript runs,
no cookies or credentials are accepted, and every initial or redirected host
must resolve exclusively to globally routable IP addresses.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_REGISTRY_BYTES = 256 * 1024
MAX_SOURCES = 32
MAX_URL_CHARS = 2_048
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 50
MAX_JSON_NODES = 10_000
DEFAULT_TIMEOUT_SECONDS = 8.0

_ALLOWED_CONTENT_TYPES = {
    "application/atom+xml",
    "application/calendar",
    "application/ics",
    "application/json",
    "application/ld+json",
    "application/rss+xml",
    "application/xhtml+xml",
    "application/xml",
    "text/calendar",
    "text/html",
    "text/xml",
}


class WebsiteSourceError(ValueError):
    """Raised with a display-safe message when a website source cannot be used."""


class WebsiteSource(BaseModel):
    """One persisted, user-approved public website definition."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    source_id: str = Field(min_length=32, max_length=64, pattern=r"^[a-z0-9-]+$")
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=MAX_URL_CHARS)
    extraction_goal: str = Field(min_length=3, max_length=240)
    created_at: str = Field(min_length=20, max_length=40)

    @field_validator("label", "extraction_goal")
    @classmethod
    def text_is_safe(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if any(unicodedata.category(char).startswith("C") for char in cleaned):
            raise ValueError("text must not contain control characters")
        return cleaned

    @field_validator("url")
    @classmethod
    def url_is_structurally_safe(cls, value: str) -> str:
        return _normalize_http_url(value, resolve=False)


class WebsiteEvent(BaseModel):
    """A bounded Schema.org or iCalendar event found in a website response."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=300)
    start_date: str | None = Field(default=None, max_length=100)
    end_date: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    organizer: str | None = Field(default=None, max_length=300)


class WebsiteScanRecord(BaseModel):
    """Generic evidence record suitable for later catalog integration."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    record_id: str = Field(min_length=16, max_length=80)
    source_id: str = Field(min_length=32, max_length=64)
    source_url: str = Field(min_length=8, max_length=MAX_URL_CHARS)
    final_url: str = Field(min_length=8, max_length=MAX_URL_CHARS)
    extraction_goal: str = Field(min_length=3, max_length=240)
    kind: Literal["event", "page"]
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=2_000)
    fields: dict[str, str] = Field(default_factory=dict)
    evidence_reference: str = Field(min_length=8, max_length=MAX_URL_CHARS)

    @field_validator("fields")
    @classmethod
    def fields_are_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 16:
            raise ValueError("website record contains too many fields")
        bounded: dict[str, str] = {}
        for key, item in value.items():
            safe_key = _bounded_text(key, 80)
            safe_value = _bounded_text(item, 1_000)
            if safe_key and safe_value:
                bounded[safe_key] = safe_value
        return bounded


class WebsiteScanResult(BaseModel):
    """Result of one bounded, on-demand scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: WebsiteSource
    final_url: str = Field(min_length=8, max_length=MAX_URL_CHARS)
    content_type: str = Field(min_length=3, max_length=100)
    bytes_read: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    scanned_at: str = Field(min_length=20, max_length=40)
    events: list[WebsiteEvent] = Field(default_factory=list, max_length=MAX_RECORDS)
    records: list[WebsiteScanRecord] = Field(min_length=1, max_length=MAX_RECORDS)


Resolver = Callable[..., list[tuple[Any, ...]]]
OpenerFactory = Callable[[urllib.request.HTTPRedirectHandler], Any]


def _bounded_text(value: object, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    cleaned = " ".join(str(value).split())
    cleaned = "".join(
        char
        for char in cleaned
        if not unicodedata.category(char).startswith("C") or char in "\t\n\r"
    )
    return cleaned[:limit]


def _port(parsed: SplitResult) -> int:
    try:
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise WebsiteSourceError("Website URL has an invalid port.") from None


def _address_is_public(address: str) -> bool:
    try:
        candidate = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(candidate, ipaddress.IPv6Address) and candidate.ipv4_mapped:
        candidate = candidate.ipv4_mapped
    return bool(
        candidate.is_global
        and not candidate.is_private
        and not candidate.is_loopback
        and not candidate.is_link_local
        and not candidate.is_multicast
        and not candidate.is_reserved
        and not candidate.is_unspecified
    )


def _normalize_http_url(
    value: str,
    *,
    resolve: bool,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    if not isinstance(value, str):
        raise WebsiteSourceError("Website URL must be text.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_URL_CHARS:
        raise WebsiteSourceError("Website URL is missing or too long.")
    if "\\" in cleaned or any(
        unicodedata.category(char).startswith("C") for char in cleaned
    ):
        raise WebsiteSourceError("Website URL contains unsafe characters.")
    parsed = urlsplit(cleaned)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise WebsiteSourceError("Website URL must use http or https.")
    if not parsed.netloc or not parsed.hostname:
        raise WebsiteSourceError("Website URL must include a public host.")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise WebsiteSourceError("Credentials are not allowed in website URLs.")

    hostname = parsed.hostname.rstrip(".").casefold()
    if not hostname or "%" in hostname:
        raise WebsiteSourceError("Website URL has an invalid host.")
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise WebsiteSourceError("Website URL has an invalid host.") from None
    if ascii_host == "localhost" or ascii_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise WebsiteSourceError("Website URL must use a public host.")

    port = _port(parsed)
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(ascii_host)
    except ValueError:
        literal = None
    if literal is not None and not _address_is_public(str(literal)):
        raise WebsiteSourceError("Website URL resolves to a non-public address.")

    if resolve:
        try:
            answers = resolver(ascii_host, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror):
            raise WebsiteSourceError(
                "Website host could not be resolved safely."
            ) from None
        addresses = {
            str(answer[4][0]) for answer in answers if len(answer) >= 5 and answer[4]
        }
        if not addresses or any(not _address_is_public(item) for item in addresses):
            raise WebsiteSourceError("Website host resolves to a non-public address.")

    display_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    netloc = display_host if port == default_port else f"{display_host}:{port}"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect destination before urllib follows it."""

    def __init__(self, resolver: Resolver) -> None:
        super().__init__()
        self._resolver = resolver

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        absolute = urljoin(req.full_url, newurl)
        safe_url = _normalize_http_url(
            absolute,
            resolve=True,
            resolver=self._resolver,
        )
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def _default_opener_factory(
    redirect_handler: urllib.request.HTTPRedirectHandler,
) -> urllib.request.OpenerDirector:
    # Avoid ambient HTTP proxy settings: the validated destination should be
    # the destination the process connects to.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        redirect_handler,
    )


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.visible_parts: list[str] = []
        self.json_ld_parts: list[list[str]] = []
        self._in_title = False
        self._ignored_depth = 0
        self._json_ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if lowered == "title":
            self._in_title = True
        if lowered == "meta" and not self.description:
            marker = (
                attributes.get("name") or attributes.get("property") or ""
            ).casefold()
            if marker in {"description", "og:description", "twitter:description"}:
                self.description = _bounded_text(attributes.get("content"), 1_000)
        if lowered in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1
        if (
            lowered == "script"
            and attributes.get("type", "").casefold().split(";", 1)[0].strip()
            == "application/ld+json"
        ):
            self._json_ld = []
            self.json_ld_parts.append(self._json_ld)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = False
        if lowered in {"script", "style", "noscript", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        if lowered == "script":
            self._json_ld = None

    def handle_data(self, data: str) -> None:
        if self._json_ld is not None:
            self._json_ld.append(data)
        if self._in_title:
            self.title_parts.append(data)
        if self._ignored_depth == 0 and data.strip():
            self.visible_parts.append(data)


def _iter_json_objects(value: object) -> Iterable[dict[str, Any]]:
    stack: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > MAX_JSON_NODES or depth > 40:
            return
        if isinstance(item, dict):
            yield item
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _is_event_type(value: object) -> bool:
    types = value if isinstance(value, list) else [value]
    for item in types:
        if not isinstance(item, str):
            continue
        name = re.split(r"[/#:]+", item.casefold())[-1]
        if name == "event" or name.endswith("event"):
            return True
    return False


def _named_value(value: object, limit: int = 300) -> str:
    if isinstance(value, str):
        return _bounded_text(value, limit)
    if isinstance(value, dict):
        name = _bounded_text(value.get("name"), limit)
        url = _bounded_text(value.get("url"), limit)
        return name or url
    if isinstance(value, list):
        values = [_named_value(item, limit) for item in value]
        return _bounded_text(", ".join(item for item in values if item), limit)
    return ""


def _address_value(value: object) -> str:
    if isinstance(value, str):
        return _bounded_text(value, 500)
    if not isinstance(value, dict):
        return ""
    parts = [
        _bounded_text(value.get(field), 150)
        for field in (
            "streetAddress",
            "addressLocality",
            "addressRegion",
            "postalCode",
            "addressCountry",
        )
    ]
    return _bounded_text(", ".join(item for item in parts if item), 500)


def _location_values(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        return _bounded_text(value, 300), ""
    if isinstance(value, list):
        locations = [_location_values(item) for item in value]
        return (
            _bounded_text(", ".join(item[0] for item in locations if item[0]), 300),
            _bounded_text(", ".join(item[1] for item in locations if item[1]), 500),
        )
    if isinstance(value, dict):
        return _named_value(value.get("name")), _address_value(value.get("address"))
    return "", ""


def _display_url(value: object, base_url: str) -> str:
    candidate = _bounded_text(value, MAX_URL_CHARS)
    if not candidate:
        return ""
    try:
        absolute = urljoin(base_url, candidate)
        return _normalize_http_url(absolute, resolve=False)
    except WebsiteSourceError:
        return ""


def _events_from_json(value: object, base_url: str) -> list[WebsiteEvent]:
    events: list[WebsiteEvent] = []
    seen: set[tuple[str, str, str]] = set()
    for item in _iter_json_objects(value):
        if not _is_event_type(item.get("@type")):
            continue
        name = _bounded_text(item.get("name"), 300)
        if not name:
            continue
        location, address = _location_values(item.get("location"))
        event = WebsiteEvent(
            name=name,
            start_date=_bounded_text(item.get("startDate"), 100) or None,
            end_date=_bounded_text(item.get("endDate"), 100) or None,
            location=location or None,
            address=address or None,
            url=_display_url(item.get("url"), base_url) or None,
            organizer=_named_value(item.get("organizer")) or None,
        )
        identity = (event.name.casefold(), event.start_date or "", event.url or "")
        if identity not in seen:
            seen.add(identity)
            events.append(event)
        if len(events) >= MAX_RECORDS:
            break
    return events


def _unfold_ical(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _events_from_ical(text: str, base_url: str) -> list[WebsiteEvent]:
    events: list[WebsiteEvent] = []
    current: dict[str, str] | None = None
    for line in _unfold_ical(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT" and current is not None:
            name = _bounded_text(current.get("SUMMARY"), 300)
            if name:
                events.append(
                    WebsiteEvent(
                        name=name,
                        start_date=_bounded_text(current.get("DTSTART"), 100) or None,
                        end_date=_bounded_text(current.get("DTEND"), 100) or None,
                        location=_bounded_text(current.get("LOCATION"), 300) or None,
                        url=_display_url(current.get("URL"), base_url) or None,
                        organizer=_bounded_text(current.get("ORGANIZER"), 300) or None,
                    )
                )
            current = None
            if len(events) >= MAX_RECORDS:
                break
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current.setdefault(key.split(";", 1)[0].upper(), value)
    return events


def _load_json_safely(text: str) -> object | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None


def _parse_content(
    data: bytes,
    content_type: str,
    charset: str,
    base_url: str,
) -> tuple[str, str, str, list[WebsiteEvent]]:
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    text = text.replace("\x00", "")
    events: list[WebsiteEvent] = []
    title = ""
    description = ""
    snippet = ""

    if content_type in {
        "application/json",
        "application/ld+json",
    } or content_type.endswith("+json"):
        payload = _load_json_safely(text)
        if payload is not None:
            events = _events_from_json(payload, base_url)
            if isinstance(payload, dict):
                title = _bounded_text(payload.get("name") or payload.get("title"), 300)
                description = _bounded_text(
                    payload.get("description") or payload.get("summary"), 1_000
                )
        snippet = _bounded_text(text, 2_000)
    elif content_type in {"text/calendar", "application/calendar", "application/ics"}:
        events = _events_from_ical(text, base_url)
        snippet = _bounded_text(text, 2_000)
    else:
        parser = _PageParser()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError):
            # HTMLParser is best-effort for untrusted, potentially malformed input.
            pass
        title = _bounded_text(" ".join(parser.title_parts), 300)
        description = _bounded_text(parser.description, 1_000)
        snippet = _bounded_text(" ".join(parser.visible_parts), 2_000)
        for parts in parser.json_ld_parts[:32]:
            payload = _load_json_safely("".join(parts))
            if payload is not None:
                events.extend(_events_from_json(payload, base_url))
            if len(events) >= MAX_RECORDS:
                break

    unique: dict[tuple[str, str, str], WebsiteEvent] = {}
    for event in events:
        key = (event.name.casefold(), event.start_date or "", event.url or "")
        unique.setdefault(key, event)
    return title, description, snippet, list(unique.values())[:MAX_RECORDS]


def _content_type_and_charset(header: str) -> tuple[str, str]:
    pieces = [piece.strip() for piece in header.split(";")]
    content_type = pieces[0].casefold()
    charset = "utf-8"
    for piece in pieces[1:]:
        if piece.casefold().startswith("charset="):
            charset = piece.split("=", 1)[1].strip().strip("\"'")[:80] or "utf-8"
    if content_type not in _ALLOWED_CONTENT_TYPES and not (
        content_type.endswith(("+json", "+xml"))
    ):
        raise WebsiteSourceError(
            "Website returned an unsupported content type; use HTML, JSON, "
            "JSON-LD, iCalendar, RSS, or XML."
        )
    return content_type, charset


def _record_id(source_id: str, index: int, event: WebsiteEvent | None) -> str:
    identity = event.model_dump_json() if event is not None else "page"
    digest = hashlib.sha256(f"{source_id}\n{index}\n{identity}".encode()).hexdigest()
    return f"web-{digest[:32]}"


def _records_for_scan(
    source: WebsiteSource,
    final_url: str,
    title: str,
    description: str,
    snippet: str,
    events: list[WebsiteEvent],
) -> list[WebsiteScanRecord]:
    if events:
        records: list[WebsiteScanRecord] = []
        for index, event in enumerate(events):
            fields = {
                "name": event.name,
                "startDate": event.start_date or "",
                "endDate": event.end_date or "",
                "location": event.location or "",
                "address": event.address or "",
                "url": event.url or "",
                "organizer": event.organizer or "",
            }
            records.append(
                WebsiteScanRecord(
                    record_id=_record_id(source.source_id, index, event),
                    source_id=source.source_id,
                    source_url=source.url,
                    final_url=final_url,
                    extraction_goal=source.extraction_goal,
                    kind="event",
                    title=event.name,
                    summary=description or snippet,
                    fields={key: value for key, value in fields.items() if value},
                    evidence_reference=event.url or final_url,
                )
            )
        return records
    page_title = title or urlsplit(final_url).hostname or "Website page"
    return [
        WebsiteScanRecord(
            record_id=_record_id(source.source_id, 0, None),
            source_id=source.source_id,
            source_url=source.url,
            final_url=final_url,
            extraction_goal=source.extraction_goal,
            kind="page",
            title=page_title,
            summary=description or snippet or "No readable page text was found.",
            fields={
                "requested_goal": source.extraction_goal,
                "page_title": page_title,
                "meta_description": description,
                "text_snippet": snippet,
            },
            evidence_reference=final_url,
        )
    ]


def _assert_no_symlink_components(path: Path) -> None:
    current = path.absolute()
    parents: list[Path] = [current, *current.parents]
    for component in reversed(parents):
        if component.exists() and component.is_symlink():
            raise WebsiteSourceError(
                "Website source registry path must not use symbolic links."
            )


def _read_registry_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise WebsiteSourceError(
            "Website source registry path must not be a symbolic link."
        )
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_REGISTRY_BYTES:
            raise WebsiteSourceError("Website source registry is not a bounded file.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise WebsiteSourceError(
                    "Website source registry changed while opening."
                )
            data = os.read(descriptor, MAX_REGISTRY_BYTES + 1)
        finally:
            os.close(descriptor)
    except WebsiteSourceError:
        raise
    except OSError:
        raise WebsiteSourceError(
            "Website source registry could not be read safely."
        ) from None
    if len(data) > MAX_REGISTRY_BYTES:
        raise WebsiteSourceError("Website source registry is too large.")
    return data


class WebsiteSourceRegistry:
    """Thread-safe local definitions plus secure, on-demand website scans."""

    def __init__(
        self,
        path: str | os.PathLike[str] = "runtime/website_sources.json",
        *,
        resolver: Resolver = socket.getaddrinfo,
        opener_factory: OpenerFactory = _default_opener_factory,
    ) -> None:
        self._path = Path(path)
        self._resolver = resolver
        self._opener_factory = opener_factory
        self._lock = threading.RLock()
        self._sources = self._load()

    def _load(self) -> dict[str, WebsiteSource]:
        if not self._path.exists() and not self._path.is_symlink():
            return {}
        _assert_no_symlink_components(self._path)
        try:
            payload = json.loads(_read_registry_bytes(self._path).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise WebsiteSourceError("Website source registry format is invalid.")
            rows = payload.get("sources")
            if not isinstance(rows, list) or len(rows) > MAX_SOURCES:
                raise WebsiteSourceError("Website source registry format is invalid.")
            sources = [WebsiteSource.model_validate(item) for item in rows]
        except WebsiteSourceError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            raise WebsiteSourceError(
                "Website source registry could not be read safely."
            ) from None
        if len({source.source_id for source in sources}) != len(sources):
            raise WebsiteSourceError(
                "Website source registry has duplicate identifiers."
            )
        return {source.source_id: source for source in sources}

    def _save(self) -> None:
        _assert_no_symlink_components(self._path.parent)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.is_symlink():
                raise WebsiteSourceError(
                    "Website source registry path must not be a symbolic link."
                )
            payload = json.dumps(
                {
                    "version": 1,
                    "sources": [
                        source.model_dump(mode="json")
                        for source in sorted(
                            self._sources.values(), key=lambda item: item.created_at
                        )
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
            if len(payload) > MAX_REGISTRY_BYTES:
                raise WebsiteSourceError("Website source registry exceeds its limit.")
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".website-sources-",
                suffix=".tmp",
                dir=self._path.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temp_name, self._path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
        except WebsiteSourceError:
            raise
        except OSError:
            raise WebsiteSourceError(
                "Website source registry could not be saved safely."
            ) from None

    def list_sources(self) -> list[WebsiteSource]:
        """Return a stable snapshot of saved website definitions."""

        with self._lock:
            return sorted(self._sources.values(), key=lambda item: item.created_at)

    def add_source(
        self,
        url: str,
        extraction_goal: str,
        *,
        label: str = "",
    ) -> WebsiteSource:
        """Validate and persist one public URL; HTTP is allowed, HTTPS preferred."""

        safe_url = _normalize_http_url(
            url,
            resolve=True,
            resolver=self._resolver,
        )
        safe_goal = _bounded_text(extraction_goal, 241)
        if not 3 <= len(safe_goal) <= 240:
            raise WebsiteSourceError(
                "Extraction goal must contain 3 to 240 characters."
            )
        safe_label = _bounded_text(label, 81) or (
            urlsplit(safe_url).hostname or "Website"
        )
        if not 1 <= len(safe_label) <= 80:
            raise WebsiteSourceError("Website label must contain 1 to 80 characters.")
        with self._lock:
            for existing in self._sources.values():
                if (
                    existing.url == safe_url
                    and existing.extraction_goal.casefold() == safe_goal.casefold()
                ):
                    return existing
            if len(self._sources) >= MAX_SOURCES:
                raise WebsiteSourceError(
                    f"Use no more than {MAX_SOURCES} website sources."
                )
            source = WebsiteSource(
                source_id=f"website-{uuid.uuid4().hex}",
                label=safe_label,
                url=safe_url,
                extraction_goal=safe_goal,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._sources[source.source_id] = source
            try:
                self._save()
            except Exception:
                self._sources.pop(source.source_id, None)
                raise
            return source

    def remove_source(self, source_id: str) -> bool:
        """Remove a saved definition; returns ``False`` when it did not exist."""

        with self._lock:
            removed = self._sources.pop(source_id, None)
            if removed is None:
                return False
            try:
                self._save()
            except Exception:
                self._sources[source_id] = removed
                raise
            return True

    def scan_source(self, source_id: str) -> WebsiteScanResult:
        """Fetch and parse one source now without executing page JavaScript."""

        with self._lock:
            source = self._sources.get(source_id)
        if source is None:
            raise WebsiteSourceError("Website source was not found.")
        initial_url = _normalize_http_url(
            source.url,
            resolve=True,
            resolver=self._resolver,
        )
        request = urllib.request.Request(
            initial_url,
            headers={
                "Accept": (
                    "text/html, application/xhtml+xml, application/ld+json, "
                    "application/json, text/calendar, application/rss+xml, "
                    "application/xml;q=0.9, text/xml;q=0.9"
                ),
                "Accept-Encoding": "identity",
                "User-Agent": "ContextGate-Website-Scanner/0.1",
            },
            method="GET",
        )
        opener = self._opener_factory(_SafeRedirectHandler(self._resolver))
        try:
            with opener.open(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                final_url = _normalize_http_url(
                    response.geturl(),
                    resolve=True,
                    resolver=self._resolver,
                )
                header = response.headers.get("Content-Type", "")
                content_type, charset = _content_type_and_charset(header)
                length_header = response.headers.get("Content-Length", "")
                try:
                    announced_length = int(length_header) if length_header else None
                except ValueError:
                    announced_length = None
                if (
                    announced_length is not None
                    and announced_length > MAX_RESPONSE_BYTES
                ):
                    raise WebsiteSourceError(
                        "Website response exceeds the 2 MB scan limit."
                    )
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except WebsiteSourceError:
            raise
        except urllib.error.HTTPError as error:
            raise WebsiteSourceError(
                f"Website request failed with HTTP status {error.code}."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise WebsiteSourceError(
                "Website request failed or timed out safely."
            ) from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise WebsiteSourceError("Website response exceeds the 2 MB scan limit.")

        title, description, snippet, events = _parse_content(
            data,
            content_type,
            charset,
            final_url,
        )
        records = _records_for_scan(
            source,
            final_url,
            title,
            description,
            snippet,
            events,
        )
        return WebsiteScanResult(
            source=source,
            final_url=final_url,
            content_type=content_type,
            bytes_read=len(data),
            scanned_at=datetime.now(UTC).isoformat(),
            events=events,
            records=records,
        )


__all__ = [
    "WebsiteEvent",
    "WebsiteScanRecord",
    "WebsiteScanResult",
    "WebsiteSource",
    "WebsiteSourceError",
    "WebsiteSourceRegistry",
]
