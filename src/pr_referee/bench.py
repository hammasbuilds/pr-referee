"""A labelled set of diffs, so both reviewers can be scored instead of admired.

Two kinds of case, and the labels come from different places on purpose:

**GOOD - behaviour-preserving, equivalent by construction.** Rename a local variable
consistently; pull a return expression into a named temporary; turn `x = x + 1` into
`x += 1`. These are not *believed* to be safe, they are safe by the definition of the
transform, so no verification is needed and none is claimed. Any flag on one of these is a
false positive.

**BAD - provably behaviour-changing.** A single-point AST mutation, kept only when an
**independent, much larger** input pool proves the two versions disagree. That
independence is the point: labelling with the same small argument sets the referee will
later receive would guarantee it perfect recall and measure nothing. Labelling with a wide
pool and then handing the referee its ordinary heuristic sets makes recall a real question
about the input generation.

A mutation that the wide pool cannot separate is discarded rather than labelled BAD. It may
be an equivalent mutant, and scoring a reviewer for missing a difference that may not exist
would be inventing a number.
"""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass
from pathlib import Path

from pr_referee.engine.differential import compare
from pr_referee.engine.mutation import mutants
from pr_referee.extract import _top_level_functions, free_names, header_for, package_context
from pr_referee.types import ChangedFunction
from pr_referee.values import argument_sets, harvest

# Deliberately wide, and unrelated to what the referee gets at review time.
LABEL_CAP = 240


@dataclass
class Case:
    fn: ChangedFunction
    label: str  # "good" or "bad"
    transform: str
    witness: dict | None = None


# --- behaviour-preserving transforms ---------------------------------------------------


class _Rename(ast.NodeTransformer):
    """Rename one local name everywhere it appears inside the function."""

    def __init__(self, old: str, new: str) -> None:
        self.old, self.new = old, new

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == self.old:
            node.id = self.new
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if node.arg == self.old:
            node.arg = self.new
        return node


def _all_names(source: str) -> set[str]:
    """Every name that appears, in any context, plus parameters.

    `free_names` only reports names that are *read*, which is not the question a
    collision guard is asking. A function that already assigns `_result` would pass a
    read-only check and then have that variable clobbered by the transform - producing a
    "behaviour-preserving" case that does not preserve behaviour, and a false positive
    charged to whichever reviewer correctly noticed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    args = {a.arg for n in ast.walk(tree) if isinstance(n, ast.arg) for a in [n]}
    return names | args


def _locals_of(fn: ast.FunctionDef) -> list[str]:
    """Names assigned inside the function - safe to rename, unlike free variables."""
    params = {a.arg for a in fn.args.args}
    out: list[str] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id not in params
            and node.id not in out
            and not node.id.startswith("__")
        ):
            out.append(node.id)
    return out


def rename_local(source: str, name: str, rnd: random.Random) -> str | None:
    tree = ast.parse(source)
    fn = tree.body[0]
    if not isinstance(fn, ast.FunctionDef):
        return None
    names = _locals_of(fn)
    if not names:
        return None
    target = rnd.choice(names)
    fresh = f"{target}_renamed"
    if fresh in _all_names(source):
        return None
    _Rename(target, fresh).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def extract_return_temp(source: str, name: str, rnd: random.Random) -> str | None:
    """`return <expr>` becomes `result = <expr>; return result`."""
    tree = ast.parse(source)
    fn = tree.body[0]
    if not isinstance(fn, ast.FunctionDef):
        return None
    returns = [
        (i, n) for i, n in enumerate(fn.body) if isinstance(n, ast.Return) and n.value is not None
    ]
    if not returns:
        return None
    if "_result" in _all_names(source):
        return None
    i, node = returns[-1]
    fn.body[i : i + 1] = [
        ast.Assign(targets=[ast.Name(id="_result", ctx=ast.Store())], value=node.value),
        ast.Return(value=ast.Name(id="_result", ctx=ast.Load())),
    ]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def augment_assign(source: str, name: str, rnd: random.Random) -> str | None:
    """`x = x + 1` becomes `x += 1`."""
    tree = ast.parse(source)
    changed = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t, v = node.targets[0], node.value
        if (
            isinstance(t, ast.Name)
            and isinstance(v, ast.BinOp)
            and isinstance(v.left, ast.Name)
            and v.left.id == t.id
        ):
            node.__class__ = ast.AugAssign
            node.target, node.op, node.value = t, v.op, v.right
            del node.targets
            changed = True
            break
    if not changed:
        return None
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


GOOD_TRANSFORMS = {
    "rename_local": rename_local,
    "extract_return_temp": extract_return_temp,
    "augment_assign": augment_assign,
}


# --- building the set ------------------------------------------------------------------


def _case(ctx: tuple, new_src: str, label: str, transform: str, witness=None) -> Case:
    rel, name, body, header, sys_path, package, lineno = ctx
    return Case(
        fn=ChangedFunction(
            path=rel,
            name=name,
            old=body,
            new=new_src,
            header=header,
            sys_path=sys_path,
            package=package,
            lineno=lineno,
        ),
        label=label,
        transform=transform,
        witness=witness,
    )


def _wide_argsets(fn_node: ast.FunctionDef, harvested: dict[str, list[str]]) -> list[str]:
    return argument_sets(fn_node, harvested, cap=LABEL_CAP)


def build(
    repo: Path,
    limit: int | None = None,
    seed: int = 0,
    per_function: int = 1,
) -> list[Case]:
    """Labelled cases from every module-level function in `repo`."""
    from pr_referee.extract import SKIP_DIRS

    rnd = random.Random(seed)
    harvested = harvest(repo)
    cases: list[Case] = []

    files = [
        p
        for p in sorted(repo.rglob("*.py"))
        if not any(part in SKIP_DIRS for part in p.relative_to(repo).parts) and "test" not in p.name
    ]

    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        rel = str(path.relative_to(repo)).replace("\\", "/")
        sys_path, package = package_context(repo, rel)
        fns = _top_level_functions(source)

        for name, (body, lineno) in fns.items():
            node = next(
                (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name),
                None,
            )
            if node is None:
                continue

            header = header_for(tree, source, free_names(body))
            wide = _wide_argsets(node, harvested)
            if not wide:
                continue

            # Both sides of every case are round-tripped through `ast.unparse`, so the
            # only textual difference is the change itself.
            #
            # Without this the "new" side is unparsed (comments stripped, spacing and
            # quoting normalised) while the "old" side is original source, and every diff
            # in the set carries a wall of cosmetic noise. That noise falls on GOOD and
            # BAD cases alike, so it does not bias which way the comparison comes out -
            # but it does hand the model a harder problem than the one being asked about,
            # and a comparison worth reporting should not need that kind of help.
            try:
                baseline = ast.unparse(ast.parse(body))
            except SyntaxError:
                continue
            ctx = (rel, name, baseline, header, sys_path, package, lineno)

            # GOOD: equivalent by construction.
            for tname, fn_transform in GOOD_TRANSFORMS.items():
                try:
                    new_src = fn_transform(body, name, rnd)
                except (SyntaxError, ValueError, AttributeError):
                    new_src = None
                # Compared against the unparsed baseline: a transform that changes only
                # formatting is a no-op case, not a GOOD one, and would pad the set with
                # diffs that contain nothing to judge.
                if new_src and new_src.strip() != baseline.strip():
                    cases.append(_case(ctx, new_src, "good", tname))
                    break

            # BAD: a mutation the wide pool can prove different.
            made = 0
            for mutant in mutants(body, cap=6):
                if made >= per_function:
                    break
                res = compare(header, body, mutant, name, wide, 30.0, sys_path, package)
                if res["status"] == "differs":
                    cases.append(_case(ctx, mutant, "bad", "mutation", res["witness"]))
                    made += 1

            if limit and len(cases) >= limit:
                return cases
    return cases
