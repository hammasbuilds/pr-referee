"""Command line entry point.

    pr-referee review <repo> --base main --head HEAD
    pr-referee bench  <repo> --limit 120

`review` is the tool. `bench` is the evidence that the tool is worth using: it builds a
labelled set of diffs where the right answer is known, runs both the referee and a model
over it, and reports precision and recall for each.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pr_referee import bench as bench_mod
from pr_referee import referee as referee_mod
from pr_referee import reviewer as reviewer_mod
from pr_referee import score as score_mod
from pr_referee.types import Kind
from pr_referee.values import harvest


def cmd_review(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    out = referee_mod.review(repo, args.base, args.head, args.workers)

    print(f"\n{repo.name}: {args.base}...{args.head}")
    print(f"  {out.examined} changed function(s) examined")
    if out.unprovable:
        print(f"  {out.unprovable} could not be executed, so nothing is claimed about them")

    if not out.findings:
        print("\n  no provable findings.")
        print("  That is not 'looks good to me' - it is 'nothing here could be proven wrong'.")
    else:
        print(f"\n  {len(out.findings)} finding(s):\n")
        for f in out.sorted():
            print(f"  [{f.kind.value}] {f.where}")
            print(f"    {f.summary}")
            for line in f.evidence.splitlines():
                print(f"    | {line}")
            if f.detail:
                print(f"    -> {f.detail}")
            print()

    print(f"  took {out.seconds:.0f}s")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "examined": out.examined,
                    "unprovable": out.unprovable,
                    "by_kind": out.by_kind(),
                    "findings": [f.as_row() for f in out.sorted()],
                },
                indent=2,
            ),
            encoding="utf-8",
            newline="",
        )
        print(f"  wrote {args.json}")

    return 1 if args.fail_on_finding and out.findings else 0


def cmd_bench(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    t0 = time.time()

    print(f"building labelled diffs from {repo.name}...")
    cases = bench_mod.build(repo, limit=args.limit, seed=args.seed)
    good = sum(c.label == "good" for c in cases)
    bad = sum(c.label == "bad" for c in cases)
    print(f"  {len(cases)} cases: {bad} provably behaviour-changing, {good} equivalent")
    if not cases:
        print("  nothing to score")
        return 1

    labels = [c.label for c in cases]
    harvested = harvest(repo)

    print("running the referee...")
    referee_said: list[bool | None] = []
    structural = 0
    for c in cases:
        findings, provable = referee_mod.examine(c.fn, harvested)
        structural += sum(f.kind is not Kind.BEHAVIOUR_CHANGE for f in findings)
        if not provable:
            referee_said.append(None)
        else:
            referee_said.append(any(f.kind is Kind.BEHAVIOUR_CHANGE for f in findings))

    scores = [score_mod.tally("pr-referee", labels, referee_said)]

    if not args.no_llm:
        print(f"running the {args.model} reviewer over the same diffs...")
        verdicts = reviewer_mod.review_all([c.fn for c in cases], args.model, args.workers)
        llm_said = [None if v is None else (v == "CHANGED") for v in verdicts]
        scores.append(score_mod.tally(args.model, labels, llm_said))

    print("\n" + "=" * 78)
    print(f"REVIEWER COMPARISON - {len(cases)} labelled diffs from {repo.name}")
    print("=" * 78)
    print(score_mod.table(scores))
    print(
        "\n  TP = flagged a diff proven to change behaviour"
        "\n  FP = flagged a diff that is equivalent by construction"
        "\n  abstained = could not execute it / did not answer in the required form"
    )
    print(f"\n  the referee also raised {structural} structural finding(s) - swallowed")
    print("  errors, widened catches, removed branches - which are not behaviour claims")
    print("  and are therefore not scored above.")
    print(f"\n  took {time.time() - t0:.0f}s")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "repo": repo.name,
                    "cases": len(cases),
                    "bad": bad,
                    "good": good,
                    "structural_findings": structural,
                    "scores": [s.row() for s in scores],
                    "seconds": round(time.time() - t0, 1),
                },
                indent=2,
            ),
            encoding="utf-8",
            newline="",
        )
        print(f"  wrote {args.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pr-referee", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="report only what can be proven about a diff")
    r.add_argument("repo")
    r.add_argument("--base", default="main")
    r.add_argument("--head", default="HEAD")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--json")
    r.add_argument("--fail-on-finding", action="store_true", help="exit 1 if anything is found")
    r.set_defaults(fn=cmd_review)

    b = sub.add_parser("bench", help="score the referee and a model on labelled diffs")
    b.add_argument("repo")
    b.add_argument("--limit", type=int, default=150)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--model", default="qwen2.5-coder:14b")
    b.add_argument("--workers", type=int, default=8)
    b.add_argument("--no-llm", action="store_true")
    b.add_argument("--json")
    b.set_defaults(fn=cmd_bench)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
