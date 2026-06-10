"""Stable exit-code matrix for the ``metacrucible`` console entry point.

This module is the single source of truth for the exit codes returned
by the ``metacrucible`` console script (declared in ``pyproject.toml``
under ``[project.scripts]``) and ``python -m metacrucible``. Pinning
the codes here means subprocess contracts in scripts and CI stay
stable as the CLI grows, and command handlers can import a symbolic
name instead of repeating literal ``2`` s in ``return`` statements.

Matrix
------

``EXIT_OK``
    Success. The command ran and produced the requested outcome.

``EXIT_USER_ERROR`` (1)
    Argparse usage error: unknown subcommand, missing required
    positional or flag, invalid argument value. Argparse internally
    defaults to ``2`` for usage errors; the console entry translates
    that to ``1`` so callers can distinguish a "you typed it wrong"
    outcome from a "we ran but a semantic precondition blocked us"
    outcome.

``EXIT_BLOCKED`` (2)
    Semantic precondition failure. The command ran to completion
    and reported at least one blocker (e.g. ``init --check`` on an
    empty benchmark, ``promote`` on a missing case id). The blocker
    details are emitted to stdout (JSON) or stderr (human) so callers
    can branch on the id; the exit code is the stable branch signal.

``EXIT_INTERNAL_ERROR`` (3)
    Uncaught exception past the command dispatcher. The dispatcher
    writes an English error message to stderr before returning this
    code; callers should treat it as a bug report, not a normal
    control-flow signal.

``CHECK_BLOCKED_EXIT_CODE``
    Compatibility alias for ``EXIT_BLOCKED``. Kept so external
    callers (test helpers, older scripts) that imported the
    original name continue to work.
"""
from __future__ import annotations

from typing import Final

#: Success — the command ran and produced the requested outcome.
EXIT_OK: Final[int] = 0

#: Argparse usage error — unknown subcommand, missing required
#: positional, or otherwise unparseable argv. Argparse's internal
#: default is ``2``; we map it to ``1`` to keep argparse codes
#: distinct from our semantic exit codes.
EXIT_USER_ERROR: Final[int] = 1

#: Blocked / precondition failure — the command ran but at least
#: one semantic blocker prevented the requested outcome (e.g.
#: ``init --check`` on an empty benchmark, ``promote`` on an
#: unknown case id).
EXIT_BLOCKED: Final[int] = 2

#: Uncaught exception past the command dispatcher. The dispatcher
#: writes an English error message to stderr before returning
#: this code; callers should treat it as a bug report, not a
#: normal control-flow signal.
EXIT_INTERNAL_ERROR: Final[int] = 3

#: Compatibility alias for :data:`EXIT_BLOCKED`. Kept so external
#: callers that imported the original name continue to work.
CHECK_BLOCKED_EXIT_CODE: Final[int] = EXIT_BLOCKED

#: Canonical mapping of symbolic exit code names to integers.
#: Use this when you need to enumerate or serialize the matrix
#: (e.g. for documentation generation or a future
#: ``--list-exit-codes`` flag). Keys are stable; values must
#: match the corresponding ``EXIT_*`` constants above.
EXIT_CODES: Final[dict[str, int]] = {
    "EXIT_OK": EXIT_OK,
    "EXIT_USER_ERROR": EXIT_USER_ERROR,
    "EXIT_BLOCKED": EXIT_BLOCKED,
    "EXIT_INTERNAL_ERROR": EXIT_INTERNAL_ERROR,
}


__all__ = [
    "EXIT_OK",
    "EXIT_USER_ERROR",
    "EXIT_BLOCKED",
    "EXIT_INTERNAL_ERROR",
    "CHECK_BLOCKED_EXIT_CODE",
    "EXIT_CODES",
]
