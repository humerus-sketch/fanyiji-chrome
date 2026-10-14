"""Retired unknown deliveries use only synthetic CDP and temporary profiles."""
import pytest

from fanyiji.danmaku_service import DanmakuService
from test_chrome_browser_service import harness, open_browser, submit, ROOM, TEXT, accepted_receipt


OTHER = "【后续正常字幕】"


def unknown_receipt():
    return {"result": {"value": {"error": "network_unknown", "request_started_epoch_ms": 1000,
                                 "before_count": 0}}}


def post_calls(page):
    return [row for row in page.calls if row[0] == "Runtime.evaluate"
            and "method:'POST'" in row[1].get("expression", "")]


def test_settled_retired_unknown_allows_different_payload_but_keeps_original_fenced(harness):
    page = open_browser(harness)
    page.values.append(unknown_receipt())
    assert submit(harness, request_id="unknown")
    assert harness.service._uncertain
    assert harness.service.retire_danmaku_submission(ROOM, TEXT)
    assert harness.service._unknown_submissions[(ROOM, TEXT)]["retired"]
    harness.state.now += 4
    assert submit(harness, request_id="next", text=OTHER)
    assert harness.results[-1]["result"]["outcome"] == "receipt_accepted"
    assert len(post_calls(page)) == 2
    assert harness.service._unknown_send == (ROOM, TEXT, 0)
    assert submit(harness, request_id="same-unknown")
    assert harness.results[-1]["result"]["outcome"] == "not_sent"
    assert len(post_calls(page)) == 2


def test_unsettled_retirement_requests_recovery_once_and_holds_next_payload(harness):
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic credential never logged"))
    assert submit(harness, request_id="unknown")
    requests = []
    harness.service.danmaku_transport_recovery_requested.connect(lambda: requests.append(True))
    assert harness.service.retire_danmaku_submission(ROOM, TEXT)
    assert harness.service.retire_danmaku_submission(ROOM, TEXT)
    assert requests == [True]
    harness.state.now += 4
    assert not submit(harness, request_id="before-recovery", text=OTHER)
    assert len(post_calls(page)) == 1
    assert harness.service.restart_danmaku_transport()
    new_page = harness.service._page
    assert new_page is not page
    assert submit(harness, request_id="after-recovery", text=OTHER)
    assert harness.results[-1]["result"]["outcome"] == "receipt_accepted"
    assert len(post_calls(new_page)) == 1
    assert submit(harness, request_id="old-after-recovery")
    assert harness.results[-1]["result"]["outcome"] == "not_sent"
    assert len(post_calls(new_page)) == 1


def test_retirement_requires_matching_record_and_never_opens_closed_browser(harness):
    assert harness.service.retire_danmaku_submission(ROOM, TEXT) is False
    assert harness.service.restart_danmaku_transport() == 0
    assert not harness.launched
    page = open_browser(harness)
    page.values.append(TimeoutError("synthetic"))
    submit(harness)
    assert not harness.service.retire_danmaku_submission(ROOM, OTHER)
    harness.service.stop_browser()
    assert harness.service.retire_danmaku_submission(ROOM, TEXT)
    assert harness.service.restart_danmaku_transport() == 0
    assert len(harness.launched) == 1


def test_two_retired_unknown_payloads_remain_separately_fenced(harness):
    page = open_browser(harness)
    page.values.append(unknown_receipt())
    submit(harness)
    harness.service.retire_danmaku_submission(ROOM, TEXT)
    harness.state.now += 4
    page.values.append(unknown_receipt())
    submit(harness, request_id="second-unknown", text=OTHER)
    harness.service.retire_danmaku_submission(ROOM, OTHER)
    assert len(harness.service._unknown_submissions) == 2
    harness.state.now += 4
    assert submit(harness, request_id="third", text="【这次正常的第三条】")
    assert len(post_calls(page)) == 3
    assert set(harness.service._unknown_submissions) == {(ROOM, TEXT), (ROOM, OTHER)}


def test_real_queue_handoff_and_real_browser_guard_continue_the_following_message(harness):
    page = open_browser(harness)
    sender = DanmakuService(harness.service, store_path=harness.root / "queue.sqlite3")
    try:
        sender.configure(True, ROOM)
        sender.timer.stop()
        sender.begin_session(7)
        sender._login_room = ROOM
        first = "【首次未知字幕】"
        for key, text in (("first", first), ("next", OTHER)):
            assert sender.store.enqueue(ROOM, "system", key, 1, text, [text])
        page.values.append(unknown_receipt())
        sender.tick()
        harness.service._executor.drain()
        assert sender.store.head()["text"] == OTHER
        assert sender.content_filter.candidates()[0]["text"] == first
        assert harness.service._unknown_submissions[(ROOM, first)]["retired"]
        harness.state.now += 4
        sender.tick()
        harness.service._executor.drain()
        assert len(post_calls(page)) == 2
        assert sender.store.head()["state"] == "verify"
        assert harness.results[-1]["result"]["outcome"] == "receipt_accepted"
    finally:
        sender.close()
