import asyncio
import json

import pytest

from kahin.tools.crawler_mirage import (
    _CrawlJob,
    _MAX_EVENTS_PER_JOB,
    _event_payload,
    _wait_for_events,
)


def _job() -> _CrawlJob:
    return _CrawlJob("crawl-test", {}, ["https://example.com/"])


def test_event_cursor_reports_eviction_and_monotonic_indexes():
    job = _job()
    for index in range(_MAX_EVENTS_PER_JOB + 2):
        job._record_event("progress", value=index)

    payload = json.loads(_event_payload(job, cursor=0, limit=10))

    assert payload["cursorReset"] is True
    assert payload["oldestCursor"] > 0
    assert payload["events"][0]["index"] == payload["oldestCursor"]
    assert payload["events"][-1]["index"] == payload["oldestCursor"] + 9


@pytest.mark.asyncio
async def test_crawl_events_waits_until_a_new_event():
    job = _job()
    task = asyncio.create_task(_wait_for_events(job, cursor=0, limit=10, wait_ms=1000))
    await asyncio.sleep(0)

    job._record_event("progress", currentUrl="https://example.com/")
    result = json.loads(await task)

    assert result["events"][0]["kind"] == "progress"
    assert result["events"][0]["currentUrl"] == "https://example.com/"


def test_empty_event_window_is_explicitly_exhausted():
    payload = json.loads(_event_payload(_job(), cursor=0, limit=10))

    assert payload["events"] == []
    assert payload["hasMore"] is False
    assert payload["nextCursor"] == 0
