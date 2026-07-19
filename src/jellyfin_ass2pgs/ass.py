from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re

import pysubs2


@dataclass(frozen=True)
class AssEvent:
    index: int
    start_ms: int
    end_ms: int
    style: str
    effect: str
    text: str

    @property
    def duration_ms(self) -> int:
        return max(1, self.end_ms - self.start_ms)


class EventKind(Enum):
    STATIC = "static"
    DYNAMIC = "dynamic"


@dataclass(frozen=True)
class EventClassification:
    kind: EventKind
    reason: str = "static"


@dataclass(frozen=True)
class TimelineInterval:
    start_ms: int
    end_ms: int
    dynamic: bool
    active_events: tuple[AssEvent, ...]

    @property
    def sample_ms(self) -> int:
        return min(self.end_ms - 1, self.start_ms + max(1, min(50, (self.end_ms - self.start_ms) // 2)))


def load(path: Path) -> pysubs2.SSAFile:
    return pysubs2.load(str(path), format_="ass")


def visible_events(subs: pysubs2.SSAFile) -> list[AssEvent]:
    events = []
    for index, event in enumerate(subs.events):
        if event.is_comment or not event.text.strip():
            continue
        if event.end <= event.start:
            continue
        events.append(
            AssEvent(
                index=index,
                start_ms=int(event.start),
                end_ms=int(event.end),
                style=event.style,
                effect=event.effect or "",
                text=event.text,
            )
        )
    return events


def classify_event(event: AssEvent) -> EventKind:
    return classify_event_detail(event).kind


def classify_event_detail(event: AssEvent) -> EventClassification:
    reason = dynamic_reason(event.text, event.effect)
    if reason:
        return EventClassification(EventKind.DYNAMIC, reason)
    return EventClassification(EventKind.STATIC, "static")


def build_timeline(
    events: list[AssEvent],
    kinds: dict[int, EventKind] | None = None,
) -> list[TimelineInterval]:
    kinds = kinds or {event.index: classify_event(event) for event in events}
    return _build_timeline_sweep(events, kinds)


def _build_timeline_sweep(
    events: list[AssEvent],
    kinds: dict[int, EventKind],
) -> list[TimelineInterval]:
    """Build active-event intervals with a sweep-line pass.

    The old implementation scanned every event for every boundary. Here each
    event is added once and removed once, after the initial timestamp sort.
    """
    starts: dict[int, list[AssEvent]] = {}
    ends: dict[int, list[AssEvent]] = {}
    for event in events:
        starts.setdefault(event.start_ms, []).append(event)
        ends.setdefault(event.end_ms, []).append(event)

    boundaries = sorted(set(starts) | set(ends))
    intervals: list[TimelineInterval] = []
    if len(boundaries) < 2:
        return intervals

    active: dict[int, AssEvent] = {}
    active_dynamic = 0
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        for event in ends.get(start, ()):
            if active.pop(event.index, None) is not None and kinds[event.index] is EventKind.DYNAMIC:
                active_dynamic -= 1
        for event in starts.get(start, ()):
            active[event.index] = event
            if kinds[event.index] is EventKind.DYNAMIC:
                active_dynamic += 1

        if end <= start:
            continue
        if not active:
            continue
        intervals.append(
            TimelineInterval(
                start_ms=start,
                end_ms=end,
                dynamic=active_dynamic > 0,
                active_events=tuple(active.values()),
            )
        )

    return intervals


def is_dynamic(text: str, effect: str = "") -> bool:
    return dynamic_reason(text, effect) is not None


def dynamic_reason(text: str, effect: str = "") -> str | None:
    tag_match = _DYNAMIC_TAG_RE.search(text)
    if tag_match:
        return f"dynamic ASS tag {tag_match.group(1)}"
    effect_match = _DYNAMIC_EFFECT_RE.search(effect)
    if effect_match:
        return f"dynamic ASS effect {effect_match.group(1)}"
    return None


def isolate_event(source: Path, event_index: int, output_ass: Path) -> tuple[Path, int]:
    subs = load(source)
    if event_index < 0 or event_index >= len(subs.events):
        raise IndexError(f"ASS event index {event_index} does not exist.")

    event = subs.events[event_index].copy()
    original_start = int(event.start)
    duration = max(1, int(event.end - event.start))
    event.start = 0
    event.end = duration

    isolated = deepcopy(subs)
    isolated.events = [event]
    output_ass.parent.mkdir(parents=True, exist_ok=True)
    isolated.save(str(output_ass), format_="ass")
    return output_ass, original_start


def ms_to_ass_time(milliseconds: int) -> str:
    centiseconds = int(round(milliseconds / 10))
    cs = centiseconds % 100
    total_seconds = centiseconds // 100
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def ms_to_seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


_DYNAMIC_TAG_RE = re.compile(r"\\(move|t\s*\(|fad\s*\(|fade\s*\(|[kK](?:f|o)?\s*)", re.IGNORECASE)
_DYNAMIC_EFFECT_RE = re.compile(r"\b(banner|scroll\s+up|scroll\s+down)\b", re.IGNORECASE)
