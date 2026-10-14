"""Conservative deltas for Chrome's rolling, revisable caption snapshots.

Chrome UI text carries no official utterance-final signal. Stable candidates
remain inferred and are never automatically eligible for danmaku publication.
By default, the first visible block and uncertain realignments are display-only
baselines. An explicitly enabled rolling publication stream can retain known
sentence identities independently and include its first completed sentences.
Latin names may be explicitly permitted in Han-containing display snapshots;
this does not change the shared Chinese-only model or publication validation.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
import unicodedata

from .asr_protocol import contains_asr_protocol
from .translation_output import _is_han, is_chinese_output


@dataclass(frozen=True)
class CaptionSnapshot:
    text: str
    window_id: str | int
    source_epoch: int
    observed_at: float
    bridge_generation: int
    source_lang: str = "ja"
    target_lang: str = "zh"
    room: str = ""
    connected: bool = True


@dataclass
class _Caption:
    segment_id: str
    text: str
    changed_at: float
    revision: int = 1
    historical: bool = False
    boundary: bool = False
    stable_revision: int = 0
    alignment_pending: bool = False
    pending_stability: bool = False


def _normalize(value: str) -> str:
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip()
                     for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                     if line.strip())


def _meaningful(value: str) -> bool:
    return any(unicodedata.category(char).startswith("L") or unicodedata.category(char) == "Nd" for char in value)


def _chinese_with_latin_names(value: str) -> bool:
    # This is display-only, not a language detector or a publication policy.
    # Chrome may show a transient replacement character during recognition;
    # accept that placeholder for display without deleting or rewriting it.
    # Keep the shared Chinese check for every character except Latin letters.
    text = unicodedata.normalize("NFC", value)
    if not any(_is_han(char) for char in text):
        return False
    without_latin = "".join(char for char in text if char != "\ufffd"
                           and not unicodedata.name(char, "").startswith("LATIN "))
    return is_chinese_output(without_latin)


def _overlap(previous: str, current: str) -> int:
    """Longest previous suffix/current prefix, in linear time for capped text."""
    combined = current + "\x00" + previous
    lengths = [0] * len(combined)
    for index in range(1, len(combined)):
        match = lengths[index - 1]
        while match and combined[index] != combined[match]:
            match = lengths[match - 1]
        if combined[index] == combined[match]:
            match += 1
        lengths[index] = match
    return min(lengths[-1], len(current)) if lengths else 0


def _ascii_period_ending(value: str, index: int) -> bool:
    previous = value[index - 1] if index else ""
    following = value[index + 1] if index + 1 < len(value) else ""
    # A growing decimal or abbreviation must not become a finished caption
    # merely because Chrome has not displayed the following character yet.
    if not previous or previous == "." or following == "." or previous.isdigit():
        return False
    if previous.isascii() and previous.isalpha():
        if following.isascii() and following.isalpha():
            return False
        word = re.search(r"[A-Za-z]+$", value[:index])
        token = word.group().lower() if word else ""
        if len(token) == 1 or token in {
            "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
            "no", "fig", "inc", "co", "ltd", "approx", "dept", "vol",
        }:
            return False
    return True


def _pieces(value: str, *, prefix: str = ""):
    # Closing quotes belong to an observed sentence ending. Visual line wraps
    # are absent from the native Document; only real newlines are boundaries.
    start, index = 0, 0
    context, context_offset = prefix + value, len(prefix)
    endings, closers = "。！？!?", "\"'”’）)]】》〉」』}"
    while index < len(value):
        char = value[index]
        if char == "\n":
            yield value[start:index], True
            start = index = index + 1
        elif char in endings or (char == "." and _ascii_period_ending(context, context_offset + index)):
            end = index + 1
            while end < len(value) and (value[end] in endings
                    or (value[end] == "." and _ascii_period_ending(context, context_offset + end))):
                end += 1
            while end < len(value) and value[end] in closers:
                end += 1
            yield value[start:end], True
            start = index = end
        else:
            index += 1
    if start < len(value):
        yield value[start:], False


def _sentence_parts(value: str) -> list[tuple[str, bool, int]]:
    parts, offset = [], 0
    for raw, boundary in _pieces(value):
        start = offset
        offset += len(raw)
        if offset < len(value) and value[offset] == "\n":
            offset += 1
        if raw.strip():
            parts.append((raw.strip(), boundary, start))
    return parts


class CaptionDeltaProtocol:
    def __init__(self, generation: int = 0, *, stable_seconds: float = 1.2,
                 max_snapshot_characters: int = 4000, max_caption_characters: int = 240,
                 max_candidates: int = 32, min_overlap_characters: int = 3,
                 freshness_seconds: float = 3.0, allow_latin_names: bool = False,
                 rolling_publication: bool = False, include_initial_completed: bool = False):
        if type(generation) is not int or generation < 0:
            raise ValueError("generation must be a nonnegative integer")
        if (type(stable_seconds) not in (int, float) or not math.isfinite(stable_seconds) or stable_seconds <= 0
                or type(freshness_seconds) not in (int, float) or not math.isfinite(freshness_seconds)
                or freshness_seconds <= stable_seconds
                or type(allow_latin_names) is not bool
                or type(rolling_publication) is not bool or type(include_initial_completed) is not bool
                or any(type(limit) is not int or limit < 1 for limit in (
                    max_snapshot_characters, max_caption_characters, max_candidates, min_overlap_characters))):
            raise ValueError("invalid caption protocol limits")
        self.generation = generation
        self.stable_seconds, self.freshness_seconds = stable_seconds, freshness_seconds
        self.max_snapshot_characters, self.max_caption_characters = max_snapshot_characters, max_caption_characters
        self.max_candidates, self.min_overlap_characters = max_candidates, min_overlap_characters
        self.allow_latin_names = allow_latin_names
        self.rolling_publication = rolling_publication
        self._include_initial_completed = rolling_publication and include_initial_completed
        self._cancelled = False
        self._identity: tuple | None = None
        self._source_epoch = -1
        self._last_at = -1.0
        self._connected = False
        self._visible = ""
        self._captions: list[_Caption] = []
        self._active: _Caption | None = None
        self._serial = 0
        self._alignment_diagnostic: dict = {}
        self._rolling_visible: list[_Caption] = []

    def cancel(self) -> None:
        self._cancelled = True
        self._clear()

    def reset_generation(self, generation: int) -> None:
        if type(generation) is not int or generation <= self.generation:
            raise ValueError("a resumed bridge requires a newer generation")
        self.generation = generation
        self._cancelled = False
        self._identity = None
        self._source_epoch, self._last_at = -1, -1.0
        self._connected = False
        self._include_initial_completed = False
        self._clear()

    def _clear(self) -> None:
        self._visible = ""
        self._captions.clear()
        self._active = None
        self._rolling_visible.clear()

    def _event(self, kind: str, *, caption: _Caption | None = None, **values) -> dict:
        window, epoch, source, target, room = self._identity or ("", self._source_epoch, "", "", "")
        event = {"kind": kind, "bridge_generation": self.generation,
                 "window_id": window, "source_epoch": epoch, "source_lang": source,
                 "target_lang": target, "room": room, "observed_at": self._last_at,
                 "is_official_final": False, "allow_push": False}
        if caption is not None:
            event.update(segment_id=caption.segment_id, text=caption.text, revision=caption.revision,
                         historical=caption.historical, inferred_stable=kind == "stable_candidate",
                         boundary_observed=caption.boundary)
            if self.rolling_publication:
                event["alignment_pending"] = caption.alignment_pending
        event.update(values)
        return event

    def _new(self, text: str, *, historical=False, boundary=False, alignment_pending=False,
             pending_stability=False) -> _Caption:
        self._serial += 1
        caption = _Caption(f"chrome:{self.generation}:{self._source_epoch}:{self._serial}",
                           text, self._last_at, historical=historical, boundary=boundary,
                           alignment_pending=alignment_pending, pending_stability=pending_stability)
        self._captions.append(caption)
        self._captions = self._captions[-self.max_candidates:]
        return caption

    def _baseline(self, text: str, reason: str) -> list[dict]:
        self._clear()
        self._visible = text
        include_initial = self._include_initial_completed
        self._include_initial_completed = False
        # Historical rows may be displayed, but splitting never makes them new
        # speech or eligible for inferred stability and candidate aggregation.
        events = []
        for part, boundary in _pieces(text):
            remaining = part.strip()
            while remaining:
                piece, remaining = remaining[:self.max_caption_characters], remaining[self.max_caption_characters:]
                caption = self._new(piece, historical=not include_initial,
                                    boundary=(boundary or bool(remaining)) if not self.rolling_publication
                                    else boundary and not remaining,
                                    alignment_pending=self.rolling_publication and len(part.strip()) > self.max_caption_characters)
                self._active = caption
                if self.rolling_publication:
                    self._rolling_visible.append(caption)
                events.append(self._event("baseline", caption=caption, reason=reason))
        return events

    def _reject(self, reason: str) -> list[dict]:
        self._clear()
        return [self._event("rejected", reason=reason)]

    def observe(self, snapshot: CaptionSnapshot) -> list[dict]:
        if not isinstance(snapshot, CaptionSnapshot):
            raise TypeError("snapshot must be CaptionSnapshot")
        if self._cancelled or type(snapshot.bridge_generation) is not int or snapshot.bridge_generation != self.generation:
            return []
        if (type(snapshot.source_epoch) is not int or snapshot.source_epoch < 0
                or type(snapshot.observed_at) not in (int, float) or not math.isfinite(snapshot.observed_at)
                or snapshot.observed_at < 0 or type(snapshot.connected) is not bool):
            return self._reject("invalid_snapshot")
        if snapshot.source_epoch < self._source_epoch or snapshot.observed_at < self._last_at:
            return []
        self._last_at = float(snapshot.observed_at)
        if not snapshot.connected:
            was_active = self._connected or bool(self._visible)
            self._connected = False
            self._include_initial_completed = False
            self._source_epoch = snapshot.source_epoch
            self._clear()
            # The controller has already advanced its epoch on disconnection.
            # Use that new fence so it can clear current rows, rather than
            # discarding the reset as an event from the previous source.
            reset = self._event("reset", reason="disconnected", source_epoch=snapshot.source_epoch)
            self._identity = None
            return [reset] if was_active else []
        if (not isinstance(snapshot.window_id, (str, int)) or isinstance(snapshot.window_id, bool)
                or not str(snapshot.window_id).strip() or snapshot.window_id == 0
                or not isinstance(snapshot.source_lang, str) or len(snapshot.source_lang) > 32
                or snapshot.target_lang not in ("zh", "zh-CN", "ja")
                or not isinstance(snapshot.room, str) or len(snapshot.room) > 128):
            return self._reject("invalid_identity")
        identity = (str(snapshot.window_id), snapshot.source_epoch, snapshot.source_lang,
                    snapshot.target_lang, snapshot.room)
        switched = self._identity is not None and identity != self._identity
        reconnected = not self._connected
        self._identity, self._source_epoch, self._connected = identity, snapshot.source_epoch, True
        events = []
        if switched:
            self._include_initial_completed = False
            self._clear()
            events.append(self._event("reset", reason="source_changed"))
        elif reconnected:
            self._clear()
        if not isinstance(snapshot.text, str) or len(snapshot.text) > self.max_snapshot_characters:
            return events + self._reject("snapshot_limit")
        if (contains_asr_protocol(snapshot.text)
                or any(ord(char) < 32 and char not in "\n\r\t" for char in snapshot.text)):
            return events + self._reject("protocol_pollution")
        text = _normalize(snapshot.text)
        if not text:
            if self.rolling_publication and self._visible:
                # A temporarily blank native label is not a new source. Keep
                # captured sentences; a real disconnect has its own fence.
                return events + self.settle(self._last_at, bridge_generation=self.generation)
            if self._visible:
                self._clear()
                return events + [self._event("reset", reason="empty_caption")]
            return events
        chinese_output = (_chinese_with_latin_names(text) if self.allow_latin_names else is_chinese_output(text))
        if snapshot.target_lang in ("zh", "zh-CN") and not chinese_output and not self.rolling_publication:
            return events + self._reject("non_chinese_output")
        if not self._visible:
            return events + self._baseline(text, "first_snapshot" if self._serial == 0 else "new_baseline")
        previous = self._visible
        if text == previous:
            return events + self.settle(self._last_at, bridge_generation=self.generation)
        if self.rolling_publication:
            events.extend(self._reconcile_rolling(text))
            self._visible = text
            return events + self.settle(self._last_at, bridge_generation=self.generation)
        if text.startswith(previous):
            delta = text[len(previous):]
            self._visible = text
            # Earlier sentences can settle while a later sentence is still
            # growing. Stability belongs to each row, not the whole viewport.
            return events + self._append(delta) + self.settle(self._last_at, bridge_generation=self.generation)
        if self._rewrite(previous, text, events):
            self._visible = text
            return events + self.settle(self._last_at, bridge_generation=self.generation)
        aligned = self._align_sentences(previous, text, events)
        if aligned is True:
            self._visible = text
            return events + self.settle(self._last_at, bridge_generation=self.generation)
        # Contradictory sentence anchors are not a plain prefix roll-off even
        # when a few characters happen to match the old block's suffix.
        overlap = _overlap(previous, text) if aligned is False else 0
        if overlap >= self.min_overlap_characters:
            self._visible = text
            # A pure viewport roll-off contains no new words. Keep the original
            # candidate text and id rather than publishing a shortened repeat.
            return (events + (self._append(text[overlap:]) if len(text) > overlap else [])
                    + self.settle(self._last_at, bridge_generation=self.generation))
        return (events + [self._event("reset", reason="uncertain_realignment", **self._alignment_diagnostic)]
                + self._baseline(text, "uncertain_realignment"))

    def _reconcile_rolling(self, current: str) -> list[dict]:
        """Align sentences locally without turning the whole viewport into history.

        Exact ordered matches keep their timers. A known replacement enclosed
        on either side by a strong unique sentence is a revision of those
        positions. New split/merge regions wait independently for stability;
        true history and relocated old sentences never become new speech.
        """
        old = self._rolling_visible
        parts = _sentence_parts(current)
        before, after = [row.text for row in old], [part[0] for part in parts]
        old_counts, new_counts = Counter(before), Counter(after)
        matcher = SequenceMatcher(None, before, after, autojunk=False)
        opcodes = matcher.get_opcodes()
        exact = {new_index: old_index
                 for tag, first, last, start, end in opcodes if tag == "equal"
                 for old_index, new_index in zip(range(first, last), range(start, end))}

        def strong(new_index):
            old_index = exact.get(new_index)
            if old_index is None:
                return False
            row = old[old_index]
            return (not row.alignment_pending and len(row.text) >= max(8, self.min_overlap_characters)
                    and old_counts[row.text] == new_counts[row.text] == 1
                    and (row.boundary or not row.historical))

        events, visible, discarded = [], [], []

        def reuse(row, index):
            text, boundary, _ = parts[index]
            if row.text != text or row.boundary != boundary:
                row.text, row.boundary = text, boundary
                row.revision += 1
                row.changed_at, row.stable_revision = self._last_at, 0
                events.append(self._event("revise", caption=row))
            visible.append(row)

        def fresh(index, *, pending, historical=False, moved=False):
            text, boundary, _ = parts[index]
            # The push protocol uses the snapshot limit as its sentence limit;
            # a caller with a smaller limit cannot manufacture a final by
            # splitting an unfinished sentence at that display boundary.
            oversized = len(text) > self.max_caption_characters
            if oversized:
                pending = True
            for offset in range(0, len(text), self.max_caption_characters):
                row = self._new(text[offset:offset + self.max_caption_characters], historical=historical,
                                boundary=boundary and not oversized, alignment_pending=pending,
                                pending_stability=pending and not historical and not oversized and not moved)
                visible.append(row)
                events.append(self._event("append", caption=row))

        for tag, first, last, start, end in opcodes:
            if tag == "equal":
                for old_index, new_index in zip(range(first, last), range(start, end)):
                    reuse(old[old_index], new_index)
                continue
            if tag == "delete":
                # An exact retained sentence to the right proves viewport
                # roll-off. Completed rows already observed keep their timer.
                if first != 0 or start not in exact:
                    discarded.extend(old[first:last])
                continue
            equal_count = last - first == end - start
            known = bool(last > first and all(not row.alignment_pending for row in old[first:last]))
            bounded_revision = equal_count and known and (strong(start - 1) or strong(end))
            moved = any(text in before[:first] + before[last:]
                        for text in after[start:end]) and not current.startswith(self._visible)
            positional_revision = (equal_count and last > first and not moved
                                   and any(not row.historical and (
                                       not row.alignment_pending or row.pending_stability)
                                           for row in old[first:last]))
            compatible_revision = equal_count and last > first and all(
                (not row.boundary and new.startswith(row.text)) or (
                    len(row.text) >= 2 and row.text[:2] == new[:2]
                    and SequenceMatcher(None, row.text, new, autojunk=False).ratio() >= 0.6)
                for row, new in zip(old[first:last], after[start:end]))
            if bounded_revision or compatible_revision or positional_revision:
                for row, index in zip(old[first:last], range(start, end)):
                    reuse(row, index)
                continue
            # A growing known incomplete tail can finish and be followed by
            # genuinely new sentences in the same snapshot.
            tail_growth = (last == len(old) and last - first == 1 and end > start
                           and not old[first].boundary and after[start].startswith(old[first].text))
            if tail_growth:
                reuse(old[first], start)
                for index in range(start + 1, end):
                    fresh(index, pending=False)
                continue
            local_revision = (tag == "replace" and last > first and end > start and not moved
                              and all(len(text) <= self.max_caption_characters for text in after[start:end])
                              and any(not row.historical and (
                                  not row.alignment_pending or row.pending_stability) for row in old[first:last]))
            if local_revision:
                # Keep existing identities wherever sentence positions can be
                # paired. Submitted identities remain frozen in the sender;
                # a punctuation split must not replay them under fresh ids.
                # Chrome can roll the leading history out while revising the
                # following known sentence. A retained right sentence anchors
                # this prefix gap from the right, not from the lost history.
                paired_start = (last - (end - start) if first == start == 0 and end in exact
                                and last - first > end - start else first)
                paired_size = min(last - paired_start, end - start)
                discarded.extend(old[first:paired_start])
                for offset in range(paired_size):
                    reuse(old[paired_start + offset], start + offset)
                discarded.extend(old[paired_start + paired_size:last])
                for index in range(start + paired_size, end):
                    fresh(index, pending=True)
                continue
            tail_insertion = (tag == "insert" and first == len(old) and bool(old)
                              and exact.get(start - 1) == len(old) - 1 and old[-1].boundary)
            if not tail_insertion:
                discarded.extend(old[first:last])
            historical = bool(last > first and all(row.historical for row in old[first:last]))
            if tag == "insert" and not tail_insertion and first < len(old):
                historical = old[first].historical
            for index in range(start, end):
                # A moved old sentence outside the ordered matching block is
                # not new speech. Keep it pending instead of replaying it.
                moved_sentence = after[index] in before and not current.startswith(self._visible)
                fresh(index, pending=not tail_insertion or moved_sentence,
                      historical=historical, moved=moved_sentence)

        if discarded:
            identifiers = {row.segment_id for row in discarded}
            self._captions = [row for row in self._captions if row.segment_id not in identifiers]
            ordered_ids = [row.segment_id for row in discarded]
            events[:0] = [self._event("retract", segment_ids=ordered_ids[index:index + 256],
                                      historical=False, reason="local_realignment")
                          for index in range(0, len(ordered_ids), 256)]
        self._rolling_visible = visible
        self._active = visible[-1] if visible else None
        return events

    def _append(self, delta: str) -> list[dict]:
        events = []
        for part, boundary in _pieces(delta, prefix=self._active.text if self._active is not None else ""):
            active = self._active
            historical = active.historical if active is not None else False
            if not part.strip():
                if boundary and active is not None and not active.boundary:
                    active.boundary = True
                    active.revision += 1
                    active.changed_at = self._last_at
                    active.stable_revision = 0
                    events.append(self._event("revise", caption=active))
                continue
            if active is None or (active.boundary and _meaningful(part)):
                if not _meaningful(part):
                    continue
                active = None
                historical = False
                part = part.lstrip()
            remaining = part
            while remaining:
                if active is None:
                    piece, remaining = remaining[:self.max_caption_characters], remaining[self.max_caption_characters:]
                    active = self._new(piece, historical=historical, boundary=boundary and not remaining)
                    self._active = active
                    events.append(self._event("append", caption=active))
                else:
                    room = self.max_caption_characters - len(active.text)
                    if room <= 0:
                        active.boundary = True
                        active = None
                        continue
                    piece, remaining = remaining[:room], remaining[room:]
                    active.text += piece
                    active.revision += 1
                    active.changed_at = self._last_at
                    active.boundary = boundary and not remaining
                    active.stable_revision = 0
                    events.append(self._event("revise", caption=active))
                if remaining:
                    active.boundary = True
                    active = None
        return events

    def _align_sentences(self, previous: str, current: str, events: list[dict]) -> bool | None:
        """Keep certain sentence identities when Chrome revises its viewport.

        Matching an unchanged, unique sentence is stronger evidence than a
        whole-block suffix. Do not use fuzzy matching to invent new history or
        move sentences across one another. Validate the entire plan before
        mutating rows, so a failed alignment still retracts through reset.
        """
        old, new = _sentence_parts(previous), _sentence_parts(current)
        self._alignment_diagnostic = {"alignment_code": "sentence_limit", "old_sentences": len(old),
                                      "new_sentences": len(new), "unique_anchors": 0}
        if (not old or not new or any(len(part[0]) > self.max_caption_characters for part in old + new)):
            return False
        old_counts, new_counts = Counter(part[0] for part in old), Counter(part[0] for part in new)
        new_positions = {part[0]: index for index, part in enumerate(new)}
        anchors = [(index, new_positions[part[0]]) for index, part in enumerate(old)
                   if old_counts[part[0]] == new_counts[part[0]] == 1
                   and len(part[0]) >= self.min_overlap_characters and _meaningful(part[0])]
        self._alignment_diagnostic.update(alignment_code="no_unique_anchor", unique_anchors=len(anchors))
        if not anchors:
            return False
        if any(left[1] >= right[1] for left, right in zip(anchors, anchors[1:])):
            self._alignment_diagnostic["alignment_code"] = "crossed_anchors"
            return None

        # Captions that already rolled out are retained for stable timing. Find
        # the visible rows from the end, avoiding a similarly spelled old row.
        rows: list[_Caption | None] = [None] * len(old)
        cursor = len(self._captions)
        for index in range(len(old) - 1, -1, -1):
            match = next((position for position in range(cursor - 1, -1, -1)
                          if self._captions[position].text == old[index][0]), None)
            if match is not None:
                rows[index] = self._captions[match]
                cursor = match
        if rows[-1] is None:
            self._alignment_diagnostic["alignment_code"] = "unmapped_tail"
            return False
        earliest = next(index for index, row in enumerate(rows) if row is not None)
        paired: list[tuple[int, int]] = []
        history_plans: list[tuple[list[_Caption], list[tuple[str, bool, int]]]] = []
        strong_anchors = [old_index for old_index, _ in anchors
                          if rows[old_index] is not None and len(old[old_index][0]) >= max(8, self.min_overlap_characters)
                          and (rows[old_index].boundary or not rows[old_index].historical)]

        def pair_gap(old_start, old_end, new_start, new_end, *, rolled_prefix=False):
            old_size, new_size = old_end - old_start, new_end - new_start
            historical_gap = bool(old_size and any(anchor >= old_end for anchor in strong_anchors))
            for old_index in range(old_start, old_end):
                row = rows[old_index]
                if (row is not None and not row.historical) or (row is None and (
                        old_index >= earliest or any(caption.text.endswith(old[old_index][0])
                                                    for caption in self._captions))):
                    historical_gap = False
                    break
            if historical_gap:
                # A strong unchanged sentence on the right proves the history
                # region's endpoint. Every replacement stays historical even
                # when Chrome changes its wording or sentence punctuation.
                history_plans.append(([row for row in rows[old_start:old_end] if row is not None],
                                      new[new_start:new_end]))
                return True
            if rolled_prefix and old_size >= new_size:
                old_start += old_size - new_size
            elif old_size != new_size:
                self._alignment_diagnostic["alignment_code"] = "known_gap_count"
                return False
            bounded_revision = bool(old_size == new_size and old_size
                and any(anchor < old_start for anchor in strong_anchors)
                and any(anchor >= old_end for anchor in strong_anchors))
            for old_index, new_index in zip(range(old_start, old_end), range(new_start, new_end)):
                before, after = old[old_index][0], new[new_index][0]
                row = rows[old_index]
                if row is None:
                    if (old_index >= earliest or (before != after
                            and any(caption.text.endswith(before) for caption in self._captions))):
                        self._alignment_diagnostic["alignment_code"] = "unmapped_sentence"
                        return False
                    # The bounded row history has already discarded this
                    # prefix. It cannot become a new publication candidate.
                    continue
                if before != after and not (not old[old_index][1] and after.startswith(before)):
                    # Two unchanged strong anchors enclose the same known
                    # sentence positions. A retranslation need not share its
                    # opening words; it is a revision, not newly found speech.
                    if bounded_revision and _meaningful(after):
                        paired.append((old_index, new_index))
                        continue
                    common = 0
                    for old_char, new_char in zip(before, after):
                        if old_char != new_char:
                            break
                        common += 1
                    if (common < 2 or not _meaningful(after)
                            or SequenceMatcher(None, before, after, autojunk=False).ratio() < 0.6):
                        self._alignment_diagnostic["alignment_code"] = "known_gap_rewrite"
                        return False
                paired.append((old_index, new_index))
            return True

        old_start = new_start = 0
        for old_index, new_index in anchors:
            if not pair_gap(old_start, old_index, new_start, new_index,
                            rolled_prefix=old_start == 0 and new_start == 0):
                return False
            if rows[old_index] is not None:
                paired.append((old_index, new_index))
            old_start, new_start = old_index + 1, new_index + 1
        old_tail = len(old) - old_start
        if len(new) - new_start < old_tail:
            self._alignment_diagnostic["alignment_code"] = "tail_removed"
            return False
        if not pair_gap(old_start, len(old), new_start, new_start + old_tail):
            return False
        added_start = new_start + old_tail
        has_addition = added_start < len(new)
        tail = rows[-1]
        if has_addition:
            aligned_tail = new[added_start - 1][0]
            if (not old[-1][1] and tail.historical
                    and not aligned_tail.startswith(old[-1][0])):
                # Rewriting an incomplete first baseline into several complete
                # sentences does not establish where new speech began.
                self._alignment_diagnostic["alignment_code"] = "unknown_tail_addition"
                return False

        original_rows = self._captions[:]
        replacements: dict[str, list[_Caption]] = {}
        removed_history: set[str] = set()
        for existing, parts in history_plans:
            if not existing:
                continue
            rebuilt = []
            for index, (text, boundary, _) in enumerate(parts):
                existing_index = index + len(existing) - len(parts)
                if existing_index >= 0:
                    row = existing[existing_index]
                    if row.text != text or row.boundary != boundary:
                        row.text, row.boundary = text, boundary
                        row.revision += 1
                        row.changed_at, row.stable_revision = self._last_at, 0
                        events.append(self._event("revise", caption=row, reason="historical_realignment"))
                else:
                    row = self._new(text, historical=True, boundary=boundary)
                    events.append(self._event("baseline", caption=row, reason="historical_realignment"))
                rebuilt.append(row)
            replacements[existing[0].segment_id] = rebuilt
            removed_history.update(row.segment_id for row in existing)
        rebuilt_rows = []
        for row in original_rows:
            if row.segment_id in replacements:
                rebuilt_rows.extend(replacements[row.segment_id])
            elif row.segment_id not in removed_history:
                rebuilt_rows.append(row)
        self._captions = rebuilt_rows[-self.max_candidates:]
        for old_index, new_index in paired:
            row = rows[old_index]
            text, boundary, _ = new[new_index]
            if row.text != text or row.boundary != boundary:
                row.text, row.boundary = text, boundary
                row.revision += 1
                row.changed_at, row.stable_revision = self._last_at, 0
                events.append(self._event("revise", caption=row))
        self._active = tail
        if has_addition:
            events.extend(self._append(current[new[added_start][2]:]))
        self._alignment_diagnostic["alignment_code"] = "aligned"
        return True

    def _rewrite(self, previous: str, current: str, events: list[dict]) -> bool:
        active = self._active
        if active is None or not previous.endswith(active.text):
            return False
        anchor = previous[:-len(active.text)]
        if not current.startswith(anchor):
            return False
        replacement = current[len(anchor):]
        if not replacement or len(replacement) > self.max_caption_characters or not _meaningful(replacement):
            return False
        common = 0
        for old, new in zip(active.text, replacement):
            if old != new:
                break
            common += 1
        if common < 2 or SequenceMatcher(None, active.text, replacement, autojunk=False).ratio() < 0.6:
            return False
        pieces = list(_pieces(replacement))
        if len(pieces) != 1:
            return False
        active.text = replacement
        active.revision += 1
        active.changed_at = self._last_at
        active.boundary = pieces[0][1]
        active.stable_revision = 0
        events.append(self._event("revise", caption=active))
        return True

    def _stable(self, now, generation):
        if (self._cancelled or type(generation) is not int or generation != self.generation or not self._connected
                or type(now) not in (int, float) or not math.isfinite(now)
                or now < self._last_at or now - self._last_at > self.freshness_seconds):
            return []
        if self.rolling_publication:
            for caption in self._captions:
                if (caption.alignment_pending and caption.pending_stability and not caption.historical
                        and caption.boundary and now - caption.changed_at >= self.stable_seconds):
                    caption.alignment_pending = False
                    caption.pending_stability = False
        return [caption for caption in self._captions if not caption.historical and not caption.alignment_pending
                and (not self.rolling_publication or caption.boundary)
                and now - caption.changed_at >= self.stable_seconds]

    def settle(self, now: float, *, bridge_generation: int) -> list[dict]:
        events = []
        for caption in self._stable(now, bridge_generation):
            if caption.stable_revision != caption.revision:
                caption.stable_revision = caption.revision
                events.append(self._event("stable_candidate", caption=caption, stable_at=float(now)))
        return events

    def stable_aggregate(self, now: float, *, bridge_generation: int) -> dict | None:
        """Return bounded fresh candidates; caller must not treat this as final.

        The identity/revision references let callers replace candidates after a
        later Chrome correction. This API never drains or authorizes publication.
        """
        captions = self._stable(now, bridge_generation)
        if not captions:
            return None
        text = " ".join(caption.text for caption in captions)
        if len(text) > self.max_snapshot_characters:
            return None
        return self._event("stable_aggregate", text=text, inferred_stable=True, historical=False,
                           candidates=[{"segment_id": caption.segment_id, "revision": caption.revision,
                                        "text": caption.text, "boundary_observed": caption.boundary}
                                       for caption in captions], stable_at=float(now))
