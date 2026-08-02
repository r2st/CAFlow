#!/usr/bin/env python
"""A mutation tester, for asking whether the suite would notice.

Line coverage says a statement ran. It does not say anything about whether the
test would still pass if that statement were wrong, and the two come apart
exactly where it matters: a boundary written ``<=`` when it should be ``<``, a
guard that returns the wrong side of a decision, a limit compared against the
wrong number. Every one of those is fully covered and none of them are tested.

So this breaks the code on purpose, one edit at a time, and runs the tests. A
mutant the suite still passes — a *survivor* — is a change nobody would have
caught, which is a gap in the tests rather than a bug in the code. Fixing a
survivor means writing the test that would have failed.

Usage::

    python tools/mutate.py app/services/billing.py --tests tests/test_invoices.py
    python tools/mutate.py app/core/periods.py --tests tests/test_periods.py -j 8

Each mutant runs in its own throwaway copy of the tree, so nothing here can
leave the working directory modified, and mutants can run in parallel.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Everything a test run needs that is not the module under mutation. Symlinked
# rather than copied: the tree is a few megabytes and a mutant only ever writes
# to the one file.
LINKED_ENTRIES = ("tests", "alembic", "alembic.ini", "pyproject.toml", ".env")


# --------------------------------------------------------------- the mutations --


COMPARE_SWAPS: dict[type[ast.cmpop], list[type[ast.cmpop]]] = {
    ast.Lt: [ast.LtE, ast.Gt],
    ast.LtE: [ast.Lt, ast.GtE],
    ast.Gt: [ast.GtE, ast.Lt],
    ast.GtE: [ast.Gt, ast.LtE],
    ast.Eq: [ast.NotEq],
    ast.NotEq: [ast.Eq],
    ast.In: [ast.NotIn],
    ast.NotIn: [ast.In],
    ast.Is: [ast.IsNot],
    ast.IsNot: [ast.Is],
}

BINOP_SWAPS: dict[type[ast.operator], list[type[ast.operator]]] = {
    ast.Add: [ast.Sub],
    ast.Sub: [ast.Add],
    ast.Mult: [ast.FloorDiv],
    ast.Div: [ast.Mult],
    ast.FloorDiv: [ast.Mult],
}

BOOLOP_SWAPS: dict[type[ast.boolop], list[type[ast.boolop]]] = {
    ast.And: [ast.Or],
    ast.Or: [ast.And],
}


@dataclass(frozen=True)
class Mutation:
    """One edit: where it lands, what it does, and how to apply it."""

    site: int  # index into the ordered list of mutable nodes
    variant: int  # which replacement at that site
    line: int
    description: str


def _describe(node: ast.AST, replacement: object) -> str:
    def name(value: object) -> str:
        return getattr(value, "__name__", type(value).__name__)

    if isinstance(node, ast.Compare):
        return f"{name(type(node.ops[0]))} -> {name(replacement)}"
    if isinstance(node, ast.BinOp):
        return f"{name(type(node.op))} -> {name(replacement)}"
    if isinstance(node, ast.BoolOp):
        return f"{name(type(node.op))} -> {name(replacement)}"
    if isinstance(node, ast.UnaryOp):
        return "drop not"
    if isinstance(node, ast.Constant):
        return f"{node.value!r} -> {replacement!r}"
    return f"{type(node).__name__} -> {replacement!r}"


def _constant_variants(value: object) -> list[object]:
    """Replacements worth trying for a literal.

    Only literals a decision can turn on. Docstrings and message text are
    skipped by the collector below, so a survivor here is always a number or a
    flag the tests never pinned down.
    """
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int):
        return [value + 1, value - 1] if value not in (0, 1) else [value + 1]
    if isinstance(value, float):
        return [value + 1.0]
    return []


class _Collector(ast.NodeVisitor):
    """Every node in the module that can be mutated, in a stable order."""

    def __init__(self) -> None:
        self.nodes: list[tuple[ast.AST, Sequence[object]]] = []
        self._skip: set[int] = set()

    def _note(self, node: ast.AST, variants: Sequence[object]) -> None:
        if variants:
            self.nodes.append((node, variants))

    def visit_Compare(self, node: ast.Compare) -> None:
        # Chained comparisons (a < b < c) mutate on the first operator only;
        # that is enough to make the suite disagree if it checks the boundary.
        self._note(node, list(COMPARE_SWAPS.get(type(node.ops[0]), [])))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self._note(node, list(BINOP_SWAPS.get(type(node.op), [])))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self._note(node, list(BOOLOP_SWAPS.get(type(node.op), [])))
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            self._note(node, [None])  # the single variant: drop the negation
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if id(node) not in self._skip:
            self._note(node, _constant_variants(node.value))
        self.generic_visit(node)

    def _skip_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            self._skip.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._skip_docstring(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._skip_docstring(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._skip_docstring(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._skip_docstring(node)
        self.generic_visit(node)


class _Applier(ast.NodeTransformer):
    """Apply exactly one mutation, identified by its position in the walk."""

    def __init__(self, target_site: int, variant: int) -> None:
        self.target_site = target_site
        self.variant = variant
        self._seen = -1
        self._skip: set[int] = set()
        self.applied = False

    def _is_target(self, variants: Sequence[object]) -> bool:
        if not variants:
            return False
        self._seen += 1
        return self._seen == self.target_site

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        variants = list(COMPARE_SWAPS.get(type(node.ops[0]), []))
        hit = self._is_target(variants)
        self.generic_visit(node)
        if hit:
            node.ops = [variants[self.variant]()] + node.ops[1:]
            self.applied = True
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        variants = list(BINOP_SWAPS.get(type(node.op), []))
        hit = self._is_target(variants)
        self.generic_visit(node)
        if hit:
            node.op = variants[self.variant]()
            self.applied = True
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        variants = list(BOOLOP_SWAPS.get(type(node.op), []))
        hit = self._is_target(variants)
        self.generic_visit(node)
        if hit:
            node.op = variants[self.variant]()
            self.applied = True
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        hit = self._is_target([None]) if isinstance(node.op, ast.Not) else False
        self.generic_visit(node)
        if hit:
            self.applied = True
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        variants = [] if id(node) in self._skip else _constant_variants(node.value)
        hit = self._is_target(variants)
        if hit:
            self.applied = True
            return ast.copy_location(ast.Constant(value=variants[self.variant]), node)
        return node

    def _skip_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            self._skip.add(id(body[0].value))

    def visit_Module(self, node: ast.AST) -> ast.AST:
        self._skip_docstring(node)
        self.generic_visit(node)
        return node

    visit_FunctionDef = visit_Module
    visit_AsyncFunctionDef = visit_Module
    visit_ClassDef = visit_Module


def collect(source: str) -> list[Mutation]:
    tree = ast.parse(source)
    collector = _Collector()
    collector.visit(tree)
    mutations = []
    for site, (node, variants) in enumerate(collector.nodes):
        for variant, replacement in enumerate(variants):
            mutations.append(
                Mutation(
                    site=site,
                    variant=variant,
                    line=getattr(node, "lineno", 0),
                    description=_describe(node, replacement),
                )
            )
    return mutations


def apply(source: str, mutation: Mutation) -> str | None:
    """The module source with ``mutation`` applied, or None if it did not land."""
    tree = ast.parse(source)
    applier = _Applier(mutation.site, mutation.variant)
    applier.visit(tree)
    if not applier.applied:
        return None
    return ast.unparse(ast.fix_missing_locations(tree))


# ------------------------------------------------------------------ the runner --


def _workspace(stage: Path, module: Path, source: str) -> Path:
    """A throwaway tree holding one mutated copy of the package."""
    root = Path(tempfile.mkdtemp(prefix="mutant-", dir=stage))
    package = module.parts[0]  # "app"
    shutil.copytree(
        BACKEND_ROOT / package,
        root / package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for entry in LINKED_ENTRIES:
        origin = BACKEND_ROOT / entry
        if origin.exists():
            (root / entry).symlink_to(origin)
    (root / module).write_text(source)
    return root


def _run_tests(root: Path, tests: list[str], timeout: float) -> bool:
    """True when the suite passed — which for a mutant means it survived."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
            cwd=root,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # A mutant that hangs is a mutant the suite noticed, in its own way.
        return False
    return completed.returncode == 0


@dataclass
class Outcome:
    mutation: Mutation
    survived: bool


def _evaluate(
    stage: Path, module: Path, source: str, mutation: Mutation, tests: list[str], timeout: float
) -> Outcome | None:
    mutated = apply(source, mutation)
    if mutated is None:
        return None
    root = _workspace(stage, module, mutated)
    try:
        return Outcome(mutation, _run_tests(root, tests, timeout))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("module", help="module to mutate, relative to backend/ (e.g. app/x.py)")
    parser.add_argument(
        "--tests", nargs="+", required=True, help="test files to run against each mutant"
    )
    parser.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after this many mutants (0 = all)"
    )
    args = parser.parse_args()

    module = Path(args.module)
    source = (BACKEND_ROOT / module).read_text()
    mutations = collect(source)
    if args.limit:
        mutations = mutations[: args.limit]

    print(f"{module}: {len(mutations)} mutants, {args.jobs} at a time")

    stage = Path(tempfile.mkdtemp(prefix="caflow-mutation-"))
    started = time.monotonic()
    survivors: list[Mutation] = []
    killed = 0
    try:
        # The unmutated round-trip runs first: ast.unparse drops comments and
        # reflows the source, and a suite that cannot pass *that* would report
        # every mutant as killed for a reason having nothing to do with it.
        control = _workspace(stage, module, ast.unparse(ast.parse(source)))
        try:
            if not _run_tests(control, args.tests, args.timeout):
                print("baseline FAILED on the unparsed source — fix that before reading results")
                return 2
        finally:
            shutil.rmtree(control, ignore_errors=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [
                pool.submit(_evaluate, stage, module, source, mutation, args.tests, args.timeout)
                for mutation in mutations
            ]
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                outcome = future.result()
                if outcome is None:
                    continue
                if outcome.survived:
                    survivors.append(outcome.mutation)
                else:
                    killed += 1
                print(
                    f"\r  {done}/{len(mutations)} — {killed} killed, {len(survivors)} survived",
                    end="",
                    flush=True,
                )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    elapsed = time.monotonic() - started
    total = killed + len(survivors)
    score = (killed / total * 100) if total else 100.0
    print(f"\n\n{module}: {score:.1f}% killed ({killed}/{total}) in {elapsed:.0f}s")

    if survivors:
        print("\nSurvivors — each is an edit the suite would not have noticed:")
        for mutation in sorted(survivors, key=lambda m: m.line):
            print(f"  {module}:{mutation.line}  {mutation.description}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
