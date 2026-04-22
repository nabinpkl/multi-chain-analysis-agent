"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import {
  fetchOverview,
  subscribeOverviewStream,
  type OverviewResponse,
  type OverviewWindow,
} from "@/lib/api";

const POLL_FALLBACK_MS = 10_000;
const MAX_SSE_ERRORS_BEFORE_FALLBACK = 2;

/**
 * Live overview hook. Opens an SSE connection for the current `window`;
 * the server projects per-window snapshots and pushes a fresh one on each
 * state-machine tick. Falls back to polling if SSE errors persist so the
 * UI never goes dark. Changing `window` re-opens the stream.
 */
export function useOverview(window: OverviewWindow) {
  const queryClient = useQueryClient();
  const sseErrorCountRef = useRef(0);
  const sseFailedRef = useRef(false);

  const query = useQuery<OverviewResponse>({
    queryKey: ["overview", window],
    queryFn: ({ signal }) => fetchOverview(window, signal),
    refetchInterval: () => (sseFailedRef.current ? POLL_FALLBACK_MS : false),
    staleTime: Infinity,
    placeholderData: (prev) => prev,
  });

  useEffect(() => {
    // Reset the SSE error gate when the window changes so a fresh stream
    // doesn't inherit a prior window's failure state.
    sseErrorCountRef.current = 0;
    sseFailedRef.current = false;

    const unsubscribe = subscribeOverviewStream(
      window,
      (snap) => {
        sseErrorCountRef.current = 0;
        sseFailedRef.current = false;
        queryClient.setQueryData<OverviewResponse>(["overview", window], snap);
      },
      () => {
        sseErrorCountRef.current += 1;
        if (sseErrorCountRef.current >= MAX_SSE_ERRORS_BEFORE_FALLBACK) {
          sseFailedRef.current = true;
          queryClient.invalidateQueries({ queryKey: ["overview", window] });
        }
      },
    );

    return () => {
      unsubscribe();
    };
  }, [queryClient, window]);

  return query;
}
