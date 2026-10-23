# SPDX-License-Identifier: Apache-2.0
"""Bring finished items up to date on a rerun of `geolens arcgis migrate`.

Hand-maintained. A service item's datasets are refreshed through
``POST /datasets/{id}/refresh`` with the run's ArcGIS credential, a data file
item's are replaced by a fresh download and a re-upload, and the item metadata
is written again. A run that stops for review is reported, never accepted.

A dataset changed in GeoLens since this command last left it is reported and
left alone. The state file records each dataset's ``updated_at``, feature
count and data revision after every write this command makes; a rerun
compares them right before each dataset's request, and sends the revision so
GeoLens stops the publication for review if the data changes after that.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from . import replace as _replace
from ._sdk_helpers import PollDeadlineExceeded, long_request_timeout, poll_until
from .arcgis_item_metadata import apply_item_metadata
from .arcgis_migrate_files import (
    EXTENSIONS,
    JOB_KEY,
    _download,
    _filename,
    _layer_name,
    _limit,
    _megabytes,
    _Skipped,
)
from .arcgis_migrate_layers import (
    JOB_POLL_INTERVAL,
    JOB_TIMEOUT,
    _layer_key,
    _layer_label,
    _send,
    attachment_flags,
    importable_layers,
)
from .arcgis_migrate_session import GeoLensError, ItemFailed, MigrationContext
from .arcgis_migrate_state import ItemState
from .refresh import (
    RefreshPollResult,
    _value,
    blocked_guidance,
    describe_failure_reason,
)

_ENDED = frozenset({"succeeded", "failed", "cancelled", "blocked"})
RUN_PAGE = 50

# Starts one dataset's refresh or replacement and records its marker: the
# dataset id, its GeoLens record and the data revision to send. Returns why it
# couldn't start, or None.
Starter = Callable[[str, Any, "int | None"], "str | None"]
Refresher = Callable[[MigrationContext, Mapping[str, Any], ItemState], list[str]]


def get_dataset(ctx: MigrationContext, dataset_id: str) -> Any:
    from geolens.api.datasets import get_single_dataset_datasets_dataset_id_get as get

    return ctx.geolens.call(get.sync_detailed, dataset_id=UUID(dataset_id))


def snapshot(dataset: Any) -> dict[str, Any]:
    """What a later run compares to tell whether GeoLens changed *dataset*."""
    return {
        "updated_at": dataset.updated_at.isoformat(),
        "feature_count": _value(dataset.feature_count),
        "data_revision": _revision(dataset),
    }


def _revision(dataset: Any) -> int | None:
    return _value(getattr(dataset, "data_revision", None))


def record_last_update(
    ctx: MigrationContext, entry: ItemState, dataset_ids: Iterable[str]
) -> None:
    """Record how this command leaves each dataset, once its writes are done."""
    for dataset_id in dataset_ids:
        _record(entry, dataset_id, get_dataset(ctx, dataset_id))
    ctx.save()


def _record(entry: ItemState, dataset_id: str, dataset: Any) -> None:
    entry.last_update[dataset_id] = snapshot(dataset)
    entry.refreshing.pop(dataset_id, None)


def _operator_wrote_last(ctx: MigrationContext, dataset: Any) -> bool:
    by = _value(getattr(dataset, "updated_by", None))
    return by is not None and str(by) == ctx.operator_id


def _find_run(
    ctx: MigrationContext, dataset_id: str, marker: Mapping[str, Any], **kwargs: Any
) -> Any | None:
    """The refresh run *marker* names, by its id or its ingest job."""
    from geolens.api.datasets import (
        list_dataset_refresh_runs_datasets_dataset_id_refresh_runs_get as list_runs,
    )

    page = ctx.geolens.call(
        list_runs.sync_detailed, dataset_id=UUID(dataset_id), limit=RUN_PAGE, **kwargs
    )
    for run in page.runs:
        if str(run.id) == marker.get("run") or (
            marker.get("job") and str(_value(run.ingest_job_id)) == marker["job"]
        ):
            return run
    return None


def _published_unchanged(
    ctx: MigrationContext, dataset: Any, expected: int | None
) -> bool:
    """Whether *dataset* is as a succeeded run published it: the operator
    wrote it last and its data revision is the one the run was sent with."""
    revision = _revision(dataset)
    return (
        _operator_wrote_last(ctx, dataset)
        and revision is not None
        and revision == expected
    )


def changed_in_geolens(
    ctx: MigrationContext, entry: ItemState, dataset_id: str, dataset: Any
) -> str | None:
    """Why *dataset* must be left alone, or None when this command may write it.

    A refresh an earlier run started and never settled explains a change only
    when it succeeded and the dataset is still as it published it. A dataset
    with no recorded last update (a state file from before they were recorded)
    counts as unchanged only when the operator wrote it last, before the item
    finished.
    """
    marker = entry.refreshing.get(dataset_id)
    run = _find_run(ctx, dataset_id, marker) if marker else None
    if run is not None:
        if str(run.status) not in _ENDED:
            return "a refresh an earlier run started is still running"
        if str(run.status) == "succeeded" and _published_unchanged(
            ctx, dataset, marker.get("revision")
        ):
            return None
    seen = entry.last_update.get(dataset_id)
    if seen is None:
        finished = entry.finished_at
        if (
            finished
            and _operator_wrote_last(ctx, dataset)
            and dataset.updated_at <= datetime.fromisoformat(finished)
        ):
            return None
        return "changed in GeoLens since it was migrated"
    if snapshot(dataset) != seen:
        return "changed in GeoLens since the last run"
    return None


def _wait_for_run(
    ctx: MigrationContext, entry: ItemState, dataset_id: str
) -> tuple[Any | None, str | None]:
    """The run ``entry.refreshing`` names for *dataset_id* once it ends, or
    None while it is still going at the deadline; and why it didn't publish."""
    marker = entry.refreshing[dataset_id]
    deadline = ctx.clock() + JOB_TIMEOUT
    while True:
        try:
            run = poll_until(
                lambda: _find_run(ctx, dataset_id, marker, reraise_timeout=True),
                deadline=deadline,
                interval=JOB_POLL_INTERVAL,
                sleep=ctx.sleep,
                monotonic=ctx.clock,
            )
        except PollDeadlineExceeded:
            run = None
        if run is not None and str(run.status) in _ENDED:
            break
        if ctx.clock() >= deadline:
            return None, (
                f"its refresh was still running after {JOB_TIMEOUT:g} s; a rerun "
                "picks up its result"
            )
        ctx.sleep(JOB_POLL_INTERVAL)
    status = str(run.status)
    if status == "succeeded":
        return run, None
    if status == "blocked":
        found = _value(run.verification)
        poll = RefreshPollResult(
            status,
            verification=found.to_dict() if hasattr(found, "to_dict") else found,
            run_id=str(run.id),
        )
        return run, " ".join(
            [
                "its refresh stopped for review and published nothing.",
                *blocked_guidance(UUID(dataset_id), poll),
            ]
        )
    error = _value(run.error_message)
    return run, f"its refresh ended {status}" + (
        f": {describe_failure_reason(str(error))}" if error else ""
    )


def _origin_url(dataset: Any) -> str | None:
    ref = _value(getattr(dataset, "origin_ref", None))
    url = ref.to_dict().get("url") if hasattr(ref, "to_dict") else None
    return url if isinstance(url, str) and url else None


def _start_refresh(
    ctx: MigrationContext,
    row: Mapping[str, Any],
    entry: ItemState,
    dataset_id: str,
    dataset: Any,
    revision: int | None,
) -> str | None:
    from geolens.api.datasets_refresh import (
        refresh_dataset_datasets_dataset_id_refresh_post as refresh,
    )
    from geolens.models.dataset_refresh_request import DatasetRefreshRequest
    from geolens.types import UNSET

    # The token goes where the dataset's stored source points, which the
    # trust check must judge rather than the inventory's URL.
    url = _origin_url(dataset)
    if url is None and (row.get("sharing") or {}).get("access") != "public":
        return "its stored source can't be read, so the ArcGIS token was not sent"
    try:
        accepted = _send(
            ctx,
            row,
            url or row["url"],
            refresh.sync_detailed,
            body=DatasetRefreshRequest(
                expected_data_revision=UNSET if revision is None else revision
            ),
            dataset_id=UUID(dataset_id),
            expected=202,
        )
    except GeoLensError as exc:
        return f"GeoLens refused the refresh, {exc}"
    except ItemFailed as exc:
        return str(exc)
    entry.refreshing[dataset_id] = {"run": str(accepted.run_id), "revision": revision}
    ctx.save()
    return None


def refresh_layers(
    ctx: MigrationContext, item: Mapping[str, Any], entry: ItemState
) -> list[str]:
    """Probe a service item, record which layers have attachments and report
    layers added since it was migrated, then refresh each dataset from its
    stored source."""
    from geolens.api.datasets import probe_service_url_services_probe_post as probe_api
    from geolens.models.probe_request import ProbeRequest

    row = ctx.inventory.get(item["id"])
    if row is None or not row.get("url"):
        return ["the inventory has no service URL for the item"]
    if entry.service_url not in (None, row["url"]):
        return [
            "its service URL changed in the portal since it was migrated, so its "
            "datasets were not refreshed from the old one"
        ]
    try:
        with long_request_timeout(ctx.geolens.client):
            probe = _send(
                ctx,
                row,
                row["url"],
                probe_api.sync_detailed,
                body=ProbeRequest(url=row["url"]),
            )
    except GeoLensError as exc:
        return [f"probe failed, {exc}"]
    except ItemFailed as exc:
        return [str(exc)]
    flags = attachment_flags(probe.layers)
    entry.attachments = {key: flags.get(key) for key in entry.layer_datasets}
    ctx.save()
    wanted, _notes = importable_layers(probe)
    added = [
        f"layer {_layer_label(layer)} was added to the service in the portal and "
        "isn't migrated"
        for layer in wanted
        if _layer_key(layer) not in entry.layer_datasets
    ]
    return added + _bring_each(
        ctx, item, entry, partial(_start_refresh, ctx, row, entry)
    )


def _start_replace(
    ctx: MigrationContext,
    entry: ItemState,
    dataset_id: str,
    dataset: Any,
    revision: int | None,
    path: Path,
) -> str | None:
    from geolens.api.datasets_reupload import (
        reupload_commit_datasets_dataset_id_reupload_job_id_commit_post as commit,
        reupload_preview_datasets_dataset_id_reupload_job_id_preview_post as preview,
    )

    refusal = _replace.origin_refusal_message(_value(dataset.origin))
    if refusal is not None:
        return refusal
    key = next(k for k, d in entry.layer_datasets.items() if d == dataset_id)
    layer = None if key == JOB_KEY else _layer_name(key)
    try:
        upload = ctx.geolens.call(
            _replace.upload_file,
            expected=_replace.UPLOAD_OK_STATUS,
            dataset_id=UUID(dataset_id),
            path=path,
        )
        # Recorded before the commit: if its answer is lost, a rerun still
        # finds the run it started.
        entry.refreshing[dataset_id] = {"job": str(upload.job_id), "revision": revision}
        ctx.save()
        with long_request_timeout(ctx.geolens.client):
            previewed = ctx.geolens.call(
                preview.sync_detailed,
                dataset_id=UUID(dataset_id),
                job_id=upload.job_id,
                body=_replace.build_preview_request(layer),
            )
        problem = None
        if layer is None and _replace.layer_summaries(previewed):
            problem = (
                "the file now holds several layers; replace it by hand with "
                f"geolens replace {dataset_id} --layer <name>"
            )
        # A swap made in GeoLens during the upload, which doesn't move the
        # data revision the commit sends, would otherwise be replaced.
        elif snapshot(get_dataset(ctx, dataset_id)) != snapshot(dataset):
            problem = "changed in GeoLens during the upload; it was left as it is"
        if problem:
            del entry.refreshing[dataset_id]
            ctx.save()
            return problem
        ctx.geolens.call(
            commit.sync_detailed,
            dataset_id=UUID(dataset_id),
            job_id=upload.job_id,
            body=_replace.build_commit_request(
                layer_name=layer,
                srid_override=None,
                expected_origin_kind=_replace.captured_origin_kind(dataset),
                expected_data_revision=revision,
            ),
            expected=_replace.COMMIT_OK_STATUS,
        )
    except GeoLensError as exc:
        return f"GeoLens refused the replacement, {exc}"
    return None


def refresh_file(
    ctx: MigrationContext, item: Mapping[str, Any], entry: ItemState
) -> list[str]:
    """Download a data file item again, then replace each of its datasets with
    it, one layer each for a multi-layer file."""
    extension = EXTENSIONS.get(item["type"])
    row = ctx.inventory.get(item["id"])
    if extension is None or row is None:
        return ["the item is not in the inventory the plan was built from"]
    limit = _limit(ctx)
    size = row.get("size_bytes")
    if isinstance(size, int) and size > limit:
        return [
            f"not replaced: the file is {_megabytes(size)}, over the instance's "
            f"upload limit of {_megabytes(limit)}"
        ]
    with tempfile.TemporaryDirectory(prefix="geolens-arcgis-") as tmp:
        path = Path(tmp) / _filename(item, extension)
        try:
            _download(ctx, item["id"], path, limit)
        except _Skipped as skipped:
            return [f"not replaced: {skipped}"]
        except ItemFailed as exc:
            return [str(exc)]
        return _bring_each(
            ctx, item, entry, partial(_start_replace, ctx, entry, path=path)
        )


def _bring_one(
    ctx: MigrationContext,
    item: Mapping[str, Any],
    entry: ItemState,
    dataset_id: str,
    start: Starter,
) -> str | None:
    """Refresh one dataset and write its metadata again; why it isn't up to
    date, or None.

    Checked right before its own request. A run that didn't publish leaves the
    previous record and writes no metadata; a published one is recorded only
    while the dataset is still as the run left it.
    """
    try:
        dataset = get_dataset(ctx, dataset_id)
    except GeoLensError as exc:
        if exc.status != 404:
            raise
        return "deleted in GeoLens; it was left as it is"
    if _revision(dataset) is None:
        return (
            "this GeoLens doesn't report data revisions, so edits made in it "
            "can't all be told apart; upgrade GeoLens to bring migrated datasets "
            "up to date safely"
        )
    reason = changed_in_geolens(ctx, entry, dataset_id, dataset)
    if reason:
        return f"{reason}; it was left as it is"
    revision = _revision(dataset)
    problem = start(dataset_id, dataset, revision)
    if problem:
        return problem
    run, problem = _wait_for_run(ctx, entry, dataset_id)
    if run is None:
        return problem
    if problem:
        return _settle_unrecorded(ctx, entry, dataset_id, problem)
    published = get_dataset(ctx, dataset_id)
    if not _published_unchanged(ctx, published, revision):
        return _settle_unrecorded(
            ctx,
            entry,
            dataset_id,
            "changed in GeoLens while its refresh ran; it was left as it is",
        )
    _record(entry, dataset_id, published)
    ctx.save()
    apply_item_metadata(ctx, item, entry, [dataset_id])
    after = get_dataset(ctx, dataset_id)
    if _revision(after) == _revision(published) and _operator_wrote_last(ctx, after):
        _record(entry, dataset_id, after)
        ctx.save()
    return None


def _settle_unrecorded(
    ctx: MigrationContext, entry: ItemState, dataset_id: str, problem: str
) -> str:
    """Drop the marker of an ended run whose result isn't recorded, so the
    next run compares with the record from before it."""
    del entry.refreshing[dataset_id]
    ctx.save()
    return problem


def _bring_each(
    ctx: MigrationContext, item: Mapping[str, Any], entry: ItemState, start: Starter
) -> list[str]:
    problems = []
    for dataset_id in dict.fromkeys(entry.layer_datasets.values()):
        try:
            problem = _bring_one(ctx, item, entry, dataset_id, start)
        except (ItemFailed, GeoLensError) as exc:
            problem = str(exc)
        if problem:
            problems.append(f"dataset {dataset_id}: {problem}")
    return problems


def bring_up_to_date(
    ctx: MigrationContext,
    item: Mapping[str, Any],
    entry: ItemState,
    refresher: Refresher,
) -> list[str]:
    """Bring a finished item's datasets up to date one at a time; why any of
    them, or the item, is not."""
    if not entry.layer_datasets:
        return []
    return refresher(ctx, item, entry)
