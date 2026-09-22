"""Tests documenting Tiny Notes behavior."""

from notes import format_note


def test_format_note_normalizes_text() -> None:
    assert format_note("  hello agent  ") == "[note] Hello agent"
