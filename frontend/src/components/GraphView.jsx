import { useEffect, useMemo, useRef, useState } from "react";
import {
  forceCenter,
  forceLink,
  forceManyBody,
  forceSimulation,
} from "d3-force";
import { getGraph } from "../api/client.js";

const SOURCE_TYPE_COLORS = {
  local_file: "#6b4fd6",
  notion: "#c0392b",
  gmail: "#1f9d55",
  // Was "#16151a", an almost-black GitHub-brand color, barely visible
  // against this dark theme's background — see DECISIONS.md.
  github: "#3b82f6",
  // Matches extractors/calendar.py's SOURCE_TYPE = "calendar" — was
  // "google_calendar" here, which never matched any real node's
  // source_type, so calendar nodes silently fell back to the generic
  // gray color and the legend entry never highlighted a filter that did
  // anything. Fixed while wiring up per-source filtering, which depends
  // on this key actually matching. See DECISIONS.md.
  calendar: "#e0a800",
  browser_history: "#8b8896",
};

// browser_history is excluded everywhere below (filter chips, legend) —
// it's excluded from relationship detection entirely (see
// pipeline/relationships.py, CLAUDE.md), so a browser_history node can
// never appear on this graph in the first place.
const FILTERABLE_SOURCE_TYPES = Object.keys(SOURCE_TYPE_COLORS).filter(
  (sourceType) => sourceType !== "browser_history",
);

const MIN_ZOOM = 0.2;
const MAX_ZOOM = 4;
const ZOOM_STEP = 1.4;
const FIT_PADDING = 60;
const DIMMED_OPACITY = 0.15;
// Screen-pixel movement below this, between pointerdown and pointerup on
// a node, counts as a click (toggle selection) rather than a completed
// drag — distinguishing the two on the same pointer sequence.
const CLICK_DRAG_THRESHOLD_PX = 4;
// GitHub item titles in particular (commit messages, PR/issue titles) run
// long and, rendered in full next to every node, overlap and clutter the
// layout — see DECISIONS.md. Truncated in the label; the full title is
// still available via the node's native <title> tooltip on hover.
const MAX_LABEL_LENGTH = 32;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function truncateLabel(title) {
  const text = title || "Untitled";
  return text.length > MAX_LABEL_LENGTH
    ? `${text.slice(0, MAX_LABEL_LENGTH - 1)}…`
    : text;
}

// Fallback size before the container has been measured (first paint) —
// the real canvas fills whatever space is available (see the
// ResizeObserver effect below), rather than a fixed 900x600 box centered
// on the page. Requested directly: the graph should use the whole screen.

/**
 * The relationship graph view (Screen 3, extension beyond
 * docs/UIUX_Wireframes.docx — see DECISIONS.md). Fetches the whole graph
 * on mount and renders it as a live, draggable force-directed layout —
 * Obsidian-style: the simulation keeps running (not a one-shot layout to
 * convergence), and grabbing a node "reheats" it so neighbors resettle
 * around wherever it's dropped, rather than a static picture. See
 * DECISIONS.md.
 *
 * Also supports: per-source-type filtering (a "Filter" toolbar button
 * opens a checkbox panel — deliberately in the toolbar rather than a
 * passive legend at the bottom, so it reads as an actual control, not
 * decoration — see DECISIONS.md), pan/zoom (wheel, +/- buttons, drag the
 * background), clicking a node to highlight its immediate neighborhood
 * and dim the rest, and fitting the whole (visible) graph into view
 * automatically once the simulation settles, whenever the filter set
 * changes, or on demand via a "Fit view" button. See DECISIONS.md.
 */
function GraphView() {
  const [graph, setGraph] = useState(null);
  const [error, setError] = useState(null);
  // What's actually rendered: a shallow-copied snapshot of the simulation's
  // node/link objects, refreshed on every tick. A ref alone can't drive
  // render (React doesn't re-render on ref mutation, and reading .current
  // during render bypasses React's model — see the react-hooks/refs lint
  // rule), so nodesRef/linksRef below hold the real, d3-mutated objects for
  // drag handlers to act on, while these two states are what JSX reads.
  const [nodes, setNodes] = useState([]);
  const [links, setLinks] = useState([]);
  const [visibleSourceTypes, setVisibleSourceTypes] = useState(
    () => new Set(FILTERABLE_SOURCE_TYPES),
  );
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [isFilterOpen, setIsFilterOpen] = useState(false);
  const filterPanelRef = useRef(null);
  // Pan/zoom transform, applied to a <g> wrapping the rendered edges/nodes:
  // viewBoxPoint = k * simulationPoint + (x, y). Node/edge coordinates
  // themselves stay in raw, untransformed simulation space always — only
  // this maps them onto the screen.
  const [transform, setTransform] = useState({ x: 0, y: 0, k: 1 });

  const simulationRef = useRef(null);
  const nodesRef = useRef([]);
  const linksRef = useRef([]);
  const svgRef = useRef(null);
  const canvasRef = useRef(null);
  const transformRef = useRef(transform);
  const draggingIdRef = useRef(null);
  const dragStartScreenRef = useRef(null);
  const panningRef = useRef(null);
  const hasAutoFittedRef = useRef(false);
  // Measured from the actual canvas element, not a fixed constant — see
  // the ResizeObserver effect below.
  const [size, setSize] = useState({ width: 900, height: 600 });
  const sizeRef = useRef(size);

  useEffect(() => {
    transformRef.current = transform;
  }, [transform]);

  useEffect(() => {
    sizeRef.current = size;
  }, [size]);

  useEffect(() => {
    let cancelled = false;
    getGraph()
      .then((result) => {
        if (!cancelled) setGraph(result);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Could not load the graph.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Keeps `size` in sync with the canvas element's real, on-screen box —
  // window resizes, sidebar toggles, or the initial mount all change it.
  // `ResizeObserver` (not a `window.resize` listener) also catches layout
  // changes that don't come from resizing the window itself.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ width, height });
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!graph || graph.nodes.length === 0) return undefined;

    const simNodes = graph.nodes.map((node) => ({ ...node }));
    const simLinks = graph.edges.map((edge) => ({
      source: edge.source_id,
      target: edge.target_id,
      label: edge.label,
      confidence: edge.confidence,
    }));
    nodesRef.current = simNodes;
    linksRef.current = simLinks;
    hasAutoFittedRef.current = false;

    const simulation = forceSimulation(simNodes)
      .force(
        "link",
        forceLink(simLinks)
          .id((node) => node.id)
          .distance(140),
      )
      .force("charge", forceManyBody().strength(-260))
      .force("center", forceCenter(size.width / 2, size.height / 2))
      .on("tick", () => {
        setNodes([...nodesRef.current]);
        setLinks([...linksRef.current]);
      })
      .on("end", () => {
        // Auto-fit once per graph load, when the layout has settled —
        // not on every tick, and not again after the user has had a
        // chance to zoom/pan manually.
        if (hasAutoFittedRef.current) return;
        hasAutoFittedRef.current = true;
        fitToView();
      });

    simulationRef.current = simulation;

    return () => {
      simulation.stop();
      simulationRef.current = null;
    };
    // Deliberately excludes `size`: re-running this whole effect on every
    // resize would restart the simulation (nodes jump back to their
    // starting positions) instead of smoothly re-centering. The effect
    // below nudges the existing simulation's center force instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  // Re-centers the already-running simulation when the canvas resizes,
  // without restarting it (see the note above) — a small nudge
  // (`alpha(0.3)`, not a full `.restart()`) so nodes drift toward the new
  // center rather than snapping.
  useEffect(() => {
    const simulation = simulationRef.current;
    if (!simulation) return;
    simulation.force("center", forceCenter(size.width / 2, size.height / 2));
    simulation.alpha(0.3).restart();
  }, [size]);

  // Re-fits whenever the filter set changes (the visible bounding box
  // changed) — but not on the very first render, before any nodes exist.
  useEffect(() => {
    if (!hasAutoFittedRef.current) return;
    fitToView();
  }, [visibleSourceTypes]);

  // Closes the filter panel on an outside click — checkbox clicks inside
  // it don't close it (so toggling several sources in a row doesn't need
  // reopening each time), only a click elsewhere does.
  useEffect(() => {
    if (!isFilterOpen) return undefined;
    function handlePointerDown(event) {
      if (
        filterPanelRef.current &&
        !filterPanelRef.current.contains(event.target)
      ) {
        setIsFilterOpen(false);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [isFilterOpen]);

  const visibleNodeIds = useMemo(
    () =>
      new Set(
        nodes
          .filter((n) => visibleSourceTypes.has(n.source_type))
          .map((n) => n.id),
      ),
    [nodes, visibleSourceTypes],
  );

  // Edges directly touching the selected node, and the set of nodes that
  // belong to the highlighted neighborhood (the selected node plus every
  // node one hop away via a visible edge). Both null when nothing is
  // selected, meaning "no dimming" rather than "dim everything."
  const { highlightNodeIds, highlightEdgeKeys } = useMemo(() => {
    if (selectedNodeId === null)
      return { highlightNodeIds: null, highlightEdgeKeys: null };
    const nodeIds = new Set([selectedNodeId]);
    const edgeKeys = new Set();
    links.forEach((edge, index) => {
      const source = typeof edge.source === "object" ? edge.source : null;
      const target = typeof edge.target === "object" ? edge.target : null;
      if (!source || !target) return;
      if (!visibleNodeIds.has(source.id) || !visibleNodeIds.has(target.id))
        return;
      if (source.id === selectedNodeId || target.id === selectedNodeId) {
        edgeKeys.add(`${source.id}-${target.id}-${index}`);
        nodeIds.add(source.id);
        nodeIds.add(target.id);
      }
    });
    return { highlightNodeIds: nodeIds, highlightEdgeKeys: edgeKeys };
  }, [selectedNodeId, links, visibleNodeIds]);

  /** Screen coordinates -> the SVG's own viewBox coordinate space,
   * accounting for however the SVG is scaled to fit its container. This
   * is "outer" space — before the pan/zoom <g> transform is applied. */
  function toViewBoxPoint(event) {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM();
    if (!svg || !ctm) return { x: 0, y: 0 };
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(ctm.inverse());
    return { x: transformed.x, y: transformed.y };
  }

  /** Screen coordinates -> raw simulation space, inverting both the SVG's
   * viewBox scaling and the pan/zoom <g> transform — dragging math must
   * stay in the same coordinate space the simulation itself uses. */
  function toSimulationPoint(event) {
    const { x: vbX, y: vbY } = toViewBoxPoint(event);
    const { x, y, k } = transformRef.current;
    return { x: (vbX - x) / k, y: (vbY - y) / k };
  }

  function fitToView() {
    const visible = nodesRef.current.filter((n) =>
      visibleSourceTypesRef.current.has(n.source_type),
    );
    if (visible.length === 0) return;
    const xs = visible.map((n) => n.x ?? 0);
    const ys = visible.map((n) => n.y ?? 0);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const bboxWidth = Math.max(maxX - minX, 1);
    const bboxHeight = Math.max(maxY - minY, 1);
    const { width, height } = sizeRef.current;
    const scale = Math.min(
      (width - FIT_PADDING * 2) / bboxWidth,
      (height - FIT_PADDING * 2) / bboxHeight,
    );
    const k = clamp(scale, MIN_ZOOM, MAX_ZOOM);
    const centerX = (minX + maxX) / 2;
    const centerY = (minY + maxY) / 2;
    setTransform({
      x: width / 2 - k * centerX,
      y: height / 2 - k * centerY,
      k,
    });
  }

  // visibleSourceTypes is read inside fitToView(), which is itself called
  // from a d3 "end" callback registered once per graph load (closed over
  // whatever visibleSourceTypes was at that time) — a ref keeps it current
  // without re-registering the simulation's callbacks.
  const visibleSourceTypesRef = useRef(visibleSourceTypes);
  useEffect(() => {
    visibleSourceTypesRef.current = visibleSourceTypes;
  }, [visibleSourceTypes]);

  function zoomBy(factor) {
    const { width, height } = sizeRef.current;
    zoomAroundPoint(factor, { x: width / 2, y: height / 2 });
  }

  function zoomAroundPoint(factor, viewBoxPoint) {
    const { x, y, k } = transformRef.current;
    const newK = clamp(k * factor, MIN_ZOOM, MAX_ZOOM);
    const ratio = newK / k;
    setTransform({
      x: viewBoxPoint.x - (viewBoxPoint.x - x) * ratio,
      y: viewBoxPoint.y - (viewBoxPoint.y - y) * ratio,
      k: newK,
    });
  }

  function handleWheel(event) {
    event.preventDefault();
    const viewBoxPoint = toViewBoxPoint(event);
    const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
    zoomAroundPoint(factor, viewBoxPoint);
  }

  function toggleSourceType(sourceType) {
    setVisibleSourceTypes((prev) => {
      const next = new Set(prev);
      if (next.has(sourceType)) next.delete(sourceType);
      else next.add(sourceType);
      return next;
    });
  }

  function handleNodePointerDown(node, event) {
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    draggingIdRef.current = node.id;
    dragStartScreenRef.current = { x: event.clientX, y: event.clientY };
    simulationRef.current?.alphaTarget(0.3).restart();
    const { x, y } = toSimulationPoint(event);
    node.fx = x;
    node.fy = y;
  }

  function handleNodePointerMove(event) {
    if (draggingIdRef.current === null) return;
    const node = nodesRef.current.find((n) => n.id === draggingIdRef.current);
    if (!node) return;
    const { x, y } = toSimulationPoint(event);
    node.fx = x;
    node.fy = y;
  }

  function handleNodePointerUp(event) {
    if (draggingIdRef.current === null) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const nodeId = draggingIdRef.current;
    const node = nodesRef.current.find((n) => n.id === nodeId);
    if (node) {
      node.fx = null;
      node.fy = null;
    }
    const start = dragStartScreenRef.current;
    const movedPx = start
      ? Math.hypot(event.clientX - start.x, event.clientY - start.y)
      : Infinity;
    if (movedPx < CLICK_DRAG_THRESHOLD_PX) {
      setSelectedNodeId((prev) => (prev === nodeId ? null : nodeId));
    }
    draggingIdRef.current = null;
    dragStartScreenRef.current = null;
    simulationRef.current?.alphaTarget(0);
  }

  function handleBackgroundPointerDown(event) {
    // Only the background itself, not a bubbled event from a node (which
    // already called stopPropagation in handleNodePointerDown).
    event.currentTarget.setPointerCapture(event.pointerId);
    panningRef.current = {
      pointerId: event.pointerId,
      startClient: { x: event.clientX, y: event.clientY },
      startTransform: transformRef.current,
    };
  }

  function handleBackgroundPointerMove(event) {
    const panning = panningRef.current;
    if (!panning || panning.pointerId !== event.pointerId) return;
    const startVb = toViewBoxPointFromClient(panning.startClient);
    const nowVb = toViewBoxPoint(event);
    setTransform({
      x: panning.startTransform.x + (nowVb.x - startVb.x),
      y: panning.startTransform.y + (nowVb.y - startVb.y),
      k: panning.startTransform.k,
    });
  }

  function toViewBoxPointFromClient({ x, y }) {
    return toViewBoxPoint({ clientX: x, clientY: y });
  }

  function handleBackgroundPointerUp(event) {
    if (panningRef.current?.pointerId === event.pointerId) {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      panningRef.current = null;
    }
    // Clicking empty space (not panning, not a node) clears the selection.
    setSelectedNodeId(null);
  }

  if (error) {
    return (
      <main className="graph-view">
        <p className="graph-view-error">{error}</p>
      </main>
    );
  }

  if (!graph) {
    return (
      <main className="graph-view">
        <p className="graph-view-loading">Loading graph…</p>
      </main>
    );
  }

  if (graph.nodes.length === 0) {
    return (
      <main className="graph-view">
        <p className="graph-view-empty">
          No relationships have been confirmed between items yet. This fills in
          as the daily ingestion batch finds connections across your sources.
        </p>
      </main>
    );
  }

  return (
    <main className="graph-view">
      <p className="graph-view-hint">
        Drag a node to move it around. Click a node to highlight its
        connections. Scroll or use the buttons to zoom, drag the background to
        pan.
      </p>
      <div className="graph-view-canvas" ref={canvasRef}>
        <div className="graph-view-toolbar">
          <button
            type="button"
            className={`graph-view-toolbar-button graph-view-filter-button ${
              visibleSourceTypes.size < FILTERABLE_SOURCE_TYPES.length
                ? "active"
                : ""
            }`}
            onClick={() => setIsFilterOpen((prev) => !prev)}
            aria-expanded={isFilterOpen}
          >
            Filter
            {visibleSourceTypes.size < FILTERABLE_SOURCE_TYPES.length &&
              ` (${visibleSourceTypes.size}/${FILTERABLE_SOURCE_TYPES.length})`}
          </button>
          <button
            type="button"
            className="graph-view-toolbar-button"
            onClick={() => zoomBy(1 / ZOOM_STEP)}
            aria-label="Zoom out"
          >
            −
          </button>
          <button
            type="button"
            className="graph-view-toolbar-button"
            onClick={() => zoomBy(ZOOM_STEP)}
            aria-label="Zoom in"
          >
            +
          </button>
          <button
            type="button"
            className="graph-view-toolbar-button graph-view-toolbar-fit"
            onClick={fitToView}
          >
            Fit view
          </button>
        </div>
        {isFilterOpen && (
          <div className="graph-view-filter-panel" ref={filterPanelRef}>
            <div className="graph-view-filter-panel-header">
              <span>Filter by source</span>
              <div className="graph-view-filter-panel-actions">
                <button
                  type="button"
                  onClick={() =>
                    setVisibleSourceTypes(new Set(FILTERABLE_SOURCE_TYPES))
                  }
                >
                  All
                </button>
                <button
                  type="button"
                  onClick={() => setVisibleSourceTypes(new Set())}
                >
                  None
                </button>
              </div>
            </div>
            {FILTERABLE_SOURCE_TYPES.map((sourceType) => {
              const active = visibleSourceTypes.has(sourceType);
              return (
                <label className="graph-view-filter-option" key={sourceType}>
                  <input
                    type="checkbox"
                    checked={active}
                    onChange={() => toggleSourceType(sourceType)}
                  />
                  <span
                    className="graph-view-legend-swatch"
                    style={{ backgroundColor: SOURCE_TYPE_COLORS[sourceType] }}
                  />
                  {sourceType.replace("_", " ")}
                </label>
              );
            })}
          </div>
        )}
        <svg
          ref={svgRef}
          className="graph-view-svg"
          viewBox={`0 0 ${size.width} ${size.height}`}
          role="img"
          aria-label="Relationship graph"
          onWheel={handleWheel}
          onPointerDown={handleBackgroundPointerDown}
          onPointerMove={handleBackgroundPointerMove}
          onPointerUp={handleBackgroundPointerUp}
          onPointerCancel={handleBackgroundPointerUp}
        >
          {/* Full-viewBox transparent hit target so panning/deselecting
              works even when clicking between nodes with nothing else
              under the cursor. */}
          <rect
            x={0}
            y={0}
            width={size.width}
            height={size.height}
            fill="transparent"
          />
          <g
            transform={`translate(${transform.x},${transform.y}) scale(${transform.k})`}
          >
            <g>
              {links.map((edge, index) => {
                const source =
                  typeof edge.source === "object" ? edge.source : null;
                const target =
                  typeof edge.target === "object" ? edge.target : null;
                if (!source || !target) return null;
                if (
                  !visibleNodeIds.has(source.id) ||
                  !visibleNodeIds.has(target.id)
                ) {
                  return null;
                }
                const key = `${source.id}-${target.id}-${index}`;
                const dimmed =
                  highlightEdgeKeys !== null && !highlightEdgeKeys.has(key);
                return (
                  <g key={key} style={{ opacity: dimmed ? DIMMED_OPACITY : 1 }}>
                    <line
                      className="graph-edge"
                      x1={source.x}
                      y1={source.y}
                      x2={target.x}
                      y2={target.y}
                    />
                    <text
                      className="graph-edge-label"
                      x={(source.x + target.x) / 2}
                      y={(source.y + target.y) / 2}
                    >
                      {edge.label}
                    </text>
                  </g>
                );
              })}
            </g>
            <g>
              {nodes.map((node) => {
                if (!visibleNodeIds.has(node.id)) return null;
                const dimmed =
                  highlightNodeIds !== null && !highlightNodeIds.has(node.id);
                return (
                  <g
                    key={node.id}
                    className="graph-node"
                    style={{
                      touchAction: "none",
                      cursor: "grab",
                      opacity: dimmed ? DIMMED_OPACITY : 1,
                    }}
                    onPointerDown={(event) =>
                      handleNodePointerDown(node, event)
                    }
                    onPointerMove={handleNodePointerMove}
                    onPointerUp={handleNodePointerUp}
                    onPointerCancel={handleNodePointerUp}
                  >
                    {/* Full, untruncated title on hover — a native SVG
                        <title> needs no extra state/positioning logic. */}
                    <title>{node.title || "Untitled"}</title>
                    <circle
                      cx={node.x}
                      cy={node.y}
                      r={10}
                      fill={SOURCE_TYPE_COLORS[node.source_type] || "#8b8896"}
                    />
                    <text
                      className="graph-node-label"
                      x={node.x + 14}
                      y={node.y + 4}
                    >
                      {truncateLabel(node.title)}
                    </text>
                  </g>
                );
              })}
            </g>
          </g>
        </svg>
      </div>
    </main>
  );
}

export default GraphView;
