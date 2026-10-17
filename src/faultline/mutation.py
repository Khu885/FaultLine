"""Deterministic, target-scoped AST mutation generation.

The generator in this module deliberately has no execution or filesystem
responsibilities.  It parses one source string, finds the exact function named
by a :class:`~faultline.models.WorkUnit`, and returns independently rendered
candidate sources.  Each candidate changes one AST node in a copy of the
parsed tree; the caller can decide how (or whether) to execute it.
"""

from __future__ import annotations

import ast
import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final

try:  # Keep the module useful with the model additions from newer snapshots.
    from .models import MutationOperator
except ImportError:  # pragma: no cover - compatibility with older snapshots.
    class MutationOperator(StrEnum):
        """Mutation families used when the shared model is unavailable."""

        COMPARISON_INVERSION = "comparison_inversion"
        ARITHMETIC_REPLACEMENT = "arithmetic_replacement"
        NUMERIC_PERTURBATION = "numeric_perturbation"
        BOOLEAN_INVERSION = "boolean_inversion"
        RETURN_VALUE_REPLACEMENT = "return_value_replacement"


class InvalidSourceError(SyntaxError):
    """Raised when the input is not valid Python source."""


# A descriptive compatibility alias is convenient for integrations that use
# ``MutationSourceError`` rather than the shorter public name.
MutationSourceError = InvalidSourceError


@dataclass(frozen=True, slots=True)
class GeneratedMutant:
    """One independently rendered source mutation.

    ``source`` is the complete mutated source file.  ``original_source`` is
    retained so a caller can audit that every candidate came from the same
    input without keeping a separate side table.
    """

    mutation_id: str
    operator: MutationOperator
    target_file: str
    target_function: str
    source_line: int
    source: str
    original_source: str
    source_column: int = 0
    original: str | None = None
    replacement: str | None = None

    @property
    def id(self) -> str:
        """Short alias for :attr:`mutation_id`."""

        return self.mutation_id

    @property
    def mutated_source(self) -> str:
        """Alias for the complete mutated source text."""

        return self.source

    @property
    def mutant_source(self) -> str:
        """Compatibility alias for :attr:`mutated_source`."""

        return self.source

    @property
    def source_code(self) -> str:
        """Alias useful to callers that call source text ``source_code``."""

        return self.source

    @property
    def line(self) -> int:
        """Alias for :attr:`source_line`."""

        return self.source_line

    @property
    def line_number(self) -> int:
        """Alias for :attr:`source_line`."""

        return self.source_line

    @property
    def column(self) -> int:
        """Alias for :attr:`source_column`."""

        return self.source_column

    @property
    def original_text(self) -> str | None:
        """Alias for the original node text, when available."""

        return self.original

    @property
    def replacement_text(self) -> str | None:
        """Alias for the replacement node text, when available."""

        return self.replacement

    @property
    def operator_name(self) -> str:
        """Return the stable serialized operator value."""

        return _operator_value(self.operator)


# Small aliases make the public representation discoverable without requiring
# callers to know which noun their integration prefers.
GeneratedMutation = GeneratedMutant
MutationCandidate = GeneratedMutant
Mutant = GeneratedMutant


@dataclass(frozen=True, slots=True)
class _FunctionRecord:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    path: tuple[int, ...]
    qualified_name: str
    order: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    kind: str
    path: tuple[int, ...]
    source_line: int
    source_column: int
    end_line: int
    end_column: int
    order: int
    slot: int | None = None
    replacement: object | None = None


_FUNCTION_NODES: Final[tuple[type[ast.FunctionDef], type[ast.AsyncFunctionDef]]] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)
_NESTED_SCOPE_NODES: Final[tuple[type[ast.FunctionDef], type[ast.AsyncFunctionDef], type[ast.ClassDef], type[ast.Lambda]]] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)

_COMPARISON_INVERSES: Final[dict[type[ast.cmpop], type[ast.cmpop]]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_COMPARISON_TEXT: Final[dict[type[ast.cmpop], str]] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.GtE: ">=",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}

_ARITHMETIC_REPLACEMENTS: Final[dict[type[ast.operator], tuple[type[ast.operator], ...]]] = {
    ast.Add: (ast.Sub, ast.Mult),
    ast.Sub: (ast.Add, ast.Mult),
    ast.Mult: (ast.Add, ast.Sub),
}
_ARITHMETIC_TEXT: Final[dict[type[ast.operator], str]] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
}


def _operator_value(operator: object) -> str:
    value = getattr(operator, "value", operator)
    return str(value)


def _target_function(work_unit: object) -> str | None:
    """Read a WorkUnit target without requiring a concrete model instance."""

    if isinstance(work_unit, Mapping):
        value = work_unit.get("target_function")
        failures = work_unit.get("failures", [])
    else:
        value = getattr(work_unit, "target_function", None)
        failures = getattr(work_unit, "failures", [])
    if not isinstance(value, str):
        value = ""
    value = value.strip()
    if value:
        return value
    for failure in failures if isinstance(failures, (list, tuple)) else ():
        location = failure.get("source_location") if isinstance(failure, Mapping) else getattr(failure, "source_location", None)
        function = location.get("function") if isinstance(location, Mapping) else getattr(location, "function", None)
        if isinstance(function, str) and function.strip():
            return function.strip()
    return None


def _parse_source(source: str, filename: str) -> ast.Module:
    if not isinstance(source, str):
        raise TypeError("source must be a Python source string")
    try:
        return ast.parse(source, filename=filename, mode="exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        raise InvalidSourceError(f"invalid Python source: {exc}") from exc


def _index_tree(
    tree: ast.Module,
) -> tuple[list[_FunctionRecord], dict[int, tuple[int, ...]]]:
    """Index functions and stable child paths in AST traversal order."""

    records: list[_FunctionRecord] = []
    paths: dict[int, tuple[int, ...]] = {}

    def visit(node: ast.AST, path: tuple[int, ...], scope: tuple[str, ...]) -> None:
        paths[id(node)] = path
        next_scope = scope
        if isinstance(node, ast.ClassDef):
            next_scope = (*scope, node.name)
        elif isinstance(node, _FUNCTION_NODES):
            qualified_name = ".".join((*scope, node.name))
            records.append(
                _FunctionRecord(
                    node=node,
                    path=path,
                    qualified_name=qualified_name,
                    order=len(records),
                )
            )
            next_scope = (*scope, node.name)
        for index, child in enumerate(ast.iter_child_nodes(node)):
            visit(child, path + (index,), next_scope)

    visit(tree, (), ())
    return records, paths


def _select_function(records: list[_FunctionRecord], target: str) -> _FunctionRecord | None:
    # Prefer the exact qualified name.  This handles methods and nested
    # functions emitted by the localizer (for example ``Service.value``).
    for record in records:
        if record.qualified_name == target:
            return record

    # Hand-built WorkUnits commonly use a top-level/simple function name.  A
    # simple-name fallback is only used when no qualified exact match exists;
    # names are never matched by prefix or substring.
    if "." not in target:
        for record in records:
            if record.node.name == target:
                return record
    return None


def _position(node: ast.AST) -> tuple[int, int, int, int]:
    line = int(getattr(node, "lineno", 0) or 0)
    column = int(getattr(node, "col_offset", 0) or 0)
    end_line = int(getattr(node, "end_lineno", line) or line)
    end_column = int(getattr(node, "end_col_offset", column) or column)
    return line, column, end_line, end_column


def _collect_candidates(
    tree: ast.Module,
    target: _FunctionRecord,
    paths: Mapping[int, tuple[int, ...]],
) -> list[_Candidate]:
    """Collect mutation sites while pruning nested lexical scopes."""

    candidates: list[_Candidate] = []

    def add(
        kind: str,
        node: ast.AST,
        path: tuple[int, ...],
        *,
        slot: int | None = None,
        replacement: object | None = None,
    ) -> None:
        line, column, end_line, end_column = _position(node)
        candidates.append(
            _Candidate(
                kind=kind,
                path=path,
                source_line=line,
                source_column=column,
                end_line=end_line,
                end_column=end_column,
                order=len(candidates),
                slot=slot,
                replacement=replacement,
            )
        )

    def visit(node: ast.AST, path: tuple[int, ...]) -> None:
        # The selected function is entered through its body below, so every
        # scope node seen here is nested and must be excluded in its entirety.
        if isinstance(node, _NESTED_SCOPE_NODES):
            return

        if isinstance(node, ast.Compare):
            for slot, operator in enumerate(node.ops):
                replacement = _COMPARISON_INVERSES.get(type(operator))
                if replacement is not None:
                    add(
                        "comparison",
                        node,
                        path,
                        slot=slot,
                        replacement=replacement,
                    )

        if isinstance(node, ast.BinOp):
            for replacement in _ARITHMETIC_REPLACEMENTS.get(type(node.op), ()):
                add(
                    "arithmetic",
                    node,
                    path,
                    replacement=replacement,
                )

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                add("boolean", node, path, replacement=not node.value)
            elif _is_finite_number(node.value):
                for replacement in _numeric_perturbations(node.value):
                    if replacement != node.value and _is_finite_number(replacement):
                        add("numeric", node, path, replacement=replacement)

        if isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                add("return", node, path)

        for index, child in enumerate(ast.iter_child_nodes(node)):
            visit(child, path + (index,))

    for statement in target.node.body:
        statement_path = paths.get(id(statement))
        if statement_path is not None:
            visit(statement, statement_path)

    # Source position is the public ordering contract.  Larger enclosing nodes
    # precede their children when they start on the same token (e.g. ``return
    # x + 1``), and the discovery ordinal makes all ties stable.
    candidates.sort(
        key=lambda item: (
            item.source_line,
            item.source_column,
            -item.end_line,
            -item.end_column,
            item.order,
        )
    )
    return candidates


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, complex):
        return math.isfinite(value.real) and math.isfinite(value.imag)
    return False


def _numeric_perturbations(value: int | float | complex) -> tuple[int | float | complex, ...]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value + 1, value - 1
    if isinstance(value, float):
        return value + 1.0, value - 1.0
    return value + 1, value - 1


def _node_at_path(tree: ast.AST, path: tuple[int, ...]) -> ast.AST | None:
    node = tree
    for index in path:
        children = tuple(ast.iter_child_nodes(node))
        if index < 0 or index >= len(children):
            return None
        node = children[index]
    return node


def _apply_candidate(tree: ast.Module, candidate: _Candidate) -> ast.Module | None:
    node = _node_at_path(tree, candidate.path)
    if node is None:
        return None

    if candidate.kind == "comparison":
        if not isinstance(node, ast.Compare) or candidate.slot is None:
            return None
        if candidate.slot < 0 or candidate.slot >= len(node.ops):
            return None
        replacement = candidate.replacement
        if not isinstance(replacement, type) or not issubclass(replacement, ast.cmpop):
            return None
        node.ops[candidate.slot] = replacement()
    elif candidate.kind == "arithmetic":
        if not isinstance(node, ast.BinOp):
            return None
        replacement = candidate.replacement
        if not isinstance(replacement, type) or not issubclass(replacement, ast.operator):
            return None
        node.op = replacement()
    elif candidate.kind in {"numeric", "boolean"}:
        if not isinstance(node, ast.Constant):
            return None
        node.value = candidate.replacement
    elif candidate.kind == "return":
        if not isinstance(node, ast.Return) or node.value is None:
            return None
        replacement = ast.Constant(value=None)
        node.value = ast.copy_location(replacement, node.value)
    else:  # pragma: no cover - candidates are created only by this module.
        return None

    ast.fix_missing_locations(tree)
    return tree


def _replacement_text(candidate: _Candidate) -> str | None:
    replacement = candidate.replacement
    if candidate.kind == "comparison" and isinstance(replacement, type):
        return _COMPARISON_TEXT.get(replacement)
    if candidate.kind == "arithmetic" and isinstance(replacement, type):
        return _ARITHMETIC_TEXT.get(replacement)
    if candidate.kind == "return":
        return "None"
    if candidate.kind in {"numeric", "boolean"}:
        return repr(replacement)
    return None


def _candidate_token(candidate: _Candidate) -> str:
    replacement = candidate.replacement
    if isinstance(replacement, type):
        replacement_token = replacement.__name__
    else:
        replacement_token = repr(replacement)
    return ":".join(
        (
            candidate.kind,
            ".".join(str(part) for part in candidate.path),
            "" if candidate.slot is None else str(candidate.slot),
            replacement_token,
        )
    )


def _mutation_id(
    *,
    target_file: str,
    target_function: str,
    candidate: _Candidate,
    source: str,
) -> str:
    payload = "\x00".join(
        (
            target_file,
            target_function,
            _operator_value(_operator_for_kind(candidate.kind)),
            str(candidate.source_line),
            str(candidate.source_column),
            _candidate_token(candidate),
            source,
        )
    )
    digest = sha256(payload.encode("utf-8")).hexdigest()[:16]
    operator = _operator_value(_operator_for_kind(candidate.kind))
    return f"mut-{operator}-{candidate.source_line}-{candidate.source_column}-{digest}"


def _operator_for_kind(kind: str) -> MutationOperator:
    values = {
        "comparison": MutationOperator.COMPARISON_INVERSION,
        "arithmetic": MutationOperator.ARITHMETIC_REPLACEMENT,
        "numeric": MutationOperator.NUMERIC_PERTURBATION,
        "boolean": MutationOperator.BOOLEAN_INVERSION,
        "return": MutationOperator.RETURN_VALUE_REPLACEMENT,
    }
    return values[kind]


def generate_mutations(
    source: str,
    work_unit: object,
    target_file: str | Path,
    max_mutants: int = 5,
) -> list[GeneratedMutant]:
    """Generate deterministic, independently mutated versions of ``source``.

    The target is the exact ``work_unit.target_function`` name.  Qualified
    names such as ``Service.value`` are supported, with a simple-name fallback
    for hand-built top-level WorkUnits.  Function, class, and lambda scopes
    nested inside the selected function are not traversed.

    The result is ordered by source location and then by operator-specific
    replacement order.  Duplicate rendered sources are removed before the
    ``max_mutants`` bound is applied.  Invalid Python raises
    :class:`InvalidSourceError`; a missing target or a non-positive maximum is
    a valid empty result.
    """

    if isinstance(max_mutants, bool) or not isinstance(max_mutants, int):
        raise TypeError("max_mutants must be an integer")

    filename = str(target_file)
    tree = _parse_source(source, filename)
    if max_mutants <= 0:
        return []

    target_name = _target_function(work_unit)
    if target_name is None:
        return []

    functions, paths = _index_tree(tree)
    target = _select_function(functions, target_name)
    if target is None:
        return []

    candidates = _collect_candidates(tree, target, paths)
    generated: list[GeneratedMutant] = []
    seen_sources: set[str] = set()
    try:
        canonical_original = ast.unparse(tree)
    except (AttributeError, TypeError, ValueError):
        canonical_original = source

    for candidate in candidates:
        mutated_tree = _apply_candidate(copy.deepcopy(tree), candidate)
        if mutated_tree is None:
            continue
        try:
            mutated_source = ast.unparse(mutated_tree)
            ast.parse(mutated_source, filename=filename, mode="exec")
        except (AttributeError, TypeError, ValueError, SyntaxError):
            continue
        if mutated_source == canonical_original or mutated_source in seen_sources:
            continue
        seen_sources.add(mutated_source)

        original_node = _node_at_path(tree, candidate.path)
        original_text = ast.get_source_segment(source, original_node) if original_node is not None else None
        generated.append(
            GeneratedMutant(
                mutation_id=_mutation_id(
                    target_file=filename,
                    target_function=target_name,
                    candidate=candidate,
                    source=mutated_source,
                ),
                operator=_operator_for_kind(candidate.kind),
                target_file=filename,
                target_function=target_name,
                source_line=candidate.source_line,
                source=mutated_source,
                original_source=source,
                source_column=candidate.source_column,
                original=original_text,
                replacement=_replacement_text(candidate),
            )
        )
        if len(generated) >= max_mutants:
            break

    return generated


# Compatibility aliases for callers that use the noun ``mutant`` or
# ``candidate`` in their integration layer.
generate_mutants = generate_mutations
generate_mutation_candidates = generate_mutations
mutate_source = generate_mutations


def calculate_kill_rate(killed: int, survived: int) -> float | None:
    """Return the unrounded percentage for conclusive mutants, if available."""
    denominator = killed + survived
    if denominator <= 0:
        return None
    return (killed / denominator) * 100.0


def run_mutation_testing(
    candidate_project_path: str | Path,
    *,
    source: str,
    work_unit: object,
    target_file: str | Path,
    baseline: object,
    execution_mode: object,
    test_node_ids: list[str] | tuple[str, ...],
    max_mutants: int = 5,
    timeout: float = 10.0,
    python_executable: str | Path | None = None,
):
    """Generate and independently evaluate mutants for a verified candidate.

    The imports are intentionally local so the pure generator remains usable
    without importing the subprocess execution layer.
    """
    import time

    from .models import MutationOutcome, MutationReport, MutationResult
    from .mutation_runner import evaluate_generated_mutant

    started = time.monotonic()
    limitations = [
        "Mutation execution uses disposable temporary copies, not an OS-level security sandbox.",
        "Mutation testing is advisory and does not alter mandatory patch acceptance.",
    ]
    try:
        mutants = generate_mutations(source, work_unit, target_file, max_mutants=max_mutants)
    except (InvalidSourceError, TypeError, ValueError) as exc:
        return MutationReport(
            work_unit_id=getattr(work_unit, "identifier", str(getattr(work_unit, "work_unit_id", ""))),
            execution_mode=execution_mode,
            duration_seconds=time.monotonic() - started,
            quality_warning=f"mutation source could not be parsed; score unavailable ({type(exc).__name__})",
            limitations=limitations,
        )

    results: list[MutationResult] = []
    for mutant in mutants:
        evidence = evaluate_generated_mutant(
            candidate_project_path,
            mutant,
            test_node_ids=test_node_ids,
            baseline=baseline,
            timeout=timeout,
            python_executable=python_executable,
        )
        results.append(
            MutationResult(
                mutation_id=mutant.mutation_id,
                operator=mutant.operator,
                target_file=str(mutant.target_file),
                target_function=mutant.target_function,
                source_line=mutant.source_line,
                outcome=MutationOutcome(evidence.outcome),
                test_node_ids=list(evidence.test_node_ids),
                exit_code=evidence.exit_code,
                stdout=evidence.stdout,
                stderr=evidence.stderr,
                duration_seconds=evidence.duration_seconds,
                failure_reason=evidence.failure_reason,
            )
        )

    killed = sum(result.outcome is MutationOutcome.KILLED for result in results)
    survived = sum(result.outcome is MutationOutcome.SURVIVED for result in results)
    inconclusive = sum(result.outcome is MutationOutcome.INCONCLUSIVE for result in results)
    if not results:
        warning = "no valid mutation opportunities were found; score unavailable"
    elif inconclusive:
        warning = f"{inconclusive} mutant(s) were inconclusive; score excludes them"
    elif survived:
        warning = f"{survived} mutant(s) survived; mutation score is advisory"
    else:
        warning = None
    return MutationReport(
        work_unit_id=getattr(work_unit, "identifier", str(getattr(work_unit, "work_unit_id", ""))),
        total_generated=len(results),
        killed=killed,
        survived=survived,
        inconclusive=inconclusive,
        kill_rate=calculate_kill_rate(killed, survived),
        results=results,
        duration_seconds=time.monotonic() - started,
        execution_mode=execution_mode,
        quality_warning=warning,
        limitations=limitations,
    )


__all__ = [
    "GeneratedMutant",
    "GeneratedMutation",
    "InvalidSourceError",
    "MutationCandidate",
    "MutationOperator",
    "MutationSourceError",
    "Mutant",
    "generate_mutation_candidates",
    "generate_mutations",
    "generate_mutants",
    "calculate_kill_rate",
    "mutate_source",
    "run_mutation_testing",
]
