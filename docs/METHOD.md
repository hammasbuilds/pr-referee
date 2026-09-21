# Method

How a finding is produced, and what each one is and is not allowed to claim.

## The rule

**No finding without a reproduction.** Every one carries an input, a pair of values or a
quoted construct that a reader can check in seconds without reading the diff. A comment
that cannot be checked costs the reader more than it saves, so this tool does not make one.

The corollary is that it stays quiet about a great deal — naming, structure, length,
whether an abstraction is the right one. All real concerns. None of them provable by
execution, so none of them here.

## The five findings

| kind | what it means | the evidence |
|---|---|---|
| `behaviour-change` | old and new return different things | the argument set, and both results |
| `swallowed-error` | an exception path that used to propagate no longer does | the handler, quoted |
| `widened-catch` | an `except` clause now catches strictly more | before and after breadth |
| `removed-branch` | a conditional test present before is gone | the `if` test, quoted |
| `weakened-tests` | the function got harder to tell from a broken version of itself | mutants distinguished, before and after |

Only the first is a claim about behaviour differing on an input. The other four are facts
about the structure of the change, and they exist because there is a class of problem the
differential engine cannot reach: a swallowed exception only shows up on an input that
triggers it, and the argument sets often will not.

## Getting the two versions

Diff hunks are not parsed. A hunk is a set of line ranges, and a line range cannot be
called — which makes it useless to an engine whose only evidence is execution. Instead both
whole files are fetched with `git show`, both are parsed, and functions are matched by name.

That loses two cases and gains one:

- **Lost:** a renamed function reads as one deletion plus one addition, and a pure move
  between files is invisible.
- **Gained:** a function whose behaviour changed because a module-level constant above it
  changed is caught, because its header changed even though its own lines did not. A
  hunk-based reader would miss that entirely.

**Methods are out of scope.** A method cannot be called without an instance, and building
one means running the code under review. They are skipped rather than guessed at.

## The differential engine

Both versions are loaded into **separate module namespaces** in one process and called on
the same arguments.

Five details here were each a bug first, found while building the sibling tool
[repo-surgeon](https://github.com/hammasbuilds/repo-surgeon). They share a shape: the
comparison produced a *confident wrong answer* rather than an error. For a review tool that
is the worst available failure — a false accusation costs the reader more than silence, and
a few of those is how a reviewer gets muted.

1. **Separate namespaces.** Defining both versions in one namespace and taking a reference
   to each breaks recursion: the body's self-call resolves through module globals, so once
   the second `def` shadows the name, the old function recurses into the new one and the
   two appear to agree.
2. **The same `__name__` for both.** A class defined inside a namespace reprs as
   `<__name__.Foo object ...>`, so distinct names make every returned instance a false
   difference.
3. **Memory addresses normalised.** `<Foo object at 0x7f...>` reprs differently on every
   allocation. Comparing raw makes any function returning a plain object a guaranteed
   disagreement — reported with the address itself as the proof.
4. **Annotations are not evaluated.** Modules routinely annotate with names that exist only
   under `if TYPE_CHECKING:`; evaluating them raises `NameError` on a perfectly callable
   function.
5. **The package is importable.** Without `sys.path` and `__package__` set the way the real
   module would have them, anything doing `from . import x` cannot load, and the result
   reads "could not verify" when the truth is "nothing put the package on the path".

**An exception is a result.** A function that raised `KeyError` and now returns `None` has
changed behaviour. Comparing only return values would call that agreement.

**Agreement only counts when the call did something.** If every input makes both sides
raise, the arguments were wrong for that function and the run established nothing. That is
`inconclusive` — reported, counted, and never treated as a clean bill of health.

## Where the inputs come from

This rung is only as good as the values it tries, and the two obvious approaches both fail:

- **Random values** almost never satisfy a real function's preconditions. Both sides raise,
  the two "agree", and nothing was established. Agreement on garbage is not evidence.
- **Type-driven values** are tidy — `"string"`, `1`, `[]` — and tidy values are exactly
  where the interesting divergences are not.

So, in order:

1. **Harvested from the repo's own tests.** Every literal in every test file, indexed by
   the keyword argument it was passed as. Real values from the real domain.
2. **Type-driven**, from the annotation where there is one.
3. **A hand-written corner pool** per parameter name: `""`, `"a/b/"`, `".bashrc"`,
   `"archive.tar.gz"`, `"/abs"`, `"a/../b"`, `0`, `-1`, `None`, `()`.

Arguments vary **one at a time** around a baseline, so a witness is immediately readable —
exactly one value differs from the baseline, so that value is the cause.

## The benchmark

Precision is measured, not asserted. Asserting "the findings are provable, therefore
precise" is circular; running the ordinary approach over identical inputs and counting is
not.

**GOOD cases** are behaviour-preserving *by construction*: rename a local consistently,
pull a return expression into a named temporary, turn `x = x + 1` into `x += 1`. These are
not believed safe, they are safe by the definition of the transform. Any flag is a false
positive.

**BAD cases** are single-point AST mutations, kept only when an **independent, 240-wide**
input pool proves the two versions disagree. The independence is the point: labelling with
the same argument sets the referee later receives would guarantee it perfect recall and
measure nothing. Labelling wide, then handing the referee its ordinary heuristic sets,
makes recall a real question about the input generation.

A mutation the wide pool cannot separate is **discarded**, not labelled BAD. It may be an
equivalent mutant, and scoring a reviewer for missing a difference that may not exist would
be inventing a number.

**Both sides of every case are round-tripped through `ast.unparse`**, so the only textual
difference is the change itself. Without that, the "new" side is unparsed while the "old"
side is original source, and every diff carries a wall of cosmetic noise. That noise falls
on GOOD and BAD alike so it does not bias the direction — but it hands the model a harder
problem than the one being asked about, and a comparison worth reporting should not need
that kind of help.

## Scoring

The two reviewers are not asked the same question in the same words, and forcing them to be
would misrepresent both. What makes them comparable is the decision a reader takes from
each: **does this diff change behaviour?**

So the referee counts as saying "changed" when it emits a `behaviour-change` finding, and
nothing else counts. Its structural findings are real and useful and are *not* claims about
behaviour differing on an input, so scoring them here would be scoring the wrong thing.
They are reported separately.

**An abstention is its own bucket.** For the referee that means it could not execute the
function; for the model, that it did not answer in the required form. Folding either into
"same" flatters the false-positive rate, and into "changed" flatters recall. Neither is a
fair reading.
