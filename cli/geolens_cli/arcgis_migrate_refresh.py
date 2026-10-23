# SPDX-License-Identifier: Apache-2.0
"""Bring finished items up to date on a rerun of `geolens arcgis migrate`.

Hand-maintained. A service item's datasets are refreshed through
``POST /datasets/{id}/refresh`` with the run's ArcGIS credential, a data file
item's are replaced by a fresh download and a re-upload, and the item metadata
is written again. A run that stops for review is reported, never accepted.

A dataset changed in GeoLens since this command last left it is reported and
left alone. The state file records each dataset's ``updated_at`` and feature
count after every write this command makes, and a rerun compares them first.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable, Mapping
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
    _send,
    attachment_flags,
)
from .arcgis_migrate_session import GeoLensError, ItemFailed, MigrationContext, StopRun
from .arcgis_migrate_state import ItemState
from .refresh import review_reason_sentence

_ENDED = frozenset({"succeeded", "failed", "cancelled", "blocked"})
RUN_PAGE = 50

Refresher = Callable[
    [MigrationContext, Mapping[str, Any], ItemState, Mapping[str, Any]], list[str]
]


def _value(value: Any) -> Any:
    from geolens.types import Unset

    return None if isinstance(value, Unset) else value


def get_dataset(ctx: MigrationContext, dataset_id: str) -> Any:
    from geolens.api.datasets import get_single_dataset_datasets_dataset_id_get as get

    return ctx.geolens.call(get.sync_detailed, dataset_id=UUID(dataset_id))


def snapshot(dataset: Any) -> dict[str, Any]:
    """What a later run compares to tell whether GeoLens changed *dataset*."""
    return {
        "updated_at": dataset.updated_at.isoformat(),
        "feature_count": _value(dataset.feature_count),
    }


def record_last_update(
    ctx: MigrationContext, entry: ItemState, dataset_ids: Iterable[str]
) -> None:
    """Record how this command leaves each dataset, once its writes are done."""
    for dataset_id in dataset_ids:
        entry.last_update[dataset_id] = snapshot(get_dataset(ctx, dataset_id))
        entry.refreshing.pop(dataset_id, None)
    ctx.save()


def _last_writer_is_operator(ctx: MigrationContext, dataset: Any) -> bool:
    by = _value(getattr(dataset, "updated_by", None))
    return by is None or str(by) == ctx.operator_id


def _find_run(
    ctx: MigrationContext, dataset_id: str, marker: Mapping[str, str], **kwargs: Any
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


def changed_in_geolens(
    ctx: MigrationContext, entry: ItemState, dataset_id: str, dataset: Any
) -> str | None:
    """Why *dataset* must be left alone, or None when this command may write it.

    A refresh an earlier run started and never saw end explains a change only
    while the dataset's last write is that run's and was made as the operator.
    A dataset with no recorded last update (a state file from before they were
    recorded) is taken as unchanged only when the operator wrote it last.
    """
    marker = entry.refreshing.get(dataset_id)
    if marker:
        run = _find_run(ctx, dataset_id, marker)
        if run is not None and str(run.status) not in _ENDED:
            return "a refresh an earlier run started is still running"
        finished = _value(run.finished_at) if run is not None else None
        if (
            finished is not None
            and dataset.updated_at <= finished
            and _last_writer_is_operator(ctx, dataset)
        ):
            return None
    seen = entry.last_update.get(dataset_id)
    if seen is None:
        if _last_writer_is_operator(ctx, dataset):
            return None
        return "changed in GeoLens since it was migrated"
    if snapshot(dataset) != seen:
        return "changed in GeoLens since the last run"
    return None


def _wait_for_run(
    ctx: MigrationContext, entry: ItemState, dataset_id: str
) -> str | None:
    """Follow the run ``entry.refreshing`` names for *dataset_id* to its end;
    why it didn't publish, or None. The marker stays until the dataset's last
    update is recorded, and is flagged ``ended`` once the run is."""
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
            return (
                f"its refresh was still running after {JOB_TIMEOUT:g} s; a rerun "
                "picks up its result"
            )
        ctx.sleep(JOB_POLL_INTERVAL)
    marker["ended"] = "yes"
    ctx.save()
    status = str(run.status)
    if status == "succeeded":
        return None
    if status == "blocked":
        found = _value(run.verification)
        if hasattr(found, "to_dict"):
            found = found.to_dict()
        reasons = found.get("review_reasons") if isinstance(found, dict) else None
        return " ".join(
            [
                "its refresh stopped for review and published nothing.",
                *(review_reason_sentence(str(r)) for r in reasons or []),
                f"Review it, then accept with: geolens refresh {dataset_id} "
                f"--accept-blocked-run {run.id}",
            ]
        )
    error = _value(run.error_message)
    return f"its refresh ended {status}" + (f": {error}" if error else "")


def _origin_url(dataset: Any) -> str | None:
    ref = _value(getattr(dataset, "origin_ref", None))
    url = ref.to_dict().get("url") if hasattr(ref, "to_dict") else None
    return url if isinstance(url, str) and url else None


def _refresh_dataset(
    ctx: MigrationContext,
    row: Mapping[str, Any],
    entry: ItemState,
    dataset_id: str,
    dataset: Any,
) -> str | None:
    from geolens.api.datasets_refresh import (
        refresh_dataset_datasets_dataset_id_refresh_post as refresh,
    )
    from geolens.models.dataset_refresh_request import DatasetRefreshRequest

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
            body=DatasetRefreshRequest(),
            dataset_id=UUID(dataset_id),
            expected=202,
        )
    except GeoLensError as exc:
        return f"GeoLens refused the refresh, {exc}"
    except ItemFailed as exc:
        return str(exc)
    entry.refreshing[dataset_id] = {"run": str(accepted.run_id)}
    ctx.save()
    return _wait_for_run(ctx, entry, dataset_id)


def refresh_layers(
    ctx: MigrationContext,
    item: Mapping[str, Any],
    entry: ItemState,
    datasets: Mapping[str, Any],
) -> list[str]:
    """Refresh a service item's *datasets* (id -> GeoLens record) from their
    stored source, after a probe that records which layers have attachments."""
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
    problems = []
    for dataset_id in datasets:
        problem = _refresh_dataset(ctx, row, entry, dataset_id, datasets[dataset_id])
        if problem:
            problems.append(f"dataset {dataset_id}: {problem}")
    return problems


def _replace_dataset(
    ctx: MigrationContext,
    entry: ItemState,
    dataset_id: str,
    dataset: Any,
    path: Path,
    layer: str | None,
) -> str | None:
    from geolens.api.datasets_reupload import (
        reupload_commit_datasets_dataset_id_reupload_job_id_commit_post as commit,
        reupload_preview_datasets_dataset_id_reupload_job_id_preview_post as preview,
    )

    refusal = _replace.origin_refusal_message(_value(dataset.origin))
    if refusal is not None:
        return refusal
    try:
        upload = ctx.geolens.call(
            _replace.upload_file,
            expected=_replace.UPLOAD_OK_STATUS,
            dataset_id=UUID(dataset_id),
            path=path,
        )
        # Recorded before the commit: if its answer is lost, a rerun still
        # finds the run it started.
        entry.refreshing[dataset_id] = {"job": str(upload.job_id)}
        ctx.save()
        with long_request_timeout(ctx.geolens.client):
            previewed = ctx.geolens.call(
                preview.sync_detailed,
                dataset_id=UUID(dataset_id),
                job_id=upload.job_id,
                body=_replace.build_preview_request(layer),
            )
        if layer is None and _replace.layer_summaries(previewed):
            del entry.refreshing[dataset_id]
            ctx.save()
            return (
                "the file now holds several layers; replace it by hand with "
                f"geolens replace {dataset_id} --layer <name>"
            )
        ctx.geolens.call(
            commit.sync_detailed,
            dataset_id=UUID(dataset_id),
            job_id=upload.job_id,
            body=_replace.build_commit_request(
                layer_name=layer,
                srid_override=None,
                expected_origin_kind=_replace.captured_origin_kind(dataset),
            ),
            expected=_replace.COMMIT_OK_STATUS,
        )
    except GeoLensError as exc:
        return f"GeoLens refused the replacement, {exc}"
    return _wait_for_run(ctx, entry, dataset_id)


def refresh_file(
    ctx: MigrationContext,
    item: Mapping[str, Any],
    entry: ItemState,
    datasets: Mapping[str, Any],
) -> list[str]:
    """Download a data file item again and replace each of its *datasets*
    (id -> GeoLens record) with it, one layer each for a multi-layer file."""
    extension = EXTENSIONS.get(item["type"])
    row = ctx.inventory.get(item["id"])
    if extension is None or row is None:
        return ["the item is not in the inventory the plan was built from"]
    targets = {
        key: dataset
        for key, dataset in entry.layer_datasets.items()
        if dataset in datasets
    }
    if not targets:
        return []
    limit = _limit(ctx)
    size = row.get("size_bytes")
    if isinstance(size, int) and size > limit:
        return [
            f"not replaced: the file is {_megabytes(size)}, over the instance's "
            f"upload limit of {_megabytes(limit)}"
        ]
    problems = []
    with tempfile.TemporaryDirectory(prefix="geolens-arcgis-") as tmp:
        path = Path(tmp) / _filename(item, extension)
        try:
            _download(ctx, item["id"], path, limit)
        except _Skipped as skipped:
            return [f"not replaced: {skipped}"]
        except ItemFailed as exc:
            return [str(exc)]
        for key, dataset_id in targets.items():
            layer = None if key == JOB_KEY else _layer_name(key)
            problem = _replace_dataset(
                ctx, entry, dataset_id, datasets[dataset_id], path, layer
            )
            if problem:
                problems.append(f"dataset {dataset_id}: {problem}")
    return problems


def bring_up_to_date(
    ctx: MigrationContext,
    item: Mapping[str, Any],
    entry: ItemState,
    refresher: Refresher,
) -> list[str]:
    """Refresh a finished item and write its metadata again; why any of its
    datasets is not up to date.

    A dataset GeoLens no longer has, or one changed there since this command
    last left it, is reported and not written.
    """
    problems: list[str] = []
    current: dict[str, Any] = {}
    for dataset_id in entry.dataset_ids:
        try:
            dataset = get_dataset(ctx, dataset_id)
            reason = changed_in_geolens(ctx, entry, dataset_id, dataset)
        except GeoLensError as exc:
            reason = "deleted in GeoLens" if exc.status == 404 else str(exc)
        if reason:
            problems.append(f"dataset {dataset_id}: {reason}; it was left as it is")
        else:
            current[dataset_id] = dataset
    if not current:
        return problems
    try:
        problems += refresher(ctx, item, entry, current)
        apply_item_metadata(ctx, item, entry, current)
    except (ItemFailed, GeoLensError) as exc:
        problems.append(str(exc))
    except StopRun:
        _settle(ctx, entry, current, problems)
        raise
    _settle(ctx, entry, current, problems)
    return problems


def _settle(
    ctx: MigrationContext, entry: ItemState, current: Iterable[str], problems: list[str]
) -> None:
    """Record the last update of every dataset whose writes have ended, so
    this command's own refresh doesn't read as a GeoLens change next time."""
    ended = [d for d in current if "ended" in entry.refreshing.get(d, {"ended": ""})]
    try:
        record_last_update(ctx, entry, ended)
    except GeoLensError as exc:
        problems.append(f"couldn't record its datasets' last update, {exc}")
