"""POST /datasets/{id}/aggregate/ computes exact widget aggregates under Rule 1 and cost bounds."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.datasets.domain import aggregate
from tests.factories import (
    create_dataset,
    create_raster_dataset,
    create_user,
    get_user_id,
)
from tests.test_api_key_scope_875 import _remove_keys_minted_by_the_test  # noqa: F401
from tests.test_embed_token_creator_access import _embed

pytestmark = pytest.mark.anyio

KEEP = {"op": "=", "args": [{"property": "kind"}, "keep"]}
# The eight points sit on the equator at x = 0.001 .. 0.008.
EXTENT = (
    "POLYGON((0.001 -0.0001, 0.008 -0.0001, 0.008 0.0001, 0.001 0.0001, 0.001 -0.0001))"
)
FIRST_THREE = [0.0005, -1, 0.0035, 1]
FIRST_TWO = [0.0005, -1, 0.0025, 1]
WHOLE_WORLD = [-180, -90, 180, 90]


async def _points(session: AsyncSession, **dataset_fields):
    """Points 1-8 with pop = 10 * i; 1-6 are ``keep`` and 7-8 ``drop``.

    ``note`` holds a 3,000-character value per row.
    """
    admin_id = await get_user_id(session, "admin")
    table = f"agg_{uuid.uuid4().hex[:10]}"
    await session.execute(
        text(
            f"CREATE TABLE data.{table} (gid SERIAL PRIMARY KEY, kind TEXT,"
            " pop INTEGER, note TEXT, geom geometry(Point, 4326),"
            " geom_4326 geometry(Point, 4326))"
        )
    )
    await session.execute(text(f"GRANT SELECT ON data.{table} TO geolens_reader"))
    await session.execute(
        text(
            f"INSERT INTO data.{table} (kind, pop, note, geom, geom_4326)"  # noqa: S608
            " SELECT CASE WHEN i <= 6 THEN 'keep' ELSE 'drop' END, i * 10,"
            " repeat('x', 3000) || i,"
            " ST_SetSRID(ST_MakePoint(i * 0.001, 0), 4326),"
            " ST_SetSRID(ST_MakePoint(i * 0.001, 0), 4326)"
            " FROM generate_series(1, 8) AS i"
        )
    )
    await session.commit()
    fields = {"visibility": "public", "feature_count": 8, **dataset_fields}
    return await create_dataset(
        session,
        created_by=admin_id,
        table_name=table,
        geometry_type="POINT",
        spatial_extent_wkt=EXTENT,
        column_info=[
            {"name": "kind", "type": "text"},
            {"name": "pop", "type": "integer"},
            {"name": "note", "type": "text"},
        ],
        **fields,
    )


async def _aggregate(client: AsyncClient, dataset_id, body: dict, headers=None):
    return await client.post(
        f"/datasets/{dataset_id}/aggregate/", json=body, headers=headers or {}
    )


async def _dropped(session: AsyncSession, **dataset_fields):
    dataset = await _points(session, **dataset_fields)
    yield dataset
    await session.execute(text(f"DROP TABLE data.{dataset.table_name}"))
    await session.commit()


@pytest.fixture
async def points(client, test_db_session):
    async for dataset in _dropped(test_db_session):
        yield dataset


@pytest.fixture
async def private_points(client, test_db_session):
    async for dataset in _dropped(test_db_session, visibility="private"):
        yield dataset


OPERATIONS = [
    ({"op": "count", "filter": KEEP}, {"count": 6}),
    (
        {"op": "stats", "column": "pop", "stats": ["count", "sum", "max"]},
        {"count": 8, "sum": 360.0, "avg": None, "min": None, "max": 80.0},
    ),
    (
        {"op": "value_counts", "column": "kind", "limit": 1},
        {"values": [{"value": "keep", "count": 6}], "other_count": 2},
    ),
    (
        {"op": "histogram", "column": "pop", "bins": 2},
        {
            "bins": [
                {"lower": 10.0, "upper": 45.0, "count": 4},
                {"lower": 45.0, "upper": 80.0, "count": 4},
            ]
        },
    ),
    (
        {"op": "group_by", "group_column": "kind", "stat": "sum", "column": "pop"},
        {"groups": [{"key": "keep", "value": 210.0}, {"key": "drop", "value": 150.0}]},
    ),
]


class TestOperations:
    @pytest.mark.parametrize(
        ("body", "expected"), OPERATIONS, ids=[body["op"] for body, _ in OPERATIONS]
    )
    async def test_each_operation_is_exact(
        self, client: AsyncClient, points, body, expected
    ):
        resp = await _aggregate(client, points.id, body)

        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == expected
        assert resp.json()["approximate"] is False

    async def test_a_view_counts_only_the_features_inside_it(
        self, client: AsyncClient, points
    ):
        resp = await _aggregate(client, points.id, {"op": "count", "bbox": FIRST_THREE})

        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == {"count": 3}

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"op": "stats", "column": "missing", "stats": ["sum"]}, "invalid_column"),
            ({"op": "histogram", "column": "kind"}, "invalid_column"),
            (
                {"op": "count", "filter": {"op": "=", "args": [{"property": "x"}, 1]}},
                "invalid_filter",
            ),
        ],
    )
    async def test_a_bad_request_is_refused_with_a_code(
        self, client: AsyncClient, points, body, code
    ):
        resp = await _aggregate(client, points.id, body)

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == code


class TestViewThreshold:
    @pytest.fixture(autouse=True)
    def _small_threshold(self, monkeypatch):
        """Three rows is "too wide", and the planner overstates every view."""

        async def _overstated(*_args, **_kwargs):
            return 10**9

        monkeypatch.setattr(aggregate, "MAX_VIEW_ROWS", 2)
        monkeypatch.setattr(aggregate, "planner_row_estimate", _overstated)

    async def test_a_view_over_the_threshold_is_refused_not_approximated(
        self, client: AsyncClient, points
    ):
        resp = await _aggregate(
            client, points.id, {"op": "count", "filter": KEEP, "bbox": FIRST_THREE}
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "too_wide"

    async def test_an_overstated_estimate_is_checked_before_refusing(
        self, client: AsyncClient, points
    ):
        resp = await _aggregate(
            client, points.id, {"op": "count", "filter": KEEP, "bbox": FIRST_TWO}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == {"count": 2}

    async def test_a_view_containing_the_dataset_gets_the_whole_layer_result(
        self, client: AsyncClient, points
    ):
        body = {"op": "stats", "column": "pop", "stats": ["sum"]}
        whole = await _aggregate(client, points.id, body)
        assert whole.status_code == 200, whole.text

        resp = await _aggregate(client, points.id, {**body, "bbox": WHOLE_WORLD})

        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["sum"] == 360.0
        assert resp.json()["cached"] is True


class TestCache:
    async def test_a_repeat_is_cached_until_the_data_changes(
        self, client: AsyncClient, admin_auth_header: dict, points
    ):
        body = {"op": "stats", "column": "pop", "stats": ["count"]}
        first = await _aggregate(client, points.id, body)
        again = await _aggregate(client, points.id, body)
        assert first.json()["cached"] is False
        assert again.json()["cached"] is True

        added = await client.post(
            f"/datasets/{points.id}/features/",
            json={
                "geometry": {"type": "Point", "coordinates": [0.0045, 0]},
                "properties": {"kind": "keep", "pop": 5},
            },
            headers=admin_auth_header,
        )
        assert added.status_code == 201, added.text

        after = await _aggregate(client, points.id, body)
        assert after.json()["cached"] is False
        assert after.json()["result"]["count"] == 9


@pytest.fixture
def stored_ttls(monkeypatch) -> list[int]:
    """The TTL of every result the endpoint writes to the cache."""
    cache = aggregate.get_cache()
    ttls: list[int] = []

    class _Recording:
        get = staticmethod(cache.get)

        @staticmethod
        async def set(key, value, ttl=300, **kwargs):
            ttls.append(ttl)
            await cache.set(key, value, ttl=ttl, **kwargs)

    monkeypatch.setattr(aggregate, "get_cache", lambda: _Recording)
    return ttls


class TestCachePolicy:
    async def test_only_an_unfiltered_whole_layer_result_is_kept_long(
        self, client: AsyncClient, points, stored_ttls
    ):
        body = {"op": "stats", "column": "pop", "stats": ["sum"]}
        await _aggregate(client, points.id, body)
        await _aggregate(client, points.id, {**body, "filter": KEEP})
        await _aggregate(client, points.id, {**body, "bbox": FIRST_THREE})

        assert stored_ttls == [24 * 3600, 300, 300]

    async def test_a_large_result_is_not_cached(self, client: AsyncClient, points):
        body = {"op": "value_counts", "column": "note", "limit": 8}
        first = await _aggregate(client, points.id, body)
        again = await _aggregate(client, points.id, body)

        assert first.status_code == 200, first.text
        assert again.json()["cached"] is False

    async def test_a_registered_table_skips_its_stored_count_and_stays_fresh(
        self, client: AsyncClient, test_db_session, stored_ttls
    ):
        """Direct SQL may have changed the table since its count was stored."""
        async for registered in _dropped(
            test_db_session, source_format=None, feature_count=99
        ):
            resp = await _aggregate(client, registered.id, {"op": "count"})
            stats = await _aggregate(
                client,
                registered.id,
                {"op": "stats", "column": "pop", "stats": ["max"]},
            )

            assert resp.json()["result"] == {"count": 8}
            assert stats.status_code == 200, stats.text
            assert stored_ttls and max(stored_ttls) <= 60


class TestAccess:
    async def test_anonymous_callers_read_public_datasets(
        self, client: AsyncClient, points
    ):
        resp = await _aggregate(client, points.id, {"op": "count", "filter": KEEP})

        assert resp.status_code == 200, resp.text

    async def test_a_private_dataset_is_404_to_anonymous_callers(
        self, client: AsyncClient, admin_auth_header: dict, private_points
    ):
        anonymous = await _aggregate(client, private_points.id, {"op": "count"})
        admin = await _aggregate(
            client, private_points.id, {"op": "count"}, admin_auth_header
        )

        assert anonymous.status_code == 404, anonymous.text
        assert admin.status_code == 200, admin.text

    async def test_a_dead_credential_is_401_not_anonymous(
        self, client: AsyncClient, points
    ):
        resp = await _aggregate(
            client,
            points.id,
            {"op": "count"},
            {"Authorization": "Bearer not-a-real-token"},
        )

        assert resp.status_code == 401, resp.text

    async def test_an_embed_token_authorizes_a_private_dataset_in_its_scope(
        self,
        client: AsyncClient,
        admin_auth_header: dict,
        test_db_session,
        private_points,
    ):
        admin_id = await get_user_id(test_db_session, "admin")
        raw, _share = await _embed(
            client, test_db_session, admin_auth_header, admin_id, private_points.id
        )

        resp = await _aggregate(
            client, private_points.id, {"op": "count"}, {"X-Embed-Token": raw}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == {"count": 8}

    async def test_a_raster_has_nothing_to_aggregate(
        self, client: AsyncClient, admin_auth_header: dict, test_db_session
    ):
        admin_id = await get_user_id(test_db_session, "admin")
        raster = await create_raster_dataset(test_db_session, created_by=admin_id)

        resp = await _aggregate(client, raster.id, {"op": "count"}, admin_auth_header)

        assert resp.status_code == 404, resp.text


class TestApiKeys:
    @pytest.mark.parametrize("scope", ["read_only", "publish"])
    async def test_a_scoped_key_reads_what_its_user_may_read(
        self,
        client: AsyncClient,
        admin_auth_header: dict,
        points,
        private_points,
        scope,
    ):
        """A private dataset its user cannot read stays 404 through the key."""
        headers, _user_id = await create_user(client, admin_auth_header, "editor")
        minted = await client.post(
            "/auth/api-keys/",
            json={"name": f"{scope} key", "scope": scope},
            headers=headers,
        )
        assert minted.status_code == 201, minted.text
        key = {"X-Api-Key": minted.json()["key"]}
        body = {"op": "count", "filter": KEEP}

        public = await _aggregate(client, points.id, body, key)
        private = await _aggregate(client, private_points.id, body, key)

        assert public.status_code == 200, public.text
        assert public.json()["result"] == {"count": 6}
        assert private.status_code == 404, private.text
