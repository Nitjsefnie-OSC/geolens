"""Only ``platform/dataset_origin.py`` writes a dataset's source binding columns.

``origin_uri`` and ``origin_ref`` change together with ``source_format``, through
``set_dataset_origin``/``set_postgis_origin``, which run ``build_origin_ref``'s
allowlist (ADR-002 invariant 4) and clear the stored probe verdict. This walks
every module under ``backend/app/`` and fails on any other write it can see:
an attribute assignment or delete, an item write or mutating method on the
stored dict, ``setattr`` by name, the columns as keywords or dict keys of an
ORM constructor or SQL write, and raw SQL that sets or inserts them.

A write through ``**kwargs`` or a name computed at runtime leaves no lexical
trace, so it is out of reach here.
"""

import ast
import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
OWNER = "app/platform/dataset_origin.py"

COLUMNS = frozenset({"origin_ref", "origin_uri"})
_DICT_MUTATORS = frozenset(
    {"update", "pop", "popitem", "setdefault", "clear", "__setitem__", "__delitem__"}
)
# Calls that write rows: the ORM constructor and the SQL statement builders.
_ROW_WRITERS = frozenset(
    {
        "Dataset",
        "values",
        "execute",
        "update",
        "bulk_insert_mappings",
        "bulk_update_mappings",
    }
)
_SQL_WRITE = re.compile(
    r"\bSET\b(?:(?!\bWHERE\b)[^;])*?\borigin_(?:ref|uri)\s*=(?!=)"
    r"|\bINSERT\s+INTO\s+[\w.\"]*datasets\"?\s*\([^)]*\borigin_(?:ref|uri)\b",
    re.IGNORECASE,
)


def _terminal_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_column(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in COLUMNS


def _dict_column_keys(node: ast.AST):
    for inner in ast.walk(node):
        if isinstance(inner, ast.Dict):
            for key in inner.keys:
                if isinstance(key, ast.Constant) and key.value in COLUMNS:
                    yield key.value


def _call_writes(call: ast.Call) -> list[str]:
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _DICT_MUTATORS
        and _is_column(func.value)
    ):
        return [f"mutates .{func.value.attr} with .{func.attr}()"]
    if (
        _terminal_name(func) == "setattr"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value in COLUMNS
    ):
        return [f"setattr(..., {call.args[1].value!r}, ...)"]
    if _terminal_name(func) not in _ROW_WRITERS:
        return []
    found = [
        f"passes {kw.arg}= to {_terminal_name(func)}()"
        for kw in call.keywords
        if kw.arg in COLUMNS
    ]
    for arg in [*call.args, *(kw.value for kw in call.keywords if kw.arg is None)]:
        found.extend(
            f"passes a {key!r} key to {_terminal_name(func)}()"
            for key in _dict_column_keys(arg)
        )
    return found


def binding_writes(source: str) -> list[tuple[int, str]]:
    """Every write to the binding columns this module's text shows, by line."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        stored = isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del))
        if stored and _is_column(node):
            found.append((node.lineno, f"writes .{node.attr}"))
        elif stored and isinstance(node, ast.Subscript) and _is_column(node.value):
            found.append((node.lineno, f"writes an item of .{node.value.attr}"))
        elif isinstance(node, ast.Call):
            found.extend((node.lineno, what) for what in _call_writes(node))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "origin_" in node.value
            and _SQL_WRITE.search(node.value)
        ):
            found.append((node.lineno, "raw SQL writes the column"))
    return found


def test_only_the_binding_module_writes_origin_columns() -> None:
    violations = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        if rel == OWNER or not any(column in source for column in COLUMNS):
            continue
        for line, what in binding_writes(source):
            violations.append(f"{rel}:{line}: {what}")
    assert not violations, (
        "origin_uri/origin_ref are written only by set_dataset_origin or "
        "set_postgis_origin in backend/app/platform/dataset_origin.py:\n"
        + "\n".join(violations)
    )


def test_the_owner_module_is_where_the_writes_are() -> None:
    owner = (BACKEND_ROOT / OWNER).read_text(encoding="utf-8")
    assert {what for _, what in binding_writes(owner)} == {
        "writes .origin_ref",
        "writes .origin_uri",
    }


@pytest.mark.parametrize(
    "snippet",
    [
        "dataset.origin_ref = {'kind': 'upload'}",
        "dataset.origin_uri: str = 'x'",
        "dataset.origin_uri += '/1'",
        "a, dataset.origin_uri = 1, 'x'",
        "del dataset.origin_ref",
        "dataset.origin_ref['url'] = 'x'",
        "del dataset.origin_ref['url']",
        "dataset.origin_ref.update(url='x')",
        "dataset.origin_ref.pop('token')",
        "setattr(dataset, 'origin_uri', 'x')",
        "Dataset(origin_ref={'kind': 'upload'})",
        "update(Dataset).values(origin_uri='x')",
        "insert(Dataset).values({'origin_ref': ref})",
        "Dataset(**{'origin_uri': 'x'})",
        "session.execute(update(Dataset), [{'id': 1, 'origin_ref': ref}])",
        "text('UPDATE catalog.datasets SET origin_uri = :u WHERE id = :id')",
        "text('INSERT INTO catalog.datasets (id, origin_ref) VALUES (:i, :r)')",
    ],
)
def test_the_scan_sees_a_write(snippet: str) -> None:
    assert binding_writes(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "pointer = (dataset.origin_uri, dataset.origin_ref, dataset.source_format)",
        "Dataset.origin_ref['url'].astext == base_url",
        "_write_reupload_catalog(session, origin_ref={'filename': name})",
        "DatasetResponse(origin_uri=dataset.origin_uri)",
        "origin_ref = {'filename': name}",
        "origin_ref['filename'] = name",
        "text('UPDATE catalog.datasets SET last_checked_at = now() "
        "WHERE origin_ref = :ref')",
        "text(\"INSERT INTO v (h) SELECT origin_ref->>'file_hash' FROM d\")",
    ],
)
def test_the_scan_leaves_a_read_alone(snippet: str) -> None:
    assert binding_writes(snippet) == [], snippet
