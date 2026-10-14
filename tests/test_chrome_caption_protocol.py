"""Synthetic rolling-caption streams, without Chrome or user profile access."""
import math

import pytest

from fanyiji.chrome_caption_protocol import CaptionDeltaProtocol, CaptionSnapshot


GENERATION = 7


def snapshot(text, at=1.0, **options):
    values = dict(text=text, window_id="caption-101", source_epoch=4,
                  observed_at=at, bridge_generation=GENERATION,
                  source_lang="ja", target_lang="zh", room="1698225")
    values.update(options)
    return CaptionSnapshot(**values)


def protocol(**options):
    return CaptionDeltaProtocol(GENERATION, **options)


def growing_caption(instance=None):
    instance = instance or protocol()
    baseline = instance.observe(snapshot("上一句。"))
    appended = instance.observe(snapshot("上一句。今天天气很好", 1.5))
    return instance, baseline[0], appended[0]


def assert_candidate_policy(events):
    for event in events:
        assert event["is_official_final"] is False
        assert event["allow_push"] is False
        assert event["bridge_generation"] == GENERATION


def test_completed_sentence_settles_during_continuous_following_speech():
    instance = protocol()
    baseline = "连接前的历史字幕。"
    complete = "这一句已经结束等待稳定输出。"
    instance.observe(snapshot(baseline, 1.0))
    first = instance.observe(snapshot(baseline + complete, 1.2))
    identifier = first[0]["segment_id"]
    instance.observe(snapshot(baseline + complete + "下一句正在继续", 1.5))
    events = instance.observe(snapshot(baseline + complete + "下一句正在继续不停增长", 2.6))
    settled = [event for event in events if event["kind"] == "stable_candidate"]
    assert len(settled) == 1
    assert settled[0]["segment_id"] == identifier
    assert settled[0]["text"] == complete and settled[0]["boundary_observed"] is True
    assert settled[0]["historical"] is False
    assert_candidate_policy(events)
    next_events = instance.observe(snapshot(baseline + complete + "下一句正在继续不停增长并且没有暂停", 2.9))
    assert not any(event["kind"] == "stable_candidate" and event["segment_id"] == identifier
                   for event in next_events)


def test_historical_prefix_correction_does_not_turn_unchanged_new_sentence_into_history():
    instance = protocol()
    history = "上一局这条路不能通过。"
    corrected_history = "上一局这条路现在可以通过。"
    complete = "现在大家准备开始新的游戏。"
    first = instance.observe(snapshot(history, 1.0))[0]
    added = instance.observe(snapshot(history + complete, 1.2))[0]
    changed = instance.observe(snapshot(corrected_history + complete, 1.5))
    assert [row["kind"] for row in changed] == ["revise"]
    assert changed[0]["segment_id"] == first["segment_id"] and changed[0]["historical"]
    settled = instance.observe(snapshot(corrected_history + complete, 2.6))
    assert [row["segment_id"] for row in settled] == [added["segment_id"]]
    assert settled[0]["text"] == complete and not settled[0]["historical"]
    assert settled[0]["revision"] == added["revision"]
    assert_candidate_policy(changed + settled)


def test_rolloff_with_tail_correction_preserves_completed_sentence_and_revises_only_tail():
    instance = protocol()
    history, complete = "连接前的历史字幕。", "今天我们准备开始新的游戏。"
    before, after = "前面这条路好像不能走", "前面这条路现在可以走"
    instance.observe(snapshot(history, 1.0))
    added = instance.observe(snapshot(history + complete + before, 1.2))
    changed = instance.observe(snapshot(complete + after, 2.6))
    assert [row["kind"] for row in changed] == ["revise", "stable_candidate"]
    assert changed[0]["segment_id"] == added[1]["segment_id"]
    assert changed[0]["text"] == after and changed[0]["revision"] == 2
    assert changed[1]["segment_id"] == added[0]["segment_id"]
    assert changed[1]["text"] == complete and not changed[1]["historical"]
    assert not any(row["kind"] in {"reset", "baseline"} for row in changed)
    assert_candidate_policy(changed)


def test_non_tail_correction_keeps_same_id_and_requires_stability_again():
    instance = protocol()
    history = "这是连接以前的字幕。"
    before, after = "今晚大家一起参加游戏。", "今晚大家一起参加新的游戏。"
    tail = "下一个比赛环节准备开始"
    instance.observe(snapshot(history, 1.0))
    added = instance.observe(snapshot(history + before + tail, 1.2))
    instance.observe(snapshot(before + tail, 1.5))
    changed = instance.observe(snapshot(after + tail, 1.8))
    assert [row["kind"] for row in changed] == ["revise"]
    assert changed[0]["segment_id"] == added[0]["segment_id"]
    assert changed[0]["text"] == after and changed[0]["revision"] == 2
    early = instance.observe(snapshot(after + tail, 2.5))
    assert not any(row["segment_id"] == added[0]["segment_id"] for row in early)
    settled = instance.observe(snapshot(after + tail, 3.1))
    assert [row["segment_id"] for row in settled] == [added[0]["segment_id"]]
    assert settled[0]["text"] == after


def test_historical_rolloff_and_rewrite_can_append_after_a_known_complete_tail():
    instance = protocol()
    first, second = "历史第一句暂时不能通过。", "历史第二句正在介绍角色。"
    changed_second = "历史第二句正在介绍新的角色。"
    complete, added = "这一句刚刚完整结束。", "接下来进入新的游戏环节。"
    baseline = instance.observe(snapshot(first + second, 1.0))
    original = instance.observe(snapshot(first + second + complete, 1.2))[0]
    changed = instance.observe(snapshot(changed_second + complete + added, 2.6))
    assert [row["kind"] for row in changed] == ["revise", "append", "stable_candidate"]
    assert changed[0]["segment_id"] == baseline[1]["segment_id"] and changed[0]["historical"]
    assert changed[1]["text"] == added and not changed[1]["historical"]
    assert changed[2]["segment_id"] == original["segment_id"]
    assert instance.observe(snapshot(changed_second + complete + added, 2.9)) == []


def test_history_rewrite_and_late_completion_of_partial_baseline_still_keep_that_sentence_historical():
    instance = protocol()
    history, corrected = "上一句还在介绍角色。", "上一句正在介绍角色。"
    anchor = "中间这一句已经完整结束。"
    partial = "大家谢谢你"
    baseline = instance.observe(snapshot(history + anchor + partial, 1.0))
    changed = instance.observe(snapshot(corrected + anchor + partial + "的留言。接下来开始游戏。", 1.5))
    assert [row["kind"] for row in changed] == ["revise", "revise", "append"]
    assert changed[1]["segment_id"] == baseline[-1]["segment_id"] and changed[1]["historical"]
    assert changed[2]["historical"] is False
    stable = instance.observe(snapshot(corrected + anchor + partial + "的留言。接下来开始游戏。", 3.0))
    assert [row["text"] for row in stable] == ["接下来开始游戏。"]


def test_alignment_preserves_candidate_when_bounded_history_already_pruned_prefix():
    instance = protocol(max_candidates=2)
    history, changed_history = "之前的字幕介绍左边角色。", "之前的字幕介绍右边角色。"
    complete, tail = "现在准备新的游戏环节。", "后面的讲话还在继续"
    instance.observe(snapshot(history, 1.0))
    added = instance.observe(snapshot(history + complete + tail, 1.2))
    assert len(instance._captions) == 2
    events = instance.observe(snapshot(changed_history + complete + tail, 2.6))
    assert not any(row["kind"] in {"reset", "baseline"} for row in events)
    assert next(row for row in events if row["boundary_observed"])["segment_id"] == added[0]["segment_id"]
    assert len(instance._captions) == 2


@pytest.mark.parametrize("replacement", [
    "这是一段完全改写并合并的历史叙述。",
    "现在第一段历史完全换了意思。第二段历史还增加了文字。第三段也是历史。",
])
def test_strong_unique_sentence_keeps_new_caption_during_arbitrary_historical_split_merge(replacement):
    instance = protocol()
    history = "前面的历史原本介绍左边角色。另一句历史接着介绍游戏背景。"
    complete, tail = "现在大家准备开始新的游戏。", "后面的讲话还在继续"
    instance.observe(snapshot(history, 1.0))
    added = instance.observe(snapshot(history + complete + tail, 1.2))
    changed = instance.observe(snapshot(replacement + complete + tail, 1.5))
    assert not any(row["kind"] == "reset" for row in changed)
    assert all(row["historical"] and row["reason"] == "historical_realignment" for row in changed)
    settled = instance.observe(snapshot(replacement + complete + tail, 2.6))
    completed = [row for row in settled if row["boundary_observed"]]
    assert [row["segment_id"] for row in completed] == [added[0]["segment_id"]]
    assert completed[0]["text"] == complete and completed[0]["revision"] == 1
    assert_candidate_policy(changed + settled)


def test_historical_split_with_many_new_rows_cannot_trim_the_existing_new_tail():
    instance = protocol(max_candidates=3)
    history = "连接之前的历史说明。"
    complete, tail = "现在大家准备开始新的游戏。", "后面的讲话还在继续"
    instance.observe(snapshot(history, 1.0))
    added = instance.observe(snapshot(history + complete + tail, 1.2))
    rewritten = "第一段已经完全换成历史说明。" * 35
    changed = instance.observe(snapshot(rewritten + complete + tail, 1.5))
    assert all(row["historical"] for row in changed)
    assert len(instance._captions) == 3
    assert [row.segment_id for row in instance._captions[-2:]] == [row["segment_id"] for row in added]
    settled = instance.observe(snapshot(rewritten + complete + tail, 2.6))
    assert next(row for row in settled if row["boundary_observed"])["segment_id"] == added[0]["segment_id"]


def test_strong_closed_historical_tail_can_anchor_a_real_new_sentence_after_history_rewrite():
    instance = protocol()
    old_history = "过去第一段历史讲左边。过去第二段历史讲右边。"
    anchor = "这一句是连接之前完整结束的字幕。"
    baseline = instance.observe(snapshot(old_history + anchor, 1.0))
    complete = "现在大家准备开始新的游戏。"
    current = "背景内容已经全部重新改写。" + anchor + complete
    changed = instance.observe(snapshot(current, 1.5))
    assert not any(row["kind"] == "reset" for row in changed)
    added = [row for row in changed if row["kind"] == "append"]
    assert len(added) == 1 and added[0]["text"] == complete and not added[0]["historical"]
    assert added[0]["segment_id"] not in {row["segment_id"] for row in baseline}
    settled = instance.observe(snapshot(current, 3.0))
    assert [row["text"] for row in settled] == [complete]


def test_historical_relaxation_requires_a_strong_anchor_and_does_not_relax_new_sentence_rewrites():
    instance = protocol()
    instance.observe(snapshot("原来的历史字幕介绍角色。", 1.0))
    instance.observe(snapshot("原来的历史字幕介绍角色。好的。", 1.2))
    reset = instance.observe(snapshot("完全不同的历史文字。好的。", 1.5))
    assert reset[0]["kind"] == "reset"
    assert reset[0]["alignment_code"] == "known_gap_rewrite"

    instance = protocol()
    history = "历史字幕完整介绍游戏角色。"
    original, current, tail = "现在先介绍左边的一位角色。", "大家不要攻击此处的友军人物。", "尾段未完"
    instance.observe(snapshot(history, 1.0))
    instance.observe(snapshot(history + original + tail, 1.2))
    reset = instance.observe(snapshot(history + current + tail, 1.5))
    assert reset[0]["kind"] == "reset" and reset[0]["alignment_code"] == "known_gap_rewrite"
    assert all(row["historical"] for row in reset[1:])


def test_strong_anchor_does_not_demarcate_an_unknown_rewritten_incomplete_historical_tail():
    instance = protocol()
    anchor = "这一句是连接之前完整结束的字幕。"
    instance.observe(snapshot(anchor + "上一句这条路还不能前进", 1.0))
    current = anchor + "上一句这条路现在可以前进。接下来还有新内容。"
    events = instance.observe(snapshot(current, 1.5))
    assert events[0]["kind"] == "reset" and events[0]["alignment_code"] == "unknown_tail_addition"
    assert all(row["historical"] for row in events[1:])
    assert instance.observe(snapshot(current, 3.0)) == []


def test_alignment_reset_diagnostics_contain_only_fixed_codes_and_counts():
    instance = protocol()
    instance.observe(snapshot("历史字幕完整结束。", 1.0))
    instance.observe(snapshot("历史字幕完整结束。现在正在介绍左边人物。", 1.2))
    events = instance.observe(snapshot("完全不相关的内容。", 1.5))
    diagnostic = events[0]
    assert diagnostic["alignment_code"] == "no_unique_anchor"
    assert diagnostic["old_sentences"] == 2 and diagnostic["new_sentences"] == 1
    assert diagnostic["unique_anchors"] == 0
    assert "text" not in diagnostic and "segment_id" not in diagnostic


@pytest.mark.parametrize("right", ["后面这一句保持原来的字幕。", "后面这一段还在连续讲话"])
@pytest.mark.parametrize("before,after,expected", [
    ("现在先介绍左边的一位角色。", "大家不要攻击此处的友军人物。", ["大家不要攻击此处的友军人物。"]),
    ("现在先介绍左边的一位角色。然后观察左边的游戏场景。",
     "大家不要攻击此处的友军人物。下一场比赛马上准备开始。",
     ["大家不要攻击此处的友军人物。", "下一场比赛马上准备开始。"]),
])
def test_two_strong_unique_anchors_allow_same_count_arbitrary_known_sentence_retranslations(right, before, after, expected):
    instance = protocol()
    left = "历史字幕完整介绍游戏角色。"
    instance.observe(snapshot(left, 1.0))
    added = instance.observe(snapshot(left + before + right, 1.2))
    instance.observe(snapshot(left + before + right, 2.6))
    changed = instance.observe(snapshot(left + after + right, 2.8))
    assert [row["kind"] for row in changed] == ["revise"] * len(expected)
    assert [row["text"] for row in changed] == expected
    assert [row["segment_id"] for row in changed] == [row["segment_id"] for row in added[:-1]]
    assert all(row["revision"] == 2 and not row["historical"] for row in changed)
    assert instance.observe(snapshot(left + after + right, 3.9)) == []
    settled = instance.observe(snapshot(left + after + right, 4.1))
    assert [row["text"] for row in settled] == expected
    assert all(row["revision"] == 2 for row in settled)
    assert_candidate_policy(changed + settled)


@pytest.mark.parametrize("left,right", [
    ("历史。", "后面这一句保持原来的字幕。"),
    ("历史字幕完整介绍游戏角色。", "尾段未完"),
])
def test_known_retranslation_still_needs_two_strong_anchors_not_merely_two_matches(left, right):
    instance = protocol()
    before, after = "现在先介绍左边的一位角色。", "大家不要攻击此处的友军人物。"
    instance.observe(snapshot(left, 1.0))
    instance.observe(snapshot(left + before + right, 1.2))
    reset = instance.observe(snapshot(left + after + right, 1.5))
    assert reset[0]["kind"] == "reset" and reset[0]["alignment_code"] == "known_gap_rewrite"
    assert all(row["historical"] for row in reset[1:])


@pytest.mark.parametrize("which", ["prefix", "suffix"])
def test_arbitrary_known_prefix_or_suffix_correction_is_not_proved_by_one_strong_anchor(which):
    instance = protocol()
    left, right = "历史字幕完整介绍游戏角色。", "后面这一句保持原来的字幕。"
    before, after = "现在先介绍左边的一位角色。", "大家不要攻击此处的友军人物。"
    instance.observe(snapshot(left, 1.0))
    instance.observe(snapshot(left + before + (right if which == "prefix" else ""), 1.2))
    current = after + right if which == "prefix" else left + after
    reset = instance.observe(snapshot(current, 1.5))
    assert reset[0]["kind"] == "reset" and reset[0]["alignment_code"] == "known_gap_rewrite"
    assert all(row["historical"] for row in reset[1:])


@pytest.mark.parametrize("replacement,code", [
    ("大家不要攻击此处的友军人物。下一场比赛马上准备开始。", "known_gap_count"),
    ("！？", "known_gap_rewrite"),
])
def test_two_strong_anchors_do_not_prove_sentence_count_change_or_meaningless_replacement(replacement, code):
    instance = protocol()
    left, right = "历史字幕完整介绍游戏角色。", "后面这一句保持原来的字幕。"
    instance.observe(snapshot(left, 1.0))
    instance.observe(snapshot(left + "现在先介绍左边的一位角色。" + right, 1.2))
    reset = instance.observe(snapshot(left + replacement + right, 1.5))
    assert reset[0]["kind"] == "reset" and reset[0]["alignment_code"] == code
    assert all(row["historical"] for row in reset[1:])


def test_initial_incomplete_history_cannot_gain_a_new_right_anchor_from_a_later_snapshot():
    instance = protocol()
    left, right = "历史字幕完整介绍游戏角色。", "后面这一句保持原来的字幕。"
    instance.observe(snapshot(left + "现在先介绍左边的一位角色", 1.0))
    current = left + "大家不要攻击此处的友军人物。" + right
    reset = instance.observe(snapshot(current, 1.5))
    assert reset[0]["kind"] == "reset"
    assert all(row["historical"] for row in reset[1:])
    assert instance.observe(snapshot(current, 3.0)) == []


@pytest.mark.parametrize("current", [
    "后面的固定尾句。前面的固定完整句。",
    "历史字幕。前面的固定完整句。插入无法对应的新句。后面的固定尾句。",
    "这是完全不同的内容。",
])
def test_reordered_inserted_or_unrelated_sentences_still_rebase_as_history(current):
    instance = protocol()
    instance.observe(snapshot("历史字幕。", 1.0))
    instance.observe(snapshot("历史字幕。前面的固定完整句。后面的固定尾句。", 1.2))
    events = instance.observe(snapshot(current, 1.5))
    assert events[0]["kind"] == "reset" and events[0]["reason"] == "uncertain_realignment"
    assert all(row["historical"] for row in events[1:])
    assert instance.observe(snapshot(current, 3.0)) == []


def test_repeated_sentence_is_not_a_unique_anchor_for_a_corrected_tail():
    instance = protocol()
    instance.observe(snapshot("历史字幕。", 1.0))
    before = "重复的完整句子。重复的完整句子。后面的讲话暂时不能走"
    current = "重复的完整句子。重复的完整句子。后面的讲话现在可以走"
    instance.observe(snapshot("历史字幕。" + before, 1.2))
    events = instance.observe(snapshot(current, 1.5))
    assert events[0]["kind"] == "reset" and events[0]["reason"] == "uncertain_realignment"
    assert all(row["historical"] for row in events[1:])


def test_rolloff_clipping_a_sentence_does_not_rebase_a_pure_suffix():
    instance = protocol()
    history, complete, tail = "历史字幕。", "这是完整的句子不能重复发送。", "下面继续下一段讲话"
    instance.observe(snapshot(history, 1.0))
    added = instance.observe(snapshot(history + complete + tail, 1.2))
    clipped = complete[5:] + tail
    assert instance.observe(snapshot(clipped, 1.5)) == []
    changed = instance.observe(snapshot(clipped + "继续增长", 2.6))
    assert next(row for row in changed if row["kind"] == "stable_candidate")["segment_id"] == added[0]["segment_id"]
    assert not any(row["kind"] in {"reset", "baseline"} for row in changed)


def test_real_newline_alignment_preserves_spaces_in_newly_appended_words():
    instance = protocol()
    history, changed_history = "历史介绍左边角色", "历史介绍右边角色"
    complete = "这句已经结束"
    instance.observe(snapshot(history + "\n", 1.0))
    # _normalize removes only a trailing empty line, so use an explicit second
    # row to establish the prior line's observed boundary.
    instance.observe(snapshot(history + "\n" + complete + "\n尾段", 1.2))
    changed = instance.observe(snapshot(changed_history + "\n" + complete + "\n尾段继续。下一句 空格保留。", 2.6))
    assert not any(row["kind"] in {"reset", "baseline"} for row in changed)
    assert next(row for row in changed if row["kind"] == "append")["text"] == "下一句 空格保留。"


@pytest.mark.parametrize("text,expected", [
    ("历史第一句.历史第二句.尾段", ["历史第一句.", "历史第二句.", "尾段"]),
    ('他说「这次真的结束.」下一句话.', ['他说「这次真的结束.」', '下一句话.']),
    ("价格是3.5元.下一句话.", ["价格是3.5元.", "下一句话."]),
])
def test_chinese_ascii_period_is_a_boundary_and_keeps_quotes_and_decimals(text, expected):
    instance = protocol()
    events = instance.observe(snapshot(text, 1.0))
    assert [row["text"] for row in events] == expected
    assert all(row["historical"] for row in events)


@pytest.mark.parametrize("text", [
    "下一句价格3.", "下一句价格3.5", "下一句停顿...", "下一句停顿…",
    "欢迎 Mr. 王先生", "欢迎 Dr. 王先生", "欢迎 U.S.A. 的朋友", "请看 example.com 的页面",
])
def test_ascii_period_does_not_promote_decimal_abbreviation_domain_or_ellipsis(text):
    instance = protocol(allow_latin_names=True)
    instance.observe(snapshot("历史字幕。", 1.0))
    added = instance.observe(snapshot("历史字幕。" + text, 1.2))
    assert len(added) == 1 and added[0]["boundary_observed"] is False
    settled = instance.observe(snapshot("历史字幕。" + text, 2.6))
    assert len(settled) == 1 and settled[0]["boundary_observed"] is False
    assert_candidate_policy(settled)


def test_late_ascii_period_completes_existing_row_instead_of_creating_a_punctuation_row():
    instance = protocol()
    instance.observe(snapshot("历史字幕.", 1.0))
    added = instance.observe(snapshot("历史字幕.新句已经说完", 1.2))[0]
    changed = instance.observe(snapshot("历史字幕.新句已经说完.", 1.5))[0]
    assert changed["kind"] == "revise" and changed["segment_id"] == added["segment_id"]
    assert changed["boundary_observed"] and changed["text"] == "新句已经说完."
    settled = instance.observe(snapshot("历史字幕.新句已经说完.", 2.8))
    assert [row["segment_id"] for row in settled] == [added["segment_id"]]


def test_decimal_growing_across_snapshots_never_marks_the_digit_period_as_a_boundary():
    instance = protocol()
    instance.observe(snapshot("历史字幕。", 1.0))
    added = instance.observe(snapshot("历史字幕。价格是3", 1.2))[0]
    point = instance.observe(snapshot("历史字幕。价格是3.", 1.3))[0]
    fraction = instance.observe(snapshot("历史字幕。价格是3.5元。", 1.5))[0]
    assert point["segment_id"] == fraction["segment_id"] == added["segment_id"]
    assert point["boundary_observed"] is False and fraction["boundary_observed"] is True
    assert fraction["text"] == "价格是3.5元。"


def test_complete_old_sentence_settles_while_active_sentence_is_corrected():
    instance = protocol()
    baseline = "连接前的历史字幕。"
    complete = "第一句已经说完并且不会改变。"
    instance.observe(snapshot(baseline, 1.0))
    first = instance.observe(snapshot(baseline + complete + "下一句攻击左边", 1.2))
    complete_id, active_id = first[0]["segment_id"], first[1]["segment_id"]
    events = instance.observe(snapshot(baseline + complete + "下一句攻击右边", 2.6))
    assert events[0]["kind"] == "revise" and events[0]["segment_id"] == active_id
    settled = [event for event in events if event["kind"] == "stable_candidate"]
    assert [event["segment_id"] for event in settled] == [complete_id]
    assert settled[0]["text"] == complete
    assert_candidate_policy(events)


@pytest.mark.parametrize("added", ["", "现在继续增加"])
def test_viewport_rolloff_can_settle_existing_sentence_without_a_new_baseline(added):
    instance = protocol()
    baseline = "连接前的历史字幕。"
    complete = "这一句已经结束等待稳定输出。"
    partial = "后面还有持续讲话"
    instance.observe(snapshot(baseline, 1.0))
    first = instance.observe(snapshot(baseline + complete, 1.2))
    instance.observe(snapshot(baseline + complete + partial, 1.5))
    events = instance.observe(snapshot(complete + partial + added, 2.6))
    settled = [event for event in events if event["kind"] == "stable_candidate" and event["boundary_observed"]]
    assert [event["segment_id"] for event in settled] == [first[0]["segment_id"]]
    assert settled[0]["text"] == complete
    assert not any(event["kind"] in {"baseline", "reset"} for event in events)
    assert_candidate_policy(events)


def test_first_visible_history_can_display_but_never_becomes_new_speech():
    instance = protocol()
    text = "之前的两句话。\n第二句话。"
    first = instance.observe(snapshot(text))
    assert [event["kind"] for event in first] == ["baseline", "baseline"]
    assert [event["text"] for event in first] == ["之前的两句话。", "第二句话。"]
    assert len({event["segment_id"] for event in first}) == 2
    assert all(event["historical"] is True and event["inferred_stable"] is False for event in first)
    assert instance.observe(snapshot(text, 2.5)) == []
    assert instance.settle(3.0, bridge_generation=GENERATION) == []
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None
    assert_candidate_policy(first)


@pytest.mark.parametrize("text,expected,boundaries", [
    ("第一句。第二句！第三句？尾段", ["第一句。", "第二句！", "第三句？", "尾段"], [True, True, True, False]),
    ("首行\n第二行\n第三行", ["首行", "第二行", "第三行"], [True, True, False]),
    ("你好！？接着说。", ["你好！？", "接着说。"], [True, True]),
    ("价格3.5元, 还不错;继续。下一句", ["价格3.5元, 还不错;继续。", "下一句"], [True, False]),
])
def test_baseline_uses_sentence_endings_or_actual_newlines_not_visual_wraps(text, expected, boundaries):
    instance = protocol()
    events = instance.observe(snapshot(text))
    assert [event["text"] for event in events] == expected
    assert [event["boundary_observed"] for event in events] == boundaries
    assert len({event["segment_id"] for event in events}) == len(expected)
    assert all(event["kind"] == "baseline" and event["historical"] for event in events)
    assert instance.observe(snapshot(text, 2.5)) == []
    assert instance.settle(3.0, bridge_generation=GENERATION) == []
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None
    assert_candidate_policy(events)


def test_long_baseline_is_split_without_lost_characters_and_keeps_only_bounded_history_state():
    instance = protocol(max_caption_characters=5, max_candidates=2)
    text = "一二三四五六七八九十甲乙丙丁戊。"
    events = instance.observe(snapshot(text))
    assert "".join(event["text"] for event in events) == text
    assert all(0 < len(event["text"]) <= 5 for event in events)
    assert len({event["segment_id"] for event in events}) == 4
    assert len(instance._captions) == 2
    assert [caption.segment_id for caption in instance._captions] == [event["segment_id"] for event in events[-2:]]
    assert all(event["historical"] for event in events)
    assert instance.stable_aggregate(2.5, bridge_generation=GENERATION) is None
    new = instance.observe(snapshot(text + "下一句。", 2.0))
    assert [event["text"] for event in new] == ["下一句。"]
    aggregate = instance.stable_aggregate(3.5, bridge_generation=GENERATION)
    assert aggregate["text"] == "下一句。"
    assert [row["segment_id"] for row in aggregate["candidates"]] == [new[0]["segment_id"]]
    assert_candidate_policy(events + new + [aggregate])


def test_eighty_character_display_limit_prefers_sentence_boundaries_and_bounds_unpunctuated_content():
    instance = protocol(max_caption_characters=80)
    long_sentence = "中文" * 90 + "！"
    baseline = "短句。" + long_sentence
    rows = instance.observe(snapshot(baseline))
    assert rows[0]["text"] == "短句。"
    assert [len(row["text"]) for row in rows[1:]] == [80, 80, 21]
    assert "".join(row["text"] for row in rows) == baseline
    assert all(row["historical"] and len(row["text"]) <= 80 for row in rows)
    addition = "后续" * 85
    new = instance.observe(snapshot(baseline + addition, 1.5))
    assert [len(row["text"]) for row in new] == [80, 80, 10]
    assert "".join(row["text"] for row in new) == addition
    assert all(not row["historical"] for row in new)
    assert instance.observe(snapshot(baseline + addition, 1.6)) == []
    assert_candidate_policy(rows + new)


def test_rolloff_after_multiline_baseline_preserves_words_and_does_not_repeat_visible_history():
    instance = protocol()
    baseline = instance.observe(snapshot("历史第一句。\n历史第二句！"))
    assert instance.observe(snapshot("历史第二句！", 1.2)) == []
    new = instance.observe(snapshot("历史第二句！第三句正在继续", 1.5))
    assert [event["text"] for event in new] == ["第三句正在继续"]
    assert new[0]["segment_id"] not in {event["segment_id"] for event in baseline}
    assert instance.observe(snapshot("历史第二句！第三句正在继续", 1.7)) == []
    correction = instance.observe(snapshot("历史第二句！第三句正在暂停", 1.8))
    assert correction[0]["kind"] == "revise"
    assert correction[0]["segment_id"] == new[0]["segment_id"]
    assert correction[0]["text"] == "第三句正在暂停"
    assert_candidate_policy(baseline + new + correction)


def test_correcting_historical_baseline_resets_old_rows_and_splits_replacement_once():
    instance = protocol()
    old = instance.observe(snapshot("先前的内容。今天天气很好"))
    changed = "先前的内容。今天天气很冷！所以留在家里。"
    result = instance.observe(snapshot(changed, 1.5))
    assert [event["kind"] for event in result] == ["reset", "baseline", "baseline", "baseline"]
    assert result[0]["reason"] == "uncertain_realignment"
    assert [event["text"] for event in result[1:]] == ["先前的内容。", "今天天气很冷！", "所以留在家里。"]
    assert "".join(event["text"] for event in result[1:]) == changed
    assert {event["segment_id"] for event in old}.isdisjoint(event["segment_id"] for event in result[1:])
    assert all(event["historical"] for event in result[1:])
    assert instance.observe(snapshot(changed, 2.0)) == []
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None


def test_reconnected_multisentence_snapshot_remains_historical_and_is_not_repeated():
    instance, _, _ = growing_caption()
    instance.observe(snapshot("", 2.0, source_epoch=5, connected=False))
    text = "重新连接后的第一句。\n第二句！"
    events = instance.observe(snapshot(text, 2.5, source_epoch=5))
    assert [event["text"] for event in events] == ["重新连接后的第一句。", "第二句！"]
    assert all(event["historical"] and event["source_epoch"] == 5 for event in events)
    assert instance.observe(snapshot(text, 3.0, source_epoch=5)) == []
    assert instance.stable_aggregate(4.0, bridge_generation=GENERATION) is None


def test_partial_historical_sentence_grows_in_place_and_next_sentence_is_a_new_row():
    instance = protocol()
    first = instance.observe(snapshot("你好。谢谢你"))
    changed = instance.observe(snapshot("你好。谢谢你的留言。下一句！", 1.5))
    assert [row["kind"] for row in changed] == ["revise", "append"]
    assert changed[0]["segment_id"] == first[-1]["segment_id"]
    assert changed[0]["text"] == "谢谢你的留言。" and changed[0]["historical"]
    assert changed[1]["text"] == "下一句！" and not changed[1]["historical"]
    assert changed[1]["segment_id"] not in {row["segment_id"] for row in first}
    assert instance.observe(snapshot("你好。谢谢你的留言。下一句！", 1.7)) == []
    stable = instance.settle(3.0, bridge_generation=GENERATION)
    assert [row["segment_id"] for row in stable] == [changed[1]["segment_id"]]
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION)["text"] == "下一句！"
    assert_candidate_policy(first + changed + stable)


def test_growing_historical_sentence_keeps_historical_policy_across_character_limit():
    instance = protocol(max_caption_characters=5)
    first = instance.observe(snapshot("历史句"))
    result = instance.observe(snapshot("历史句不断继续增长。新句。", 1.5))
    history_rows = [row for row in result if row["historical"]]
    assert history_rows[0]["segment_id"] == first[0]["segment_id"]
    assert "".join(row["text"] for row in history_rows) == "历史句不断继续增长。"
    assert all(len(row["text"]) <= 5 for row in result)
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION)["text"] == "新句。"


def test_late_sentence_punctuation_updates_historical_row_without_losing_it():
    instance = protocol()
    initial = instance.observe(snapshot("今天天气很好"))[0]
    punctuation = instance.observe(snapshot("今天天气很好。", 1.5))
    assert len(punctuation) == 1 and punctuation[0]["kind"] == "revise"
    assert punctuation[0]["segment_id"] == initial["segment_id"]
    assert punctuation[0]["text"] == "今天天气很好。" and punctuation[0]["historical"]
    next_row = instance.observe(snapshot("今天天气很好。接下来玩游戏", 2.0))[0]
    assert next_row["text"] == "接下来玩游戏" and next_row["segment_id"] != initial["segment_id"]
    assert instance.stable_aggregate(3.5, bridge_generation=GENERATION)["text"] == "接下来玩游戏"


@pytest.mark.parametrize("historical", [False, True])
def test_same_anchor_correction_precedes_suffix_overlap_that_repeats_the_first_sentence(historical):
    instance = protocol()
    initial = "你好。第二句又说你好。"
    if historical:
        first = instance.observe(snapshot(initial))[-1]
    else:
        instance.observe(snapshot("你好。"))
        first = instance.observe(snapshot(initial, 1.2))[0]
    corrected = instance.observe(snapshot("你好。第二句改说再见。", 1.5))
    assert len(corrected) == 1 and corrected[0]["kind"] == "revise"
    assert corrected[0]["segment_id"] == first["segment_id"]
    assert corrected[0]["text"] == "第二句改说再见。"
    assert corrected[0]["historical"] is historical
    aggregate = instance.stable_aggregate(3.0, bridge_generation=GENERATION)
    if historical:
        assert aggregate is None
    else:
        assert aggregate["text"] == "第二句改说再见。"


def test_ascii_sentence_endings_keep_closing_quotes_and_brackets_with_the_sentence():
    text = '他说：“真的吗?!”（下一句!）再试一次?价格3.5元。'
    instance = protocol()
    result = instance.observe(snapshot(text))
    assert [row["text"] for row in result] == ['他说：“真的吗?!”', '（下一句!）', '再试一次?', '价格3.5元。']
    assert all(row["boundary_observed"] for row in result)
    assert "".join(row["text"] for row in result) == text


@pytest.mark.parametrize("historical", [True, False])
def test_opt_in_replacement_placeholder_is_preserved_then_corrected_in_the_same_row(historical):
    placeholder = "中文暂时�修订"
    assert protocol().observe(snapshot(placeholder))[0]["reason"] == "non_chinese_output"
    instance = protocol(allow_latin_names=True)
    if historical:
        first = instance.observe(snapshot(placeholder))[0]
        current = "中文暂时已修订"
    else:
        instance.observe(snapshot("历史。"))
        first = instance.observe(snapshot("历史。" + placeholder, 1.2))[0]
        current = "历史。中文暂时已修订"
    assert "�" in first["text"]
    fixed = instance.observe(snapshot(current, 1.5))
    assert len(fixed) == 1 and fixed[0]["kind"] == "revise"
    assert fixed[0]["segment_id"] == first["segment_id"]
    assert fixed[0]["text"] == "中文暂时已修订"
    assert fixed[0]["historical"] is historical
    assert_candidate_policy([first, *fixed])


@pytest.mark.parametrize("name", ["Aya", "Ela Som", "José", "E\u0301la"])
def test_opt_in_latin_names_keep_chinese_snapshot_and_preserve_original_spelling(name):
    text = f"谢谢{name}的留言。今天很开心！"
    assert protocol().observe(snapshot(text))[0]["reason"] == "non_chinese_output"
    instance = protocol(allow_latin_names=True)
    rows = instance.observe(snapshot(text))
    assert [row["text"] for row in rows] == [f"谢谢{name}的留言。", "今天很开心！"]
    assert all(row["historical"] and not row["allow_push"] and not row["is_official_final"] for row in rows)
    assert instance.stable_aggregate(2.5, bridge_generation=GENERATION) is None
    # Shared model and publication validation remains strict despite this opt-in.
    from fanyiji.translation_output import is_chinese_output
    assert not is_chinese_output(text)


@pytest.mark.parametrize("text", [
    "Aya Ela Som", "This is entirely English.", "123.4！？", "これは日本語です", "今日はゲームをします 中文译文",
    "谢谢Ayaちゃん的留言", "中文夹杂한국어", "中文夹杂Привет", "中文夹杂\u200b不可见字符",
])
def test_latin_display_opt_in_still_rejects_non_chinese_or_other_scripts(text):
    result = protocol(allow_latin_names=True).observe(snapshot(text))
    assert result[0]["kind"] == "rejected" and result[0]["reason"] == "non_chinese_output"
    assert "text" not in result[0]


@pytest.mark.parametrize("text", ["谢谢Aya<asr_text>留言", "语言 None<asr_text>", "谢谢Aya\x00留言"])
def test_latin_display_opt_in_cannot_bypass_asr_protocol_or_control_character_guards(text):
    result = protocol(allow_latin_names=True).observe(snapshot(text))
    assert result[0]["reason"] == "protocol_pollution"
    assert "text" not in result[0]


def test_same_caption_grows_and_is_corrected_without_a_new_segment_id():
    instance, _, initial = growing_caption()
    extended = instance.observe(snapshot("上一句。今天天气很好 适合出门", 1.8))
    corrected = instance.observe(snapshot("上一句。今天天气很好 不适合出门", 2.0))
    assert [event["kind"] for event in extended + corrected] == ["revise", "revise"]
    assert extended[0]["text"] == "今天天气很好 适合出门"
    assert corrected[0]["text"] == "今天天气很好 不适合出门"
    assert {event["segment_id"] for event in [initial, *extended, *corrected]} == {initial["segment_id"]}
    assert [initial["revision"], extended[0]["revision"], corrected[0]["revision"]] == [1, 2, 3]
    assert_candidate_policy([initial, *extended, *corrected])


def test_incremental_word_separator_is_preserved():
    instance = protocol()
    instance.observe(snapshot("历史。"))
    first = instance.observe(snapshot("历史。现在开始", 1.2))[0]
    second = instance.observe(snapshot("历史。现在开始 下一关", 1.5))[0]
    third = instance.observe(snapshot("历史。现在开始 下一关 试试看", 1.8))[0]
    assert second["text"] == "现在开始 下一关"
    assert third["text"] == "现在开始 下一关 试试看"
    assert second["segment_id"] == third["segment_id"] == first["segment_id"]


def test_spaces_after_observed_boundary_do_not_create_indented_new_row():
    instance = protocol()
    instance.observe(snapshot("历史。"))
    rows = instance.observe(snapshot("历史。 第一条。  第二条！", 1.3))
    assert [event["text"] for event in rows] == ["第一条。", "第二条！"]
    assert all(event["boundary_observed"] for event in rows)
    assert len({event["segment_id"] for event in rows}) == 2


def test_newline_only_delta_closes_current_caption_before_next_row():
    instance = protocol()
    instance.observe(snapshot("历史。"))
    first = instance.observe(snapshot("历史。第一段话", 1.2))[0]
    result = instance.observe(snapshot("历史。第一段话\n第二段话", 1.5))
    assert [event["kind"] for event in result] == ["revise", "append"]
    assert result[0]["text"] == "第一段话"
    assert result[0]["segment_id"] == first["segment_id"]
    assert result[0]["boundary_observed"] is True
    assert result[0]["revision"] == 2
    assert result[1]["text"] == "第二段话"
    assert result[1]["segment_id"] != first["segment_id"]


def test_decimal_and_western_punctuation_are_not_caption_boundaries():
    instance = protocol()
    instance.observe(snapshot("历史。"))
    rows = instance.observe(snapshot("历史。价格是3.5元, 还不错;继续。下一句？", 1.5))
    assert [row["text"] for row in rows] == ["价格是3.5元, 还不错;继续。", "下一句？"]


def test_rolling_viewport_suffix_does_not_repeat_old_words():
    instance = protocol()
    instance.observe(snapshot("历史。"))
    first = instance.observe(snapshot("历史。第一段话。", 1.2))[0]
    second = instance.observe(snapshot("第一段话。第二段话。", 1.5))[0]
    assert second["kind"] == "append"
    assert second["text"] == "第二段话。"
    assert second["segment_id"] != first["segment_id"]
    assert instance.observe(snapshot("第二段话。", 1.8)) == []
    aggregate = instance.stable_aggregate(3.0, bridge_generation=GENERATION)
    assert aggregate["text"] == "第一段话。 第二段话。"
    assert len(aggregate["candidates"]) == 2
    assert_candidate_policy([aggregate])


def test_rolloff_during_partial_sentence_preserves_original_candidate():
    instance, _, first = growing_caption()
    assert instance.observe(snapshot("天气很好", 1.8)) == []
    revised = instance.observe(snapshot("天气很好 继续出门", 2.0))[0]
    assert revised["segment_id"] == first["segment_id"]
    assert revised["text"] == "今天天气很好 继续出门"


def test_uncertain_realignment_shows_only_baseline_without_republishing_block():
    instance, _, initial = growing_caption()
    result = instance.observe(snapshot("完全不同的另一段内容。", 2.0))
    assert [event["kind"] for event in result] == ["reset", "baseline"]
    assert all(event["reason"] == "uncertain_realignment" for event in result)
    assert result[1]["historical"] is True
    assert result[1]["segment_id"] != initial["segment_id"]
    assert instance.stable_aggregate(3.5, bridge_generation=GENERATION) is None
    assert_candidate_policy(result)


def test_stability_is_inferred_once_per_revision_and_later_changes_invalidate_it():
    instance, _, first = growing_caption()
    assert instance.settle(2.6, bridge_generation=GENERATION) == []
    stable = instance.settle(2.8, bridge_generation=GENERATION)
    assert [event["kind"] for event in stable] == ["stable_candidate"]
    assert stable[0]["segment_id"] == first["segment_id"]
    assert stable[0]["inferred_stable"] is True
    assert instance.settle(3.0, bridge_generation=GENERATION) == []
    corrected = instance.observe(snapshot("上一句。今天天气很冷", 3.2))[0]
    assert corrected["segment_id"] == first["segment_id"]
    assert corrected["revision"] == 2
    assert instance.stable_aggregate(3.6, bridge_generation=GENERATION) is None
    stable_again = instance.settle(4.5, bridge_generation=GENERATION)
    assert stable_again[0]["revision"] == 2
    aggregate = instance.stable_aggregate(4.5, bridge_generation=GENERATION)
    assert aggregate["candidates"] == [{"segment_id": first["segment_id"], "revision": 2,
                                        "text": "今天天气很冷", "boundary_observed": False}]
    assert_candidate_policy(stable + stable_again + [aggregate])


def test_repeated_snapshots_keep_candidate_fresh_without_changing_revision():
    instance, _, first = growing_caption()
    result = instance.observe(snapshot("上一句。今天天气很好", 2.8))
    assert result[0]["kind"] == "stable_candidate"
    assert result[0]["revision"] == first["revision"]
    assert instance.observe(snapshot("上一句。今天天气很好", 5.0)) == []
    assert instance.stable_aggregate(5.1, bridge_generation=GENERATION)["text"] == first["text"]


@pytest.mark.parametrize("now,generation", [
    (1.0, GENERATION), (9.0, GENERATION), (math.nan, GENERATION),
    (math.inf, GENERATION), (True, GENERATION), ("3.0", GENERATION),
    (3.0, GENERATION - 1), (3.0, GENERATION + 1),
])
def test_candidate_stability_rejects_stale_clock_and_generation(now, generation):
    instance, _, _ = growing_caption()
    assert instance.settle(now, bridge_generation=generation) == []
    assert instance.stable_aggregate(now, bridge_generation=generation) is None


def test_boolean_generation_cannot_match_integer_bridge_generation():
    instance = CaptionDeltaProtocol(1)
    instance.observe(snapshot("历史。", bridge_generation=1))
    instance.observe(snapshot("历史。当前内容", 1.5, bridge_generation=1))
    assert instance.observe(snapshot("历史。当前内容追加", 2.0, bridge_generation=True)) == []
    assert instance.settle(3.0, bridge_generation=True) == []
    assert instance.stable_aggregate(3.0, bridge_generation=True) is None


def test_disconnect_reset_uses_incoming_epoch_fence_and_reconnect_is_history():
    instance, _, first = growing_caption()
    result = instance.observe(snapshot("", 2.0, connected=False, window_id="", source_epoch=5))
    assert [event["kind"] for event in result] == ["reset"]
    assert result[0]["reason"] == "disconnected"
    assert result[0]["source_epoch"] == 5
    assert result[0]["window_id"] == "caption-101"
    assert result[0]["room"] == "1698225"
    assert result[0]["observed_at"] == 2.0
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None
    assert instance.observe(snapshot("上一句。今天天气很好迟到文本", 2.2, source_epoch=4)) == []
    assert instance.observe(snapshot("", 2.3, connected=False, source_epoch=5)) == []
    reconnected = instance.observe(snapshot("当前已有的字幕", 2.5, source_epoch=5))
    assert [event["kind"] for event in reconnected] == ["baseline"]
    assert reconnected[0]["historical"] is True
    assert reconnected[0]["source_epoch"] == 5
    assert reconnected[0]["segment_id"] != first["segment_id"]
    assert instance.stable_aggregate(4.0, bridge_generation=GENERATION) is None
    assert_candidate_policy(result + reconnected)


def test_late_disconnect_from_old_epoch_cannot_clear_new_source():
    instance, _, _ = growing_caption()
    instance.observe(snapshot("新的直播来源。", 2.0, source_epoch=5))
    new = instance.observe(snapshot("新的直播来源。新来源的讲话", 2.2, source_epoch=5))[0]
    assert instance.observe(snapshot("", 2.5, connected=False, source_epoch=4)) == []
    aggregate = instance.stable_aggregate(3.5, bridge_generation=GENERATION)
    assert aggregate["source_epoch"] == 5
    assert aggregate["text"] == new["text"]


def test_stable_aggregate_is_read_only_and_repeated_calls_have_same_revision_references():
    instance, _, first = growing_caption()
    one = instance.stable_aggregate(3.0, bridge_generation=GENERATION)
    two = instance.stable_aggregate(3.1, bridge_generation=GENERATION)
    assert one["text"] == two["text"] == first["text"]
    assert one["candidates"] == two["candidates"]
    assert one["allow_push"] is two["allow_push"] is False
    assert one["is_official_final"] is two["is_official_final"] is False


@pytest.mark.parametrize("changed", [
    {"window_id": "caption-202"}, {"source_epoch": 5}, {"room": "21399689"},
    {"source_lang": "en"}, {"target_lang": "zh-CN"}, {"target_lang": "ja"},
])
def test_source_identity_switch_cannot_carry_forward_text_or_stable_rows(changed):
    instance, _, old = growing_caption()
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is not None
    switched = instance.observe(snapshot("上一句。今天天气很好 这次是新来源", 3.1, **changed))
    assert [event["kind"] for event in switched] == ["reset", "baseline", "baseline"]
    assert switched[0]["reason"] == "source_changed"
    assert all(event["historical"] is True for event in switched[1:])
    assert switched[1]["segment_id"] != old["segment_id"]
    assert instance.stable_aggregate(4.4, bridge_generation=GENERATION) is None
    for key, value in changed.items():
        assert all(event[key] == value for event in switched)


def test_old_generation_and_out_of_order_snapshot_cannot_restore_old_text():
    instance, _, first = growing_caption()
    assert instance.observe(snapshot("错误的旧结果", 1.4)) == []
    assert instance.observe(snapshot("错误的旧代结果", 2.0, bridge_generation=GENERATION - 1)) == []
    aggregate = instance.stable_aggregate(3.0, bridge_generation=GENERATION)
    assert aggregate["text"] == first["text"]


def test_empty_caption_clears_visible_candidates_and_next_block_is_baseline():
    instance, _, _ = growing_caption()
    result = instance.observe(snapshot("  \r\n\t ", 2.0))
    assert [event["kind"] for event in result] == ["reset"]
    assert result[0]["reason"] == "empty_caption"
    assert instance.observe(snapshot("", 2.1)) == []
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None
    later = instance.observe(snapshot("后来才出现的内容", 2.3))
    assert later[0]["kind"] == "baseline"
    assert later[0]["historical"] is True


def test_cancel_and_resume_require_new_generation_with_clean_source():
    instance, _, first = growing_caption()
    instance.cancel()
    assert instance.observe(snapshot("上一句。今天天气很好迟到文本", 2.0)) == []
    assert instance.settle(3.0, bridge_generation=GENERATION) == []
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None
    instance.reset_generation(GENERATION + 1)
    assert instance.observe(snapshot("旧代结果", 3.0)) == []
    resumed = instance.observe(snapshot("新代初始字幕", 0.5, source_epoch=0,
                                        bridge_generation=GENERATION + 1))
    assert resumed[0]["kind"] == "baseline"
    assert resumed[0]["segment_id"] != first["segment_id"]
    assert resumed[0]["bridge_generation"] == GENERATION + 1
    assert resumed[0]["source_epoch"] == 0


@pytest.mark.parametrize("value", [GENERATION - 1, GENERATION, True, 8.0, "8"])
def test_reset_generation_rejects_non_new_integer_values(value):
    with pytest.raises(ValueError, match="newer generation"):
        protocol().reset_generation(value)


@pytest.mark.parametrize("bad_text,reason", [
    ("正常文字<asr_text>污染</asr_text>", "protocol_pollution"),
    ("语言 None<asr_text>", "protocol_pollution"),
    ("正常文字\x00污染", "protocol_pollution"),
    ("正常文字\x1b污染", "protocol_pollution"),
    ("これは日本語です", "non_chinese_output"),
    ("中文夹杂English", "non_chinese_output"),
    ("中文夹杂レレン", "non_chinese_output"),
    (None, "snapshot_limit"),
])
def test_invalid_caption_is_rejected_without_exposing_text_or_stable_state(bad_text, reason):
    instance, _, _ = growing_caption()
    rejected = instance.observe(snapshot(bad_text, 2.0))
    assert [event["kind"] for event in rejected] == ["rejected"]
    assert rejected[0]["reason"] == reason
    assert "text" not in rejected[0]
    assert "segment_id" not in rejected[0]
    assert instance.stable_aggregate(3.0, bridge_generation=GENERATION) is None
    assert_candidate_policy(rejected)
    valid = instance.observe(snapshot("合法的新内容", 2.5))
    assert valid[0]["kind"] == "baseline"
    assert valid[0]["historical"] is True


def test_japanese_target_can_display_japanese_without_bypassing_protocol_guard():
    instance = protocol()
    baseline = instance.observe(snapshot("以前の字幕。", target_lang="ja"))
    new = instance.observe(snapshot("以前の字幕。これは新しい文章です", 1.5, target_lang="ja"))
    assert baseline[0]["kind"] == "baseline"
    assert new[0]["text"] == "これは新しい文章です"
    rejected = instance.observe(snapshot("<asr_text>これは新しい文章です", 2.0, target_lang="ja"))
    assert rejected[0]["reason"] == "protocol_pollution"


@pytest.mark.parametrize("changes,reason", [
    ({"source_epoch": True}, "invalid_snapshot"),
    ({"source_epoch": -1}, "invalid_snapshot"),
    ({"observed_at": math.nan}, "invalid_snapshot"),
    ({"observed_at": True}, "invalid_snapshot"),
    ({"connected": 1}, "invalid_snapshot"),
    ({"window_id": ""}, "invalid_identity"),
    ({"window_id": 0}, "invalid_identity"),
    ({"window_id": True}, "invalid_identity"),
    ({"source_lang": "a" * 33}, "invalid_identity"),
    ({"target_lang": "en"}, "invalid_identity"),
    ({"room": "1" * 129}, "invalid_identity"),
])
def test_malformed_snapshot_metadata_is_rejected(changes, reason):
    result = protocol().observe(snapshot("当前字幕", **changes))
    assert result[0]["kind"] == "rejected"
    assert result[0]["reason"] == reason
    assert "text" not in result[0]


def test_non_snapshot_input_is_a_programming_error():
    with pytest.raises(TypeError, match="CaptionSnapshot"):
        protocol().observe({"text": "不是已校验的输入"})


def test_snapshot_limit_prevents_large_blocks_and_recovers_as_new_baseline():
    instance = protocol(max_snapshot_characters=8)
    first = instance.observe(snapshot("历史"))
    assert first[0]["kind"] == "baseline"
    rejected = instance.observe(snapshot("过长字幕" * 3, 2.0))
    assert rejected[0]["reason"] == "snapshot_limit"
    assert "text" not in rejected[0]
    assert instance.observe(snapshot("新基线", 2.5))[0]["historical"] is True


def test_long_new_content_is_bounded_and_aggregation_retains_latest_candidates():
    instance = protocol(max_caption_characters=5, max_candidates=2)
    instance.observe(snapshot("历史。"))
    content = "一二三四五六七八九十甲乙丙丁戊"
    rows = instance.observe(snapshot("历史。" + content, 1.5))
    assert [event["text"] for event in rows] == ["一二三四五", "六七八九十", "甲乙丙丁戊"]
    assert all(len(event["text"]) <= 5 for event in rows)
    assert len({event["segment_id"] for event in rows}) == 3
    aggregate = instance.stable_aggregate(3.0, bridge_generation=GENERATION)
    assert aggregate["text"] == "六七八九十 甲乙丙丁戊"
    assert [row["segment_id"] for row in aggregate["candidates"]] == [event["segment_id"] for event in rows[-2:]]


def test_event_identity_and_source_timestamps_are_complete():
    instance = protocol()
    result = instance.observe(snapshot("历史。", window_id=101))
    result += instance.observe(snapshot("历史。当前字幕", 1.5, window_id=101))
    result += instance.settle(3.0, bridge_generation=GENERATION)
    result += [instance.stable_aggregate(3.0, bridge_generation=GENERATION)]
    for event in result:
        assert event["window_id"] == "101"
        assert event["source_epoch"] == 4
        assert event["source_lang"] == "ja"
        assert event["target_lang"] == "zh"
        assert event["room"] == "1698225"
        assert event["observed_at"] in (1.0, 1.5)
    assert_candidate_policy(result)


@pytest.mark.parametrize("options", [
    {"stable_seconds": 0}, {"stable_seconds": math.nan}, {"stable_seconds": True},
    {"freshness_seconds": math.inf}, {"freshness_seconds": True},
    {"stable_seconds": 2.0, "freshness_seconds": 2.0},
    {"max_snapshot_characters": 0}, {"max_caption_characters": 0},
    {"max_candidates": 0}, {"min_overlap_characters": 0},
    {"max_candidates": True}, {"max_caption_characters": 4.5},
    {"allow_latin_names": 1}, {"allow_latin_names": "true"}, {"allow_latin_names": None},
])
def test_invalid_protocol_limits_are_rejected_before_any_stream_is_processed(options):
    with pytest.raises(ValueError, match="invalid caption protocol limits"):
        protocol(**options)


def rolling_protocol(**options):
    return protocol(rolling_publication=True, max_caption_characters=4000, **options)


def stable_events(events):
    return [event for event in events if event["kind"] == "stable_candidate"]


def test_explicit_initial_completed_sentences_publish_individually_after_stability():
    instance = rolling_protocol(include_initial_completed=True)
    text = "你还好吗。逃跑。母亲。"
    first = instance.observe(snapshot(text))
    assert [row["text"] for row in first] == ["你还好吗。", "逃跑。", "母亲。"]
    assert all(not row["historical"] for row in first)
    assert len({row["segment_id"] for row in first}) == 3
    assert stable_events(instance.observe(snapshot(text, 2.0))) == []
    settled = instance.observe(snapshot(text, 2.3))
    assert [row["segment_id"] for row in settled] == [row["segment_id"] for row in first]
    assert all(row["boundary_observed"] for row in settled)
    assert instance.observe(snapshot(text, 2.5)) == []
    assert_candidate_policy(first + settled)


def test_explicit_initial_partial_waits_for_real_boundary_and_keeps_its_id():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("大家晚上好"))[0]
    assert not first["historical"] and not first["boundary_observed"]
    assert instance.observe(snapshot("大家晚上好", 2.5)) == []
    completed = instance.observe(snapshot("大家晚上好。下一句正在说", 2.6))
    assert completed[0]["kind"] == "revise" and completed[0]["segment_id"] == first["segment_id"]
    assert completed[0]["boundary_observed"]
    settled = stable_events(instance.observe(snapshot("大家晚上好。下一句正在说", 3.9)))
    assert [row["segment_id"] for row in settled] == [first["segment_id"]]


@pytest.mark.parametrize("changes", [
    {"window_id": "other-caption"}, {"source_epoch": 5}, {"room": "21399689"},
    {"source_lang": "en"}, {"target_lang": "ja"},
])
def test_initial_publication_permission_never_rearms_on_source_identity_change(changes):
    instance = rolling_protocol(include_initial_completed=True)
    instance.observe(snapshot("首次明确开启时显示这一句。"))
    changed = instance.observe(snapshot("切换后已经显示的旧字幕。", 1.5, **changes))
    assert changed[0]["kind"] == "reset"
    assert all(row["historical"] for row in changed if row["kind"] == "baseline")
    assert stable_events(instance.observe(snapshot("切换后已经显示的旧字幕。", 3.0, **changes))) == []


def test_disconnect_and_new_generation_do_not_republish_initial_view():
    instance = rolling_protocol(include_initial_completed=True)
    instance.observe(snapshot("首次开启时这一句可以推送。"))
    instance.observe(snapshot("", 1.5, connected=False, source_epoch=5))
    again = instance.observe(snapshot("恢复后可见的旧字幕。", 2.0, source_epoch=5))
    assert all(row["historical"] for row in again)
    assert stable_events(instance.observe(snapshot("恢复后可见的旧字幕。", 3.5, source_epoch=5))) == []
    instance.reset_generation(GENERATION + 1)
    newer = instance.observe(snapshot("新代际恢复后的旧字幕。", 4.0, bridge_generation=GENERATION + 1))
    assert all(row["historical"] for row in newer)


def test_rolling_without_initial_permission_keeps_baseline_history_but_admits_new_tail():
    instance = rolling_protocol()
    initial = instance.observe(snapshot("恢复后这一句已经在窗口里。"))[0]
    events = instance.observe(snapshot("恢复后这一句已经在窗口里。接下来这是新说的一句。", 1.5))
    added = [row for row in events if row["kind"] == "append"][0]
    settled = stable_events(instance.observe(snapshot("恢复后这一句已经在窗口里。接下来这是新说的一句。", 2.8)))
    assert initial["historical"] and not added["historical"]
    assert [row["segment_id"] for row in settled] == [added["segment_id"]]


def test_rolling_known_tail_arbitrary_retranslation_with_one_strong_prefix_keeps_id():
    instance = rolling_protocol()
    history = "连接以前的这一句话已经完整。"
    instance.observe(snapshot(history))
    first = instance.observe(snapshot(history + "前面的路没有办法通过。", 1.2))[0]
    revised = instance.observe(snapshot(history + "从这里直接离开游戏场景。", 1.5))
    assert [row["kind"] for row in revised] == ["revise"]
    assert revised[0]["segment_id"] == first["segment_id"]
    assert not revised[0]["historical"]
    assert instance.observe(snapshot(history + "从这里直接离开游戏场景。", 2.5)) == []
    settled = stable_events(instance.observe(snapshot(history + "从这里直接离开游戏场景。", 2.8)))
    assert [row["segment_id"] for row in settled] == [first["segment_id"]]
    assert settled[0]["revision"] == first["revision"] + 1


def test_rolling_exact_completed_row_settles_while_other_gap_changes_sentence_count():
    instance = rolling_protocol(include_initial_completed=True)
    original = instance.observe(snapshot("这一句刚刚完整结束。前面还在介绍游戏角色。"))
    changed = instance.observe(snapshot("这一句刚刚完整结束。完全重新描述。拆成另外一句。", 1.5))
    assert not any(row["kind"] in {"reset", "baseline"} for row in changed)
    revised = [row for row in changed if row["kind"] == "revise"]
    assert revised[0]["segment_id"] == original[1]["segment_id"]
    assert not any(row["kind"] == "retract" for row in changed)
    assert all(row["alignment_pending"] for row in changed if row["kind"] == "append")
    settled = stable_events(instance.observe(snapshot("这一句刚刚完整结束。完全重新描述。拆成另外一句。", 2.5)))
    assert [row["segment_id"] for row in settled] == [original[0]["segment_id"]]
    later = stable_events(instance.observe(snapshot("这一句刚刚完整结束。完全重新描述。拆成另外一句。", 2.8)))
    assert [row["text"] for row in later] == ["完全重新描述。", "拆成另外一句。"]
    assert all(not row["alignment_pending"] for row in later)


def test_rolling_unknown_region_does_not_block_a_later_proven_new_tail():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("完全不同的原来一句。"))[0]
    unknown = instance.observe(snapshot("新窗口中的不确定内容。", 1.5))
    assert len(unknown) == 1 and unknown[0]["kind"] == "revise"
    assert unknown[0]["segment_id"] == first["segment_id"]
    added = instance.observe(snapshot("新窗口中的不确定内容。后来确实新增的这一句。", 1.8))
    identifier = [row["segment_id"] for row in added if row["kind"] == "append"][0]
    settled = stable_events(instance.observe(snapshot("新窗口中的不确定内容。后来确实新增的这一句。", 3.1)))
    assert [row["segment_id"] for row in settled] == [first["segment_id"], identifier]


def test_rolling_resegmented_prefix_does_not_erase_known_completed_tail():
    instance = rolling_protocol(include_initial_completed=True)
    original = instance.observe(snapshot("前一部分内容。后一部分内容。这一句已经完整结束。"))
    changes = instance.observe(snapshot("合并重新翻译这两部分。这一句已经完整结束。", 1.5))
    assert not any(row["kind"] == "reset" for row in changes)
    settled = stable_events(instance.observe(snapshot("合并重新翻译这两部分。这一句已经完整结束。", 2.5)))
    assert [row["segment_id"] for row in settled] == [original[2]["segment_id"]]
    assert settled[0]["text"] == original[2]["text"]


def test_rolling_viewport_rolloff_keeps_already_observed_completed_sentences():
    instance = rolling_protocol(include_initial_completed=True)
    original = instance.observe(snapshot("第一句已经完整显示。第二句也已经显示结束。"))
    changed = instance.observe(snapshot("第二句也已经显示结束。第三句正在继续讲话", 1.5))
    assert not any(row["kind"] in {"reset", "retract"} for row in changed)
    settled = stable_events(instance.observe(snapshot("第二句也已经显示结束。第三句正在继续讲话", 2.5)))
    assert [row["segment_id"] for row in settled] == [row["segment_id"] for row in original]


@pytest.mark.parametrize("foreign", ["谢谢 Aya。", "谢谢 クちゃん。", "现在有韩语 한국。", "字幕有占位符�。"])
def test_rolling_keeps_foreign_sentence_raw_without_rejecting_neighboring_chinese(foreign):
    instance = rolling_protocol(include_initial_completed=True)
    text = "大家晚上好。" + foreign + "接下来开始新的游戏。"
    first = instance.observe(snapshot(text))
    assert not any(row["kind"] == "rejected" for row in first)
    assert [row["text"] for row in first] == ["大家晚上好。", foreign, "接下来开始新的游戏。"]
    settled = stable_events(instance.observe(snapshot(text, 2.5)))
    assert [row["text"] for row in settled] == [row["text"] for row in first]
    # Publication still validates each raw sentence in DanmakuService; the
    # protocol must never delete foreign characters to fabricate Chinese.
    from fanyiji.translation_output import is_chinese_output
    assert [row["text"] for row in settled if is_chinese_output(row["text"])] == [
        "大家晚上好。", "接下来开始新的游戏。"]


def test_rolling_protocol_pollution_still_rejects_whole_snapshot_and_drops_candidates():
    instance = rolling_protocol(include_initial_completed=True)
    instance.observe(snapshot("完整中文句子。"))
    rejected = instance.observe(snapshot("完整中文句子。<asr_text>污染内容", 1.5))
    assert rejected[0]["kind"] == "rejected" and rejected[0]["reason"] == "protocol_pollution"
    assert instance.settle(2.5, bridge_generation=GENERATION) == []
    restored = instance.observe(snapshot("完整中文句子。", 2.6))
    assert all(row["historical"] for row in restored)


def test_rolling_temporary_empty_caption_does_not_rescan_or_reclassify_known_rows():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("空窗之前完整显示这一句。"))[0]
    assert instance.observe(snapshot("", 1.5)) == []
    settled = stable_events(instance.observe(snapshot("空窗之前完整显示这一句。", 2.5)))
    assert [row["segment_id"] for row in settled] == [first["segment_id"]]


def test_rolling_long_complete_sentence_is_not_split_at_ui_eighty_characters():
    instance = rolling_protocol(include_initial_completed=True)
    text = "这是中文长句的真实完整内容" * 20 + "。"
    first = instance.observe(snapshot(text))
    assert len(first) == 1 and first[0]["text"] == text
    settled = stable_events(instance.observe(snapshot(text, 2.5)))
    assert len(settled) == 1 and settled[0]["text"] == text


def test_rolling_literal_append_of_repeated_sentence_has_a_distinct_sentence_id():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("大家谢谢你。"))[0]
    appended = instance.observe(snapshot("大家谢谢你。大家谢谢你。", 1.5))
    second = [row for row in appended if row["kind"] == "append"][0]
    assert not second["alignment_pending"] and second["segment_id"] != first["segment_id"]
    settled = stable_events(instance.observe(snapshot("大家谢谢你。大家谢谢你。", 2.8)))
    assert [row["segment_id"] for row in settled] == [first["segment_id"], second["segment_id"]]


def test_rolling_crossed_old_sentences_are_not_republished_as_new_speech():
    instance = rolling_protocol(include_initial_completed=True)
    text = "第一句话已经完整显示。第二句话同样完整显示。第三句话介绍另外内容。"
    instance.observe(snapshot(text))
    instance.observe(snapshot(text, 2.5))
    reordered = "第三句话介绍另外内容。第二句话同样完整显示。第一句话已经完整显示。"
    changed = instance.observe(snapshot(reordered, 2.8))
    assert not any(row["kind"] == "stable_candidate" for row in changed)
    assert all(row["alignment_pending"] for row in changed if row["kind"] == "append")
    assert stable_events(instance.observe(snapshot(reordered, 4.2))) == []


@pytest.mark.parametrize("options", [
    {"rolling_publication": 1}, {"rolling_publication": "true"},
    {"include_initial_completed": 1}, {"include_initial_completed": None},
])
def test_rolling_flags_require_explicit_boolean_values(options):
    with pytest.raises(ValueError):
        protocol(**options)


def test_rolling_single_partial_correction_keeps_id_without_a_neighboring_sentence():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("前面这条路暂时不能走"))[0]
    revised = instance.observe(snapshot("前面这条路现在不能走。", 1.5))
    assert len(revised) == 1 and revised[0]["kind"] == "revise"
    assert revised[0]["segment_id"] == first["segment_id"]
    assert revised[0]["boundary_observed"]
    settled = stable_events(instance.observe(snapshot("前面这条路现在不能走。", 2.8)))
    assert [row["segment_id"] for row in settled] == [first["segment_id"]]


def test_rolling_retract_chunks_match_the_bounded_service_contract():
    instance = rolling_protocol(include_initial_completed=True, max_candidates=4000)
    original_text = "".join(f"原文{i}。" for i in range(350))
    replacement = "原文0。"
    first = instance.observe(snapshot(original_text))
    changed = instance.observe(snapshot(replacement, 1.5))
    retracts = [row for row in changed if row["kind"] == "retract"]
    assert [len(row["segment_ids"]) for row in retracts] == [256, 93]
    assert [identifier for row in retracts for identifier in row["segment_ids"]] == [
        row["segment_id"] for row in first[1:]]
    assert all(row["historical"] is False for row in retracts)
    assert_candidate_policy(retracts)
    settled = stable_events(instance.observe(snapshot(replacement, 2.8)))
    assert [row["segment_id"] for row in settled] == [first[0]["segment_id"]]


def test_rolling_explicit_initial_window_with_many_sentences_keeps_every_sentence_when_configured():
    instance = rolling_protocol(include_initial_completed=True, max_candidates=4000)
    text = "".join(f"这是第{i}句话。" for i in range(70))
    first = instance.observe(snapshot(text))
    settled = stable_events(instance.observe(snapshot(text, 2.5)))
    assert len(first) == len(settled) == 70
    assert [row["segment_id"] for row in settled] == [row["segment_id"] for row in first]


def test_rolling_display_length_limit_cannot_forge_a_completed_partial():
    instance = protocol(rolling_publication=True, include_initial_completed=True,
                        max_caption_characters=5)
    rows = instance.observe(snapshot("这一句话还没有真实结束"))
    assert len(rows) > 1 and all(not row["boundary_observed"] for row in rows)
    assert all(row["alignment_pending"] for row in rows)
    assert instance.observe(snapshot("这一句话还没有真实结束", 2.5)) == []


@pytest.mark.parametrize("tail", ["这句话还没结束...", "这个数值是3.14", "介绍 Mr. 新角色", "这句话还没结束……"])
def test_rolling_initial_partial_punctuation_does_not_authorize_publication(tail):
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot(tail))
    assert len(first) == 1 and not first[0]["boundary_observed"]
    assert stable_events(instance.observe(snapshot(tail, 2.5))) == []


def test_rolling_cancellation_and_old_generation_cannot_release_captured_initial_sentences():
    instance = rolling_protocol(include_initial_completed=True)
    instance.observe(snapshot("这一句已经完整结束。"))
    instance.cancel()
    assert instance.observe(snapshot("这一句已经完整结束。", 2.5)) == []
    assert instance.settle(2.5, bridge_generation=GENERATION) == []
    instance.reset_generation(GENERATION + 1)
    assert instance.observe(snapshot("旧代际不能重新进入队列。", 3.0)) == []


def test_rolling_historical_partial_retranslation_does_not_become_new_speech():
    instance = rolling_protocol()
    original = "原有完整字幕已经显示结束。历史尾句还没说完"
    first = instance.observe(snapshot(original))
    changed_text = "原有完整字幕已经显示结束。这些是历史尾句的重新翻译。"
    changed = instance.observe(snapshot(changed_text, 1.5))
    assert changed[0]["kind"] == "revise"
    assert changed[0]["segment_id"] == first[1]["segment_id"] and changed[0]["historical"]
    assert stable_events(instance.observe(snapshot(changed_text, 2.8))) == []


def test_rolling_complete_inserted_region_becomes_stable_independently_after_one_point_two_seconds():
    instance = rolling_protocol(include_initial_completed=True)
    old = "前面这句话已经结束。后面这句话也已经结束。"
    first = instance.observe(snapshot(old))
    new = "前面这句话已经结束。重新补充的完整中文句子。后面这句话也已经结束。"
    changed = instance.observe(snapshot(new, 1.5))
    added = [row for row in changed if row["kind"] == "append"][0]
    assert added["alignment_pending"] and not added["historical"]
    early = stable_events(instance.observe(snapshot(new, 2.5)))
    assert [row["segment_id"] for row in early] == [row["segment_id"] for row in first]
    settled = stable_events(instance.observe(snapshot(new, 2.8)))
    assert len(settled) == 1 and settled[0]["segment_id"] == added["segment_id"]
    assert settled[0]["text"] == added["text"] and not settled[0]["alignment_pending"]
    assert instance.observe(snapshot(new, 3.0)) == []


def test_rolling_pending_complete_region_timer_is_local_while_next_sentence_keeps_growing():
    instance = rolling_protocol(include_initial_completed=True)
    old = "前面这句话已经完整。后面这句话已经完整。"
    instance.observe(snapshot(old))
    revised = "前面这句话已经完整。插入的一句已经完整。后面这句话已经完整。"
    added = instance.observe(snapshot(revised, 1.5))
    identifier = [row["segment_id"] for row in added if row["kind"] == "append"][0]
    instance.observe(snapshot(revised + "下一个句子正在", 2.0))
    settled = stable_events(instance.observe(snapshot(revised + "下一个句子正在持续增长", 2.8)))
    assert any(row["segment_id"] == identifier and not row["alignment_pending"] for row in settled)
    assert not any(row["text"].startswith("下一个句子") for row in settled)


def test_rolling_incomplete_tail_arbitrary_rewrite_can_finish_and_add_multiple_new_sentences():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("前面这一句话已经完整。最开始的尾句还没有说完"))
    text = "前面这一句话已经完整。这次完全换一种表达。接下来说第二句。然后说第三句。"
    changed = instance.observe(snapshot(text, 1.5))
    revised = [row for row in changed if row["kind"] == "revise"]
    added = [row for row in changed if row["kind"] == "append"]
    assert revised[0]["segment_id"] == first[1]["segment_id"]
    assert revised[0]["boundary_observed"]
    assert [row["text"] for row in added] == ["接下来说第二句。", "然后说第三句。"]
    settled = stable_events(instance.observe(snapshot(text, 2.8)))
    assert [row["text"] for row in settled] == [
        "前面这一句话已经完整。", "这次完全换一种表达。", "接下来说第二句。", "然后说第三句。"]
    assert all(not row["alignment_pending"] for row in settled)


def test_rolling_arbitrary_equal_count_retranslation_preserves_ids_without_any_exact_anchor():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("原来第一句正在介绍游戏。原来第二句正在谈论角色。"))
    changed_text = "完全不同的中文表达方式。另一句话的全部重新翻译。"
    changed = instance.observe(snapshot(changed_text, 1.5))
    assert [row["kind"] for row in changed] == ["revise", "revise"]
    assert [row["segment_id"] for row in changed] == [row["segment_id"] for row in first]
    assert instance.observe(snapshot(changed_text, 2.5)) == []
    settled = stable_events(instance.observe(snapshot(changed_text, 2.8)))
    assert [row["segment_id"] for row in settled] == [row["segment_id"] for row in first]
    assert_candidate_policy(changed + settled)


def test_rolling_already_stable_retranslation_keeps_old_id_for_sender_freeze_guard():
    instance = rolling_protocol(include_initial_completed=True)
    first = instance.observe(snapshot("之前已经稳定的完整译文。"))[0]
    assert stable_events(instance.observe(snapshot("之前已经稳定的完整译文。", 2.5)))
    changed = instance.observe(snapshot("现在完全重新翻译这一句话。", 2.8))
    assert len(changed) == 1 and changed[0]["segment_id"] == first["segment_id"]
    assert changed[0]["kind"] == "revise"
    settled = stable_events(instance.observe(snapshot("现在完全重新翻译这一句话。", 4.1)))
    assert len(settled) == 1 and settled[0]["segment_id"] == first["segment_id"]
    # The sender owns POST status. It receives a revision of the frozen id,
    # never a freshly minted id that could bypass the already-sent guard.
    assert not any(row["kind"] == "append" for row in changed + settled)


def test_rolling_historical_inserted_region_never_promotes_after_stability():
    instance = rolling_protocol()
    old = "恢复后的第一句旧字幕。恢复后的第二句旧字幕。"
    instance.observe(snapshot(old))
    text = "恢复后的第一句旧字幕。历史区域补充重新断句。恢复后的第二句旧字幕。"
    added = instance.observe(snapshot(text, 1.5))
    assert all(row["historical"] for row in added)
    assert stable_events(instance.observe(snapshot(text, 2.8))) == []


def test_rolling_pending_partial_requires_real_boundary_before_stability_promotion():
    instance = rolling_protocol(include_initial_completed=True)
    old = "前面这一句已经完整。后面这一句也已经完整。"
    instance.observe(snapshot(old))
    # A real newline supplies a boundary for the following known sentence;
    # the final newly inserted tail itself remains unfinished.
    text = old + "下一句还没有完整结束"
    added = instance.observe(snapshot(text, 1.5))[0]
    assert not added["boundary_observed"]
    assert not any(row["segment_id"] == added["segment_id"]
                   for row in stable_events(instance.observe(snapshot(text, 2.8))))
    finished = instance.observe(snapshot(text + "。", 3.0))
    assert finished[0]["segment_id"] == added["segment_id"]
    assert [row["segment_id"] for row in stable_events(instance.observe(snapshot(text + "。", 4.3)))] == [
        added["segment_id"]]


def test_rolling_prefix_rolloff_and_retranslation_reuses_known_id_instead_of_historical_id():
    instance = rolling_protocol()
    history = "连接以前的历史字幕。"
    before, after = "今晚大家一起参加游戏。", "今晚大家一起参加新的游戏。"
    tail = "下一个游戏环节仍在准备"
    historical = instance.observe(snapshot(history))[0]
    added = instance.observe(snapshot(history + before + tail, 1.2))
    instance.observe(snapshot(history + before + tail, 2.5))
    changed = instance.observe(snapshot(after + tail, 2.8))
    revised = [row for row in changed if row["kind"] == "revise"]
    assert len(revised) == 1 and revised[0]["segment_id"] == added[0]["segment_id"]
    assert revised[0]["historical"] is False
    assert revised[0]["segment_id"] != historical["segment_id"]
    settled = stable_events(instance.observe(snapshot(after + tail, 4.1)))
    assert len(settled) == 1 and settled[0]["segment_id"] == added[0]["segment_id"]
    assert settled[0]["text"] == after


@pytest.mark.parametrize("generation", [-1, True, 1.5, "1"])
def test_constructor_requires_nonnegative_integer_generation(generation):
    with pytest.raises(ValueError, match="nonnegative integer"):
        CaptionDeltaProtocol(generation)
