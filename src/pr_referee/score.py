"""Score both reviewers on the labelled set.

The two are not asked the same question in the same words, and forcing them to be would
misrepresent both. The referee reports *findings*; the model answers CHANGED or SAME. What
makes them comparable is the decision a reader actually takes from each: **does this diff
change behaviour?**

So the referee counts as saying "changed" when it emits a BEHAVIOUR_CHANGE finding, and
nothing else counts. Its structural findings - a swallowed error, a widened catch - are
real and useful and are *not* claims about behaviour differing on an input, so scoring them
here would be scoring the wrong thing. They are reported separately.

An unparseable model answer is its own bucket. Folding it into SAME flatters the false
positive rate; folding it into CHANGED flatters recall.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Score:
    name: str
    tp: int = 0  # flagged a genuinely behaviour-changing diff
    fp: int = 0  # flagged a diff that is equivalent by construction
    tn: int = 0
    fn: int = 0
    abstained: int = 0  # said nothing either way
    notes: list[str] = field(default_factory=list)

    @property
    def decided(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self) -> float | None:
        flagged = self.tp + self.fp
        return self.tp / flagged if flagged else None

    @property
    def recall(self) -> float | None:
        real = self.tp + self.fn
        return self.tp / real if real else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def row(self) -> dict:
        def pct(v):
            return None if v is None else round(v, 4)

        return {
            "reviewer": self.name,
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "abstained": self.abstained,
            "precision": pct(self.precision),
            "recall": pct(self.recall),
            "f1": pct(self.f1),
        }


def tally(name: str, labels: list[str], said_changed: list[bool | None]) -> Score:
    """`said_changed[i]` is True/False, or None for an abstention."""
    s = Score(name)
    for label, said in zip(labels, said_changed, strict=True):
        if said is None:
            s.abstained += 1
            continue
        if label == "bad":
            s.tp += said
            s.fn += not said
        else:
            s.fp += said
            s.tn += not said
    return s


def table(scores: list[Score]) -> str:
    def fmt(v):
        return "  -  " if v is None else f"{v:5.1%}"

    lines = [
        f"  {'reviewer':16} {'TP':>4} {'FP':>4} {'TN':>4} {'FN':>4} {'abst':>5} "
        f"{'precision':>10} {'recall':>8} {'F1':>7}",
        "  " + "-" * 74,
    ]
    for s in scores:
        lines.append(
            f"  {s.name:16} {s.tp:4} {s.fp:4} {s.tn:4} {s.fn:4} {s.abstained:5} "
            f"{fmt(s.precision):>10} {fmt(s.recall):>8} {fmt(s.f1):>7}"
        )
    return "\n".join(lines)
