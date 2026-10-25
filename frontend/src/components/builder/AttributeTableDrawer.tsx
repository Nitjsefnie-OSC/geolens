import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Table2, X } from 'lucide-react';
import type { Map as MaplibreMap } from 'maplibre-gl';
import { useBuilderLayout } from '@/components/builder/hooks/use-builder-layout';
import { canOpenAttributeTable } from '@/lib/layer-capabilities';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { AttributeTable } from '@/components/dataset/AttributeTable';
import { geometryBounds } from '@/components/viewer/accessible-map-data';
import { getFeature } from '@/api/features';
import { maplibreFilterToCql2 } from '@/lib/maplibre-filter-utils';
import { toFitBounds } from '@/lib/bbox';
import { MAP_COLORS } from '@/lib/map-colors';
import type { MapLayerResponse } from '@/types/api';

// The map highlight the drawer draws for a clicked row. Own source and layer
// ids, cleared on close, layer switch, and unmount — never the ephemeral
// preview's overlay, which has its own lifecycle and owner.
const HIGHLIGHT_SOURCE = 'attribute-table-highlight';
const HIGHLIGHT_LINE_LAYER = 'attribute-table-highlight-line';
const HIGHLIGHT_POINT_LAYER = 'attribute-table-highlight-point';

const MIN_DRAWER_HEIGHT = 120;
const MAX_DRAWER_HEIGHT_RATIO = 0.7;

export interface AttributeTableDrawerProps {
  /** The layer whose dataset the table reads; must satisfy canOpenAttributeTable. */
  layer: MapLayerResponse;
  open: boolean;
  onClose: () => void;
  /** The builder map, for the fit-and-highlight on a row click. */
  mapRef: React.RefObject<MaplibreMap | null>;
  /**
   * The map's layers in map order, for the header's layer picker (options:
   * the ones passing canOpenAttributeTable). Omitted, no picker renders.
   */
  layers?: MapLayerResponse[];
  /** Receives the picker's chosen layer id; omitted, the picker never renders. */
  onLayerChange?: (layerId: string) => void;
}

/**
 * Read-only attribute table for one builder layer, docked under the map at
 * >=800px and a full-screen sheet below that. The layer's filter applies to
 * the table via the rows endpoint's CQL2 `filter` param; when the layer filter
 * cannot be converted to CQL2 the table falls back to every row plus a notice,
 * so the table never silently disagrees with the map.
 */
export function AttributeTableDrawer({
  layer,
  open,
  onClose,
  mapRef,
  layers,
  onLayerChange,
}: AttributeTableDrawerProps) {
  const { t } = useTranslation('builder');
  // The layer editor's breakpoint: the table docks at >=800px and goes
  // full-screen sheet below it. Never a separate media-query helper.
  const { isEditorHidden } = useBuilderLayout();
  const titleId = useId();
  const [height, setHeight] = useState(320);
  const heightRef = useRef(height);
  heightRef.current = height;

  const datasetId = layer.dataset_id;
  const columns = layer.dataset_column_info;

  // Same conversion the builder analysis sends: null = no filter, a CQL2
  // expression when it can be translated, 'unsupported' when it cannot.
  const cql2 = useMemo(
    () => maplibreFilterToCql2(layer.filter ?? null, columns, { requireKnownFields: true }),
    [layer.filter, columns],
  );
  const filterUnsupported = cql2 === 'unsupported';
  const cql2Filter = cql2 && cql2 !== 'unsupported' ? JSON.stringify(cql2) : undefined;

  const clearHighlight = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;
    for (const id of [HIGHLIGHT_LINE_LAYER, HIGHLIGHT_POINT_LAYER] as const) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (map.getSource(HIGHLIGHT_SOURCE)) map.removeSource(HIGHLIGHT_SOURCE);
  }, [mapRef]);

  // Monotonic token for the row-click request: bumped at every click start
  // below, and again in the cleanup (layer change and unmount), so neither an
  // older click on the same layer nor a response from the previous layer can
  // repaint the map.
  const rowRequestSeqRef = useRef(0);

  // Clear on close, on layer change, and on unmount (the cleanup of the
  // previous effect run), so a replaced table never leaves the previous
  // layer's highlight on the map.
  useEffect(() => {
    if (!open) return;
    return () => {
      rowRequestSeqRef.current += 1;
      clearHighlight();
    };
  }, [open, layer.id, clearHighlight]);

  // The selected row mirrors the map highlight: set where the highlight
  // paints (a click without usable geometry leaves it), cleared when the
  // layer changes and with the drawer itself.
  const [selectedGid, setSelectedGid] = useState<number | null>(null);
  useEffect(() => {
    setSelectedGid(null);
  }, [layer.id]);

  // The layer picker keeps focus across a switch: switching remounts
  // AttributeTable (key={datasetId}) and the focused select with it, so the
  // drawer — which persists — flags the switch and the fresh picker takes
  // focus here. Opening never steals focus: the flag starts false and is set
  // only in the picker's onChange.
  const pickerRef = useRef<HTMLSelectElement | null>(null);
  const pickerFocusOnSwitchRef = useRef(false);
  useEffect(() => {
    if (!pickerFocusOnSwitchRef.current) return;
    pickerFocusOnSwitchRef.current = false;
    pickerRef.current?.focus();
  }, [layer.id]);

  const handleRowClick = useCallback(
    async (gid: number) => {
      if (!datasetId) return;
      const seq = ++rowRequestSeqRef.current;
      let feature: Awaited<ReturnType<typeof getFeature>>;
      try {
        feature = await getFeature(datasetId, gid);
      } catch {
        // A deleted row (404) or a lost request: leave the table usable and
        // the map untouched.
        return;
      }
      if (seq !== rowRequestSeqRef.current) return;
      const map = mapRef.current;
      if (!map) return;
      // Bounds before paint: without usable geometry the map stays exactly as
      // it was — the previous highlight included.
      const bbox = geometryBounds(feature.geometry);
      if (!bbox) return;
      setSelectedGid(gid);
      clearHighlight();
      map.addSource(HIGHLIGHT_SOURCE, {
        type: 'geojson',
        data: { type: 'Feature', geometry: feature.geometry, properties: {} },
      });
      map.addLayer({
        id: HIGHLIGHT_LINE_LAYER,
        type: 'line',
        source: HIGHLIGHT_SOURCE,
        filter: ['!=', '$type', 'Point'],
        paint: {
          'line-color': MAP_COLORS.selection.stroke,
          'line-width': 3,
        },
      });
      map.addLayer({
        id: HIGHLIGHT_POINT_LAYER,
        type: 'circle',
        source: HIGHLIGHT_SOURCE,
        filter: ['==', '$type', 'Point'],
        paint: {
          'circle-radius': 8,
          'circle-color': MAP_COLORS.selection.fill,
          'circle-stroke-color': MAP_COLORS.selection.stroke,
          'circle-stroke-width': 2,
        },
      });
      try {
        map.fitBounds(toFitBounds(bbox), { padding: 40, maxZoom: 18 });
      } catch {
        // Out-of-range coordinates: keep the highlight, skip the fit.
      }
    },
    [datasetId, mapRef, clearHighlight],
  );

  // --- Resizing -------------------------------------------------------------
  const dragOrigin = useRef<{ pointerY: number; height: number } | null>(null);
  // The column's height follows the window (the page root fills <main>'s
  // remaining viewport under the navbar/footer), so the maximum derives from
  // window.innerHeight and keeps the map a usable minimum inside the column.
  const [maxHeight, setMaxHeight] = useState(
    () => Math.round(window.innerHeight * MAX_DRAWER_HEIGHT_RATIO),
  );
  const clampHeight = useCallback(
    (next: number): number => Math.max(MIN_DRAWER_HEIGHT, Math.min(maxHeight, next)),
    [maxHeight],
  );
  // When the window shrinks the maximum with it, an over-tall table reclamps.
  useEffect(() => {
    const onWindowResize = () => {
      const max = Math.round(window.innerHeight * MAX_DRAWER_HEIGHT_RATIO);
      setMaxHeight(max);
      setHeight((current) => Math.max(MIN_DRAWER_HEIGHT, Math.min(max, current)));
    };
    window.addEventListener('resize', onWindowResize);
    return () => window.removeEventListener('resize', onWindowResize);
  }, []);

  // map.resize() coalesced at most once per animation frame while the handle
  // drags, plus a final call on drag end: MapLibre re-reads its container's
  // size only on resize().
  const resizeFrameRef = useRef<number | null>(null);
  const scheduleMapResize = useCallback(() => {
    if (resizeFrameRef.current !== null) return;
    resizeFrameRef.current = window.requestAnimationFrame(() => {
      resizeFrameRef.current = null;
      mapRef.current?.resize();
    });
  }, [mapRef]);
  useEffect(
    () => () => {
      if (resizeFrameRef.current !== null) window.cancelAnimationFrame(resizeFrameRef.current);
    },
    [],
  );

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    dragOrigin.current = { pointerY: e.clientY, height: heightRef.current };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragOrigin.current) return;
    const delta = dragOrigin.current.pointerY - e.clientY;
    setHeight(clampHeight(dragOrigin.current.height + delta));
    scheduleMapResize();
  };
  const handlePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    dragOrigin.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    // Final resize now; drop any queued frame so it cannot double-call.
    if (resizeFrameRef.current !== null) {
      window.cancelAnimationFrame(resizeFrameRef.current);
      resizeFrameRef.current = null;
    }
    mapRef.current?.resize();
  };
  const handleResizeKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    // Proportional steps: a fixed ±px step returns to exactly the starting
    // height after up-then-down, while a proportional step scales the table
    // against its current size. Home/End still pin the exact bounds.
    const factor = e.shiftKey ? 0.25 : 0.1;
    let next: number | null = null;
    if (e.key === 'ArrowUp') next = heightRef.current * (1 + factor);
    else if (e.key === 'ArrowDown') next = heightRef.current * (1 - factor);
    else if (e.key === 'Home') next = maxHeight;
    else if (e.key === 'End') next = MIN_DRAWER_HEIGHT;
    if (next === null) return;
    e.preventDefault();
    setHeight(clampHeight(next));
    scheduleMapResize();
  };

  const layerName = layer.display_name ?? layer.dataset_name;
  const pickerLayers = layers?.filter(canOpenAttributeTable);

  // The one merged header, composed from AttributeTable's own pieces: name,
  // (picker,) row count, filter notice, Columns control, close — in the
  // maintainer's order. The name keeps truncate + min-w-0; nothing that can
  // squeeze it away carries shrink-0 (the close button sits last and never
  // competes for width).
  const renderHeader = ({ count, columns }: { count: ReactNode; columns: ReactNode }) => (
    <header className="flex items-center gap-2 border-b px-4 py-2 shrink-0">
      <Table2 className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
      <h2
        id={titleId}
        data-testid="attribute-table-drawer-title"
        className="min-w-0 flex-1 truncate text-sm"
      >
        {layerName}
      </h2>
      {pickerLayers && onLayerChange && (
        <select
          ref={pickerRef}
          aria-label={t('attributeTableDrawer.layerPicker', { defaultValue: 'Attribute table layer' })}
          className="h-7 max-w-40 cursor-pointer rounded-md border bg-transparent px-1.5 text-xs"
          value={layer.id}
          onChange={(e) => {
            pickerFocusOnSwitchRef.current = true;
            onLayerChange(e.target.value);
          }}
        >
          {pickerLayers.map((pickerLayer) => (
            <option key={pickerLayer.id} value={pickerLayer.id}>
              {pickerLayer.display_name ?? pickerLayer.dataset_name}
            </option>
          ))}
        </select>
      )}
      {count}
      {filterUnsupported && (
        <p
          role="status"
          data-testid="attribute-table-filter-unsupported"
          className="text-xs text-muted-foreground"
        >
          {t('attributeTableDrawer.filterUnsupported', {
            defaultValue: "The layer's filter could not be applied — showing every row.",
          })}
        </p>
      )}
      {columns}
      <button
        type="button"
        onClick={onClose}
        aria-label={t('attributeTableDrawer.close', { defaultValue: 'Close attribute table' })}
        className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-[var(--surface-2)] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </header>
  );

  const rows = datasetId && (
    <AttributeTable
      key={datasetId}
      datasetId={datasetId}
      compact
      cql2Filter={cql2Filter}
      onRowClick={handleRowClick}
      selectedGid={selectedGid}
      renderHeader={renderHeader}
    />
  );

  // Docked under the map at >=800px: a plain section beside the map, no dialog
  // semantics and no Escape handler — Escape stays with the map; the ✕ closes.
  if (!isEditorHidden) {
    return (
      <section
        aria-labelledby={titleId}
        className="flex flex-col overflow-hidden border-t bg-background shrink-0"
        style={{ height }}
      >
        {/* Resize handle: pointer drag or Arrow/Home/End keys. */}
        {/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- role="separator" with aria-orientation is the WAI-ARIA separator (resizable) pattern; the rules' default interactive lists lag it. */}
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label={t('attributeTableDrawer.resizeHandle', { defaultValue: 'Resize attribute table' })}
          aria-valuenow={height}
          aria-valuemin={MIN_DRAWER_HEIGHT}
          aria-valuemax={maxHeight}
          tabIndex={0}
          data-testid="attribute-table-drawer-resize"
          className="group/handle flex h-3 shrink-0 cursor-row-resize items-center justify-center touch-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
          onKeyDown={handleResizeKeyDown}
        >
          <span aria-hidden="true" className="h-1 w-10 rounded-full bg-muted-foreground/40 group-hover/handle:bg-muted-foreground/70" />
        </div>
        {rows}
      </section>
    );
  }

  // Below 800px: full-screen sheet on the editor's breakpoint; Escape stays
  // with the map, the ✕ closes.
  return (
    <Sheet open={open} modal={false} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent
        side="bottom"
        overlay={false}
        showCloseButton={false}
        onEscapeKeyDown={(event) => event.preventDefault()}
        className="h-full w-full gap-0 border-t p-0"
        aria-labelledby={titleId}
        aria-describedby={undefined}
      >
        {rows}
      </SheetContent>
    </Sheet>
  );
}
