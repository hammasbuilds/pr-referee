"""Structural checks. No execution, but still no opinions.

Each of these compares the old and new ASTs and reports a fact about the difference. They
exist because there is a class of change the differential engine cannot reach: a swallowed
exception or a deleted branch only shows up on an input that triggers it, and the argument
sets often will not.

They are deliberately narrow. Every one names a *specific* construct that was there before
and is not now, or vice versa - never "this looks risky". The evidence is the construct
itself, quoted, so the reader confirms it by looking at the diff rather than by trusting
the tool.
"""

from __future__ import annotations

import ast

from pr_referee.types import ChangedFunction, Finding, Kind

# Ordered broadest-first. An `except` clause moving up this list catches strictly more.
BREADTH = ["BaseException", "Exception"]


def _parse(src: str) -> ast.AST | None:
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def _handlers(tree: ast.AST) -> list[ast.ExceptHandler]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """A handler whose entire body discards the error."""
    body = [n for n in handler.body if not isinstance(n, ast.Pass)]
    if not body:
        return True
    if len(body) == 1 and isinstance(body[0], ast.Return):
        v = body[0].value
        return v is None or (isinstance(v, ast.Constant) and v.value in (None, False))
    return False


def _caught_names(handler: ast.ExceptHandler) -> list[str]:
    t = handler.type
    if t is None:
        return ["<bare except>"]
    if isinstance(t, ast.Tuple):
        return [ast.unparse(e) for e in t.elts]
    return [ast.unparse(t)]


def _breadth(names: list[str]) -> int:
    """How much a handler catches. Higher is broader."""
    if "<bare except>" in names:
        return 3
    if "BaseException" in names:
        return 3
    if "Exception" in names:
        return 2
    return 1


def check_swallowed(fn: ChangedFunction) -> list[Finding]:
    """An error that used to propagate and now does not."""
    old, new = _parse(fn.old), _parse(fn.new)
    if old is None or new is None:
        return []

    old_silent = {tuple(_caught_names(h)) for h in _handlers(old) if _is_silent(h)}
    out = []
    for h in _handlers(new):
        if not _is_silent(h):
            continue
        names = tuple(_caught_names(h))
        if names in old_silent:
            continue  # it was already being swallowed; this change did not do it
        out.append(
            Finding(
                kind=Kind.SWALLOWED_ERROR,
                where=f"{fn.path}::{fn.name}",
                summary=f"`except {', '.join(names)}` now discards the error",
                evidence=ast.unparse(h)[:300],
                detail=(
                    "The caller can no longer tell a failure from a success. If this is "
                    "intended, the handler usually wants a log line or a comment saying so."
                ),
            )
        )
    return out


def check_widened_catch(fn: ChangedFunction) -> list[Finding]:
    """`except ValueError` becoming `except Exception` catches strictly more."""
    old, new = _parse(fn.old), _parse(fn.new)
    if old is None or new is None:
        return []

    old_max = max((_breadth(_caught_names(h)) for h in _handlers(old)), default=0)
    new_handlers = _handlers(new)
    new_max = max((_breadth(_caught_names(h)) for h in new_handlers), default=0)

    if new_max <= old_max or not new_handlers:
        return []

    broadest = max(new_handlers, key=lambda h: _breadth(_caught_names(h)))
    names = ", ".join(_caught_names(broadest))
    return [
        Finding(
            kind=Kind.WIDENED_CATCH,
            where=f"{fn.path}::{fn.name}",
            summary=f"the exception clause widened to `{names}`",
            evidence=f"broadest handler before: level {old_max}; after: level {new_max}",
            detail=(
                "A broader clause also catches the errors nobody meant to handle - "
                "KeyboardInterrupt and MemoryError among them, if it reaches BaseException."
            ),
        )
    ]


def check_removed_branch(fn: ChangedFunction) -> list[Finding]:
    """A conditional test that was there and is gone.

    Compared by the unparsed text of each `if` test, so reordering or reformatting the
    same conditions produces nothing. Only a test that no longer appears at all counts.
    """
    old, new = _parse(fn.old), _parse(fn.new)
    if old is None or new is None:
        return []

    def tests(tree: ast.AST) -> list[str]:
        return [ast.unparse(n.test) for n in ast.walk(tree) if isinstance(n, ast.If)]

    old_tests, new_tests = tests(old), tests(new)
    gone = [t for t in old_tests if t not in new_tests]
    if not gone:
        return []

    return [
        Finding(
            kind=Kind.REMOVED_BRANCH,
            where=f"{fn.path}::{fn.name}",
            summary=f"{len(gone)} conditional path(s) no longer present",
            evidence="; ".join(f"`if {t}`" for t in gone[:3]),
            detail=(
                "Whatever the removed branch did is now unreachable. That is often the "
                "point of the change - but it is worth confirming it was the point."
            ),
        )
    ]


ALL = [check_swallowed, check_widened_catch, check_removed_branch]
