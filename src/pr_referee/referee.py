"""Run every check over every changed function, and report only what ran.

The order is by cost. The structural checks are free, so they run on everything. The
differential engine spawns a subprocess per function, so it runs once and its result feeds
the mutation check too.

The counter that matters as much as the findings is `unprovable`. When a function cannot be
called - it needs an object the harness cannot build, or a dependency that is not installed
- this tool has nothing to say about its behaviour, and it says so out loud. A reviewer
that quietly skips what it cannot handle looks more thorough than it is, and the gap is
exactly where a real problem would sit.
"""

from __future__ import annotations

import ast
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pr_referee.checks import statics
from pr_referee.engine import mutation
from pr_referee.engine.differential import compare
from pr_referee.extract import changed_functions
from pr_referee.types import ChangedFunction, Finding, Kind, Review
from pr_referee.values import argument_sets, harvest

# How much of the original's distinguishability a change may lose before it is reported.
# Not zero: the score is measured over a capped mutant set and a legitimate rewrite shifts
# which mutants get generated, so exact parity would report noise as a finding.
MUTATION_TOLERANCE = 0.25


def _argsets(fn: ChangedFunction, harvested: dict[str, list[str]]) -> list[str]:
    try:
        tree = ast.parse(fn.old)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == fn.name:
            return argument_sets(node, harvested)
    return []


def examine(fn: ChangedFunction, harvested: dict[str, list[str]]) -> tuple[list[Finding], bool]:
    """(findings, provable). `provable` is False when nothing could be executed."""
    findings: list[Finding] = []
    for check in statics.ALL:
        findings.extend(check(fn))

    argsets = _argsets(fn, harvested)
    ctx = (30.0, fn.sys_path, fn.package)
    result = compare(fn.header, fn.old, fn.new, fn.name, argsets, *ctx)

    if result["status"] == "differs":
        w = result["witness"]
        findings.append(
            Finding(
                kind=Kind.BEHAVIOUR_CHANGE,
                where=f"{fn.path}::{fn.name}",
                summary="this change alters what the function returns",
                evidence=f"{fn.name}{w['args']}\n  before: {w['old']}\n  after:  {w['new']}",
                detail=result["detail"],
            )
        )
        # A function whose behaviour already changed makes the mutation comparison
        # meaningless - the two versions are different functions, so a difference in
        # distinguishability says nothing about the tests.
        return findings, True

    if result["status"] != "agree":
        return findings, False

    before = mutation.kill_rate(fn.header, fn.old, fn.name, argsets, *ctx)
    after = mutation.kill_rate(fn.header, fn.new, fn.name, argsets, *ctx)
    if before["rate"] is not None and after["rate"] is not None:
        drop = before["rate"] - after["rate"]
        if drop > MUTATION_TOLERANCE:
            findings.append(
                Finding(
                    kind=Kind.WEAKENED_TESTS,
                    where=f"{fn.path}::{fn.name}",
                    summary="the change made this function harder to tell from a broken one",
                    evidence=(
                        f"single-point mutants distinguished: "
                        f"{before['detail']} -> {after['detail']}"
                    ),
                    detail=(
                        "Behaviour is unchanged on every input tried, but fewer broken "
                        "versions of the new code can be told apart from it - usually a "
                        "lost branch or a collapsed comparison."
                    ),
                )
            )
    return findings, True


def review(
    repo: Path,
    base: str,
    head: str,
    workers: int = 4,
) -> Review:
    t0 = time.time()
    out = Review()

    fns = changed_functions(repo, base, head)
    out.examined = len(fns)
    if not fns:
        out.seconds = time.time() - t0
        return out

    harvested = harvest(repo)

    def one(fn: ChangedFunction) -> tuple[list[Finding], bool]:
        return examine(fn, harvested)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for findings, provable in pool.map(one, fns):
            out.findings.extend(findings)
            if not provable:
                out.unprovable += 1

    out.seconds = time.time() - t0
    return out
