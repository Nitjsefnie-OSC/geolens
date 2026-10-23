# SPDX-License-Identifier: Apache-2.0
"""The ``import_map`` action of `geolens arcgis migrate`.

Hand-maintained. Reads a Web Map's configuration from the portal, matches its
operational layers to the datasets the run imported for the services they
point at, and has GeoLens translate it into a map. The layer-to-map-layer ids
GeoLens returns are kept in the item's state, and the loss report is written
next to the state file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import arcgis_report as _report
from ._sdk_helpers import long_request_timeout
from .arcgis_migrate_session import (
    GeoLensError,
    ItemFailed,
    ItemResult,
    MigrationContext,
)
from .arcgis_migrate_state import DONE, EXISTS, FINISHED, ItemState
from .arcgis_portal import PortalError, redact_json

LOSS_DIRNAME = "webmap-loss"
_ITEM_ID = re.compile(r"[0-9A-Za-z]{32}")
_TRAILING_LAYER = re.compile(r"/(\d+)/?$")
_MAX_NOTE_LAYERS = 5


def _walk(layers: Any) -> list[Mapping[str, Any]]:
    """Operational layers including group members."""
    found: list[Mapping[str, Any]] = []
    pending = [layers]
    while pending:
        current = pending.pop()
        if not isinstance(current, list):
            continue
        for layer in current:
            if isinstance(layer, dict):
                found.append(layer)
                pending.append(layer.get("layers"))
    return found


def _dataset_for(layer: Mapping[str, Any], source: ItemState) -> str | None:
    """The dataset imported for *layer*, or None when the item holds no
    unambiguous one."""
    url = layer.get("url")
    path = urlsplit(url).path if isinstance(url, str) else ""
    sub = _TRAILING_LAYER.search(path)
    if sub and sub.group(1) in source.layer_datasets:
        return source.layer_datasets[sub.group(1)]
    if len(source.dataset_ids) == 1:
        return source.dataset_ids[0]
    return None


def match_datasets(ctx: MigrationContext, webmap: Mapping[str, Any]) -> dict[str, str]:
    """Operational layer id -> dataset id, for the layers whose item the run
    imported."""
    matches: dict[str, str] = {}
    for layer in _walk(webmap.get("operationalLayers")):
        layer_id, item_id = layer.get("id"), layer.get("itemId")
        source = ctx.items.get(item_id) if isinstance(item_id, str) else None
        if not isinstance(layer_id, str) or source is None:
            continue
        if source.status not in FINISHED:
            continue
        dataset = _dataset_for(layer, source)
        if dataset:
            matches[layer_id] = dataset
    return matches


def _fetch(ctx: MigrationContext, item_id: str) -> dict[str, Any]:
    if ctx.portal is None:
        raise ItemFailed("the run can't read this item from the portal")
    try:
        data = ctx.portal.client.get_json(f"content/items/{item_id}/data")
    except PortalError as exc:
        raise ItemFailed(f"reading the web map failed, {exc}") from None
    if not isinstance(data, dict):
        raise ItemFailed("the web map's data is not a JSON object")
    # A layer URL can carry a stored token, which must not reach GeoLens.
    return redact_json(data, ctx.redact)


def _write_loss(ctx: MigrationContext, item_id: str, report: Any) -> str:
    if ctx.export_dir is None:
        raise ItemFailed("the run has no folder for the loss report")
    folder = ctx.export_dir.parent
    path = Path(LOSS_DIRNAME) / f"{item_id}.json"
    text = json.dumps(redact_json(report, ctx.redact), indent=2, sort_keys=True)
    try:
        (folder / LOSS_DIRNAME).mkdir(parents=True, exist_ok=True)
        _report.write_private(folder / path, text + "\n")
    except OSError as exc:
        raise ItemFailed(
            f"writing the loss report failed: {exc.strerror or type(exc).__name__}"
        ) from None
    return path.as_posix()


def import_map(
    ctx: MigrationContext, item: Mapping[str, Any], entry: ItemState
) -> ItemResult:
    """Create the GeoLens map for a Web Map item; a map the state already
    holds is kept rather than imported again."""
    if entry.map_id:
        if entry.pending_loss is not None:
            return _finish_loss(ctx, item["id"], entry)
        return ItemResult(status=EXISTS, notes=list(entry.notes))
    if not _ITEM_ID.fullmatch(item["id"]):
        raise ItemFailed("the item id is not an ArcGIS item id")

    from geolens.api.maps import (
        import_arcgis_webmap_endpoint_maps_import_arcgis_webmap_post as import_webmap,
    )
    from geolens.models.arcgis_webmap_import_request import ArcgisWebmapImportRequest
    from geolens.models.arcgis_webmap_import_request_basemap_map import (
        ArcgisWebmapImportRequestBasemapMap,
    )
    from geolens.models.arcgis_webmap_import_request_dataset_matches import (
        ArcgisWebmapImportRequestDatasetMatches,
    )
    from geolens.models.arcgis_webmap_import_request_webmap import (
        ArcgisWebmapImportRequestWebmap,
    )
    from geolens.models.map_visibility import check_map_visibility

    webmap = _fetch(ctx, item["id"])
    matches = match_datasets(ctx, webmap)
    title = item["target"]["title"] or item["title"]
    body = ArcgisWebmapImportRequest(
        webmap=ArcgisWebmapImportRequestWebmap.from_dict(webmap),
        name=title[:255],
        title=item["title"][:255],
        visibility=check_map_visibility(item["target"]["visibility"]),
        basemap_map=ArcgisWebmapImportRequestBasemapMap.from_dict(ctx.basemap_map),
        dataset_matches=ArcgisWebmapImportRequestDatasetMatches.from_dict(matches),
    )
    try:
        with long_request_timeout(ctx.geolens.client):
            result = ctx.geolens.call(
                import_webmap.sync_detailed,
                expected=201,
                body=body,
            )
    except GeoLensError as exc:
        raise ItemFailed(f"the web map import failed, {exc}") from None

    entry.map_id = str(result.map_.id)
    entry.layer_ids = {
        k: str(v) for k, v in result.layer_ids.additional_properties.items()
    }
    # Kept in the state until the loss report file is written, so a failed
    # write is finished on the rerun instead of dropping the losses.
    entry.pending_loss = {
        "loss_report": result.loss_report.to_dict(),
        "unmatched_layers": [layer.to_dict() for layer in result.unmatched_layers],
    }
    ctx.save()
    return _finish_loss(ctx, item["id"], entry)


def _finish_loss(ctx: MigrationContext, item_id: str, entry: ItemState) -> ItemResult:
    payload = entry.pending_loss or {}
    notes: list[str] = []
    unmatched = [
        layer.get("title") or layer.get("layer_id")
        for layer in payload.get("unmatched_layers", [])
    ]
    if unmatched:
        shown = ", ".join(unmatched[:_MAX_NOTE_LAYERS])
        more = len(unmatched) - _MAX_NOTE_LAYERS
        notes.append(
            f"{len(unmatched)} layer(s) not imported: {shown}"
            + (f" and {more} more" if more > 0 else "")
        )
    lost = payload.get("loss_report", {}).get("items", [])
    if lost:
        notes.append(f"{len(lost)} part(s) of the web map were not carried over")
    if lost or unmatched:
        entry.loss_report_path = _write_loss(ctx, item_id, payload)
    entry.pending_loss = None
    ctx.save()
    return ItemResult(status=DONE, notes=notes)
