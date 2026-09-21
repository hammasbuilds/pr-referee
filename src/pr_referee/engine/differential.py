"""Run both versions of a function on the same inputs and compare.

Every non-trivial detail here was a bug first, found while building the sibling tool
`repo-surgeon`. They are all the same shape: the comparison produced a *confident wrong
answer* rather than an error, which for a review tool is the worst available failure - a
false accusation costs the reader more than silence would.

- **Separate module namespaces per version.** Defining both in one namespace and taking a
  reference to each breaks recursion: the body's self-call resolves through module globals,
  so after the second `def` shadows the name, the old function recurses into the new one
  and they appear to agree.
- **The same `__name__` for both namespaces.** A class defined inside one reprs as
  `<__name__.Foo object ...>`, so distinct names make every returned instance look like a
  difference.
- **Memory addresses normalised.** `<Foo object at 0x7f...>` reprs differently on every
  allocation; comparing raw makes any function returning a plain object a guaranteed
  "disagreement", reported with the address as proof.
- **Annotations are not evaluated.** Modules routinely annotate with names that only exist
  under `if TYPE_CHECKING:`, and evaluating them raises `NameError` on a function that is
  perfectly callable.
- **The package is importable.** Without it, a module doing `from . import x` cannot load
  at all, and the result reads "could not verify" when the truth is "nothing put the
  package on the path".
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = """
import json, re, sys, types

SYS_PATH = {sys_path!r}
PACKAGE = {package!r}
HEADER = {header!r}
OLD = {old!r}
NEW = {new!r}
NAME = {name!r}

if SYS_PATH:
    sys.path.insert(0, SYS_PATH)

PREAMBLE = "from __future__ import annotations\\n"
ADDR = re.compile(r"0x[0-9a-fA-F]{{4,}}")


def _load(tag, source):
    # Both namespaces take the SAME __name__: they are separate module objects either
    # way, and a class defined inside reprs with its module, so differing names would
    # turn every returned instance into a false disagreement.
    name = (PACKAGE + ".rs_probe") if PACKAGE else "rs_probe"
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    mod.__dict__["__package__"] = PACKAGE
    exec(compile(PREAMBLE + HEADER + "\\n\\n" + source, "<" + tag + ">", "exec"), mod.__dict__)
    return mod.__dict__[NAME]


for tag, src in (("old", OLD), ("new", NEW)):
    try:
        globals()[tag.upper() + "_FN"] = _load(tag, src)
    except Exception as exc:
        print("__PR_LOAD_FAIL__" + tag + ": " + type(exc).__name__ + ": " + str(exc)[:200])
        raise SystemExit(0)


def _norm(text):
    return ADDR.sub("0x...", text)


MAX_ITEMS = 64


def _render(value):
    # repr, except that a lazy iterator is drained first.
    #
    # `<itertools.islice object at 0x...>` reprs identically no matter what it would
    # yield, so a function returning a generator or an islice compares equal to any
    # other - and a real change in what it produces is invisible. Draining it is the only
    # way to see the behaviour at all.
    #
    # Bounded, because an infinite generator is a perfectly ordinary return value. When
    # the bound is hit that is shown, so a difference past it is not claimed absent.
    if hasattr(value, "__next__") and not isinstance(value, (str, bytes)):
        items = []
        try:
            for i, item in enumerate(value):
                if i >= MAX_ITEMS:
                    return repr(items) + "...(truncated)"
                items.append(item)
        except Exception as exc:
            # Consuming it raised: that is behaviour too, and part of the comparison.
            # Plain concatenation, not an f-string: this whole module is a .format()
            # template, and an f-string's braces would be eaten as placeholders.
            return repr(items) + "...then " + type(exc).__name__ + ": " + str(exc)[:120]
        return repr(items)
    return repr(value)


def _call(fn, args):
    try:
        return ("ok", _norm(_render(fn(*args))))
    except Exception as exc:
        return ("raise", _norm(type(exc).__name__ + ": " + str(exc)[:200]))


ARGSETS = {argsets}

rows = []
for args in ARGSETS:
    a = _call(OLD_FN, args)
    b = _call(NEW_FN, args)
    rows.append({{"args": repr(args), "old": a, "new": b, "same": a == b}})

print("__PR_JSON__" + json.dumps(rows))
"""


def compare(
    header: str,
    old: str,
    new: str,
    func: str,
    argsets: list[str],
    timeout: float = 30.0,
    sys_path: str = "",
    package: str = "",
) -> dict:
    """Compare two versions. Returns a verdict dict; never raises.

    Statuses: `differs` (with a witness), `agree`, `inconclusive` (nothing was exercised),
    `old_uncallable` / `new_uncallable`, `timeout`, `error`.
    """
    if not argsets:
        return {"status": "inconclusive", "detail": "no argument sets could be built"}

    script = RUNNER.format(
        sys_path=sys_path,
        package=package,
        header=header,
        old=old,
        new=new,
        name=func,
        argsets="[" + ", ".join(argsets) + "]",
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "compare.py"
        # newline="" or Windows rewrites newlines to CR CR LF, breaking any line
        # continuation inside the source under comparison.
        path.write_text(script, encoding="utf-8", newline="")
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "detail": f"no result within {timeout}s"}

    out = proc.stdout or ""

    if "__PR_LOAD_FAIL__" in out:
        which = out.split("__PR_LOAD_FAIL__", 1)[1].strip().splitlines()[0]
        side = "old" if which.startswith("old") else "new"
        return {"status": f"{side}_uncallable", "detail": which[:300]}

    marker = out.find("__PR_JSON__")
    if marker < 0:
        err = (proc.stderr or out).strip().splitlines()
        return {"status": "error", "detail": err[-1][:300] if err else "no output"}

    rows = json.loads(out[marker + len("__PR_JSON__") :])
    disagreements = [r for r in rows if not r["same"]]
    exercised = [r for r in rows if r["old"][0] == "ok" or r["new"][0] == "ok"]

    if not exercised:
        # Every input raised on both sides: the arguments were wrong for this function
        # and the run established nothing. Reporting that as agreement is how an
        # unverified change gets a clean bill of health.
        return {
            "status": "inconclusive",
            "detail": f"all {len(rows)} argument sets raised on both sides",
            "tried": len(rows),
        }

    if disagreements:
        # Pick the most legible disagreement, not the first one found.
        #
        # Two values that differ ("[30, 40, 50]" vs "[40, 50]") make the change obvious at
        # a glance. Two different exception types on a nonsense input are just as real a
        # difference and prove far less to a reader, who has to work out whether the input
        # was even meaningful. Same evidence, much worse as evidence.
        def legibility(row) -> int:
            both_ok = row["old"][0] == "ok" and row["new"][0] == "ok"
            one_ok = row["old"][0] == "ok" or row["new"][0] == "ok"
            return 0 if both_ok else (1 if one_ok else 2)

        w = min(disagreements, key=legibility)
        return {
            "status": "differs",
            "detail": f"{len(disagreements)} of {len(rows)} inputs disagree",
            "witness": {
                "args": w["args"],
                "old": f"{w['old'][0]}: {w['old'][1]}",
                "new": f"{w['new'][0]}: {w['new'][1]}",
            },
            "tried": len(rows),
            "exercised": len(exercised),
        }

    return {
        "status": "agree",
        "detail": f"{len(exercised)} of {len(rows)} inputs exercised it, all agree",
        "tried": len(rows),
        "exercised": len(exercised),
    }
