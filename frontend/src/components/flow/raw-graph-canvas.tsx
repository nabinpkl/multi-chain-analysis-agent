"use client";

import "@react-sigma/core/lib/style.css";

import {
  SigmaContainer,
  useRegisterEvents,
  useSigma,
} from "@react-sigma/core";
import type Graph from "graphology";
import { useEffect, useMemo } from "react";

import { useGraphFocus } from "@/stores/use-graph-focus";

interface RawGraphCanvasProps {
  graph: Graph;
}

const BG = "#0c0d12";
const LABEL_COLOR = "#e9ebf1";

/**
 * Wires Sigma node-level events into the focus store so the agent
 * panel can read what the user clicked. Per D-6: structured frontend
 * context is the strongest disambiguation signal. Click sets focus
 * (sticky); hover updates the transient hover field. Clicking the
 * stage (off any node) clears focus.
 */
function FocusEventBridge() {
  const registerEvents = useRegisterEvents();
  const setFocus = useGraphFocus((s) => s.setFocus);
  const setHover = useGraphFocus((s) => s.setHover);

  useEffect(() => {
    registerEvents({
      clickNode: (e) => setFocus(e.node),
      enterNode: (e) => setHover(e.node),
      leaveNode: () => setHover(null),
      clickStage: () => setFocus(null),
    });
  }, [registerEvents, setFocus, setHover]);

  return null;
}

/**
 * Refits the camera to the graph as it grows. Sigma only auto-fits on
 * construction, and our graph starts empty  without this, new nodes
 * land off-camera and the canvas looks empty even though the graph
 * has hundreds of them.
 *
 * We snapshot the node count each animation frame and reset the camera
 * when it's grown meaningfully. After ~15s we stop auto-fitting so the
 * user's pan/zoom isn't constantly overridden.
 */
function CameraAutoFit({ graph }: { graph: Graph }) {
  const sigma = useSigma();
  useEffect(() => {
    const start = performance.now();
    let lastOrder = 0;
    let rafId: number | null = null;
    let userTookOver = false;
    // Any manual wheel/drag on the container means the user is driving
    // the camera now  stop auto-fitting or we fight them on every
    // tick.
    const container = sigma.getContainer();
    const onUserInteract = () => {
      userTookOver = true;
    };
    container.addEventListener("wheel", onUserInteract, { passive: true });
    container.addEventListener("mousedown", onUserInteract);
    container.addEventListener("touchstart", onUserInteract, { passive: true });
    const tick = () => {
      rafId = requestAnimationFrame(tick);
      const elapsed = performance.now() - start;
      if (elapsed > 45_000 || userTookOver) {
        if (rafId !== null) cancelAnimationFrame(rafId);
        return;
      }
      // Refit whenever the graph has grown by at least 20% or at
      // least 10 nodes since the last fit. Avoids thrashing when
      // edges stream in one at a time.
      const grew = graph.order - lastOrder;
      if (grew >= 10 || (lastOrder > 0 && grew / lastOrder > 0.2)) {
        lastOrder = graph.order;
        sigma.getCamera().animatedReset({ duration: 300 });
      }
    };
    rafId = requestAnimationFrame(tick);
    return () => {
      container.removeEventListener("wheel", onUserInteract);
      container.removeEventListener("mousedown", onUserInteract);
      container.removeEventListener("touchstart", onUserInteract);
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, [graph, sigma]);
  return null;
}

export function RawGraphCanvas({ graph }: RawGraphCanvasProps) {
  const settings = useMemo(
    () => ({
      allowInvalidContainer: true,
      defaultEdgeColor: "rgba(200, 210, 235, 0.35)",
      labelColor: { color: LABEL_COLOR },
      labelSize: 11,
      labelWeight: "500",
      labelDensity: 0.4,
      labelGridCellSize: 160,
      labelRenderedSizeThreshold: 8,
      renderEdgeLabels: false,
      defaultNodeColor: "#d9c8a9",
      zIndex: true,
      hideEdgesOnMove: true,
    }),
    [],
  );

  return (
    <SigmaContainer
      graph={graph}
      style={{ width: "100%", height: "100%", background: BG }}
      settings={settings}
    >
      <CameraAutoFit graph={graph} />
      <FocusEventBridge />
    </SigmaContainer>
  );
}
