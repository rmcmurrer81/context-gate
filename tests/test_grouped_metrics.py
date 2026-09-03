from __future__ import annotations

from pathlib import Path

import pytest

from context_gate.grouped_metrics import (
    GroupedMetricError,
    TrackingTopicStore,
    answer_grouped_metric_question,
    parse_grouped_metric_artifact,
    proposed_tracking_topic,
)


def test_csv_grouped_metric_totals_are_generic_and_evidence_backed() -> None:
    dataset = parse_grouped_metric_artifact(
        "fictional-office-sales.csv",
        "text/csv",
        b"office,sales\nNew York,100\nAustin,40\nNew York,89\nAustin,33\n",
        preferred_group_fields=["Office"],
        preferred_metric_fields=["Sales"],
        source_reference="upload://sample",
    )
    assert dataset is not None
    assert dataset.group_field == "office"
    assert dataset.metric_field == "sales"
    assert dataset.group_totals == {"New York": 189, "Austin": 73}
    assert dataset.fictional is True

    answer = answer_grouped_metric_question(
        "What are total sales for New York?", [dataset]
    )
    assert answer is not None
    assert "New York: 189 sales" in str(answer["text"])
    assert [item["reference"] for item in answer["evidence"]] == [
        "upload://sample#row=2",
        "upload://sample#row=4",
    ]
    assert answer_grouped_metric_question("Tell me about New York", [dataset]) is None


def test_explicit_json_schema_supports_another_metric_without_core_vocabulary() -> None:
    dataset = parse_grouped_metric_artifact(
        "quarterly.json",
        "application/json",
        b'{"dataset":"Fictional throughput","group_by":"line","metric":"widgets","unit":"units","fictional":true,"rows":[{"line":"North","widgets":1.25},{"line":"North","widgets":2.5},{"line":"South","widgets":4}]}',
        source_reference="upload://throughput",
    )
    assert dataset is not None
    assert dataset.group_totals == {"North": 3.75, "South": 4}
    answer = answer_grouped_metric_question("Show widget totals by line", [dataset])
    assert answer is not None
    assert "North: 3.75 units widgets" in str(answer["text"])
    assert "South: 4 units widgets" in str(answer["text"])


def test_invalid_explicit_rows_are_rejected_with_a_bounded_error() -> None:
    with pytest.raises(GroupedMetricError, match="Row 2"):
        parse_grouped_metric_artifact(
            "metrics.csv",
            "text/csv",
            b"office,sales\nNew York,not-a-number\n",
            preferred_group_fields=["office"],
            preferred_metric_fields=["sales"],
        )


def test_tracking_topics_persist_independently_and_switch_without_removal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tracking-topics.json"
    store = TrackingTopicStore(path)
    sales = store.add_topic(
        name="office sales",
        kind="grouped_metric",
        metric_field="sales",
        group_fields=["office"],
        query_scope="sales by office",
    )
    robotics = store.add_topic(
        name="robotics",
        kind="named_filter",
        query_scope="robotics",
    )
    assert store.active_topic() == robotics

    reloaded = TrackingTopicStore(path)
    assert [item.name for item in reloaded.topics()] == ["office sales", "robotics"]
    assert reloaded.activate("sales") == sales
    assert len(reloaded.topics()) == 2
    assert reloaded.activate(previous=True) == robotics
    assert "does not remove other topics" in str(reloaded.snapshot()["switching_note"])


def test_chat_tracking_proposals_are_conservative_and_require_later_confirmation() -> (
    None
):
    proposal = proposed_tracking_topic("Also track office sales")
    assert proposal == {
        "name": "office sales",
        "kind": "grouped_metric",
        "metric_field": "sales",
        "group_fields": ["office"],
        "query_scope": "sales by office",
    }
    assert proposed_tracking_topic("Track revenue totals by region") == {
        "name": "region revenue",
        "kind": "grouped_metric",
        "metric_field": "revenue",
        "group_fields": ["region"],
        "query_scope": "revenue by region",
    }
    assert proposed_tracking_topic("How many events are there?") is None


def test_natural_grouped_metric_configuration_and_queries_remain_scoped() -> None:
    proposal = proposed_tracking_topic(
        "Set our important detail to sales and identify records by office"
    )
    assert proposal == {
        "name": "office sales",
        "kind": "grouped_metric",
        "metric_field": "sales",
        "group_fields": ["office"],
        "query_scope": "sales by office",
    }
    dataset = parse_grouped_metric_artifact(
        "fictional-office-sales.csv",
        "text/csv",
        b"office,sales\nNew York,100\nAustin,40\nNew York,89\nAustin,33\n",
        preferred_group_fields=["office"],
        preferred_metric_fields=["sales"],
        source_reference="upload://natural-language-sample",
    )
    assert dataset is not None

    new_york = answer_grouped_metric_question(
        "How much did the New York office sell?", [dataset]
    )
    assert new_york is not None
    assert "New York: 189 sales" in str(new_york["text"])

    austin = answer_grouped_metric_question("What were sales in Austin?", [dataset])
    assert austin is not None
    assert "Austin: 73 sales" in str(austin["text"])

    # A location shared with another domain is not enough: the configured
    # metric or grouping field must also be mentioned.
    assert (
        answer_grouped_metric_question(
            "How many events are in New York City?", [dataset]
        )
        is None
    )
