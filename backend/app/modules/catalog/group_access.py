"""Group membership changes that move who may read the group's datasets.

Adding or removing a member, or deleting a group, changes the audience of
every dataset granted to the group without writing the dataset. Such a change
gets the checks a visibility change gets: no VRT may newly reach past one of
its members, a narrowing may not cut off a viewer of the owner's shared maps,
and outstanding tile signatures are retired.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import HTTPException, status
from sqlalchemy import Uuid, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Group, GroupMember, User
from app.modules.catalog.authorization import (
    answers_audience_questions,
    audience_admits,
    vrt_links_beyond_audience,
)
from app.modules.catalog.datasets.domain.models import (
    Dataset,
    DatasetGrant,
    DatasetGroupGrant,
    Record,
)
from app.modules.catalog.maps.models import Map, MapLayer
from app.platform.catalog_locks import bump_publication_versions
from app.platform.extensions import RecordAudienceQuery, get_permission_extension

T = TypeVar("T")

# The map audiences a narrowing can cut a viewer out of, as in the
# dataset visibility guard.
_SHARED_MAP_VISIBILITIES = ("public", "internal")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
    )


async def change_group_access(
    db: AsyncSession,
    group_id: uuid.UUID,
    change: Callable[[], Awaitable[T]],
    *,
    narrowing: bool,
) -> T:
    """Run ``change``, a membership change to ``group_id`` that flushes its
    writes, and return its result.

    Raises 422 when the change makes a VRT reach past one of its members, or,
    when ``narrowing``, cuts an active member off a dataset that its owner's
    public or internal maps show. Does not commit.
    """
    await db.execute(select(Group.id).where(Group.id == group_id).with_for_update())
    granted = list(
        await db.scalars(
            select(DatasetGroupGrant.dataset_id).where(
                DatasetGroupGrant.group_id == group_id
            )
        )
    )
    if not granted:
        return await change()

    exceeded_before = await vrt_links_beyond_audience(db, granted)
    members, watched = await _readers_before(db, group_id) if narrowing else ([], [])
    result = await change()

    exceeded_after = await vrt_links_beyond_audience(db, granted)
    newly = sorted(
        {title for pair, title in exceeded_after.items() if pair not in exceeded_before}
    )
    if newly:
        raise _unprocessable(
            "This change would let VRTs reach people who cannot see all of "
            f"their source datasets: {', '.join(newly)}"
        )
    stranded = await _stranded_maps(db, members, watched)
    if stranded:
        raise _stranding(stranded)
    await bump_publication_versions(db, granted)
    return result


def _stranding(names: Iterable[str]) -> HTTPException:
    return _unprocessable(
        "This change would remove access to datasets used in shared maps: "
        + ", ".join(sorted(set(names)))
    )


@dataclass(frozen=True)
class _Watched:
    """A granted dataset its owner's shared maps show, and its readers before
    a narrowing."""

    audience: Any
    maps: list[str]
    readers: set[uuid.UUID]


async def _readers_before(
    db: AsyncSession, group_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[_Watched]]:
    """The group's members, and each granted dataset its owner's shared maps
    show with the members who read it now."""
    shown = await _shown_on_owner_shared_maps(db, group_id)
    permission = get_permission_extension()
    if shown and not answers_audience_questions(permission):
        # An authority whose audience can't be trusted gets every map, as the
        # visibility guard gives it.
        raise _stranding(name for _, names in shown.values() for name in names)
    members = list(
        await db.scalars(
            select(GroupMember.user_id).where(GroupMember.group_id == group_id)
        )
    )
    watched = []
    for query, maps in shown.values():
        audience = await permission.record_audience(query, User, grant_cls=DatasetGrant)
        # As in the visibility guard, only maps the owner can read count.
        if await db.scalar(select(audience_admits(audience, query.owner_id))):
            readers = await _readers(db, members, audience.users.is_not(False))
            watched.append(_Watched(audience, maps, readers))
    return members, watched


async def _stranded_maps(
    db: AsyncSession, members: list[uuid.UUID], watched: list[_Watched]
) -> list[str]:
    """The maps of each watched dataset a member can no longer read.

    An account the authority leaves unclassified (NULL) counted as a reader
    before and doesn't now, so it counts as cut off, as in the visibility
    guard.
    """
    names: set[str] = set()
    for item in watched:
        if item.readers - await _readers(db, members, item.audience.users.is_(True)):
            names.update(item.maps)
    return sorted(names)


async def _shown_on_owner_shared_maps(
    db: AsyncSession, group_id: uuid.UUID
) -> dict[uuid.UUID, tuple[RecordAudienceQuery, list[str]]]:
    """The group's datasets that their owner's public or internal maps show,
    with each one's audience question and those maps' names."""
    rows = await db.execute(
        select(
            Dataset.id,
            Record.id.label("record_id"),
            Record.created_by,
            Record.visibility,
            Record.record_status,
            Map.name,
        )
        .select_from(DatasetGroupGrant)
        .join(Dataset, Dataset.id == DatasetGroupGrant.dataset_id)
        .join(Record, Record.id == Dataset.record_id)
        .join(MapLayer, MapLayer.dataset_id == Dataset.id)
        .join(Map, Map.id == MapLayer.map_id)
        .where(DatasetGroupGrant.group_id == group_id)
        .where(Map.created_by == Record.created_by)
        .where(Map.visibility.in_(_SHARED_MAP_VISIBILITIES))
    )
    shown: dict[uuid.UUID, tuple[RecordAudienceQuery, list[str]]] = {}
    for row in rows.all():
        query = RecordAudienceQuery(
            dataset_id=row.id,
            record_id=row.record_id,
            owner_id=row.created_by,
            visibility=row.visibility,
            record_status=row.record_status,
        )
        shown.setdefault(row.id, (query, []))[1].append(row.name)
    return shown


async def _readers(
    db: AsyncSession, user_ids: Sequence[uuid.UUID], admitted: Any
) -> set[uuid.UUID]:
    """The active accounts among ``user_ids`` for which ``admitted`` holds."""
    ids = bindparam("user_ids", list(user_ids), type_=ARRAY(Uuid))
    return set(
        await db.scalars(
            select(User.id)
            .where(User.id == any_(ids))
            .where(User.is_active.is_(True))
            .where(User.status == "active")
            .where(admitted)
        )
    )
