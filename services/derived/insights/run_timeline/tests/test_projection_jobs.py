from __future__ import annotations

import threading

import pytest

from services.derived.insights.run_timeline.projection.jobs import IndexJobs


def test_pending_agents_rotate_after_each_segment_and_admission_is_bounded() -> None:
    started, release, second = threading.Event(), threading.Event(), threading.Event()
    visits: list[int] = []

    def step(agent: int) -> bool:
        visits.append(agent)
        if len(visits) == 1:
            started.set()
            assert release.wait(5)
            return True
        if agent == 2:
            second.set()
        return False

    jobs = IndexJobs(step)
    try:
        assert jobs.request(1).status == "queued"
        assert started.wait(5)
        assert jobs.request(1).status == "indexing"
        for agent in range(2, 17):
            assert jobs.request(agent).status == "queued"
        assert jobs.request(17).status == "busy"
        release.set()
        assert second.wait(5)
        assert visits[:2] == [1, 2]
    finally:
        release.set()
        jobs.close()


def test_failed_preparation_is_reported_even_after_shutdown() -> None:
    failed, release = threading.Event(), threading.Event()

    def step(_agent: int) -> bool:
        failed.set()
        assert release.wait(5)
        raise ValueError("unreadable checkpoint")

    jobs = IndexJobs(step)
    jobs.request(1)
    assert failed.wait(5)
    release.set()

    jobs.close()
    assert jobs.request(1).status == "failed"
    assert jobs.request(1).status == "busy"


def test_unread_failures_do_not_consume_admission_and_explicit_retry_runs() -> None:
    failed, complete, retried = threading.Event(), threading.Event(), threading.Event()
    visits: list[int] = []

    def step(agent: int) -> bool:
        visits.append(agent)
        if agent <= 16:
            if agent == 16:
                failed.set()
            raise ValueError("unreadable checkpoint")
        if agent == 17:
            complete.set()
        if agent == 18:
            retried.set()
        return False

    jobs = IndexJobs(step)
    try:
        for agent in range(1, 17):
            assert jobs.request(agent).status == "queued"
        assert failed.wait(5)
        assert jobs.request(17).status == "queued"
        assert complete.wait(5)
        assert jobs.request(16).status == "failed"
        assert jobs.request(16).status == "queued"
        jobs.request(18)
        assert retried.wait(5)
        assert jobs.request(16).status == "failed"
        assert visits.count(16) == 2
    finally:
        jobs.close()


def test_close_reports_an_active_segment_and_never_starts_queued_work() -> None:
    started, release = threading.Event(), threading.Event()
    visits: list[int] = []

    def step(agent: int) -> bool:
        visits.append(agent)
        started.set()
        assert release.wait(5)
        return True

    jobs = IndexJobs(step)
    jobs.request(1)
    assert started.wait(5)
    jobs.request(2)
    try:
        assert not jobs.close()
    finally:
        release.set()
        assert jobs.close()
    assert visits == [1]


class FatalIndexFailure(BaseException):
    pass


def test_fatal_worker_failure_reaches_shutdown_and_later_requests() -> None:
    started = threading.Event()

    def step(_agent: int) -> bool:
        started.set()
        raise FatalIndexFailure("worker stopped")

    jobs = IndexJobs(step)
    jobs.request(1)
    assert started.wait(5)
    with pytest.raises(FatalIndexFailure, match="worker stopped"):
        jobs.close()
    assert jobs.request(2).status == "failed"
