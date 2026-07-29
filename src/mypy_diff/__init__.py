"""mypy-diff — show only the mypy errors that changed between two revisions."""

from __future__ import annotations

__version__ = "0.1.2"


class MypyDiffError(Exception):
    """Any failure that should end the run with exit code 2 and a readable message.

    ``details`` are printed as indented follow-up lines, typically actionable hints.
    """

    def __init__(self, message: str, *details: str) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
