"""Tests for the machinery that decides. No model, no network.

Every test here is about a way the tool could produce a *confident wrong answer*, because
for a review tool that is the failure that matters. A missed finding costs the reader
nothing they did not already have; a false accusation costs them the time to disprove it,
and a few of those is how a reviewer gets muted.
"""

from __future__ import annotations

import ast
import random

import pytest

from pr_referee import bench, score
from pr_referee.checks import statics
from pr_referee.engine.differential import compare
from pr_referee.engine.mutation import kill_rate, mutants
from pr_referee.extract import free_names, header_for, package_context
from pr_referee.reviewer import parse_verdict
from pr_referee.types import ChangedFunction, Finding, Kind, Review


def cf(old: str, new: str, header: str = "") -> ChangedFunction:
    return ChangedFunction(path="m.py", name="f", old=old, new=new, header=header)


# --- the execution engine --------------------------------------------------------------


def test_a_real_difference_is_caught_with_a_witness():
    r = compare("", "def f(x):\n    return x + 1\n", "def f(x):\n    return x + 2\n", "f", ["(1,)"])
    assert r["status"] == "differs"
    assert r["witness"] == {"args": "(1,)", "old": "ok: 2", "new": "ok: 3"}


def test_an_equivalent_rewrite_agrees():
    old = "def f(x):\n    return x * 2\n"
    new = "def f(x):\n    _r = x * 2\n    return _r\n"
    r = compare("", old, new, "f", ["(1,)", "(0,)", "(-3,)"])
    assert r["status"] == "agree"


def test_each_version_recurses_into_itself():
    # Sharing a namespace makes the old function's self-call resolve to the new one, so
    # the two appear to agree. Correct isolation gives 8 and 27; a leak gives 18.
    old = "def f(n):\n    return 1 if n <= 0 else 2 * f(n - 1)\n"
    new = "def f(n):\n    return 1 if n <= 0 else 3 * f(n - 1)\n"
    r = compare("", old, new, "f", ["(3,)"])
    assert r["status"] == "differs"
    assert r["witness"]["old"] == "ok: 8"
    assert r["witness"]["new"] == "ok: 27"


def test_memory_addresses_are_not_a_difference():
    # `<Foo object at 0x7f...>` reprs differently per allocation, so a raw comparison
    # makes any function returning an object a guaranteed false accusation - reported
    # with the address itself as the proof.
    src = "class Foo:\n    pass\n\ndef f(x):\n    return Foo()\n"
    assert compare("", src, src, "f", ["(1,)", "(2,)"])["status"] == "agree"


def test_an_exception_is_part_of_the_behaviour():
    old = "def f(a, b):\n    return a / b\n"
    new = (
        "def f(a, b):\n"
        "    try:\n"
        "        return a / b\n"
        "    except ZeroDivisionError:\n"
        "        return None\n"
    )
    r = compare("", old, new, "f", ["(1, 1)", "(1, 0)"])
    assert r["status"] == "differs"
    assert "ZeroDivisionError" in r["witness"]["old"]


def test_a_lazy_iterator_is_drained_before_comparing():
    # `<generator object at 0x...>` reprs identically whatever it would yield, so without
    # draining, a function returning a generator compares equal to any other and a real
    # change in what it produces is invisible. On toolz this hid 10 of 17 differences.
    old = "def f(xs):\n    return (x for x in xs)\n"
    new = "def f(xs):\n    return (x + 1 for x in xs)\n"
    r = compare("", old, new, "f", ["([1, 2],)"])
    assert r["status"] == "differs"
    assert r["witness"]["old"] == "ok: [1, 2]"
    assert r["witness"]["new"] == "ok: [2, 3]"


def test_an_equivalent_generator_still_agrees():
    old = "def f(xs):\n    return (x for x in xs)\n"
    new = "def f(xs):\n    return iter(list(xs))\n"
    assert compare("", old, new, "f", ["([1, 2],)", "([],)"])["status"] == "agree"


def test_an_infinite_generator_does_not_hang_the_comparison():
    src = "import itertools\n\ndef f(x):\n    return itertools.count(x)\n"
    r = compare("", src, src, "f", ["(0,)"])
    assert r["status"] == "agree"


def test_an_error_raised_while_draining_is_part_of_the_behaviour():
    old = "def f(xs):\n    return (x for x in xs)\n"
    new = "def f(xs):\n    return (1 // x for x in xs)\n"
    r = compare("", old, new, "f", ["([0],)"])
    assert r["status"] == "differs"
    assert "ZeroDivisionError" in r["witness"]["new"]


def test_the_most_legible_disagreement_is_chosen_as_the_witness():
    # Two values that differ show the change at a glance; two different exception types
    # on a nonsense input prove as much and communicate far less.
    old = "def f(n):\n    if n < 0:\n        raise ValueError('neg')\n    return n\n"
    new = "def f(n):\n    if n < 0:\n        raise TypeError('neg')\n    return n + 1\n"
    r = compare("", old, new, "f", ["(-1,)", "(5,)"])
    assert r["status"] == "differs"
    assert r["witness"]["args"] == "(5,)"  # not the (-1,) raise/raise pair
    assert r["witness"]["old"] == "ok: 5"


def test_raising_on_every_input_is_inconclusive_not_agreement():
    old = "def f(x):\n    return x.nope()\n"
    new = "def f(x):\n    return x.also_nope()\n"
    assert compare("", old, new, "f", ["(1,)"])["status"] == "inconclusive"


def test_no_argument_sets_is_inconclusive():
    assert (
        compare("", "def f():\n    return 1\n", "def f():\n    return 1\n", "f", [])["status"]
        == "inconclusive"
    )


def test_an_unloadable_side_is_named():
    old = "def f(x):\n    return x\n"
    new = "import definitely_not_a_module\n\ndef f(x):\n    return x\n"
    r = compare("", old, new, "f", ["(1,)"])
    assert r["status"] == "new_uncallable"


# --- structural checks -----------------------------------------------------------------


def test_a_newly_swallowed_error_is_reported():
    old = "def f(x):\n    return int(x)\n"
    new = "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        pass\n"
    found = statics.check_swallowed(cf(old, new))
    assert len(found) == 1
    assert found[0].kind is Kind.SWALLOWED_ERROR
    assert "ValueError" in found[0].summary


def test_an_error_already_being_swallowed_is_not_reported():
    # The change did not introduce it, so it is not this diff's finding.
    same = "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        pass\n"
    assert statics.check_swallowed(cf(same, same + "\n# touched\n")) == []


def test_returning_none_from_a_handler_counts_as_swallowing():
    old = "def f(x):\n    return int(x)\n"
    new = (
        "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        return None\n"
    )
    assert len(statics.check_swallowed(cf(old, new))) == 1


def test_a_handler_that_logs_and_reraises_is_not_swallowing():
    old = "def f(x):\n    return int(x)\n"
    new = "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        raise\n"
    assert statics.check_swallowed(cf(old, new)) == []


def test_a_widened_catch_is_reported():
    old = "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        return 0\n"
    new = "def f(x):\n    try:\n        return int(x)\n    except Exception:\n        return 0\n"
    found = statics.check_widened_catch(cf(old, new))
    assert len(found) == 1 and found[0].kind is Kind.WIDENED_CATCH


def test_a_narrowed_catch_is_not_reported():
    old = "def f(x):\n    try:\n        return int(x)\n    except Exception:\n        return 0\n"
    new = "def f(x):\n    try:\n        return int(x)\n    except ValueError:\n        return 0\n"
    assert statics.check_widened_catch(cf(old, new)) == []


def test_a_removed_branch_is_reported():
    old = "def f(x):\n    if x < 0:\n        return 0\n    return x\n"
    new = "def f(x):\n    return x\n"
    found = statics.check_removed_branch(cf(old, new))
    assert len(found) == 1 and "x < 0" in found[0].evidence


def test_reordering_the_same_branches_is_not_a_removal():
    # Compared by the text of each test, so a reshuffle of identical conditions is silent.
    lo = "    if x < 0:\n        return 0\n"
    hi = "    if x > 9:\n        return 9\n"
    old = f"def f(x):\n{lo}{hi}    return x\n"
    new = f"def f(x):\n{hi}{lo}    return x\n"
    assert statics.check_removed_branch(cf(old, new)) == []


# --- the benchmark's labels ------------------------------------------------------------
#
# These matter more than they look. If a "good" transform is not actually equivalent, the
# benchmark invents false positives and every precision number it reports is wrong.


@pytest.mark.parametrize(
    "source",
    [
        "def f(x):\n    total = 0\n    for i in range(x):\n"
        "        total = total + i\n    return total\n",
        "def f(a, b):\n    tmp = a * b\n    return tmp + 1\n",
        "def f(xs):\n    out = []\n    for x in xs:\n        out.append(x)\n    return out\n",
    ],
)
def test_good_transforms_really_do_preserve_behaviour(source):
    rnd = random.Random(0)
    argsets = ["(3,)", "(0,)", "(1,)"] if "(x)" in source else ["(2, 3)", "(0, 0)", "(-1, 5)"]
    if "xs" in source:
        argsets = ["([1, 2],)", "([],)", "([0],)"]

    for name, transform in bench.GOOD_TRANSFORMS.items():
        new = transform(source, "f", rnd)
        if new is None or new.strip() == source.strip():
            continue
        r = compare("", source, new, "f", argsets)
        assert r["status"] in ("agree", "inconclusive"), f"{name} changed behaviour: {r}"


def test_rename_local_leaves_parameters_alone():
    src = "def f(x):\n    total = x + 1\n    return total\n"
    out = bench.rename_local(src, "f", random.Random(0))
    assert "total_renamed" in out
    assert "def f(x)" in out  # the signature is untouched


def test_augment_assign_only_fires_on_the_right_shape():
    assert (
        bench.augment_assign("def f(x):\n    y = 1\n    return y\n", "f", random.Random(0)) is None
    )
    out = bench.augment_assign(
        "def f(x):\n    y = 1\n    y = y + 2\n    return y\n", "f", random.Random(0)
    )
    assert "y += 2" in out


def test_extract_return_temp_is_skipped_when_the_name_is_taken():
    src = "def f(x):\n    _result = 1\n    return x\n"
    assert bench.extract_return_temp(src, "f", random.Random(0)) is None


# --- mutation --------------------------------------------------------------------------


def test_mutants_are_distinct_and_exclude_the_original():
    src = "def f(n):\n    if n > 0:\n        return n + 1\n    return 0\n"
    ms = mutants(src)
    assert len(ms) >= 3 and len(set(ms)) == len(ms) and src not in ms


def test_kill_rate_rises_with_better_inputs():
    src = "def f(n):\n    if n > 2:\n        return n + 1\n    return 0\n"
    wide = kill_rate("", src, "f", ["(0,)", "(1,)", "(2,)", "(3,)", "(10,)"])
    thin = kill_rate("", src, "f", ["(0,)"])
    assert wide["rate"] is not None and thin["rate"] is not None
    assert wide["rate"] >= thin["rate"]


# --- scoring ---------------------------------------------------------------------------


def test_tally_counts_the_four_cells():
    s = score.tally("x", ["bad", "bad", "good", "good"], [True, False, True, False])
    assert (s.tp, s.fn, s.fp, s.tn) == (1, 1, 1, 1)
    assert s.precision == 0.5 and s.recall == 0.5


def test_an_abstention_is_not_counted_either_way():
    # Folding it into "same" flatters the false-positive rate; into "changed", recall.
    s = score.tally("x", ["bad", "good"], [None, None])
    assert s.abstained == 2 and s.decided == 0
    assert s.precision is None and s.recall is None


def test_a_reviewer_that_flags_everything_has_poor_precision():
    s = score.tally("noisy", ["bad", "good", "good", "good"], [True] * 4)
    assert s.recall == 1.0
    assert s.precision == 0.25


# --- the LLM arm's parser --------------------------------------------------------------


def test_verdicts_parse():
    assert parse_verdict("VERDICT: CHANGED\nREASON: off by one") == "CHANGED"
    assert parse_verdict("verdict:same") == "SAME"


def test_an_unparseable_answer_is_not_silently_a_verdict():
    assert parse_verdict("I think it's probably fine, though check the loop.") is None
    assert parse_verdict("") is None
    assert parse_verdict(None) is None


# --- extraction ------------------------------------------------------------------------


def test_header_is_pruned_to_what_the_function_reads():
    src = "import os\nimport unrelated_thing\nLIMIT = 5\n\ndef f(p):\n    return os.sep\n"
    header = header_for(ast.parse(src), src, free_names("def f(p):\n    return os.sep\n"))
    assert "import os" in header
    assert "unrelated_thing" not in header
    assert "LIMIT" not in header


def test_package_context_walks_up_while_packages(tmp_path):
    pkg = tmp_path / "src" / "mypkg" / "sub"
    pkg.mkdir(parents=True)
    (tmp_path / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text("", encoding="utf-8")

    sys_path, package = package_context(tmp_path, "src/mypkg/sub/m.py")
    assert sys_path.endswith("src")
    assert package == "mypkg.sub"


# --- the review object -----------------------------------------------------------------


def test_findings_sort_by_severity():
    r = Review()
    r.findings = [
        Finding(Kind.REMOVED_BRANCH, "a", "s", "e"),
        Finding(Kind.BEHAVIOUR_CHANGE, "b", "s", "e"),
        Finding(Kind.WIDENED_CATCH, "c", "s", "e"),
    ]
    assert [f.kind for f in r.sorted()][0] is Kind.BEHAVIOUR_CHANGE
