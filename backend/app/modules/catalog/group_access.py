"""Group membership changes that move who may read the group's datasets.

Adding or removing a member, or deleting a group, changes the audience of
every dataset granted to the group without writing the dataset. Outstanding
tile signatures are retired either way. A widening may not let a VRT newly
reach past one of its members; a narrowing is a revocation and is never
refused, so a VRT it leaves reaching past a member stops serving tiles and a
map stops showing the layer to whoever lost it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Group
from app.modules.catalog.authorization import vrt_links_beyond_audience
from app.modules.catalog.datasets.domain.models import DatasetGroupGrant
from app.platform.catalog_locks import bump_publication_versions

T = TypeVar("T")


async def change_group_access(
    db: AsyncSession,
    group_id: uuid.UUID,
    change: Callable[[], Awaitable[T]],
    *,
    narrowing: bool,
) -> T:
    """Run ``change``, a membership change to ``group_id`` that flushes its
    writes, and return its result.

    Raises 422 when a widening change makes a VRT reach past one of its
    members. Does not commit.
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

    exceeded_before = {} if narrowing else await vrt_links_beyond_audience(db, granted)
    result = await change()
    if not narrowing:
        exceeded_after = await vrt_links_beyond_audience(db, granted)
        newly = sorted(
            {
                title
                for pair, title in exceeded_after.items()
                if pair not in exceeded_before
            }
        )
        if newly:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="This change would let VRTs reach people who cannot see "
                f"all of their source datasets: {', '.join(newly)}",
            )
    await bump_publication_versions(db, granted)
    return result
