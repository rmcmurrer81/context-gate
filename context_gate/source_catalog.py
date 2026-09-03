"""In-memory source catalog and transparent event-counting helpers."""

from __future__ import annotations

import re
import threading
from collections import Counter
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .email_connectors import MailSummary


class SourceRecord(BaseModel):
    """One bounded source item used for explainable aggregation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    record_id: str = Field(min_length=1, max_length=512)
    event_key: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    source_name: str = Field(min_length=1, max_length=200)
    location: str = Field(default="Unknown", max_length=200)
    organization: str | None = Field(default=None, max_length=200)
    event_date: str | None = Field(default=None, max_length=100)
    event_time: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=300)
    evidence_reference: str = Field(min_length=1, max_length=1000)
    fictional: bool = False


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _matches_target(record: SourceRecord, target: str) -> bool:
    normalized = _normal(target)
    if not normalized:
        return False
    return normalized in _normal(
        f"{record.organization or ''} {record.source_name} {record.title}"
    )


def _event_key(subject: str) -> str:
    cleaned = re.sub(
        r"^(re|fwd?|updated?|update|reminder|tickets? for|registration for)\s*[:\-]\s*",
        "",
        subject,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(reminder|updated?|registration confirmation)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _normal(cleaned)[:300] or "unknown-event"


def _source_name(sender: str, text: str) -> str:
    combined = _normal(f"{sender} {text}")
    if "eventbrite" in combined or "eventbright" in combined:
        return "Eventbrite"
    if "meetup" in combined:
        return "Meetup"
    if "ticketmaster" in combined:
        return "Ticketmaster"
    return sender[:200] or "Email"


def _location(text: str) -> str:
    normalized = _normal(text)
    new_york_terms = (
        "new york city",
        "new york ny",
        "nyc",
        "manhattan",
        "brooklyn",
        "queens",
        "bronx",
        "staten island",
    )
    if any(term in normalized for term in new_york_terms):
        return "New York City"
    for city in ("Boston", "Philadelphia", "Chicago", "San Francisco"):
        if _normal(city) in normalized:
            return city
    if "virtual" in normalized or "online" in normalized:
        return "Online"
    return "Unknown"


def _organization(sender: str, text: str) -> str:
    """Return a bounded organizer label without claiming authenticated identity."""

    combined = _normal(f"{sender} {text}")
    known = ("Hanson Robotics", "Confluent", "Eventbrite", "Meetup")
    for candidate in known:
        if _normal(candidate) in combined:
            return candidate
    display = re.sub(r"\s*<[^>]+>\s*$", "", sender).strip().strip('"')
    if display and "@" not in display:
        return display[:200]
    domain = sender.rsplit("@", 1)[-1].strip("> ") if "@" in sender else ""
    return domain[:200] or "Unknown organizer"


def _event_details(text: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort visible date, time, and street address extraction for display."""

    date_match = re.search(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
        r"Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:,\s*|\s+)\d{4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        text,
        flags=re.IGNORECASE,
    )
    time_match = re.search(
        r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)"
        r"(?:\s+(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT))?\b",
        text,
        flags=re.IGNORECASE,
    )
    address_match = re.search(
        r"\b\d{1,6}\s+[A-Za-z0-9.'’\- ]{2,90}\s"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
        r"Way|Place|Pl|Parkway|Pkwy)\b"
        r"(?:[.,]?\s*(?:Suite|Ste|Floor|Fl)\s*[A-Za-z0-9-]+)?",
        text,
        flags=re.IGNORECASE,
    )
    return (
        date_match.group(0) if date_match else None,
        time_match.group(0) if time_match else None,
        address_match.group(0) if address_match else None,
    )


def _calendar_date(value: str | None) -> str | None:
    """Normalize a bounded source date for calendar placement without guessing."""

    if not value:
        return None
    candidate = " ".join(value.strip().split())
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s]|$)", candidate)
    if iso_match is not None:
        try:
            return date.fromisoformat(iso_match.group(1)).isoformat()
        except ValueError:
            return None

    month_names = {
        name: index
        for index, variants in enumerate(
            (
                ("january", "jan"),
                ("february", "feb"),
                ("march", "mar"),
                ("april", "apr"),
                ("may",),
                ("june", "jun"),
                ("july", "jul"),
                ("august", "aug"),
                ("september", "sept", "sep"),
                ("october", "oct"),
                ("november", "nov"),
                ("december", "dec"),
            ),
            start=1,
        )
        for name in variants
    }
    named_match = re.match(r"^([A-Za-z]+)\s+(\d{1,2})(?:,\s*|\s+)(\d{4})$", candidate)
    if named_match is not None:
        month = month_names.get(named_match.group(1).casefold())
        if month is None:
            return None
        try:
            return date(
                int(named_match.group(3)), month, int(named_match.group(2))
            ).isoformat()
        except ValueError:
            return None

    numeric_match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$", candidate)
    if numeric_match is not None:
        year = int(numeric_match.group(3))
        if year < 100:
            year += 2000 if year <= 68 else 1900
        try:
            return date(
                year, int(numeric_match.group(1)), int(numeric_match.group(2))
            ).isoformat()
        except ValueError:
            return None
    return None


def synthetic_event_records() -> list[SourceRecord]:
    """Return a fictional inbox with deliberate duplicates and useful patterns."""

    rows = (
        ("m01", "ai-builders-nyc", "AI Builders NYC", "Eventbrite", "New York City"),
        (
            "m02",
            "ai-builders-nyc",
            "Updated: AI Builders NYC",
            "Eventbrite",
            "New York City",
        ),
        (
            "m03",
            "brooklyn-data-night",
            "Brooklyn Data Night",
            "Eventbrite",
            "New York City",
        ),
        ("m04", "boston-robotics", "Boston Robotics Forum", "Eventbrite", "Boston"),
        (
            "m05",
            "queens-tech-social",
            "Queens Tech Social",
            "Eventbrite",
            "New York City",
        ),
        (
            "m06",
            "philly-product-lab",
            "Philadelphia Product Lab",
            "Eventbrite",
            "Philadelphia",
        ),
        (
            "m07",
            "confluent-ai-day-nyc",
            "Confluent AI Day NYC",
            "Organizer email",
            "New York City",
        ),
        (
            "m08",
            "manhattan-ml-meetup",
            "Manhattan ML Meetup",
            "Meetup",
            "New York City",
        ),
        (
            "m09",
            "javits-future-expo",
            "Javits Future Expo",
            "Organizer email",
            "New York City",
        ),
        (
            "m10",
            "streaming-webinar",
            "Streaming Agents Webinar",
            "Organizer email",
            "Online",
        ),
    )
    details = {
        "m08": {
            "organization": "Posh",
            "event_date": "September 12, 2026",
            "event_time": "6:30 PM ET",
            "address": "35 Main Street, New York, NY 10004",
        },
        "m09": {
            "organization": "Hanson Robotics",
            "event_date": "September 18, 2026",
            "event_time": "10:00 AM ET",
            "address": "429 11th Avenue, New York, NY 10001",
        },
        "m10": {
            "organization": "Hanson Robotics",
            "event_date": "September 22, 2026",
            "event_time": "1:00 PM ET",
            "address": "Online",
        },
    }
    return [
        SourceRecord(
            record_id=f"fictional-{record_id}",
            event_key=event_key,
            title=title,
            source_name=source,
            location=location,
            evidence_reference=f"fictional-email://{record_id}",
            fictional=True,
            **details.get(record_id, {}),
        )
        for record_id, event_key, title, source, location in rows
    ]


class SourceCatalog:
    """Thread-safe, replaceable catalog for chat counts and evidence lists."""

    def __init__(self, records: list[SourceRecord] | None = None) -> None:
        self._lock = threading.RLock()
        self._hidden_targets: set[str] = set()
        self._deleted_targets: set[str] = set()
        self._records = {
            item.record_id: item
            for item in (records if records is not None else synthetic_event_records())
        }

    def records(self) -> list[SourceRecord]:
        with self._lock:
            return sorted(
                (
                    item
                    for item in self._records.values()
                    if not any(
                        _matches_target(item, target)
                        for target in self._hidden_targets | self._deleted_targets
                    )
                ),
                key=lambda item: item.record_id,
            )

    def configure_visibility(
        self, *, hidden_sources: list[str], deleted_sources: list[str]
    ) -> None:
        """Apply persisted display exclusions and deletion tombstones."""

        hidden = {_normal(item) for item in hidden_sources if _normal(item)}
        deleted = {_normal(item) for item in deleted_sources if _normal(item)}
        with self._lock:
            self._hidden_targets = hidden - deleted
            self._deleted_targets = deleted
            self._records = {
                record_id: item
                for record_id, item in self._records.items()
                if not any(_matches_target(item, target) for target in deleted)
            }

    def matching_count(self, target: str) -> int:
        """Count stored records for a target, including currently hidden records."""

        with self._lock:
            return sum(
                _matches_target(item, target)
                for item in self._records.values()
                if not any(
                    _matches_target(item, deleted) for deleted in self._deleted_targets
                )
            )

    def delete_source(self, target: str) -> int:
        """Remove matching records and retain a content-free re-import exclusion."""

        normalized = _normal(target)
        if not normalized:
            return 0
        with self._lock:
            matching_ids = [
                record_id
                for record_id, item in self._records.items()
                if _matches_target(item, normalized)
            ]
            for record_id in matching_ids:
                del self._records[record_id]
            self._hidden_targets.discard(normalized)
            self._deleted_targets.add(normalized)
            return len(matching_ids)

    def add_record(self, record: SourceRecord) -> None:
        """Add or replace one validated record by its stable identifier."""

        if not isinstance(record, SourceRecord):
            record = SourceRecord.model_validate(record)
        with self._lock:
            if any(_matches_target(record, target) for target in self._deleted_targets):
                return
            self._records[record.record_id] = record

    def add_external_records(self, records: list[SourceRecord]) -> int:
        """Add validated real-source records and retire the fictional proof set."""

        additions: dict[str, SourceRecord] = {}
        for record in records:
            if not isinstance(record, SourceRecord):
                record = SourceRecord.model_validate(record)
            additions[record.record_id] = record
        with self._lock:
            additions = {
                record_id: item
                for record_id, item in additions.items()
                if not any(
                    _matches_target(item, target) for target in self._deleted_targets
                )
            }
            if additions and any(item.fictional for item in self._records.values()):
                self._records = {
                    record_id: item
                    for record_id, item in self._records.items()
                    if not item.fictional
                }
            self._records.update(additions)
        return len(additions)

    def reset_fictional_demo(self) -> None:
        """Replace all current records with the bundled fictional inbox."""

        with self._lock:
            self._records = {
                item.record_id: item
                for item in synthetic_event_records()
                if not any(
                    _matches_target(item, target) for target in self._deleted_targets
                )
            }

    def add_mail(self, messages: list[MailSummary]) -> int:
        additions: dict[str, SourceRecord] = {}
        for message in messages:
            combined = f"{message.subject} {message.preview} {message.sender}"
            event_date, event_time, address = _event_details(combined)
            record_id = f"{message.provider}-mail-{message.message_id}"
            additions[record_id] = SourceRecord(
                record_id=record_id,
                event_key=_event_key(message.subject),
                title=message.subject or "(no subject)",
                source_name=_source_name(message.sender, combined),
                location=_location(combined),
                organization=_organization(message.sender, combined),
                event_date=event_date,
                event_time=event_time,
                address=address,
                evidence_reference=f"{message.provider}-mail://{message.message_id}",
                fictional=False,
            )
        return self.add_external_records(list(additions.values()))

    def answer_tracking_question(self, question: str) -> dict[str, object] | None:
        """Handle an organizer tracking instruction or event-detail question."""

        if not isinstance(question, str) or len(question) > 2_000:
            return None
        normalized = _normal(question)
        visible_records = self.records()
        if re.fullmatch(
            r"(?:show|list)(?: me)? all (?:visible )?events?"
            r"(?: and (?:their )?sources?)?",
            normalized,
        ):
            unique: dict[str, SourceRecord] = {}
            for item in visible_records:
                unique.setdefault(item.event_key, item)
            selected = list(unique.values())[:6]
            fictional = bool(visible_records) and all(
                item.fictional for item in visible_records
            )
            mode = (
                "fictional demo inbox"
                if fictional
                else "currently scanned or uploaded sources"
            )
            if selected:
                rows = "; ".join(
                    f"{item.title} — {item.organization or item.source_name} via "
                    f"{item.source_name}; {item.event_date or 'date not found in source'}"
                    for item in selected
                )
                remaining = len(unique) - len(selected)
                text = (
                    f"I found {len(unique)} distinct visible event"
                    f"{'s' if len(unique) != 1 else ''} in the {mode}. Showing "
                    f"{len(selected)} with their sources: {rows}."
                )
                if remaining:
                    text += (
                        f" Open Calendar for the remaining {remaining} event"
                        f"{'s' if remaining != 1 else ''} and full details."
                    )
            else:
                text = (
                    "There are no visible event records in the source catalog. "
                    "Connect, scan, or upload a source; ContextGate will not invent "
                    "events or provenance."
                )
            evidence = [
                {
                    "title": item.title,
                    "organization": item.organization or item.source_name,
                    "date": item.event_date or "Date not found in source",
                    "time": item.event_time or "Time not found in source",
                    "address": item.address or "Address not found in source",
                    "reference": item.evidence_reference,
                }
                for item in selected
            ]
            return {
                "text": text,
                "target": None,
                "evidence": evidence,
                "remember": False,
                "fictional": fictional,
            }

        match = re.search(
            r"(?P<intent>keep\s+track\s+of|keep\s+tracking|track|"
            r"show(?:\s+me)?|list(?:\s+me)?|find|"
            r"what(?:\s+are)?)"
            r"(?:\s+all)?\s+events?\s+(?:from|by|for)\s+"
            r"(?P<target>.+?)(?:[?.!]|$)",
            question,
            flags=re.IGNORECASE,
        )
        if match is None:
            match = re.search(
                r"(?P<intent>what)\s+events?\s+(?:came|come)\s+from\s+"
                r"(?P<target>.+?)(?:[?.!]|$)",
                question,
                flags=re.IGNORECASE,
            )
        if match is None:
            match = re.search(
                r"(?P<intent>what)\s+(?P<target>.+?)\s+events?\s+"
                r"do\s+you\s+have(?:[?.!]|$)",
                question,
                flags=re.IGNORECASE,
            )
        if match is None:
            return None
        target = " ".join(match.group("target").split()).strip(" -:;,\"'")[:120]
        if not target:
            return None
        intent = _normal(match.group("intent"))
        remember = intent in {"keep track of", "keep tracking", "track"}
        target_normal = _normal(target)
        records = [
            item
            for item in visible_records
            if target_normal
            in _normal(f"{item.organization or ''} {item.source_name} {item.title}")
        ]
        unique: dict[str, SourceRecord] = {}
        for item in records:
            unique.setdefault(item.event_key, item)
        evidence = [
            {
                "title": item.title,
                "organization": item.organization or item.source_name,
                "date": item.event_date or "Date not found in source",
                "time": item.event_time or "Time not found in source",
                "address": item.address or "Address not found in source",
                "reference": item.evidence_reference,
            }
            for item in unique.values()
        ]
        mode = (
            "fictional demo inbox"
            if all(item.fictional for item in visible_records)
            else "currently scanned sources"
        )
        if evidence and remember:
            rows = "; ".join(
                f"{item['title']} — {item['date']}, {item['time']}, {item['address']}"
                for item in evidence
            )
            text = (
                f"I’ll keep track of events from {target}. I found "
                f"{len(evidence)} distinct matching event"
                f"{'s' if len(evidence) != 1 else ''} in the {mode}: {rows}. "
                "I will preserve the source reference and flag conflicting updates "
                "instead of silently replacing them."
            )
        elif evidence:
            rows = "; ".join(
                f"{item['title']} — {item['date']}, {item['time']}, {item['address']}"
                for item in evidence
            )
            text = (
                f"I found {len(evidence)} distinct matching event"
                f"{'s' if len(evidence) != 1 else ''} from {target} in the "
                f"{mode}: {rows}. Each result retains its source reference."
            )
        elif remember:
            text = (
                f"I’ll keep track of events from {target}. I do not have a matching "
                f"event in the {mode} yet. After a source is scanned, I’ll report "
                "the address, date, time, and evidence reference or say which detail "
                "was not found."
            )
        else:
            text = (
                f"I do not have a matching event from {target} in the {mode}. "
                "After a source is scanned, I can report the address, date, time, "
                "and evidence reference or say which detail was not found."
            )
        return {
            "text": text,
            "target": target,
            "evidence": evidence,
            "remember": remember,
            "fictional": all(item.fictional for item in visible_records),
        }

    def answer_inventory_question(self, question: str) -> dict[str, object] | None:
        """Summarize the visible catalog for ordinary data-inventory questions.

        This deliberately recognizes only bounded inventory wording. Case-specific
        provenance questions and commands continue to the more specific chat routes.
        """

        if not isinstance(question, str) or len(question) > 2_000:
            return None
        normalized = _normal(question)
        eventbrite = r"eventbr(?:ite|ight)"
        noun = (
            r"(?:sources? and data|data and sources?|sources? data|data sources?|"
            r"source data|data|information|records?|items?|events?|sources?)"
        )
        asks_eventbrite_inventory = any(
            re.fullmatch(pattern, normalized)
            for pattern in (
                (
                    rf"(?:what|which)(?: {noun})? (?:did|do) you "
                    rf"(?:get|collect|find|have|receive) from (?:the )?{eventbrite}"
                ),
                (
                    rf"(?:what|which)(?: {noun})? have you "
                    rf"(?:got|gotten|collected|found|received) from "
                    rf"(?:the )?{eventbrite}"
                ),
                (
                    rf"(?:show|tell)(?: me)? what you "
                    rf"(?:got|get|collected|collect|found|have|received) from "
                    rf"(?:the )?{eventbrite}"
                ),
            )
        )
        asks_general_inventory = bool(
            re.fullmatch(
                rf"(?:what|which) {noun} "
                r"(?:are you (?:collecting|tracking|holding|using)|"
                r"do you (?:collect|have|track)|"
                r"have you (?:collected|got|gotten|stored))",
                normalized,
            )
            or re.fullmatch(
                r"what (?:exactly )?are you (?:collecting|tracking)", normalized
            )
            or re.fullmatch(
                rf"(?:show|list)(?: me)? (?:your|the) (?:current |visible )?{noun}",
                normalized,
            )
            or re.fullmatch(
                rf"tell me what (?:current |visible )?{noun} you have", normalized
            )
        )
        if not asks_eventbrite_inventory and not asks_general_inventory:
            return None

        records = self.records()
        unique: dict[str, SourceRecord] = {}
        for item in records:
            unique.setdefault(item.event_key, item)
        fictional = bool(records) and all(item.fictional for item in records)
        mode = (
            "fictional demo inbox"
            if fictional
            else "currently visible scanned or uploaded sources"
        )

        if asks_eventbrite_inventory:
            matching_messages = [
                item for item in records if item.source_name.casefold() == "eventbrite"
            ]
            matching_events: dict[str, SourceRecord] = {}
            for item in matching_messages:
                matching_events.setdefault(item.event_key, item)
            selected = list(matching_events.values())
            if selected:
                rows = "; ".join(
                    f"{item.title} — {item.location}; "
                    f"{item.event_date or 'date not found in source'}"
                    for item in selected
                )
                duplicate_count = len(matching_messages) - len(selected)
                duplicate_note = (
                    f" I treated {duplicate_count} additional message"
                    f"{'s' if duplicate_count != 1 else ''} as "
                    "duplicate/update evidence rather than another event."
                    if duplicate_count
                    else ""
                )
                text = (
                    f"From Eventbrite, I have {len(selected)} distinct visible event"
                    f"{'s' if len(selected) != 1 else ''} across "
                    f"{len(matching_messages)} source record"
                    f"{'s' if len(matching_messages) != 1 else ''} in the {mode}: "
                    f"{rows}.{duplicate_note}"
                )
            else:
                text = (
                    f"I have no visible Eventbrite records in the {mode}. Hidden or "
                    "deleted data stays excluded, and I will not invent items."
                )
        else:
            selected = list(unique.values())[:6]
            if selected:
                source_counts = Counter(item.source_name for item in unique.values())
                location_counts = Counter(item.location for item in unique.values())
                sources = "; ".join(
                    f"{name} {count}" for name, count in sorted(source_counts.items())
                )
                locations = "; ".join(
                    f"{name} {count}" for name, count in sorted(location_counts.items())
                )
                rows = "; ".join(
                    f"{item.title} — {item.source_name}, {item.location}"
                    for item in selected
                )
                duplicate_count = len(records) - len(unique)
                duplicate_note = (
                    f" {duplicate_count} duplicate/update source record"
                    f"{'s are' if duplicate_count != 1 else ' is'} excluded from "
                    "the distinct-event total."
                    if duplicate_count
                    else ""
                )
                remaining = len(unique) - len(selected)
                remaining_note = (
                    f" {remaining} more distinct event"
                    f"{'s are' if remaining != 1 else ' is'} available in Calendar."
                    if remaining
                    else ""
                )
                text = (
                    f"The {mode} contains {len(records)} visible source record"
                    f"{'s' if len(records) != 1 else ''} representing "
                    f"{len(unique)} distinct event"
                    f"{'s' if len(unique) != 1 else ''}.{duplicate_note} "
                    f"By source: {sources}. By location: {locations}. "
                    f"Showing {len(selected)} items: {rows}.{remaining_note}"
                )
            else:
                text = (
                    "I have no visible source data in the current catalog. Connect, "
                    "scan, or upload a source; I will not invent records or provenance."
                )

        evidence = [
            {
                "title": item.title,
                "source": item.source_name,
                "location": item.location,
                "date": item.event_date or "Date not found in source",
                "reference": item.evidence_reference,
            }
            for item in selected
        ]
        return {
            "text": text,
            "evidence": evidence,
            "query_kind": "eventbrite" if asks_eventbrite_inventory else "catalog",
            "fictional": fictional,
        }

    def calendar_events(self) -> list[dict[str, object]]:
        """Return one visible, grounded record per event for calendar display.

        Hidden and deleted source preferences are already applied by ``records``.
        When an update and an earlier message share an event key, the record with
        the most explicit scheduling fields is selected. Values are never filled
        from unrelated records or inferred from the current date.
        """

        unique: dict[str, SourceRecord] = {}

        def detail_score(item: SourceRecord) -> tuple[int, int, str]:
            populated = sum(
                bool(value)
                for value in (
                    _calendar_date(item.event_date),
                    item.event_time,
                    item.address,
                    item.organization,
                )
            )
            # Prefer a direct subject over an "Updated:" label when equally rich.
            direct_title = int(
                not re.match(r"^updated?\s*:", item.title, re.IGNORECASE)
            )
            return populated, direct_title, item.record_id

        for item in self.records():
            current = unique.get(item.event_key)
            if current is None or detail_score(item) > detail_score(current):
                unique[item.event_key] = item

        events = [
            {
                "record_id": item.record_id,
                "event_key": item.event_key,
                "title": item.title,
                "organization": item.organization,
                "source_name": item.source_name,
                "location": item.location,
                "date": item.event_date,
                "date_iso": _calendar_date(item.event_date),
                "time": item.event_time,
                "address": item.address,
                "evidence_reference": item.evidence_reference,
                "fictional": item.fictional,
                "data_origin": (
                    "fictional_demo" if item.fictional else "scanned_or_uploaded"
                ),
            }
            for item in unique.values()
        ]
        return sorted(
            events,
            key=lambda item: (
                item["date_iso"] is None,
                item["date_iso"] or "",
                str(item["title"]).casefold(),
                str(item["record_id"]),
            ),
        )

    def answer_calendar_question(
        self, question: str, *, today: date | None = None
    ) -> dict[str, object] | None:
        """Answer bounded calendar, upcoming-event, and missing-date questions."""

        if not isinstance(question, str) or len(question) > 2_000:
            return None
        normalized = _normal(question)
        mentions_event_or_calendar = "event" in normalized or "calendar" in normalized
        asks_undated = mentions_event_or_calendar and bool(
            re.search(
                r"\b(?:undated|missing dates?|need(?:ing)? (?:a )?dates?|"
                r"without (?:a )?dates?|do not have (?:a )?dates?|no dates?)\b",
                normalized,
            )
        )
        asks_upcoming = bool(
            re.search(r"\bupcoming events?\b", normalized)
            or re.search(r"\bevents? (?:are )?coming up\b", normalized)
            or normalized
            in {
                "what is coming up",
                "what do i have coming up",
                "show me what is coming up",
            }
        )
        asks_overview = "calendar" in normalized and bool(
            re.search(
                r"\b(?:what(?: is| s)? on|show(?: me)?|list|view|open) "
                r"(?:my |the )?calendar\b",
                normalized,
            )
            or re.search(
                r"\b(?:what|how many) events? (?:are|is) on "
                r"(?:my |the )?calendar\b",
                normalized,
            )
            or re.search(r"\bcalendar events?\b", normalized)
        )
        if not (asks_undated or asks_upcoming or asks_overview):
            return None
        if asks_undated:
            query_kind = "undated"
        elif asks_upcoming:
            query_kind = "upcoming"
        else:
            query_kind = "overview"

        events = self.calendar_events()
        scheduled = [item for item in events if item.get("date_iso")]
        undated = [item for item in events if not item.get("date_iso")]
        if not events:
            return {
                "text": (
                    "There are no visible event records in the source catalog. "
                    "I cannot place anything on the calendar until a source provides "
                    "an event and date. Hidden and deleted sources remain excluded."
                ),
                "evidence": [],
                "query_kind": query_kind,
                "fictional": False,
            }

        fictional = all(bool(item.get("fictional")) for item in events)
        mode = (
            "fictional demo catalog"
            if fictional
            else "currently visible scanned or uploaded sources"
        )
        limit = 4

        def row(item: dict[str, object], *, missing_date: bool = False) -> str:
            title = str(item.get("title") or "Untitled event")
            organization = str(
                item.get("organization")
                or item.get("source_name")
                or "Organizer not found"
            )
            source = str(item.get("source_name") or "Source not labeled")
            if missing_date:
                return (
                    f"{title} — date not found in source; {organization} via {source}"
                )
            timing = str(item.get("date") or item.get("date_iso"))
            if item.get("time"):
                timing = f"{timing}, {item['time']}"
            address = item.get("address") or item.get("location")
            location = f"; {address}" if address and address != "Unknown" else ""
            return f"{title} — {timing}{location}; {organization} via {source}"

        if asks_undated:
            selected = undated[:limit]
            if selected:
                extra = len(undated) - len(selected)
                answer_text = (
                    f"I found {len(undated)} visible event"
                    f"{'s' if len(undated) != 1 else ''} without a usable source "
                    f"date in the {mode}: "
                    + "; ".join(row(item, missing_date=True) for item in selected)
                    + (
                        f". {extra} additional undated event"
                        f"{'s are' if extra != 1 else ' is'} available in Calendar."
                        if extra
                        else "."
                    )
                    + " I left them under Events needing a date instead of guessing."
                )
            else:
                answer_text = (
                    f"Every visible event in the {mode} has a usable source date; "
                    "there are no events in Events needing a date."
                )
        elif asks_upcoming:
            cutoff = today or datetime.now(UTC).astimezone().date()
            upcoming = [
                item
                for item in scheduled
                if str(item.get("date_iso")) >= cutoff.isoformat()
            ]
            selected = upcoming[:limit]
            if selected:
                extra = len(upcoming) - len(selected)
                answer_text = (
                    f"I found {len(upcoming)} scheduled upcoming event"
                    f"{'s' if len(upcoming) != 1 else ''} on or after "
                    f"{cutoff.isoformat()} in the {mode}: "
                    + "; ".join(row(item) for item in selected)
                    + (
                        f". {extra} additional upcoming events are in Calendar."
                        if extra
                        else "."
                    )
                )
            else:
                answer_text = (
                    f"I found no visible scheduled events on or after "
                    f"{cutoff.isoformat()} in the {mode}."
                )
            if undated:
                answer_text += (
                    f" I did not treat {len(undated)} undated event"
                    f"{'s' if len(undated) != 1 else ''} as upcoming because the "
                    "sources did not provide usable dates."
                )
        else:
            selected = scheduled[:limit]
            if selected:
                extra = len(scheduled) - len(selected)
                answer_text = (
                    f"In the {mode}, your calendar has {len(scheduled)} scheduled distinct "
                    f"event{'s' if len(scheduled) != 1 else ''} and {len(undated)} "
                    f"event{'s' if len(undated) != 1 else ''} needing a source date. "
                    "Scheduled: "
                    + "; ".join(row(item) for item in selected)
                    + (
                        f". {extra} additional scheduled events are in Calendar."
                        if extra
                        else "."
                    )
                )
            else:
                answer_text = (
                    f"The {mode} contains {len(undated)} visible event"
                    f"{'s' if len(undated) != 1 else ''}, but none can be placed "
                    "on the calendar because their sources do not provide usable dates."
                )
            if undated:
                answer_text += " Missing dates remain in Events needing a date; I did not guess them."

        evidence = [
            {
                "title": item.get("title"),
                "source": item.get("source_name"),
                "date": item.get("date"),
                "time": item.get("time"),
                "reference": item.get("evidence_reference"),
            }
            for item in selected
        ]
        return {
            "text": answer_text,
            "evidence": evidence,
            "query_kind": query_kind,
            "fictional": fictional,
        }

    def summary(self) -> dict[str, object]:
        records = self.records()
        unique: dict[str, SourceRecord] = {}
        for item in records:
            unique.setdefault(item.event_key, item)
        source_counts = Counter(item.source_name for item in unique.values())
        location_counts = Counter(item.location for item in unique.values())
        return {
            "messages_scanned": len(records),
            "distinct_events": len(unique),
            "duplicate_updates": len(records) - len(unique),
            "source_counts": dict(sorted(source_counts.items())),
            "location_counts": dict(sorted(location_counts.items())),
            "fictional": all(item.fictional for item in records),
        }

    def answer_count_question(self, question: str) -> dict[str, object] | None:
        """Answer supported source/location count questions with exact evidence."""

        normalized = _normal(question)
        is_count = any(
            phrase in normalized
            for phrase in ("how many", "count", "number of", "total")
        )
        if not is_count:
            return None
        asks_eventbrite = "eventbrite" in normalized or "eventbright" in normalized
        asks_new_york = any(
            phrase in normalized
            for phrase in (
                "new york city",
                "new york ny",
                "new york",
                "nyc",
                "manhattan",
                "brooklyn",
                "queens",
            )
        )
        if not asks_eventbrite and not asks_new_york:
            return None

        records = self.records()

        def summarize(
            *, source: str | None = None, location: str | None = None
        ) -> tuple[list[SourceRecord], dict[str, SourceRecord], list[dict[str, str]]]:
            matched = [
                item
                for item in records
                if (source is None or item.source_name.casefold() == source.casefold())
                and (
                    location is None or item.location.casefold() == location.casefold()
                )
            ]
            unique_records: dict[str, SourceRecord] = {}
            for item in matched:
                unique_records.setdefault(item.event_key, item)
            evidence_rows = [
                {
                    "title": item.title,
                    "source": item.source_name,
                    "location": item.location,
                    "reference": item.evidence_reference,
                }
                for item in unique_records.values()
            ]
            return matched, unique_records, evidence_rows

        mode = (
            "fictional demo inbox"
            if all(item.fictional for item in records)
            else "currently scanned sources"
        )

        if asks_eventbrite and asks_new_york and normalized.count("how many") >= 2:
            source_messages, source_events, source_evidence = summarize(
                source="Eventbrite"
            )
            location_messages, location_events, location_evidence = summarize(
                location="New York City"
            )
            evidence_by_reference = {
                item["reference"]: item
                for item in (*location_evidence, *source_evidence)
            }
            return {
                "text": (
                    f"I found {len(location_events)} distinct New York City events "
                    f"across {len(location_messages)} matching messages, and "
                    f"{len(source_events)} distinct Eventbrite events across "
                    f"{len(source_messages)} matching messages in the {mode}. These "
                    "are separate counts and may overlap, so I did not add them together."
                ),
                "counts": {
                    "New York City": len(location_events),
                    "Eventbrite": len(source_events),
                },
                "evidence": list(evidence_by_reference.values()),
                "fictional": all(item.fictional for item in records),
            }

        if asks_eventbrite and asks_new_york:
            matched_messages, unique, evidence = summarize(
                source="Eventbrite", location="New York City"
            )
            target_phrase = (
                f"Eventbrite event{'s' if len(unique) != 1 else ''} in New York City"
            )
        elif asks_eventbrite:
            matched_messages, unique, evidence = summarize(source="Eventbrite")
            target_phrase = f"Eventbrite event{'s' if len(unique) != 1 else ''}"
        else:
            matched_messages, unique, evidence = summarize(location="New York City")
            target_phrase = f"New York City event{'s' if len(unique) != 1 else ''}"

        duplicate_count = len(matched_messages) - len(unique)
        duplicate_note = (
            f" I excluded {duplicate_count} duplicate/update message"
            f"{'s' if duplicate_count != 1 else ''} from the event total."
            if duplicate_count
            else ""
        )
        return {
            "text": (
                f"I found {len(unique)} distinct {target_phrase} in the {mode}, across "
                f"{len(matched_messages)} matching message"
                f"{'s' if len(matched_messages) != 1 else ''}.{duplicate_note}"
            ),
            "count": len(unique),
            "matching_messages": len(matched_messages),
            "evidence": evidence,
            "fictional": all(item.fictional for item in records),
        }
