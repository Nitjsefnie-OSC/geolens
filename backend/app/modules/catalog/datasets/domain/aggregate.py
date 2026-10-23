"""Exact aggregates over one dataset's rows, for dashboard and story widgets.

Server-built SQL from a validated request runs through the read-only sandbox
(``execute_safe``) under a statement timeout. A map-view request the planner
expects to read more than ``MAX_VIEW_ROWS`` rows is refused, never
approximated. Results are cached per dataset ``tile_cache_version``, which
rolls on every in-app write to the live table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.geo import extent_to_span_bbox
from app.modules.catalog.datasets.domain.column_stats import (
    _NUMERIC_TYPES,
    _json_number,
    _sql_quote_ident,
    value_expr,
)
from app.modules.catalog.datasets.domain.models import Dataset
from app.modules.catalog.datasets.domain.service import (
    _safe_table_ref,
    compile_layer_filter,
    preview_slot_bound,
)
from app.modules.catalog.features.service import parse_bbox, planner_row_estimate
from app.platform.analysis_sql import binds_referenced_by, render_filtered_table_ref
from app.platform.dataset_origin import geolens_owns_table
from app.platform.cache import (
    get_cache,
    tenant_cache_context_available,
    tenant_cache_key,
)
from app.platform.extensions import get_catalog_port
from app.platform.sandbox.executor import execute_safe
from app.platform.sandbox.schemas import SandboxError

# A 500k-row view took 0.4-0.7 s per aggregate on a 2M-row point table in a
# laptop's Docker PostgreSQL, well inside the timeout. Raise it once a
# measurement on the smallest supported deployment (Azure B1ms) allows.
MAX_VIEW_ROWS = 500_000
AGGREGATE_TIMEOUT_MS = 4_000
_MAX_ITEMS = 50
# Category keys are column values of any size; numbers need no cap.
_MAX_KEYED_RESULT_BYTES = 1 << 20
_WHOLE_LAYER_TTL = 24 * 3600
# Filters and views are caller-chosen keys. Long-lived entries for them would
# push the rate-limit counters out of a volatile-ttl Valkey, so they live as
# long as a tile and stay small.
_KEYED_TTL = 300
_MAX_CACHED_BYTES = 16 * 1024
# Direct SQL can change a registered table without rolling its version.
_REGISTERED_TTL = 60


# The same pool-derived size as analysis previews, as a separate pool so
# anonymous dashboard traffic cannot hold every preview slot.
_aggregate_slots = asyncio.Semaphore(preview_slot_bound())

StatName = Literal["count", "sum", "avg", "min", "max"]
ColumnName = Annotated[str, Field(min_length=1, max_length=63)]
Scalar = str | int | float | bool | None


class _AggregateBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filter: dict[str, Any] | None = Field(
        None,
        description="CQL2-JSON filter, as the OGC API Features `filter` accepts.",
    )
    bbox: list[float] | None = Field(
        None,
        min_length=4,
        max_length=4,
        description=(
            "Map view `[west, south, east, north]` in WGS 84; west greater than "
            "east crosses the antimeridian. Omit for the whole layer."
        ),
    )

    @field_validator("bbox")
    @classmethod
    def _valid_bbox(cls, value: list[float] | None) -> list[float] | None:
        return None if value is None else parse_bbox(value)


class CountRequest(_AggregateBase):
    op: Literal["count"]


class StatsRequest(_AggregateBase):
    op: Literal["stats"]
    column: ColumnName
    stats: list[StatName] = Field(min_length=1, max_length=5)

    @field_validator("stats")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ValueCountsRequest(_AggregateBase):
    op: Literal["value_counts"]
    column: ColumnName
    limit: int = Field(10, ge=1, le=_MAX_ITEMS)


class HistogramRequest(_AggregateBase):
    op: Literal["histogram"]
    column: ColumnName
    bins: int = Field(10, ge=1, le=_MAX_ITEMS)


class GroupByRequest(_AggregateBase):
    op: Literal["group_by"]
    group_column: ColumnName
    stat: StatName
    column: ColumnName | None = Field(
        None, description="The numeric column `stat` reads; omit for `count`."
    )
    limit: int = Field(10, ge=1, le=_MAX_ITEMS)

    @model_validator(mode="after")
    def _column_for_stat(self) -> GroupByRequest:
        if self.stat != "count" and self.column is None:
            raise ValueError(f"group_by with stat {self.stat!r} needs a column")
        return self


AggregateRequest = Annotated[
    CountRequest
    | StatsRequest
    | ValueCountsRequest
    | HistogramRequest
    | GroupByRequest,
    Field(discriminator="op"),
]


class CountResult(BaseModel):
    count: int


class StatsResult(BaseModel):
    """The requested statistics; one over no rows is null.

    A value a float cannot carry (NaN, infinity) comes back as text.
    """

    count: int | None = None
    sum: float | str | None = None
    avg: float | str | None = None
    min: float | str | None = None
    max: float | str | None = None


class ValueCount(BaseModel):
    value: Scalar
    count: int


class ValueCountsResult(BaseModel):
    values: list[ValueCount] = Field(description="Most frequent first.")
    other_count: int = Field(description="Rows whose value is not listed.")


class HistogramBin(BaseModel):
    lower: float
    upper: float
    count: int


class HistogramResult(BaseModel):
    bins: list[HistogramBin] = Field(
        description="Equal-width bins over the finite values; empty with none."
    )


class GroupValue(BaseModel):
    key: Scalar
    value: float | int | str | None


class GroupByResult(BaseModel):
    groups: list[GroupValue] = Field(description="Largest value first.")


_RESULT_MODELS: dict[str, type[BaseModel]] = {
    "count": CountResult,
    "stats": StatsResult,
    "value_counts": ValueCountsResult,
    "histogram": HistogramResult,
    "group_by": GroupByResult,
}


class AggregateResponse(BaseModel):
    op: Literal["count", "stats", "value_counts", "histogram", "group_by"]
    result: (
        CountResult | StatsResult | ValueCountsResult | HistogramResult | GroupByResult
    )
    approximate: Literal[False] = Field(
        False, description="Always false: a view too wide to count exactly is refused."
    )
    cached: bool


class AggregateError(Exception):
    """A request refused before or while it runs; ``code`` is stable for clients."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _covers(bbox: Sequence[float], extent: object | None) -> bool:
    """Whether a non-crossing view contains the dataset's whole extent."""
    bounds = extent_to_span_bbox(extent)
    if bounds is None or bbox[0] > bbox[2]:
        return False
    return (
        bbox[0] <= bounds[0]
        and bbox[1] <= bounds[1]
        and bbox[2] >= bounds[2]
        and bbox[3] >= bounds[3]
    )


def _bbox_predicate(bbox: Sequence[float], alias: str) -> str:
    """Exact intersection with the view; a crossing view is two envelopes."""
    west, south, east, north = (float(v) for v in bbox)
    envelopes = [(west, east)] if west <= east else [(west, 180.0), (-180.0, east)]
    parts = [
        f"ST_Intersects({alias}.geom_4326, "
        f"ST_MakeEnvelope({x0!r}, {south!r}, {x1!r}, {north!r}, 4326))"
        for x0, x1 in envelopes
    ]
    return parts[0] if len(parts) == 1 else f"({' OR '.join(parts)})"


def _column_type(columns: dict[str, str], name: str, *, numeric: bool) -> str:
    data_type = columns.get(name)
    if data_type is None:
        raise AggregateError("invalid_column", f"Unknown column: {name!r}")
    if numeric and data_type not in _NUMERIC_TYPES:
        raise AggregateError(
            "invalid_column", f"Column {name!r} is not numeric ({data_type})"
        )
    return data_type


def _stat_sql(stat: str, column: str) -> str:
    quoted = _sql_quote_ident(column)
    if stat == "count":
        return f"count({quoted})"
    return f"{stat}({quoted}::numeric)"


def _stat_value(stat: str, value: Any) -> Any:
    if value is None:
        return None
    return int(value) if stat == "count" else _json_number(value)


def build_aggregate_sql(
    request: AggregateRequest, source: str, predicates: list[str], columns: dict
) -> str:
    """The aggregate SELECT for one request, reading ``source AS _t``.

    ``source`` comes from ``_safe_table_ref`` (narrowed by the compiled layer
    filter) and every column has been checked against the live table.
    """

    def from_where(*extra: str) -> str:
        preds = [*predicates, *extra]
        where = f" WHERE {' AND '.join(preds)}" if preds else ""
        return f" FROM {source} AS _t{where}"

    if request.op == "count":
        return f"SELECT count(*) AS _n{from_where()}"
    if request.op == "stats":
        _column_type(columns, request.column, numeric=True)
        selects = ", ".join(
            f"{_stat_sql(stat, request.column)} AS _s{i}"
            for i, stat in enumerate(request.stats)
        )
        return f"SELECT {selects}{from_where()}"
    if request.op == "value_counts":
        key = value_expr(
            _sql_quote_ident(request.column),
            _column_type(columns, request.column, numeric=False),
        )
        return (
            f"SELECT {key} AS _k, count(*) AS _n, sum(count(*)) OVER () AS _total"
            f"{from_where()}"
            f" GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST LIMIT {request.limit}"
        )
    if request.op == "histogram":
        _column_type(columns, request.column, numeric=True)
        quoted = _sql_quote_ident(request.column)
        bins = request.bins
        return (
            f"WITH _v AS (SELECT {quoted}::numeric::float8 AS x"
            f"{from_where(f'{quoted} IS NOT NULL')}),"
            " _f AS (SELECT x FROM _v"
            " WHERE x > '-Infinity'::float8 AND x < 'Infinity'::float8),"
            " _r AS (SELECT min(x) AS lo, max(x) AS hi FROM _f)"
            " SELECT _r.lo AS _lo, _r.hi AS _hi, CASE WHEN _r.hi = _r.lo THEN 1"
            f" ELSE least(width_bucket(_f.x, _r.lo, _r.hi, {bins}), {bins}) END AS _b,"
            " count(*) AS _n FROM _f CROSS JOIN _r GROUP BY 1, 2, 3 ORDER BY 3"
        )
    key = value_expr(
        _sql_quote_ident(request.group_column),
        _column_type(columns, request.group_column, numeric=False),
    )
    if request.stat == "count":
        value = "count(*)"
    else:
        _column_type(columns, request.column, numeric=True)
        value = _stat_sql(request.stat, request.column)
    return (
        f"SELECT {key} AS _k, {value} AS _v{from_where()}"
        f" GROUP BY 1 ORDER BY 2 DESC NULLS LAST, 1 NULLS LAST LIMIT {request.limit}"
    )


def _histogram(rows: Sequence[Sequence[Any]], bins: int) -> HistogramResult:
    if not rows:
        return HistogramResult(bins=[])
    low, high = float(rows[0][0]), float(rows[0][1])
    if low == high:
        return HistogramResult(
            bins=[HistogramBin(lower=low, upper=high, count=int(rows[0][3]))]
        )
    counts = {int(row[2]): int(row[3]) for row in rows}
    width = (high - low) / bins
    return HistogramResult(
        bins=[
            HistogramBin(
                lower=low + i * width,
                upper=high if i == bins - 1 else low + (i + 1) * width,
                count=counts.get(i + 1, 0),
            )
            for i in range(bins)
        ]
    )


def _result(request: AggregateRequest, rows: Sequence[Sequence[Any]]) -> BaseModel:
    if request.op == "count":
        return CountResult(count=int(rows[0][0]))
    if request.op == "stats":
        return StatsResult(
            **{
                stat: _stat_value(stat, value)
                for stat, value in zip(request.stats, rows[0])
            }
        )
    if request.op == "value_counts":
        listed = [ValueCount(value=_json_number(r[0]), count=int(r[1])) for r in rows]
        total = int(rows[0][2]) if rows else 0
        return ValueCountsResult(
            values=listed, other_count=total - sum(v.count for v in listed)
        )
    if request.op == "histogram":
        return _histogram(rows, request.bins)
    return GroupByResult(
        groups=[
            GroupValue(
                key=_json_number(r[0]),
                value=_json_number(r[1]),
            )
            for r in rows
        ]
    )


async def _sandboxed(
    db: AsyncSession,
    sql: str,
    binds: list[Any],
    caller_key: str,
    *,
    row_limit: int,
    max_result_bytes: int | None = None,
):
    return await execute_safe(
        db,
        sql,
        row_limit=row_limit,
        timeout_ms=AGGREGATE_TIMEOUT_MS,
        concurrency_key=f"aggregate:{caller_key}",
        require_reader_role=True,
        max_result_bytes=max_result_bytes,
        binds=binds_referenced_by(sql, binds),
    )


def _cache_key(dataset: Dataset, request: AggregateRequest, bbox) -> str | None:
    if not tenant_cache_context_available():
        return None
    body = request.model_dump(mode="json") | {"bbox": bbox}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return tenant_cache_key(
        f"aggregate:{dataset.id}:{dataset.tile_cache_version}:{digest}"
    )


async def run_aggregate(
    db: AsyncSession, dataset: Dataset, request: AggregateRequest, *, caller_key: str
) -> AggregateResponse:
    """Compute one aggregate exactly, from the cache when it holds the answer.

    The caller has already authorized ``dataset`` and checked it has a feature
    table. ``caller_key`` names the caller for the one-query-at-a-time lock.
    Raises ``AggregateError`` for a request it refuses and ``SandboxError``
    when the query cannot run. Releases ``db``'s connection before the query,
    so the caller must not read ORM state from it afterwards.
    """
    bbox = request.bbox
    if bbox is not None and not dataset.geometry_type:
        raise AggregateError("no_geometry", "This dataset has no geometry to bound")
    # A registered table's stored count and extent can be stale.
    owned = geolens_owns_table(
        dataset.source_format, dataset.record.record_type, dataset.origin_ref
    )
    if owned and bbox is not None and _covers(bbox, dataset.record.spatial_extent):
        bbox = None
    if (
        owned
        and request.op == "count"
        and bbox is None
        and request.filter is None
        and dataset.feature_count is not None
    ):
        return AggregateResponse(
            op="count", result=CountResult(count=dataset.feature_count), cached=False
        )

    cache_key = _cache_key(dataset, request, bbox)
    cached = await get_cache().get(cache_key) if cache_key is not None else None
    if cached is not None:
        return AggregateResponse(
            op=request.op,
            result=_RESULT_MODELS[request.op].model_validate(cached),
            cached=True,
        )

    try:
        layer_filter = await compile_layer_filter(
            db, dataset, request.filter, bind_prefix="agg_filter"
        )
    except ValueError as exc:
        raise AggregateError("invalid_filter", str(exc)) from exc
    where_sql, binds = layer_filter if layer_filter else (None, [])
    live = await get_catalog_port().get_column_info(db, dataset.table_name)
    columns = {col["name"]: str(col["type"]).lower() for col in live}
    source = render_filtered_table_ref(_safe_table_ref(dataset.table_name), where_sql)
    predicates = [_bbox_predicate(bbox, "_t")] if bbox is not None else []
    sql = build_aggregate_sql(request, source, predicates, columns)

    over_estimate = False
    if bbox is not None:
        # The request session has no tenant schema rewrite, so EXPLAIN reads
        # the resolved table rather than the sandbox's logical reference.
        estimate = await planner_row_estimate(
            db,
            render_filtered_table_ref(
                get_catalog_port().quote_table(dataset.table_name), where_sql
            ),
            f"WHERE {_bbox_predicate(bbox, 't')}",
            {},
            lambda stmt: stmt.bindparams(*binds) if binds else stmt,
        )
        over_estimate = estimate > MAX_VIEW_ROWS

    # Detached first: the rollback that returns the pooled connection would
    # expire every loaded instance, the caller's user included.
    db.expunge_all()
    await db.rollback()

    if _aggregate_slots.locked():
        raise SandboxError(
            "query_at_capacity",
            "The server is running its maximum number of aggregates. "
            "Try again in a moment.",
        )
    async with _aggregate_slots:
        if over_estimate:
            # Statistics can overstate a view; count it exactly, up to the
            # limit, before telling the reader to zoom in.
            probe = (
                f"SELECT count(*) FROM (SELECT 1 FROM {source} AS _t"
                f" WHERE {predicates[0]} LIMIT {MAX_VIEW_ROWS + 1}) AS _n"
            )
            counted = await _sandboxed(db, probe, binds, caller_key, row_limit=1)
            if int(counted.rows[0][0]) > MAX_VIEW_ROWS:
                raise AggregateError(
                    "too_wide",
                    "This view holds too many features to summarize exactly. Zoom in.",
                )
        result = await _sandboxed(
            db,
            sql,
            binds,
            caller_key,
            row_limit=_MAX_ITEMS,
            max_result_bytes=(
                _MAX_KEYED_RESULT_BYTES
                if request.op in ("value_counts", "group_by")
                else None
            ),
        )

    payload = _result(request, result.rows)
    encoded = payload.model_dump(mode="json")
    if cache_key is not None and len(json.dumps(encoded)) <= _MAX_CACHED_BYTES:
        ttl = (
            _WHOLE_LAYER_TTL if bbox is None and request.filter is None else _KEYED_TTL
        )
        await get_cache().set(
            cache_key, encoded, ttl=ttl if owned else min(ttl, _REGISTERED_TTL)
        )
    return AggregateResponse(op=request.op, result=payload, cached=False)
