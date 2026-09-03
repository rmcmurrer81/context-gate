"""Optional Kafka producer with a graceful, explicit local fallback."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    mode: str
    topic: str
    key: str
    delivered: bool
    detail: str


class ConfluentAdapter:
    """No network connection is attempted unless CONTEXTGATE_MODE=confluent."""

    def __init__(self) -> None:
        self.mode = os.getenv("CONTEXTGATE_MODE", "local").casefold()
        self._producer: Any = None

    def _get_producer(self) -> Any:
        if self.mode != "confluent":
            return None
        if self._producer is not None:
            return self._producer
        required = {
            "bootstrap.servers": os.getenv("CONFLUENT_BOOTSTRAP_SERVERS"),
            "sasl.username": os.getenv("CONFLUENT_API_KEY"),
            "sasl.password": os.getenv("CONFLUENT_API_SECRET"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"missing Confluent settings: {', '.join(missing)}")
        try:
            from confluent_kafka import Producer
        except ImportError as exc:
            raise RuntimeError("install the optional 'confluent' dependency") from exc
        self._producer = Producer(
            {
                **required,
                "security.protocol": "SASL_SSL",
                "sasl.mechanism": "PLAIN",
                "client.id": "context-gate",
            }
        )
        return self._producer

    def publish(
        self, topic: str, key: str, value: BaseModel | dict[str, Any]
    ) -> PublishReceipt:
        payload = (
            value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        )
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        producer = self._get_producer()
        if producer is None:
            return PublishReceipt(
                "local", topic, key, False, "Local fallback: no network action taken"
            )

        delivery_confirmed = False
        delivery_failure: str | None = None

        def delivery_report(error: Any, _message: Any) -> None:
            nonlocal delivery_confirmed, delivery_failure
            if error is None:
                delivery_confirmed = True
                return
            code = getattr(error, "code", None)
            name = getattr(error, "name", None)
            safe_code = code() if callable(code) else None
            safe_name = name() if callable(name) else type(error).__name__
            delivery_failure = (
                f"{safe_name} ({safe_code})" if safe_code is not None else safe_name
            )

        producer.produce(
            topic,
            key=key.encode("utf-8"),
            value=encoded,
            on_delivery=delivery_report,
        )
        undelivered = producer.flush(10)
        if undelivered:
            return PublishReceipt(
                "confluent",
                topic,
                key,
                False,
                f"Kafka delivery timed out with {undelivered} message(s) pending",
            )
        if delivery_failure is not None:
            return PublishReceipt(
                "confluent",
                topic,
                key,
                False,
                f"Kafka broker rejected the message: {delivery_failure}",
            )
        if not delivery_confirmed:
            return PublishReceipt(
                "confluent",
                topic,
                key,
                False,
                "Kafka queue drained without an observed broker acknowledgement",
            )
        return PublishReceipt(
            "confluent", topic, key, True, "Kafka broker acknowledged the message"
        )
