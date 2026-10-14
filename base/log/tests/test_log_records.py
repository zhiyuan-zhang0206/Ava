"""`loguru_records` captures records and reads them back by level and by structured event."""

from loguru import logger

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


def test_messages_are_formatted_and_filterable_by_level(loguru_records: LogRecords) -> None:
    logger.info("started {}", "worker")
    logger.warning("slow: {} ms", 12)
    logger.debug("detail")

    assert loguru_records.messages() == ["started worker", "slow: 12 ms", "detail"]
    assert loguru_records.messages("WARNING") == ["slow: 12 ms"]
    assert loguru_records.messages("ERROR") == []


def test_events_selects_records_logged_with_that_event_name(loguru_records: LogRecords) -> None:
    logger.warning("first", event="scan_failed", agent_id=3)
    logger.warning("other", event="other_event")
    logger.warning("second", event="scan_failed", agent_id=4)

    failures = loguru_records.events("scan_failed")
    assert [r["message"] for r in failures] == ["first", "second"]
    assert [r["extra"]["agent_id"] for r in failures] == [3, 4]
    assert loguru_records.events("missing") == []
