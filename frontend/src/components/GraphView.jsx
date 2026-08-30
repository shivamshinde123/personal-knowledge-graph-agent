import { useEffect, useMemo, useState } from "react";
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

const WIDTH = 900;
const HEIGHT = 600;

/**
 * Runs a d3-force layout synchronously to convergence and returns
 * positioned nodes/edges — a static picture, not a live simulation, since
 * this graph is expected to stay small (docs/Database_Schema.docx section
 * 5: an item only gets a node once it has a confirmed relationship) and a
 * one-shot layout is simpler than wiring up continuous re-renders for a
 * local, single-user tool. See DECISIONS.md.
 */
function layoutGraph(nodes, edges) {
  const simNodes = nodes.map((node) => ({ ...node }));
  const simLinks = edges.map((edge) => ({
    source: edge.source_id,
    target: edge.target_id,
    label: edge.label,
    confidence: edge.confidence,
  }));

  const simulation = forceSimulation(simNodes)
    .force(
      "link",
      forceLink(simLinks)
        .id((node) => node.id)
        .distance(140),
    )
    .force("charge", forceManyBody().strength(-260))
    .force("center", forceCenter(WIDTH / 2, HEIGHT / 2))
    .stop();

  for (let i = 0; i < 300; i += 1) {
    simulation.tick();
  }

  return { nodes: simNodes, edges: simLinks };
}

/**
 * The relationship graph view (Screen 3, extension beyond
 * docs/UIUX_Wireframes.docx — see DECISIONS.md). Fetches the whole graph
 * on mount and renders it as a static force-directed SVG layout.
 */
function GraphView() {
  const [graph, setGraph] = useState(null);
  const [error, setError] = useState(null);

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

  const laidOut = useMemo(() => {
    if (!graph || graph.nodes.length === 0) return null;
    return layoutGraph(graph.nodes, graph.edges);
  }, [graph]);

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
      <svg
        className="graph-view-svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="Relationship graph"
      >
        <g>
          {laidOut.edges.map((edge, index) => (
            <g key={`${edge.source.id}-${edge.target.id}-${index}`}>
              <line
                className="graph-edge"
                x1={edge.source.x}
                y1={edge.source.y}
                x2={edge.target.x}
                y2={edge.target.y}
              />
              <text
                className="graph-edge-label"
                x={(edge.source.x + edge.target.x) / 2}
                y={(edge.source.y + edge.target.y) / 2}
              >
                {edge.label}
              </text>
            </g>
          ))}
        </g>
        <g>
          {laidOut.nodes.map((node) => (
            <g key={node.id} className="graph-node">
              <circle
                cx={node.x}
                cy={node.y}
                r={10}
                fill={SOURCE_TYPE_COLORS[node.source_type] || "#8b8896"}
              />
              <text className="graph-node-label" x={node.x + 14} y={node.y + 4}>
                {node.title || "Untitled"}
              </text>
            </g>
          ))}
        </g>
      </svg>
      <ul className="graph-view-legend">
        {Object.entries(SOURCE_TYPE_COLORS).map(([sourceType, color]) => (
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
