from __future__ import annotations

import pytest

from jellyfin_ass2pgs import ass


def test_sweep_line_matches_old_algorithm_edge_cases() -> None:
    events = [
        _event(1, 0, 1000),
        _event(2, 250, 750),
        _event(3, 500, 500),
        _event(4, 1000, 900),
        _event(5, 750, 1250),
        _event(6, 750, 1250, r"{\fad(100,100)}dynamic"),
        _event(7, 1250, 1300),
        _event(8, 1250, 1300),
    ]
    visible = [event for event in events if event.end_ms > event.start_ms]
    kinds = {event.index: ass.classify_event(event) for event in visible}

    assert _timeline_signature(ass.build_timeline(visible, kinds)) == _old_timeline_signature(visible, kinds)


def test_sweep_line_matches_old_algorithm_dense_karaoke() -> None:
    events = []
    for index in range(80):
        start = index * 25
        events.append(_event(index, start, start + 120, rf"{{\k20}}syllable {index}"))
    kinds = {event.index: ass.classify_event(event) for event in events}

    assert _timeline_signature(ass.build_timeline(events, kinds)) == _old_timeline_signature(events, kinds)


@pytest.mark.parametrize(
    ("text", "effect", "reason_fragment"),
    [
        (r"{\move(0,0,100,100)}text", "", "tag move"),
        (r"{\t(0,500,\fscx120)}text", "", "tag t("),
        (r"{\fad(100,100)}text", "", "tag fad("),
        (r"{\fade(0,255,0,0,100,900,1000)}text", "", "tag fade("),
        (r"{\k20}text", "", "tag k"),
        (r"{\K20}text", "", "tag K"),
        (r"{\kf20}text", "", "tag kf"),
        (r"{\ko20}text", "", "tag ko"),
        ("text", "Banner;5;0;20", "effect Banner"),
        ("text", "Scroll Up;0;100;20", "effect Scroll Up"),
        ("text", "Scroll Down;0;100;20", "effect Scroll Down"),
    ],
)
def test_dynamic_classification_exposes_reason(text: str, effect: str, reason_fragment: str) -> None:
    classification = ass.classify_event_detail(_event(1, 0, 1_000, text, effect=effect))

    assert classification.kind is ass.EventKind.DYNAMIC
    assert reason_fragment in classification.reason


def test_static_classification_exposes_static_reason() -> None:
    classification = ass.classify_event_detail(_event(1, 0, 1_000, r"{\pos(100,100)\clip(0,0,200,200)}text"))

    assert classification == ass.EventClassification(ass.EventKind.STATIC, "static")


def _event(
    index: int,
    start_ms: int,
    end_ms: int,
    text: str = "text",
    *,
    effect: str = "",
) -> ass.AssEvent:
    return ass.AssEvent(index=index, start_ms=start_ms, end_ms=end_ms, style="Default", effect=effect, text=text)


def _timeline_signature(intervals: list[ass.TimelineInterval]) -> list[tuple[int, int, bool, tuple[int, ...]]]:
    return [
        (interval.start_ms, interval.end_ms, interval.dynamic, tuple(sorted(event.index for event in interval.active_events)))
        for interval in intervals
    ]


def _old_timeline_signature(
    events: list[ass.AssEvent],
    kinds: dict[int, ass.EventKind],
) -> list[tuple[int, int, bool, tuple[int, ...]]]:
    boundaries = sorted({time for event in events for time in (event.start_ms, event.end_ms)})
    result = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        if end <= start:
            continue
        active = tuple(event for event in events if event.start_ms < end and event.end_ms > start)
        if not active:
            continue
        result.append(
            (
                start,
                end,
                any(kinds[event.index] is ass.EventKind.DYNAMIC for event in active),
                tuple(sorted(event.index for event in active)),
            )
        )
    return result
