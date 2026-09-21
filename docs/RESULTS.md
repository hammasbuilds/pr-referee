# Results

One run, reproduced by:

```bash
git clone --depth 1 https://github.com/pytoolz/toolz targets/toolz
pr-referee bench targets/toolz --limit 140
```

Model `qwen2.5-coder:14b` via Ollama on one Quadro RTX 5000. Every number below is read out
of `docs/bench-result.json` from that run, not typed by hand. It took 1242s, almost all
of it the model arm.

## The set

116 labelled diffs from `toolz`:

- **24 proven to change behaviour** — single-point AST mutations, kept only where an
  independent 240-wide input pool separates the two versions. A mutation the pool could not
  separate was discarded rather than labelled, since it may be an equivalent mutant and
  scoring a reviewer for missing a difference that may not exist would invent a number.
- **92 equivalent by construction** — a local renamed consistently, a return
  expression pulled into a temporary, `x = x + 1` written `x += 1`. Any flag on one of these
  is a false positive.

Both sides of every case are round-tripped through `ast.unparse`, so the only textual
difference is the change itself.

## Scores

| reviewer | TP | FP | TN | FN | abstained | precision | recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **pr-referee** | 22 | **0** | 45 | 0 | 49 | **100.0%** | 100.0% | 100.0% |
| `qwen2.5-coder:14b` | 18 | 1 | 91 | 6 | 0 | 94.7% | 75.0% | 83.7% |

Coverage: the referee decided **67/116 (57.8%)**; the model
decided all 116.

Of the 24 real behaviour changes, the referee could execute 22 and
caught every one. The model saw all 24 and **missed 6 (25%)**.

## What the numbers say

**The referee is a gate; the model is a reader.** Zero false alarms in 67 decisions is
what lets a check block a merge — one false alarm a week and it gets disabled. But it
declined 49 of 116 cases, and a gate silent on 42% of changes is not a
substitute for review.

**The model is good at this, and better than expected.** 94.7% precision against the 44.8%
measured in [code-llm-lab](https://github.com/hammasbuilds/code-llm-lab) project 08. The
difference is the question, not the model: project 08 showed one version and asked *"is
there a bug?"*; this shows both and asks *"did behaviour change?"*. A comparison is an
easier question than a judgement, and the practical lesson is to pose the comparison.

**Its weakness is recall, not precision.** It missed 1 real behaviour change in 4. A missed
change is silent, and silence from a reviewer reads as approval — which is the failure mode
that matters when the thing is wired into CI.

## Not scored

The referee also raised **35 structural findings** — swallowed
errors, widened catches, removed branches. They are facts about the shape of a change rather
than claims that behaviour differs on an input, so scoring them against these labels would be
scoring the wrong thing.

## Limits

- **One repository.** `toolz` is small, pure and functional, which suits an engine that has
  to call things. A repo of stateful classes would push the abstention rate far higher.
- **Mutations are not human mistakes.** A single-point AST mutation is a proxy for a real
  bug, not a sample of one.
- **92 good cases against 24 bad ones** is not a realistic base rate for a
  code review, and precision depends on the base rate. The counts are given so the numbers
  can be recomputed for another one.
