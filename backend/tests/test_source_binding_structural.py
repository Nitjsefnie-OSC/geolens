"""Only ``platform/dataset_origin.py`` writes a dataset's source binding columns.

``origin_uri`` and ``origin_ref`` change together with ``source_format``, through
``set_dataset_origin``/``set_postgis_origin``, which run ``build_origin_ref``'s
allowlist (ADR-002 invariant 4) and clear the stored probe verdict. This walks
every module under ``backend/app/`` and fails on any other write it can see:
an attribute assignment or delete, an item write or mutating method on the
stored dict, ``setattr`` by name, the columns as keywords or dict keys of an
ORM constructor or SQL write, and raw SQL that sets or inserts them.

Inside the owner module, each write is counted by the function it sits in, so
a second write site, or a write outside the setter, fails too.

A write through ``**kwargs`` or a name computed at runtime leaves no lexical
trace, so it is out of reach here.
"""

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
OWNER = "app/platform/dataset_origin.py"
# Every write the owner module makes, by enclosing function. set_postgis_origin
# writes through set_dataset_origin.
OWNER_WRITES = Counter(
    {
        ("set_dataset_origin", "writes .origin_ref"): 1,
        ("set_dataset_origin", "writes .origin_uri"): 1,
    }
)

COLUMNS = frozenset({"origin_ref", "origin_uri"})
_DICT_MUTATORS = frozenset(
    {"update", "pop", "popitem", "setdefault", "clear", "__setitem__", "__delitem__"}
)
# SQLAlchemy's statement builders, called as functions or through the module.
_STATEMENTS = frozenset({"update", "insert"})
_SQLALCHEMY_MODULES = frozenset({"sa", "sqlalchemy"})
# Methods whose arguments become row values. A dict's own ``values()`` and
# ``update()`` are not here: ``payload.update({"origin_ref": ...})`` writes no row.
_ROW_METHODS = frozenset({"values", "bulk_insert_mappings", "bulk_update_mappings"})
_SQL_WRITE = re.compile(
    r"\bSET\b(?:(?!\bWHERE\b)[^;])*?\borigin_(?:ref|uri)\"?"
    # A JSON subscript or path between the column and ``=`` still writes it.
    r"(?:\s*\[[^\]]*\]|\s*->>?\s*'[^']*')*\s*=(?!=)"
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


def _dict_column_keys(node: ast.AST, dataset_names: frozenset[str]):
    """Column keys of the dicts under ``node``: ``"origin_ref"`` or ``Dataset.origin_ref``."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Dict):
            for key in inner.keys:
                if isinstance(key, ast.Constant) and key.value in COLUMNS:
                    yield key.value
                elif _is_column(key) and _terminal_name(key.value) in dataset_names:
                    yield key.attr


def _may_write(source: str) -> bool:
    """Whether the text names a column at all, in any case, so it is worth parsing."""
    lowered = source.lower()
    return any(column in lowered for column in COLUMNS)


def _is_statement_builder(func: ast.expr) -> bool:
    return _terminal_name(func) in _STATEMENTS and (
        isinstance(func, ast.Name)
        or (isinstance(func.value, ast.Name) and func.value.id in _SQLALCHEMY_MODULES)
    )


def _is_sql_write(text: object) -> bool:
    return (
        isinstance(text, str)
        and "origin_" in text.lower()
        and _SQL_WRITE.search(text) is not None
    )


def _binds_a_write(statement: ast.expr) -> bool:
    """Whether execute()'s parameters are row values: the statement writes, or is unknown.

    A statement passed by name can't be read here, so its parameters are checked.
    """
    if isinstance(statement, (ast.Name, ast.Attribute)):
        return True
    return any(
        (isinstance(node, ast.Call) and _is_statement_builder(node.func))
        or (isinstance(node, ast.Constant) and _is_sql_write(node.value))
        for node in ast.walk(statement)
    )


def _dataset_names(tree: ast.AST) -> frozenset[str]:
    """Names this module binds to the Dataset model, for spotting its constructor.

    ``Dataset`` itself, an import of it under another name, and a name assigned
    from the processing port's ``get_dataset_orm_class()``.
    """
    names = {"Dataset"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname for alias in node.names if alias.name == "Dataset"
            )
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _terminal_name(node.value.func) == "get_dataset_orm_class"
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    names.discard(None)
    return frozenset(names)


def _call_writes(call: ast.Call, dataset_names: frozenset[str]) -> list[str]:
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
    name = _terminal_name(func)
    if name in dataset_names or _is_statement_builder(func):
        args = call.args
    elif isinstance(func, ast.Attribute) and name in _ROW_METHODS:
        args = call.args
    elif isinstance(func, ast.Attribute) and name == "execute":
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        statement = call.args[0] if call.args else keywords.get("statement")
        if statement is None or not _binds_a_write(statement):
            return []
        # The statement is checked as its own call; the rest are bind parameters.
        args = [*call.args[1:], *([keywords["params"]] if "params" in keywords else [])]
    else:
        return []
    found = [
        f"passes {kw.arg}= to {name}()" for kw in call.keywords if kw.arg in COLUMNS
    ]
    for arg in [*args, *(kw.value for kw in call.keywords if kw.arg is None)]:
        found.extend(
            f"passes a {key!r} key to {name}()"
            for key in _dict_column_keys(arg, dataset_names)
        )
    return found


def _node_writes(node: ast.AST, dataset_names: frozenset[str]) -> list[str]:
    stored = isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del))
    if stored and _is_column(node):
        return [f"writes .{node.attr}"]
    if stored and isinstance(node, ast.Subscript) and _is_column(node.value):
        return [f"writes an item of .{node.value.attr}"]
    if isinstance(node, ast.Call):
        return _call_writes(node, dataset_names)
    if isinstance(node, ast.Constant) and _is_sql_write(node.value):
        return ["raw SQL writes the column"]
    return []


def binding_writes(source: str) -> list[tuple[int, str | None, str]]:
    """Every write to the binding columns this module's text shows.

    Each is ``(line, enclosing function or None, description)``.
    """
    found: list[tuple[int, str | None, str]] = []
    tree = ast.parse(source)
    dataset_names = _dataset_names(tree)

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        found.extend(
            (node.lineno, function, what) for what in _node_writes(node, dataset_names)
        )
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


def owner_writes(source: str) -> Counter:
    return Counter((function, what) for _, function, what in binding_writes(source))


def test_only_the_binding_module_writes_origin_columns() -> None:
    violations = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        if rel == OWNER or not _may_write(source):
            continue
        for line, _, what in binding_writes(source):
            violations.append(f"{rel}:{line}: {what}")
    assert not violations, (
        "origin_uri/origin_ref are written only by set_dataset_origin or "
        "set_postgis_origin in backend/app/platform/dataset_origin.py:\n"
        + "\n".join(violations)
    )


def test_the_owner_module_writes_only_inside_its_setter() -> None:
    owner = (BACKEND_ROOT / OWNER).read_text(encoding="utf-8")
    assert owner_writes(owner) == OWNER_WRITES


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "\ndef project_unknown(",
            "\ndef _sneak(dataset):\n    dataset.origin_ref = {}\n\n\n"
            "def project_unknown(",
        ),
        (
            "    dataset.origin_uri = uri\n",
            "    dataset.origin_uri = uri\n    dataset.origin_uri = uri\n",
        ),
    ],
    ids=["second_function", "second_site_in_the_setter"],
)
def test_the_owner_check_sees_an_extra_write(old: str, new: str) -> None:
    owner = (BACKEND_ROOT / OWNER).read_text(encoding="utf-8")
    assert owner.count(old) == 1
    assert owner_writes(owner.replace(old, new)) != OWNER_WRITES


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
        "sa.update(Dataset).values({'origin_ref': ref})",
        "sqlalchemy.insert(Dataset).values(origin_uri='x')",
        "session.execute(update(Dataset).values(origin_ref=ref))",
        "insert(Dataset).values({'origin_ref': ref})",
        "Dataset(**{'origin_uri': 'x'})",
        "session.execute(update(Dataset), [{'id': 1, 'origin_ref': ref}])",
        "text('UPDATE catalog.datasets SET origin_uri = :u WHERE id = :id')",
        "text('INSERT INTO catalog.datasets (id, origin_ref) VALUES (:i, :r)')",
        "text('UPDATE catalog.datasets SET \"origin_ref\" = :ref WHERE id = :id')",
        "text('INSERT INTO catalog.datasets (id, \"origin_uri\") VALUES (:i, :u)')",
        "text('UPDATE catalog.datasets SET ORIGIN_REF = :ref WHERE id = :id')",
        "session.execute(stmt, [{'id': 1, 'origin_ref': ref}])",
        "from app.modules.catalog.datasets.domain.models import Dataset as DatasetModel"
        "\nDatasetModel(origin_ref={'kind': 'upload'})",
        "from app.modules.catalog.datasets.domain import models\n"
        "models.Dataset(origin_uri='x')",
        "DatasetCls = get_processing_port().get_dataset_orm_class()\n"
        "DatasetCls(origin_uri='x')",
        "update(Dataset).values({Dataset.origin_ref: ref})",
        "from app.modules.catalog.datasets.domain.models import Dataset as DatasetModel"
        "\ninsert(DatasetModel).values({DatasetModel.origin_uri: uri})",
        "text(\"UPDATE catalog.datasets SET origin_ref['token'] = :v WHERE id = :i\")",
        "text('UPDATE catalog.datasets SET \"origin_ref\"[\\'url\\'] = :v')",
        "text('UPDATE catalog.datasets SET origin_ref = "
        "jsonb_set(origin_ref, :path, :v) WHERE id = :i')",
        "session.execute(update(Dataset), params=[{'id': 1, 'origin_ref': ref}])",
        "session.execute(stmt, params={'origin_uri': uri})",
        "session.execute(statement=update(Dataset), params=[{'origin_ref': ref}])",
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
        "payload.update({'origin_ref': ref})",
        "payload.update(origin_uri=uri)",
        "response.values()",
        "session.execute(select(Dataset.origin_ref))",
        "update(Dataset).values({Dataset.title: title})",
        "update(Record).values({Record.origin_ref: ref})",
        'text("UPDATE catalog.datasets SET last_checked_at = now() '
        "WHERE origin_ref['url'] = :u\")",
        "session.execute(text('SELECT id FROM catalog.datasets "
        "WHERE origin_ref = :origin_ref'), params={'origin_ref': ref})",
        "class DatasetModel(BaseModel):\n    origin_ref: dict | None = None\n"
        "DatasetModel(origin_ref={'kind': 'upload'})",
        "from app.modules.catalog.datasets.domain.schemas import DatasetResponse "
        "as DatasetModel\nDatasetModel(origin_uri=uri)",
        "session.execute(text('SELECT id FROM catalog.datasets "
        "WHERE origin_ref = :origin_ref'), {'origin_ref': ref})",
        "session.execute(select(Dataset.id).where(Dataset.origin_uri == bindparam("
        "'origin_uri')), {'origin_uri': uri})",
        "text('UPDATE catalog.datasets SET last_checked_at = now() "
        "WHERE origin_ref = :ref')",
        "text(\"INSERT INTO v (h) SELECT origin_ref->>'file_hash' FROM d\")",
    ],
)
def test_the_scan_leaves_a_read_alone(snippet: str) -> None:
    assert binding_writes(snippet) == [], snippet


def test_a_module_naming_a_column_only_in_upper_case_is_still_scanned() -> None:
    assert _may_write("text('UPDATE catalog.datasets SET ORIGIN_URI = :u')")
    assert not _may_write("text('UPDATE catalog.datasets SET title = :t')")
