"use client";

import "@react-sigma/core/lib/style.css";

import { SigmaContainer, useLoadGraph, useSigma } from "@react-sigma/core";
import Graph from "graphology";
import { useEffect, useMemo, useRef } from "react";
import type { EdgeView, NodeView } from "@/lib/api";
import { colorForComponent } from "@/lib/cluster-colors";

interface GraphCanvasProps {
  nodes: NodeView[];
  edges: EdgeView[];
}

// Canvas colors are hex/rgba  Sigma's WebGL parser doesn't accept oklch().
const BG = "#0c0d12";
const LABEL_COLOR = "#e9ebf1";
const EDGE_ALPHA_MIN = 0.08;
const EDGE_ALPHA_MAX = 0.85;
const POSITION_TWEEN_MS = 500;

function sizeForNode(degree: number, volumeSol: number): number {
  if (degree <= 1) return 0.6;
  return 1.5 + Math.pow(degree, 0.6) * 1.6 + Math.log1p(volumeSol) * 0.1;
}

function widthFromVolume(volumeSol: number): number {
  return 0.8 + Math.log1p(volumeSol) * 0.7;
}

function maxEdgeVolume(edges: EdgeView[]): number {
  let max = 0;
  for (const e of edges) if (e.volume_sol > max) max = e.volume_sol;
  return Math.max(max, 1);
}

function edgeColor(volumeSol: number, maxVol: number): string {
  const t = Math.log1p(volumeSol) / Math.log1p(maxVol);
  const alpha = EDGE_ALPHA_MIN + t * (EDGE_ALPHA_MAX - EDGE_ALPHA_MIN);
  return `rgba(200, 210, 235, ${alpha.toFixed(3)})`;
}

function nodeLabel(id: string): string {
  return `${id.slice(0, 4)}…${id.slice(-4)}`;
}

function edgeKey(from: string, to: string): string {
  return from < to ? `${from}|${to}` : `${to}|${from}`;
}

function buildGraph(nodes: NodeView[], edges: EdgeView[]): Graph {
  const graph = new Graph({ multi: false, type: "undirected" });
  const maxVol = maxEdgeVolume(edges);
  for (const n of nodes) {
    graph.addNode(n.id, {
      x: n.x,
      y: n.y,
      size: sizeForNode(n.degree, n.volume_sol),
      color: colorForComponent(n.component),
      label: nodeLabel(n.id),
    });
  }
  for (const e of edges) {
    if (!graph.hasNode(e.from) || !graph.hasNode(e.to)) continue;
    if (graph.hasEdge(e.from, e.to) || graph.hasEdge(e.to, e.from)) continue;
    graph.addEdge(e.from, e.to, {
      size: widthFromVolume(e.volume_sol),
      color: edgeColor(e.volume_sol, maxVol),
    });
  }
  return graph;
}

interface PositionTween {
  nodeId: string;
  fromX: number;
  fromY: number;
  toX: number;
  toY: number;
}

/**
 * Reconcile the live sigma graph against a fresh backend snapshot.
 * Drops nodes/edges that left, adds ones that arrived, updates visual
 * attributes in place, and collects tween targets for any node whose
 * backend position moved.
 */
function applyDiff(
  graph: Graph,
  nodes: NodeView[],
  edges: EdgeView[],
): PositionTween[] {
  const nextNodeIds = new Set(nodes.map((n) => n.id));
  const nextEdgeKeys = new Set(edges.map((e) => edgeKey(e.from, e.to)));

  graph.forEachNode((id) => {
    if (!nextNodeIds.has(id)) graph.dropNode(id);
  });
  graph.forEachEdge((eid, _attrs, source, target) => {
    if (!nextEdgeKeys.has(edgeKey(source, target))) graph.dropEdge(eid);
  });

  const maxVol = maxEdgeVolume(edges);
  const tweens: PositionTween[] = [];

  for (const n of nodes) {
    const attrs = {
      size: sizeForNode(n.degree, n.volume_sol),
      color: colorForComponent(n.component),
      label: nodeLabel(n.id),
    };
    if (graph.hasNode(n.id)) {
      const cur = graph.getNodeAttributes(n.id) as { x: number; y: number };
      graph.mergeNodeAttributes(n.id, attrs);
      if (cur.x !== n.x || cur.y !== n.y) {
        tweens.push({
          nodeId: n.id,
          fromX: cur.x,
          fromY: cur.y,
          toX: n.x,
          toY: n.y,
        });
      }
    } else {
      // New node  paint at destination immediately so it doesn't pop
      // in from the origin.
      graph.addNode(n.id, { x: n.x, y: n.y, ...attrs });
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
    };
    if (existing) {
      graph.mergeEdgeAttributes(existing, attrs);
    } else {
      graph.addEdge(e.from, e.to, attrs);
    }
  }
  return tweens;
}

function animateTweens(
  graph: Graph,
  tweens: PositionTween[],
  durationMs: number,
): () => void {
  if (tweens.length === 0) return () => {};
  let cancelled = false;
  const start = performance.now();
  const tick = () => {
    if (cancelled) return;
    const t = Math.min(1, (performance.now() - start) / durationMs);
    const ease = 1 - Math.pow(1 - t, 3);
    for (const p of tweens) {
      if (!graph.hasNode(p.nodeId)) continue;
      graph.setNodeAttribute(
        p.nodeId,
        "x",
        p.fromX + (p.toX - p.fromX) * ease,
      );
      graph.setNodeAttribute(
        p.nodeId,
        "y",
        p.fromY + (p.toY - p.fromY) * ease,
      );
    }
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return () => {
    cancelled = true;
  };
}

function GraphLoader({ nodes, edges }: GraphCanvasProps) {
  const sigma = useSigma();
  const loadGraph = useLoadGraph();
  const loaded = useRef(false);

  useEffect(() => {
    if (!loaded.current) {
      loadGraph(buildGraph(nodes, edges));
      loaded.current = true;
      return;
    }
    const tweens = applyDiff(sigma.getGraph(), nodes, edges);
    const cancel = animateTweens(sigma.getGraph(), tweens, POSITION_TWEEN_MS);
    return () => cancel();
  }, [nodes, edges, sigma, loadGraph]);

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
