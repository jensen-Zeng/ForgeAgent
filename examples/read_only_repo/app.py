"""Tiny Notes command-line entry point."""

from notes import format_note


def main() -> None:
    """Format and print one example note."""
    print(format_note("learn agent loops"))


if __name__ == "__main__":
    main()
