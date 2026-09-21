"""What a finding is, and the rule that governs all of them.

**No finding without a reproduction.** Every `Finding` carries `evidence` - an input, a
command, a pair of numbers - that a reader can check in seconds without reading the diff.
That constraint is the entire design, and it is what separates this from an LLM reviewer:
a comment that cannot be checked costs the reader more than it saves.

The corollary is that this tool stays quiet about a great deal. Naming, structure, style,
whether a function is too long - all real concerns, none of them provable by execution, so
none of them reported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    """The only things this tool claims. Each is backed by something that ran."""

    BEHAVIOUR_CHANGE = "behaviour-change"
    """The old and new versions return different things for some input."""

    WEAKENED_TESTS = "weakened-tests"
    """The change made the function harder to distinguish from a broken version."""

    SWALLOWED_ERROR = "swallowed-error"
    """An exception path that used to propagate now does not."""

    REMOVED_BRANCH = "removed-branch"
    """A conditional path present before is gone, so its behaviour is unreachable."""

    WIDENED_CATCH = "widened-catch"
    """An `except SomeError` became broader, catching more than it used to."""


SEVERITY = {
    Kind.BEHAVIOUR_CHANGE: 3,
    Kind.SWALLOWED_ERROR: 2,
    Kind.WIDENED_CATCH: 2,
    Kind.REMOVED_BRANCH: 1,
    Kind.WEAKENED_TESTS: 1,
}


@dataclass
class ChangedFunction:
    """One function that differs between the two revisions."""

    path: str
    name: str
    old: str
    new: str
    header: str = ""  # imports the function reads, pruned from its module
    sys_path: str = ""
    package: str = ""
    lineno: int = 0

    @property
    def key(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass
class Finding:
    kind: Kind
    where: str
    summary: str
    evidence: str
    """How to see it for yourself. An input, two values, a command - never a claim."""

    detail: str = ""

    @property
    def severity(self) -> int:
        return SEVERITY[self.kind]

    def as_row(self) -> dict:
        return {
            "kind": self.kind.value,
            "where": self.where,
            "summary": self.summary,
            "evidence": self.evidence,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass
class Review:
    findings: list[Finding] = field(default_factory=list)
    examined: int = 0
    unprovable: int = 0
    """Functions the engine could not call, so could say nothing about either way.

    Counted and reported. A reviewer that silently skips what it cannot handle looks
    more thorough than it is, and the gap is exactly where a real problem hides.
    """
    seconds: float = 0.0

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (-f.severity, f.where))

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind.value] = out.get(f.kind.value, 0) + 1
        return out
