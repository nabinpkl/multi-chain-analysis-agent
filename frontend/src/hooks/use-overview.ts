"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchOverview, type OverviewResponse, type OverviewWindow } from "@/lib/api";

const POLL_INTERVAL_MS = 10_000;

export function useOverview(window: OverviewWindow) {
  return useQuery<OverviewResponse>({
    queryKey: ["overview", window],
    queryFn: ({ signal }) => fetchOverview(window, signal),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
    placeholderData: (prev) => prev,
  });
}
