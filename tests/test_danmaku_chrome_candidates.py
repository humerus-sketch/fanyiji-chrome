"""Chrome display candidates publish only through fake browser transports."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from fanyiji.chrome_caption_protocol import CaptionDeltaProtocol, CaptionSnapshot
from fanyiji.danmaku_protocol import danmaku_utf16_length, danmaku_weighted_length
from fanyiji.danmaku_service import DanmakuService
from fanyiji.danmaku_store import DanmakuStore


class Browser(QObject):
    danmaku_result = Signal(dict)
    event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.is_browser_ready = True
        self.requests = []

    def request_danmaku(self, operation, request_id, room, text=""):
        self.requests.append({"operation": operation, "request_id": request_id, "room": room, "text": text})
        return True

    def reply(self, **payload):
        request = self.requests[-1]
        if request["operation"] == "login":
            payload.setdefault("max_weighted_length", 80)
        if request["operation"] == "send":
            payload.setdefault("before_count", 0)
            payload.setdefault("request_started_epoch_ms", 1000000)
        self.danmaku_result.emit({"request_id": request["request_id"], "operation": request["operation"], "result": payload})


@pytest.fixture
def service(tmp_path):
    app = QApplication.instance() or QApplication([])
    browser = Browser()
    instance = DanmakuService(browser, store_path=tmp_path / "queue.sqlite3")
    now = [100.0]
    instance._clock = lambda: now[0]
    instance.configure(True, "1698225")
    instance.timer.stop()
    instance.begin_session(7)
    assert instance.begin_chrome_source(session=7, bridge_generation=3, source_epoch=2)
    instance._login_room = "1698225"
    yield instance, browser, now
    instance.close()
    assert app is not None


def candidate(instance, **overrides):
    event = {"kind": "stable_candidate", "session": 7, "bridge_generation": 3, "source_epoch": 2,
             "window_id": "101", "segment_id": "chrome:3:2:1", "revision": 1,
             "source_lang": "ja", "target_lang": "zh", "observed_at": 100.0,
             "historical": False, "inferred_stable": True, "boundary_observed": True,
             "is_official_final": False, "allow_push": False, "text": "主播正在介绍下一场比赛。"}
    event.update(overrides)
    return instance.handle_chrome_candidate(event)


def finish(instance, browser, now):
    browser.reply(outcome="receipt_accepted", code=0, http_status=200)
    instance.tick()
    assert browser.requests[-1]["operation"] == "check"
    browser.reply(present=True, room_matches=True, chat_ready=True, occurrence_count=1)
    assert instance.store.counts()["page_visible"] >= 1
    now[0] += 3.5


def test_stable_candidates_are_durable_before_an_actual_send_slot(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert instance.store.head()["state"] == "pending"
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.snapshot()["durable_queued"] == 1
    assert instance.snapshot()["queued"] == 1
    assert browser.requests == []
    instance.tick()
    assert browser.requests[-1]["operation"] == "send"
    assert browser.requests[-1]["text"] == "【主播正在介绍下一场比赛】"
    assert instance.store.head()["channel"] == "chrome_caption"
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance._segments == {}


@pytest.mark.parametrize("overrides", [
    {"historical": True}, {"historical": 0}, {"inferred_stable": False}, {"inferred_stable": 1},
    {"boundary_observed": False}, {"boundary_observed": 1}, {"is_official_final": True},
    {"is_official_final": None}, {"session": 6}, {"session": True}, {"bridge_generation": 2},
    {"bridge_generation": True}, {"source_epoch": 1}, {"source_epoch": True},
    {"revision": 0}, {"revision": "1"}, {"revision": True}, {"target_lang": "ja"},
    {"source_lang": []}, {"window_id": ""}, {"window_id": []}, {"segment_id": ""},
    {"observed_at": float("nan")}, {"observed_at": float("inf")}, {"observed_at": True},
    {"text": "谢谢Aya的留言"}, {"text": "谢谢�你的留言"}, {"text": "谢谢レレン的留言"},
    {"text": "语言 None<asr_text>"}, {"text": "翻译中…"}, {"text": "你好<script>"},
    {"text": "只有韩文한국"}, {"text": "谢谢ＡＢＣ"}, {"text": "中文\x01控制"},
    {"text": None}, {"text": "字" * 4001}, {"kind": "translation"}, {"kind": "baseline"}, {"kind": {}},
])
def test_uncertain_historical_foreign_or_malformed_inputs_never_publish(service, overrides):
    instance, browser, _ = service
    assert candidate(instance, **overrides) is False
    instance.tick()
    assert browser.requests == []
    assert instance.store.head() is None
    # Display-only rejection must not claim a model failure or risk-word rule.
    assert instance.content_filter.candidates() == []


def test_revise_retracts_old_revision_and_waits_for_corrected_stability(service):
    instance, browser, _ = service
    assert candidate(instance, text="这一局需要攻击左边的敌人。")
    assert not candidate(instance, kind="revise", revision=2, text="这一局需要攻击右边的敌人。", inferred_stable=False)
    assert candidate(instance, segment_id="next", text="接下来大家可以继续前进。")
    instance.tick()
    assert browser.requests == []
    assert not candidate(instance, revision=1, text="这一局需要攻击左边的敌人。")
    assert candidate(instance, revision=2, text="这一局需要攻击右边的敌人。")
    instance.tick()
    assert browser.requests[-1]["text"] == "【这一局需要攻击右边的敌人】"
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.snapshot()["durable_queued"] == 2


def test_append_invalidates_a_waiting_stable_revision(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert not candidate(instance, kind="append", revision=2, text="主播正在介绍下一场比赛但尚未开始", inferred_stable=False, boundary_observed=False)
    instance.tick()
    assert browser.requests == []
    assert candidate(instance, revision=2, text="主播正在介绍下一场比赛但尚未开始")
    instance.tick()
    assert "尚未开始" in browser.requests[-1]["text"]


def test_same_revision_with_different_text_is_not_an_implicit_rewrite(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert not candidate(instance, text="偷偷替换为不同的内容。")
    instance.tick()
    assert "介绍下一场" in browser.requests[-1]["text"]


def test_older_observation_cannot_replace_or_reset_current_candidates(service):
    instance, browser, _ = service
    assert candidate(instance, observed_at=100.2)
    assert not candidate(instance, kind="reset", observed_at=100.1)
    assert not candidate(instance, revision=2, observed_at=100.1, text="迟到的修订不能替换当前内容。")
    instance.tick()
    assert "介绍下一场" in browser.requests[-1]["text"]


def test_browser_not_ready_never_freezes_candidates_while_waiting(service):
    instance, browser, _ = service
    browser.is_browser_ready = False
    assert candidate(instance)
    instance.tick()
    assert instance.store.head()["state"] == "pending"
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.snapshot()["durable_queued"] == 1
    assert instance.snapshot()["state"] == "waiting_browser"
    assert candidate(instance, revision=2, text="主播已经修正下一场比赛时间。")
    browser.is_browser_ready = True
    instance.tick()
    assert browser.requests[-1]["text"] == "【主播已经修正下一场比赛时间】"


def test_same_text_different_segments_obeys_shared_ten_second_duplicate_rule(service):
    instance, browser, now = service
    assert candidate(instance)
    instance.tick()
    finish(instance, browser, now)
    assert candidate(instance, segment_id="duplicate")
    instance.tick()
    assert instance.store.head() is None
    assert instance.snapshot()["chrome_candidates"] == 0
    assert sum(request["operation"] == "send" for request in browser.requests) == 1


def test_disabled_or_microphone_only_source_cannot_admit_chrome_candidates(service):
    instance, browser, _ = service
    instance.configure(False, "1698225")
    assert not candidate(instance)
    instance.configure(True, "1698225", source="mic")
    instance.timer.stop()
    assert instance.begin_chrome_source(session=7, bridge_generation=4)
    assert not candidate(instance, bridge_generation=4, source_epoch=0)
    instance.tick()
    assert browser.requests == []


def test_reset_and_new_epoch_do_not_accept_a_delayed_old_source(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    assert instance.snapshot()["chrome_candidates"] == 0
    assert not candidate(instance, observed_at=102.0)
    assert candidate(instance, source_epoch=3, segment_id="chrome:3:3:2", observed_at=102.0, window_id="202")
    assert not candidate(instance, source_epoch=3, segment_id="other-window", observed_at=103.0, window_id="101")
    instance.tick()
    assert len(browser.requests) == 1


def test_new_generation_and_end_source_do_not_revive_old_events(service):
    instance, browser, _ = service
    assert candidate(instance)
    instance.end_chrome_source()
    assert not candidate(instance)
    assert not instance.begin_chrome_source(session=7, bridge_generation=3)
    assert instance.begin_chrome_source(session=7, bridge_generation=4)
    assert not candidate(instance)
    assert candidate(instance, bridge_generation=4, source_epoch=0, segment_id="chrome:4:0:1")
    instance.begin_session(8)
    assert not candidate(instance, bridge_generation=4, source_epoch=0)
    instance.tick()
    assert browser.requests[-1]["text"] == "【主播正在介绍下一场比赛】"
    assert instance.snapshot()["durable_queued"] == 1


def test_committed_segment_cannot_be_resent_by_revision_or_rebind(service):
    instance, browser, now = service
    assert candidate(instance)
    instance.tick()
    finish(instance, browser, now)
    assert not candidate(instance, revision=2, text="主播正在更正下一场比赛。")
    instance.end_chrome_source()
    assert instance.begin_chrome_source(session=7, bridge_generation=4)
    assert not candidate(instance, bridge_generation=4, source_epoch=0)
    instance.tick()
    assert sum(request["operation"] == "send" for request in browser.requests) == 1


def test_short_sentences_send_independently_without_waiting_for_next_sentence(service):
    instance, browser, _ = service
    assert candidate(instance, text="好，谢谢。")
    instance.tick()
    assert browser.requests[-1]["text"] == "【好 谢谢】"
    assert candidate(instance, segment_id="two", text="接下来继续游戏。")
    assert [row["text"] for row in instance.store.connection.execute("SELECT * FROM messages ORDER BY id")] == [
        "【好 谢谢】", "【接下来继续游戏】"]
    assert instance.snapshot()["chrome_candidates"] == 0


def test_short_sentence_revision_keeps_independent_payload_fresh(service):
    instance, browser, _ = service
    assert candidate(instance, text="谢谢大家。")
    assert not candidate(instance, kind="revise", revision=2, text="请等一下。", inferred_stable=False)
    assert candidate(instance, segment_id="two", text="现在可以继续游戏了。")
    instance.tick()
    assert browser.requests == []
    assert candidate(instance, revision=2, text="请等一下。")
    instance.tick()
    assert browser.requests[-1]["text"] == "【请等一下】"
    assert instance.snapshot()["durable_queued"] == 2


def test_long_chinese_is_split_with_existing_length_limits_and_send_interval(service):
    instance, browser, now = service
    assert candidate(instance, text="甲" * 85 + "。")
    instance.tick()
    finish(instance, browser, now)
    instance.tick()
    assert browser.requests[-1]["operation"] == "send"
    browser.reply(outcome="receipt_accepted", code=0, http_status=200)
    instance.tick()
    browser.reply(present=True, room_matches=True, chat_ready=True, occurrence_count=1)
    instance.tick()
    sends = [request for request in browser.requests if request["operation"] == "send"]
    assert len(sends) == 2
    assert instance.snapshot()["state"] == "cooldown"
    now[0] += 3.5
    instance.tick()
    sends = [request for request in browser.requests if request["operation"] == "send"]
    assert len(sends) == 3
    assert "".join(request["text"][1:-1] for request in sends) == "甲" * 54
    finish(instance, browser, now)
    for _ in range(2):
        instance.tick()
        finish(instance, browser, now)
    sends = [request for request in browser.requests if request["operation"] == "send"]
    assert "".join(request["text"][1:-1] for request in sends) == "甲" * 85
    assert all(danmaku_utf16_length(request["text"]) <= 40 for request in sends)
    assert all(danmaku_weighted_length(request["text"]) <= 40 for request in sends)


def test_filter_and_unknown_account_cap_are_applied_at_commit(service):
    instance, browser, _ = service
    instance.content_filter.add_rule("评价", "评语", action="replace")
    assert candidate(instance, text="谢谢大家给出的评价" + "甲" * 20 + "。")
    instance._apply_limits({"max_weighted_length": 24})
    instance.tick()
    assert "评价" not in browser.requests[-1]["text"]
    assert "评语" in "".join(row["text"] for row in instance.store.connection.execute("SELECT text FROM messages"))
    assert danmaku_weighted_length(browser.requests[-1]["text"]) <= 24


def test_storage_failure_preserves_mutable_candidate_and_retries(service, monkeypatch):
    instance, browser, _ = service
    original = instance.store.enqueue
    def fail(*args, **kwargs):
        raise OSError("simulated write failure")
    monkeypatch.setattr(instance.store, "enqueue", fail)
    assert candidate(instance)
    instance.tick()
    assert instance.snapshot()["state"] == "storage_error"
    assert instance.snapshot()["chrome_candidates"] == 1
    assert browser.requests == []
    monkeypatch.setattr(instance.store, "enqueue", original)
    instance.tick()
    assert browser.requests[-1]["operation"] == "send"


def test_read_only_login_probe_uses_already_persisted_real_caption(service):
    instance, browser, _ = service
    instance._login_room = ""
    assert candidate(instance)
    instance.tick()
    assert browser.requests[-1]["operation"] == "login"
    assert instance.store.head()["text"] == "【主播正在介绍下一场比赛】"
    assert instance._inflight["row"]["id"] == instance.store.head()["id"]
    browser.reply(is_login=True, code=0)
    instance.tick()
    assert browser.requests[-1]["operation"] == "send"


def test_same_room_login_result_can_authorize_a_durable_caption_after_source_rebind(service):
    instance, browser, _ = service
    instance._login_room = ""
    assert candidate(instance)
    instance.tick()
    assert instance.begin_chrome_source(session=7, bridge_generation=4)
    browser.reply(is_login=True, code=0)
    assert instance._login_room == "1698225"
    assert instance._inflight is None
    assert instance.store.head()["state"] == "pending"


def test_old_formal_queue_survives_chrome_retractions_and_is_sent_first(service):
    instance, browser, _ = service
    assert instance.store.enqueue("1698225", "system", "old-formal", 1, "之前正式确认的完整字幕", ["【之前正式确认的完整字幕】"])
    assert candidate(instance)
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    assert instance.store.head()["segment_key"] == "old-formal"
    assert candidate(instance, source_epoch=3, observed_at=102.0, segment_id="chrome:3:3:1")
    instance.tick()
    assert browser.requests[-1]["text"] == "【之前正式确认的完整字幕】"
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.snapshot()["durable_queued"] == 2
    instance.end_chrome_source()
    assert instance.store.head()["segment_key"] == "old-formal"


def test_explicit_clear_removes_candidates_and_does_not_replay_same_event(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert instance.snapshot()["clearable"] == 1
    assert instance.clear_pending()["removed"] == 1
    assert not candidate(instance)
    instance.tick()
    assert browser.requests == []


def test_real_protocol_baseline_is_never_relabelled_as_a_final(service):
    instance, browser, now = service
    protocol = CaptionDeltaProtocol(generation=3)
    initial = protocol.observe(CaptionSnapshot("这句是连接前的历史字幕。", "101", 2, 100.0, 3))
    for event in initial:
        assert not instance.handle_chrome_candidate({**event, "session": 7})
    updates = protocol.observe(CaptionSnapshot("这句是连接前的历史字幕。现在开始新的游戏环节。", "101", 2, 100.5, 3))
    for event in updates:
        instance.handle_chrome_candidate({**event, "session": 7})
    now[0] = 101.8
    stable = protocol.settle(now[0], bridge_generation=3)
    assert len(stable) == 1
    assert stable[0]["allow_push"] is False
    assert stable[0]["is_official_final"] is False
    assert instance.handle_chrome_candidate({**stable[0], "session": 7})
    instance.tick()
    assert browser.requests[-1]["text"] == "【现在开始新的游戏环节】"


@pytest.mark.parametrize("gate", ["browser", "login", "cooldown", "backlog", "inflight"])
def test_stable_caption_persists_at_admission_despite_every_sender_gate(service, gate):
    instance, browser, _ = service
    if gate == "browser":
        browser.is_browser_ready = False
    elif gate == "login":
        instance._login_room = ""
    elif gate == "cooldown":
        instance._next_at = 1000000.0
    else:
        assert instance.store.enqueue("1698225", "system", "earlier", 1,
                                      "更早的完整字幕必须保持在队首", ["【更早的完整字幕必须保持在队首】"])
        if gate == "inflight":
            instance.tick()
            assert browser.requests[-1]["operation"] == "send"
    before_requests = len(browser.requests)
    assert candidate(instance)
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.snapshot()["durable_queued"] == (2 if gate in {"backlog", "inflight"} else 1)
    row = instance.store.connection.execute("SELECT * FROM messages WHERE channel='chrome_caption'").fetchone()
    assert row["state"] == "pending"
    assert row["text"] == "【主播正在介绍下一场比赛】"
    assert len(browser.requests) == before_requests


def test_every_reset_preserves_already_stable_captions_in_fifo(service):
    instance, browser, _ = service
    browser.is_browser_ready = False
    for index in range(6):
        assert candidate(instance, segment_id=f"line-{index}", source_epoch=index + 2,
                         observed_at=100.0 + index, text=f"这是第{index}条完整中文字幕。")
        assert candidate(instance, kind="reset", source_epoch=index + 2, observed_at=100.0 + index)
    assert instance.snapshot()["durable_queued"] == 6
    assert [row["text"] for row in instance.store.connection.execute("SELECT * FROM messages ORDER BY id")] == [
        f"【这是第{index}条完整中文字幕】" for index in range(6)]
    instance.end_chrome_source()
    instance.tick()
    assert instance.snapshot()["durable_queued"] == 6
    assert browser.requests == []


def test_reset_releases_revision_gate_without_deleting_the_saved_stable_caption(service):
    instance, browser, _ = service
    assert candidate(instance)
    saved = instance.store.head()
    assert not candidate(instance, kind="revise", revision=2, text="正在发生尚未稳定的改写", inferred_stable=False)
    instance.tick()
    assert browser.requests == []
    assert instance.snapshot()["state"] == "chrome_revising"
    assert instance.snapshot()["queued"] == instance.snapshot()["durable_queued"] == 1
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    assert instance.store.head()["id"] == saved["id"]
    instance.tick()
    assert browser.requests[-1]["text"] == saved["text"]


def test_source_suspend_and_close_preserve_short_caption_for_independent_delivery(service):
    instance, browser, _ = service
    assert candidate(instance, text="谢谢大家。")
    assert instance.snapshot()["holding"] == 0
    assert instance.snapshot()["durable_queued"] == 1
    assert instance.snapshot()["chrome_candidates"] == 0
    instance.suspend_chrome_candidates()
    saved = instance.store.head()
    restored = DanmakuStore(instance.store.path)
    try:
        assert restored.head()["id"] == saved["id"]
        assert restored.head()["text"] == "【谢谢大家】"
        assert restored.head()["state"] == "pending"
    finally:
        restored.close()
    assert browser.requests == []


def test_repeated_stable_notification_does_not_fence_a_future_unsubmitted_revision(service):
    instance, browser, _ = service
    assert candidate(instance)
    first = instance.store.head()["id"]
    assert candidate(instance)
    assert candidate(instance, revision=2, text="主播已修正下一场比赛时间。")
    assert instance.store.head()["id"] == first
    assert instance.snapshot()["durable_queued"] == 1
    instance.tick()
    assert browser.requests[-1]["text"] == "【主播已修正下一场比赛时间】"


def test_clear_revision_memory_counts_its_durable_caption_only_once(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert not candidate(instance, kind="revise", revision=2, text="尚未稳定的修订文本", inferred_stable=False)
    assert instance.snapshot()["clearable"] == 1
    assert instance.clear_pending()["removed"] == 1
    assert not candidate(instance, revision=2, text="修改后也不能恢复清空的字幕。")
    assert instance.snapshot()["durable_queued"] == 0
    assert browser.requests == []


def test_broken_filter_cannot_prevent_safe_caption_persistence_or_allow_a_post(service, monkeypatch):
    instance, browser, _ = service
    def broken(*args, **kwargs):
        raise ValueError("synthetic filter fault")
    monkeypatch.setattr(instance.content_filter, "apply", broken)
    assert candidate(instance)
    assert instance.store.head()["text"] == "【主播正在介绍下一场比赛】"
    assert instance.snapshot()["durable_queued"] == 1
    instance.tick()
    assert browser.requests == []
    assert instance.snapshot()["state"] == "filter_error"


def test_source_reset_logs_only_fixed_reason_and_metadata(service):
    instance, _, _ = service
    assert candidate(instance)
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    records = instance.log_path.read_text(encoding="utf-8")
    assert '"reason": "protocol_reset"' in records
    assert '"reason": "source_epoch_changed"' in records
    assert "主播正在介绍" not in records


def test_independent_caption_revision_reapplies_rules_without_merging_sources(service):
    instance, browser, _ = service
    assert candidate(instance, text="评价不错。")
    assert candidate(instance, segment_id="two", text="接下来可以继续游戏了。")
    instance.content_filter.add_rule("评价", "评语", action="replace")
    assert candidate(instance, segment_id="two", revision=2, text="接下来可以观看比赛了。")
    instance.tick()
    assert browser.requests[-1]["text"] == "【评语不错】"
    assert instance.store.connection.execute("SELECT text FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0] == "【接下来可以观看比赛了】"


def test_committed_write_with_unknown_caller_result_restores_mutable_identity(service, monkeypatch):
    instance, browser, _ = service
    enqueue = instance.store.enqueue
    def completed_then_interrupted(*args, **kwargs):
        enqueue(*args, **kwargs)
        raise OSError("synthetic caller interruption after commit")
    monkeypatch.setattr(instance.store, "enqueue", completed_then_interrupted)
    assert candidate(instance)
    assert instance.snapshot()["chrome_candidates"] == 1
    assert instance.store.counts()["queued"] == 1
    monkeypatch.setattr(instance.store, "enqueue", enqueue)
    instance._next_at = 1000.0
    instance.tick()
    assert instance.snapshot()["chrome_candidates"] == 0
    assert candidate(instance, revision=2, text="尚未发送的这段字幕可以安全修订。")
    assert instance.store.counts()["queued"] == 1
    assert instance.store.head()["text"] == "【尚未发送的这段字幕可以安全修订】"
    assert browser.requests == []


@pytest.mark.parametrize("withdrawal", ["reset", "suspend", "end", "new_session", "new_room"])
def test_stable_write_retry_survives_source_reset_or_loss(service, monkeypatch, withdrawal):
    instance, browser, _ = service
    enqueue = instance.store.enqueue
    def unavailable(*args, **kwargs):
        raise OSError("synthetic temporary SQLite failure")
    monkeypatch.setattr(instance.store, "enqueue", unavailable)
    assert candidate(instance)
    assert instance.snapshot()["chrome_stable_candidates"] == 1
    if withdrawal == "reset":
        assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    elif withdrawal == "suspend":
        instance.suspend_chrome_candidates()
    elif withdrawal == "end":
        instance.end_chrome_source()
    elif withdrawal == "new_session":
        instance.begin_session(8)
    else:
        instance.configure(True, "21399689")
        instance.timer.stop()
    assert instance.snapshot()["chrome_stable_candidates"] == 1
    monkeypatch.setattr(instance.store, "enqueue", enqueue)
    browser.is_browser_ready = False
    instance.tick()
    assert instance.snapshot()["durable_queued"] == 1
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.store.head()["room"] == "1698225"
    assert instance.store.head()["text"] == "【主播正在介绍下一场比赛】"
    assert browser.requests == []


def test_new_unstable_revision_does_not_drop_a_stable_write_retry_on_reset(service, monkeypatch):
    instance, browser, _ = service
    enqueue = instance.store.enqueue
    monkeypatch.setattr(instance.store, "enqueue", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert candidate(instance)
    assert not candidate(instance, kind="revise", revision=2, text="这句修订仍未完成", inferred_stable=False)
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    monkeypatch.setattr(instance.store, "enqueue", enqueue)
    browser.is_browser_ready = False
    instance.tick()
    assert instance.store.head()["text"] == "【主播正在介绍下一场比赛】"
    assert instance.snapshot()["durable_queued"] == 1


def test_clear_explicitly_cancels_detached_stable_write_retry(service, monkeypatch):
    instance, browser, _ = service
    enqueue = instance.store.enqueue
    monkeypatch.setattr(instance.store, "enqueue", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert candidate(instance)
    instance.end_chrome_source()
    assert instance.clear_pending()["removed"] == 1
    monkeypatch.setattr(instance.store, "enqueue", enqueue)
    instance.tick()
    assert instance.store.head() is None
    assert instance.snapshot()["chrome_candidates"] == 0
    assert browser.requests == []


def test_detached_write_failure_keeps_storage_error_visible_until_recovered(service, monkeypatch):
    instance, browser, _ = service
    enqueue = instance.store.enqueue
    def unavailable(*args, **kwargs):
        raise OSError("synthetic temporary SQLite failure")
    monkeypatch.setattr(instance.store, "enqueue", unavailable)
    assert candidate(instance)
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    assert not instance._chrome_candidates
    assert len(instance._chrome_detached_writes) == 1
    observed = []
    instance.status.connect(observed.append)
    instance.tick()
    instance.tick()
    assert instance.store.head() is None
    assert instance.snapshot()["state"] == "storage_error"
    assert observed[-1]["state"] == "storage_error"
    assert instance.snapshot()["chrome_stable_candidates"] == 1
    assert instance.snapshot()["durable_queued"] == 0
    assert browser.requests == []
    monkeypatch.setattr(instance.store, "enqueue", enqueue)
    browser.is_browser_ready = False
    instance.tick()
    assert instance.snapshot()["durable_queued"] == 1
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.snapshot()["state"] == "waiting_browser"
    assert browser.requests == []


@pytest.mark.parametrize("code", ["sentence_limit", "no_unique_anchor", "crossed_anchors", "unmapped_tail",
    "known_gap_count", "unmapped_sentence", "known_gap_rewrite", "tail_removed", "unknown_tail_addition"])
def test_reset_alignment_log_preserves_only_whitelisted_code_and_bounded_counts(service, code):
    instance, _, _ = service
    assert candidate(instance, kind="reset", alignment_code=code, old_sentences=4096,
                     new_sentences=2, unique_anchors=0, text="PRIVATE_RESET_TEXT", token="PRIVATE_TOKEN")
    encoded = instance.log_path.read_text(encoding="utf-8")
    recorded = json.loads(encoded.splitlines()[-1])
    assert recorded["reason"] == "protocol_reset"
    assert recorded["alignment_code"] == code
    assert recorded["old_sentences"] == 4096
    assert recorded["new_sentences"] == 2
    assert recorded["unique_anchors"] == 0
    assert "PRIVATE_RESET_TEXT" not in encoded
    assert "PRIVATE_TOKEN" not in encoded


@pytest.mark.parametrize("code", ["aligned", "PRIVATE_INVALID_CODE", {"text": "PRIVATE_INVALID_CODE"}])
def test_reset_alignment_log_does_not_record_untrusted_code_or_payload(service, code):
    instance, _, _ = service
    assert candidate(instance, kind="reset", alignment_code=code, old_sentences=2,
                     new_sentences=2, unique_anchors=1, text="PRIVATE_RESET_TEXT", token="PRIVATE_TOKEN")
    encoded = instance.log_path.read_text(encoding="utf-8")
    recorded = json.loads(encoded.splitlines()[-1])
    assert not {"alignment_code", "old_sentences", "new_sentences", "unique_anchors"} & recorded.keys()
    assert "PRIVATE_" not in encoded


@pytest.mark.parametrize("invalid", [True, -1, 4097, 10**20, "2", None])
def test_reset_alignment_log_ignores_non_integer_or_out_of_range_counts(service, invalid):
    instance, _, _ = service
    assert candidate(instance, kind="reset", alignment_code="known_gap_rewrite", old_sentences=invalid,
                     new_sentences=invalid, unique_anchors=invalid)
    recorded = json.loads(instance.log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert recorded["alignment_code"] == "known_gap_rewrite"
    assert not {"old_sentences", "new_sentences", "unique_anchors"} & recorded.keys()


@pytest.mark.parametrize("kind", ["revise", "append", "stable_candidate"])
@pytest.mark.parametrize("formatted", ["这一局，需要攻击左边的敌人！", "这一局 需要攻击 左边的敌人", " 这一局需要攻击左边的敌人？ "])
def test_formatting_only_revision_does_not_delay_an_already_stable_public_body(service, kind, formatted):
    instance, browser, _ = service
    original = "这一局需要攻击左边的敌人。"
    assert candidate(instance, text=original)
    saved = instance.store.head()
    assert candidate(instance, kind=kind, revision=2, text=formatted,
                     inferred_stable=kind == "stable_candidate") is (kind == "stable_candidate")
    assert instance.snapshot()["chrome_candidates"] == 0
    assert instance.store.head()["id"] == saved["id"]
    assert instance.store.head()["text"] == saved["text"]
    assert instance.store.caption_member_source("1698225", saved["segment_key"]) == original
    instance.tick()
    assert browser.requests[-1]["operation"] == "send"
    assert browser.requests[-1]["text"] == saved["text"]


def test_formatting_only_revision_of_short_member_does_not_merge_or_delay_fifo(service):
    instance, browser, _ = service
    assert candidate(instance, text="谢谢大家。")
    assert candidate(instance, segment_id="two", text="现在可以继续游戏了。")
    saved = instance.store.head()
    assert saved["text"] == "【谢谢大家】"
    assert not candidate(instance, kind="revise", revision=2, text="谢谢，大家！", inferred_stable=False)
    assert instance.snapshot()["chrome_candidates"] == 0
    instance.tick()
    assert browser.requests[-1]["text"] == saved["text"]


def test_actual_word_revision_still_blocks_until_same_sentence_is_stable_again(service):
    instance, browser, _ = service
    assert candidate(instance, text="这一局需要攻击左边的敌人。")
    assert not candidate(instance, kind="revise", revision=2, text="这一局需要攻击右边的敌人！", inferred_stable=False)
    instance.tick()
    assert instance.snapshot()["state"] == "chrome_revising"
    assert browser.requests == []
    assert candidate(instance, revision=2, text="这一局需要攻击右边的敌人！")
    instance.tick()
    assert browser.requests[-1]["text"] == "【这一局需要攻击右边的敌人】"


def test_formatting_noop_keeps_revision_fence_after_source_reset(service):
    instance, browser, _ = service
    assert candidate(instance)
    assert not candidate(instance, kind="revise", revision=3, text="主播正在介绍下一场比赛？", inferred_stable=False)
    assert candidate(instance, kind="reset", source_epoch=3, observed_at=101.0)
    assert not candidate(instance, revision=2, source_epoch=3, observed_at=102.0)
    assert not candidate(instance, revision=2, source_epoch=3, observed_at=103.0, text="主播已经更改介绍内容。")
    instance.tick()
    assert browser.requests[-1]["text"] == "【主播正在介绍下一场比赛】"


def test_post_frozen_caption_never_becomes_mutable_on_formatting_update(service):
    instance, browser, _ = service
    assert candidate(instance)
    instance.tick()
    saved = instance.store.head()
    assert saved["state"] == "sending"
    assert not candidate(instance, kind="revise", revision=2, text="主播正在介绍下一场比赛！", inferred_stable=False)
    assert instance.store.head()["text"] == saved["text"]
    assert instance.store.head()["request_id"] == saved["request_id"]
    assert instance.snapshot()["chrome_candidates"] == 0
    assert len(browser.requests) == 1


def test_ascii_number_spaces_are_not_removed_by_formatting_body_comparison(service):
    instance, browser, _ = service
    assert candidate(instance, text="这局结果是数字12 3。")
    assert not candidate(instance, kind="revise", revision=2, text="这局结果是数字123。", inferred_stable=False)
    instance.tick()
    assert instance.snapshot()["state"] == "chrome_revising"
    assert browser.requests == []


def test_return_to_original_body_clears_only_its_waiting_word_revision(service):
    instance, browser, _ = service
    assert candidate(instance, text="这一局需要攻击左边的敌人。")
    assert not candidate(instance, kind="revise", revision=2, text="这一局需要攻击右边的敌人。", inferred_stable=False)
    assert not candidate(instance, kind="revise", revision=3, text="这一局需要攻击左边的敌人！", inferred_stable=False)
    assert instance.snapshot()["chrome_candidates"] == 0
    instance.tick()
    assert browser.requests[-1]["text"] == "【这一局需要攻击左边的敌人】"


@pytest.mark.parametrize("text", ["母亲", "逃跑", "第33页"])
def test_two_han_short_caption_sends_by_itself_immediately(service, text):
    instance, browser, _ = service
    assert candidate(instance, text=text)
    assert instance.store.head()["text"] == "【" + text + "】"
    instance.tick()
    assert browser.requests[-1]["operation"] == "send"
    assert browser.requests[-1]["text"] == "【" + text + "】"


@pytest.mark.parametrize("text", ["嗯", "1"])
def test_one_body_character_is_held_in_fifo_and_only_merged_with_next_sentence(service, text):
    instance, browser, _ = service
    assert candidate(instance, text=text)
    assert instance.store.head()["state"] == "holding_short"
    instance.tick()
    assert instance.snapshot()["state"] == "waiting_merge"
    assert instance.snapshot()["chrome_rejected_count"] == 0
    assert instance.content_filter.candidates() == []
    assert candidate(instance, segment_id="next", text="继续")
    instance.tick()
    assert browser.requests[-1]["text"] == "【" + text + " 继续】"


def test_partial_retract_drops_only_named_unstable_revision_and_keeps_all_durable_rows(service):
    instance, browser, _ = service
    assert candidate(instance, text="这是第一条完整字幕。")
    first = instance.store.head()
    assert not candidate(instance, kind="revise", revision=2, text="这条修订暂时未稳定", inferred_stable=False)
    assert candidate(instance, segment_id="other", text="这是第二条完整字幕。")
    assert not candidate(instance, kind="revise", segment_id="other", revision=2,
                         text="第二条也在修改当中", inferred_stable=False)
    assert candidate(instance, kind="retract", segment_ids=["chrome:3:2:1"], observed_at=101.0)
    assert instance.snapshot()["chrome_candidates"] == 1
    assert "other" in instance._chrome_candidates
    assert instance.store.head()["id"] == first["id"]
    assert instance.store.counts()["queued"] == 2
    instance.tick()
    assert browser.requests[-1]["text"] == first["text"]


def test_partial_retract_preserves_stable_write_retry_and_does_not_change_binding(service, monkeypatch):
    instance, browser, _ = service
    enqueue = instance.store.enqueue
    def fail(*args, **kwargs):
        raise OSError("synthetic SQLite failure")
    monkeypatch.setattr(instance.store, "enqueue", fail)
    assert candidate(instance)
    binding = dict(instance._chrome_binding)
    assert candidate(instance, kind="retract", segment_ids=["chrome:3:2:1"], observed_at=101.0)
    assert instance._chrome_binding == binding
    assert instance.snapshot()["chrome_stable_candidates"] == 1
    monkeypatch.setattr(instance.store, "enqueue", enqueue)
    browser.is_browser_ready = False
    instance.tick()
    assert instance.store.head()["text"] == "【主播正在介绍下一场比赛】"
    assert browser.requests == []


@pytest.mark.parametrize("changes", [
    {"session": 6}, {"bridge_generation": 2}, {"source_epoch": 1}, {"source_epoch": 3},
    {"observed_at": 99.0}, {"window_id": "wrong"}, {"segment_ids": []}, {"segment_ids": "one"},
    {"segment_ids": ["one", "one"]}, {"segment_ids": [None]}, {"segment_ids": ["x" * 257]},
])
def test_partial_retract_stale_or_invalid_events_cannot_remove_candidates(service, changes):
    instance, _, _ = service
    assert candidate(instance)
    assert not candidate(instance, kind="revise", revision=2, text="这句仍在等待重新稳定", inferred_stable=False)
    payload = {"segment_ids": ["chrome:3:2:1"], **changes}
    assert not candidate(instance, kind="retract", **payload)
    assert instance.snapshot()["chrome_candidates"] == 1
    assert instance.snapshot()["durable_queued"] == 1


def test_partial_retract_never_changes_a_current_post_or_unknown_result(service):
    instance, browser, _ = service
    assert candidate(instance)
    instance.tick()
    saved = instance.store.head()
    assert candidate(instance, kind="retract", segment_ids=["chrome:3:2:1"], observed_at=101.0)
    assert instance.store.head() == saved
    assert len(browser.requests) == 1
