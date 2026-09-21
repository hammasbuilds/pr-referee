"""Pull the old and new version of every changed function out of two git revisions.

Diff hunks are deliberately not parsed. A hunk is a set of line ranges, and a line range
is not a thing that can be called - which makes it useless to an engine whose only evidence
is execution. Instead both whole files are fetched with `git show`, both are parsed, and
functions are matched by name. Anything whose source text differs is a changed function.

That loses two cases and gains one. Lost: a renamed function reads as one deletion and one
addition, and a pure move between files is invisible. Gained: a function whose behaviour
changed because a *module-level constant* above it changed is caught, because its header
changed even though its own lines did not - and a hunk-based reader would miss that
entirely.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from pr_referee.types import ChangedFunction

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".eggs",
    ".ruff_cache",
    "node_modules",
    "site-packages",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120)
    return out.stdout


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    """Python files that differ between the two revisions, tests excluded."""
    raw = _git(repo, "diff", "--name-only", f"{base}...{head}")
    out = []
    for line in raw.splitlines():
        p = Path(line.strip())
        if p.suffix != ".py" or not line.strip():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(line.strip())
    return out


def file_at(repo: Path, rev: str, path: str) -> str | None:
    """A file's contents at a revision, or None if it did not exist there."""
    out = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return out.stdout if out.returncode == 0 else None


def _bound_names(node: ast.stmt) -> set[str]:
    out: set[str] = set()
    if isinstance(node, ast.Import | ast.ImportFrom):
        for alias in node.names:
            out.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.add(n.id)
    return out


def free_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    attrs = {
        n.value.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    return names | attrs


def header_for(tree: ast.Module, source: str, needed: set[str]) -> str:
    """Only the imports and literal constants the function actually reads.

    Carrying the whole module would run it, side effects and all. Carrying every import
    means a function touching nothing but the standard library becomes unverifiable
    whenever an unrelated third-party import at the top of its file is missing.
    """
    lines = source.splitlines()
    kept: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Import | ast.ImportFrom | ast.Assign):
            continue
        if isinstance(node, ast.Assign) and not isinstance(
            node.value, ast.Constant | ast.Tuple | ast.List | ast.Dict | ast.Set
        ):
            continue
        if not (_bound_names(node) & needed):
            continue
        kept.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    return "\n".join(kept)


def _top_level_functions(source: str) -> dict[str, tuple[str, int]]:
    """name -> (source, lineno), for module-level functions only.

    Methods are excluded: one cannot be called without an instance, and constructing an
    instance means running the code under review. A tool that reports on what it cannot
    execute is back to guessing.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    lines = source.splitlines()
    out: dict[str, tuple[str, int]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out[node.name] = ("\n".join(lines[start - 1 : node.end_lineno]), start)
    return out


def package_context(repo: Path, path: str) -> tuple[str, str]:
    """(sys.path entry, dotted package) so the function's own imports resolve."""
    parts: list[str] = []
    current = (repo / path).parent
    while (current / "__init__.py").is_file():
        parts.append(current.name)
        if current.parent == current:
            break
        current = current.parent
    return str(current.resolve()), ".".join(reversed(parts))


def changed_functions(repo: Path, base: str, head: str) -> list[ChangedFunction]:
    out: list[ChangedFunction] = []
    for path in changed_files(repo, base, head):
        old_src = file_at(repo, base, path)
        new_src = file_at(repo, head, path)
        if old_src is None or new_src is None:
            continue  # added or deleted file: nothing to compare against

        old_fns = _top_level_functions(old_src)
        new_fns = _top_level_functions(new_src)
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError:
            continue

        sys_path, package = package_context(repo, path)
        for name, (new_body, lineno) in new_fns.items():
            if name not in old_fns:
                continue  # newly added: nothing to compare it to
            old_body, _ = old_fns[name]
            if old_body.strip() == new_body.strip():
                continue
            out.append(
                ChangedFunction(
                    path=path,
                    name=name,
                    old=old_body,
                    new=new_body,
                    header=header_for(
                        new_tree, new_src, free_names(new_body) | free_names(old_body)
                    ),
                    sys_path=sys_path,
                    package=package,
                    lineno=lineno,
                )
            )
    return out
