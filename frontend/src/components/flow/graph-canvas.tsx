"use client";

import "@react-sigma/core/lib/style.css";

import { SigmaContainer, useLoadGraph, useSigma } from "@react-sigma/core";
import { useWorkerLayoutForceAtlas2 } from "@react-sigma/layout-forceatlas2";
import Graph from "graphology";
import { useEffect, useMemo, useRef } from "react";
import type { EdgeView, NodeView } from "@/lib/api";
import { colorForComponent } from "@/lib/cluster-colors";

interface GraphCanvasProps {
  nodes: NodeView[];
  edges: EdgeView[];
}

// Canvas colors are hex/rgba — Sigma's WebGL parser doesn't accept oklch().
// CSS tokens in globals.css remain canonical for HTML surfaces.
const BG = "#0c0d12";
const LABEL_COLOR = "#e9ebf1";
const EDGE_ALPHA_MIN = 0.08;
const EDGE_ALPHA_MAX = 0.85;

function sizeForNode(degree: number, volumeSol: number): number {
  // Degree dominates — a hub's significance is how many wallets touch
  // it, not how much volume flowed. Volume is a soft tiebreaker among
  // equal-degree nodes so whales visually outrank random lonely wallets.
  return 2 + Math.sqrt(degree) * 1.6 + Math.log1p(volumeSol) * 0.15;
}

function widthFromVolume(volumeSol: number): number {
  return 0.8 + Math.log1p(volumeSol) * 0.7;
}

function edgeWeight(fromDeg: number, toDeg: number): number {
  // Weaken edges touching high-degree hubs so spokes have room to spread
  // instead of piling against the hub. With edgeWeightInfluence=1, FA2
  // scales attraction linearly with this value.
  const hub = Math.max(fromDeg, toDeg, 1);
  return 1 / Math.log2(hub + 1);
}

function nodeDegreeMap(nodes: NodeView[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const n of nodes) m.set(n.id, n.degree);
  return m;
}

function maxEdgeVolume(edges: EdgeView[]): number {
  let max = 0;
  for (const e of edges) if (e.volume_sol > max) max = e.volume_sol;
  return Math.max(max, 1);
}

/**
 * Edge alpha scales with log-normalized volume so the structural backbone
 * (heavy corridors) reads prominently while single-transfer drive-bys recede
 * to background mesh. Replaces the old flat-alpha EDGE_COLOR.
 */
function edgeColor(volumeSol: number, maxVol: number): string {
  const t = Math.log1p(volumeSol) / Math.log1p(maxVol);
  const alpha = EDGE_ALPHA_MIN + t * (EDGE_ALPHA_MAX - EDGE_ALPHA_MIN);
  return `rgba(200, 210, 235, ${alpha.toFixed(3)})`;
}

function seedPosition(i: number, total: number): { x: number; y: number } {
  const angle = (i / Math.max(total, 1)) * Math.PI * 2;
  const radius = 1 + (i % 7) * 0.15;
  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
}

function perimeterSeed(outerRadius: number): { x: number; y: number } {
  const angle = Math.random() * Math.PI * 2;
  const r = outerRadius * (1.08 + Math.random() * 0.18);
  return { x: Math.cos(angle) * r, y: Math.sin(angle) * r };
}

function nodeLabel(id: string): string {
  return `${id.slice(0, 4)}…${id.slice(-4)}`;
}

function edgeKey(from: string, to: string): string {
  return from < to ? `${from}|${to}` : `${to}|${from}`;
}

function initialGraph(nodes: NodeView[], edges: EdgeView[]): Graph {
  const graph = new Graph({ multi: false, type: "undirected" });
  const degrees = nodeDegreeMap(nodes);
  const maxVol = maxEdgeVolume(edges);

  // Backend sends nodes pre-sorted by degree desc, so iterating in
  // received order places hubs first and spokes find their hub already
  // in the graph by the time they seed.
  nodes.forEach((n, i) => {
    const { x, y } = seedPosition(i, nodes.length);
    graph.addNode(n.id, {
      x,
      y,
      size: sizeForNode(n.degree, n.volume_sol),
      color: colorForComponent(n.component),
      label: nodeLabel(n.id),
    });
  });

  edges.forEach((e) => {
    if (!graph.hasNode(e.from) || !graph.hasNode(e.to)) return;
    if (graph.hasEdge(e.from, e.to) || graph.hasEdge(e.to, e.from)) return;
    graph.addEdge(e.from, e.to, {
      size: widthFromVolume(e.volume_sol),
      color: edgeColor(e.volume_sol, maxVol),
      weight: edgeWeight(degrees.get(e.from) ?? 0, degrees.get(e.to) ?? 0),
    });
  });
  return graph;
}

function graphRadius(graph: Graph): number {
  let max = 0;
  graph.forEachNode((_id, attrs) => {
    const x = typeof attrs.x === "number" ? attrs.x : 0;
    const y = typeof attrs.y === "number" ? attrs.y : 0;
    const r = Math.sqrt(x * x + y * y);
    if (r > max) max = r;
  });
  return max || 1;
}

function neighborsByNode(edges: EdgeView[]): Map<string, string[]> {
  const m = new Map<string, string[]>();
  for (const e of edges) {
    if (!m.has(e.from)) m.set(e.from, []);
    if (!m.has(e.to)) m.set(e.to, []);
    m.get(e.from)!.push(e.to);
    m.get(e.to)!.push(e.from);
  }
  return m;
}

function seedForNewNode(
  graph: Graph,
  id: string,
  neighbors: Map<string, string[]>,
  outerRadius: number,
): { x: number; y: number; isLonely: boolean } {
  const peers = neighbors.get(id) ?? [];
  const placed = peers.find((p) => graph.hasNode(p));
  if (placed) {
    const a = graph.getNodeAttributes(placed) as { x: number; y: number };
    const jitter = 0.5;
    return {
      x: a.x + (Math.random() - 0.5) * jitter,
      y: a.y + (Math.random() - 0.5) * jitter,
      isLonely: false,
    };
  }
  if (peers.length > 0) {
    // Has edges in current data but no placed neighbor yet — still edge-connected.
    const { x, y } = perimeterSeed(outerRadius * 0.8);
    return { x, y, isLonely: false };
  }
  const { x, y } = perimeterSeed(outerRadius);
  return { x, y, isLonely: true };
}

function applyDiff(graph: Graph, nodes: NodeView[], edges: EdgeView[]) {
  const nextNodeIds = new Set(nodes.map((n) => n.id));
  const nextEdgeKeys = new Set(edges.map((e) => edgeKey(e.from, e.to)));

  graph.forEachNode((id) => {
    if (!nextNodeIds.has(id)) graph.dropNode(id);
  });

  graph.forEachEdge((eid, _attrs, source, target) => {
    if (!nextEdgeKeys.has(edgeKey(source, target))) graph.dropEdge(eid);
  });

  const neighbors = neighborsByNode(edges);
  const degrees = nodeDegreeMap(nodes);
  const maxVol = maxEdgeVolume(edges);
  const outerRadius = graphRadius(graph);

  // Nodes arrive pre-sorted hubs-first from the backend.
  for (const n of nodes) {
    const attrs = {
      size: sizeForNode(n.degree, n.volume_sol),
      color: colorForComponent(n.component),
      label: nodeLabel(n.id),
    };
    if (graph.hasNode(n.id)) {
      graph.mergeNodeAttributes(n.id, attrs);
    } else {
      const seed = seedForNewNode(graph, n.id, neighbors, outerRadius);
      graph.addNode(n.id, { x: seed.x, y: seed.y, ...attrs });
    }
  }

  for (const e of edges) {
    if (!graph.hasNode(e.from) || !graph.hasNode(e.to)) continue;
    const existing =
      graph.hasEdge(e.from, e.to) ? graph.edge(e.from, e.to)
      : graph.hasEdge(e.to, e.from) ? graph.edge(e.to, e.from)
      : null;
    const attrs = {
      size: widthFromVolume(e.volume_sol),
      color: edgeColor(e.volume_sol, maxVol),
      weight: edgeWeight(degrees.get(e.from) ?? 0, degrees.get(e.to) ?? 0),
    };
    if (existing) {
      graph.mergeEdgeAttributes(existing, attrs);
    } else {
      graph.addEdge(e.from, e.to, attrs);
    }
  }
}

function GraphLoader({ nodes, edges }: GraphCanvasProps) {
  const sigma = useSigma();
  const loadGraph = useLoadGraph();
  const loaded = useRef(false);
  const { start, stop } = useWorkerLayoutForceAtlas2({
    settings: {
      // Light gravity, non-strong: just enough pull to resist drift, not
      // enough to flatten local hub-spoke clusters into the center.
      // StrongGravityMode scales force with distance which smooshes the
      // whole graph toward center — off.
      gravity: 0.3,
      strongGravityMode: false,
      // Higher repulsion keeps spokes from crowding their hub.
      scalingRatio: 60,
      slowDown: 4,
      barnesHutOptimize: true,
      linLogMode: true,
      // Honor per-edge weight (see edgeWeight()): edges touching
      // high-degree hubs pull less, giving spokes breathing room.
      edgeWeightInfluence: 1,
    },
  });

  useEffect(() => {
    if (!loaded.current) {
      loadGraph(initialGraph(nodes, edges));
      loaded.current = true;
      start();
      const firstRun = setTimeout(stop, 4000);
      return () => clearTimeout(firstRun);
    }

    applyDiff(sigma.getGraph(), nodes, edges);
    start();
    const relax = setTimeout(stop, 1200);
    return () => {
      clearTimeout(relax);
      stop();
    };
  }, [nodes, edges, sigma, loadGraph, start, stop]);

  return null;
}

export function GraphCanvas({ nodes, edges }: GraphCanvasProps) {
  const settings = useMemo(
    () => ({
      allowInvalidContainer: true,
      defaultEdgeColor: `rgba(200, 210, 235, ${EDGE_ALPHA_MAX})`,
      labelColor: { color: LABEL_COLOR },
      labelSize: 11,
      labelWeight: "500",
      labelDensity: 0.6,
      labelGridCellSize: 140,
      labelRenderedSizeThreshold: 6,
      renderEdgeLabels: false,
      defaultNodeColor: "#888",
      zIndex: true,
    }),
    [],
  );

  return (
    <SigmaContainer
      style={{ width: "100%", height: "100%", background: BG }}
      settings={settings}
    >
      <GraphLoader nodes={nodes} edges={edges} />
    </SigmaContainer>
  );
}
