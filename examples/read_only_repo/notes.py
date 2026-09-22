"""Small domain function used by the read-only ForgeAgent demo."""


def format_note(text: str) -> str:
    """Normalize a note and prefix a visible marker."""
    return f"[note] {text.strip().capitalize()}"
