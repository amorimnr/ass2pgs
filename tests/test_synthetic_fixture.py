from __future__ import annotations

from pathlib import Path

from jellyfin_ass2pgs import ass
from jellyfin_ass2pgs.libass_renderer import validate_attached_fonts


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic.ass"


def test_public_ass_fixture_covers_static_and_dynamic_events() -> None:
    events = ass.visible_events(ass.load(FIXTURE))
    classifications = [ass.classify_event(event) for event in events]

    assert len(events) == 6
    assert classifications == [ass.EventKind.STATIC] + [ass.EventKind.DYNAMIC] * 5
    assert any(interval.dynamic for interval in ass.build_timeline(events))


def test_public_ass_fixture_references_only_a_synthetic_custom_font() -> None:
    validation = validate_attached_fonts(FIXTURE, ())

    assert "Synthetic Fixture Sans" in validation.required
    assert "Synthetic Fixture Sans" in validation.missing_from_attachments
