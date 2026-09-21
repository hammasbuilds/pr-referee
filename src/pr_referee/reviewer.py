"""The comparison arm: a model reviewing the same diffs.

This exists so the referee's precision is a measurement rather than a claim. Asserting
"our findings are provable, therefore precise" is circular; running the ordinary approach
over the identical inputs and counting is not.

The prompt is written to give the model its best shot, not a straw man. It gets the whole
function before and after, it is told to answer only about behaviour, and it is explicitly
warned off style - which is the single biggest source of noise in a naive review prompt.
If it still flags unchanged behaviour, that is a property of the approach rather than of a
lazy prompt.

`code-llm-lab` project 08 measured a related number on whole functions rather than diffs:
43.3% of genuinely broken code caught, 16.8% of the model's own working code flagged, and
38.8% of MBPP's human-written reference solutions flagged - 44.8% precision overall.
"""

from __future__ import annotations

import re

from pr_referee.model import generate_many
from pr_referee.types import ChangedFunction

PROMPT = """You are reviewing one function in a pull request.

BEFORE:
```python
{old}
```

AFTER:
```python
{new}
```

Does this change alter the function's behaviour for any input?

Judge behaviour only. Do not comment on naming, style, formatting, readability, type hints
or performance. A rename, a reordering, or an added comment is NOT a behaviour change.

Answer with exactly two lines:
VERDICT: CHANGED
REASON: <one line>

or:

VERDICT: SAME
REASON: <one line>
"""

_VERDICT = re.compile(r"VERDICT\s*:\s*(CHANGED|SAME)\b", re.I)


def parse_verdict(raw: str | None) -> str | None:
    """`CHANGED`, `SAME`, or None when the model did not answer in the required shape.

    Unparseable answers stay their own category. Folding them into `SAME` would flatter
    the false-positive rate; folding them into `CHANGED` would flatter recall. Neither is
    a fair reading of a model that did not answer the question.
    """
    if not raw:
        return None
    m = _VERDICT.search(raw)
    return m.group(1).upper() if m else None


def review_all(
    fns: list[ChangedFunction],
    model: str = "qwen2.5-coder:14b",
    workers: int = 8,
) -> list[str | None]:
    """One verdict per changed function, in order."""
    if not fns:
        return []
    raws = generate_many(
        [PROMPT.format(old=f.old, new=f.new) for f in fns],
        model=model,
        temperature=0.0,
        workers=workers,
        num_predict=160,
        progress="llm review",
    )
    return [parse_verdict(r) for r in raws]
