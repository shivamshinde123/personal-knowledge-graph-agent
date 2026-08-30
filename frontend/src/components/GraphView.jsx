import { useEffect, useRef, useState } from "react";
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
  github: "#16151a",
  google_calendar: "#e0a800",
  browser_history: "#8b8896",
};

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

  const simulationRef = useRef(null);
  const nodesRef = useRef([]);
  const linksRef = useRef([]);
  const svgRef = useRef(null);
  const canvasRef = useRef(null);
  const draggingIdRef = useRef(null);
  // Measured from the actual canvas element, not a fixed constant — see
  // the ResizeObserver effect below.
  const [size, setSize] = useState({ width: 900, height: 600 });

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

  /** Converts a pointer event's screen coordinates into the SVG's own
   * viewBox coordinate space, accounting for however the SVG is scaled to
   * fit its container — dragging math must stay in the same coordinate
   * space the simulation itself uses. */
  function toSimulationPoint(event) {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM();
    if (!svg || !ctm) return { x: 0, y: 0 };
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(ctm.inverse());
    return { x: transformed.x, y: transformed.y };
  }

  function handlePointerDown(node, event) {
    event.currentTarget.setPointerCapture(event.pointerId);
    draggingIdRef.current = node.id;
    simulationRef.current?.alphaTarget(0.3).restart();
    const { x, y } = toSimulationPoint(event);
    node.fx = x;
    node.fy = y;
  }

  function handlePointerMove(event) {
    if (draggingIdRef.current === null) return;
    const node = nodesRef.current.find((n) => n.id === draggingIdRef.current);
    if (!node) return;
    const { x, y } = toSimulationPoint(event);
    node.fx = x;
    node.fy = y;
  }

  function handlePointerUp(event) {
    if (draggingIdRef.current === null) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const node = nodesRef.current.find((n) => n.id === draggingIdRef.current);
    if (node) {
      node.fx = null;
      node.fy = null;
    }
    draggingIdRef.current = null;
    simulationRef.current?.alphaTarget(0);
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
      <p className="graph-view-hint">Drag a node to move it around.</p>
      <div className="graph-view-canvas" ref={canvasRef}>
        <svg
          ref={svgRef}
          className="graph-view-svg"
          viewBox={`0 0 ${size.width} ${size.height}`}
          role="img"
          aria-label="Relationship graph"
        >
          <g>
            {links.map((edge, index) => {
              const source =
                typeof edge.source === "object" ? edge.source : null;
              const target =
                typeof edge.target === "object" ? edge.target : null;
              if (!source || !target) return null;
              return (
                <g key={`${source.id}-${target.id}-${index}`}>
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
            {nodes.map((node) => (
              <g
                key={node.id}
                className="graph-node"
                style={{ touchAction: "none", cursor: "grab" }}
                onPointerDown={(event) => handlePointerDown(node, event)}
                onPointerMove={handlePointerMove}
                onPointerUp={handlePointerUp}
                onPointerCancel={handlePointerUp}
              >
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
                  {node.title || "Untitled"}
                </text>
              </g>
            ))}
          </g>
        </svg>
      </div>
      <ul className="graph-view-legend">
        {Object.entries(SOURCE_TYPE_COLORS)
          // browser_history is excluded from relationship detection entirely
          // (see pipeline/relationships.py, CLAUDE.md) — a node only exists
          // here once it has a confirmed relationship, so a browser_history
          // node can never appear on this graph. Listing it in the legend
          // would promise something that can never show up.
          .filter(([sourceType]) => sourceType !== "browser_history")
          .map(([sourceType, color]) => (
            <li key={sourceType}>
              <span
                className="graph-view-legend-swatch"
                style={{ backgroundColor: color }}
              />
              {sourceType.replace("_", " ")}
            </li>
          ))}
      </ul>
    </main>
  );
}

export default GraphView;
