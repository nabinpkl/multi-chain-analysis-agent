"use client";

import { useSyncExternalStore } from "react";
import { useTheme } from "next-themes";
import { Moon, Sun } from "lucide-react";

// Mount detection for SSR-hydration guard. `useSyncExternalStore` returns
// the server snapshot (`false`) during SSR and the client snapshot
// (`true`) after hydration. This is the React-recommended replacement
// for the classic `useState(false)` + `useEffect(() => setState(true))`
// pattern, which trips `react-hooks/set-state-in-effect` because the
// effect's only job is to schedule a cascading render.
function useMounted(): boolean {
  return useSyncExternalStore(
    () => () => {},
    () => true,
    () => false,
  );
}

export function ThemeToggle() {
  const mounted = useMounted();
  const { resolvedTheme, setTheme } = useTheme();

  const isDark = resolvedTheme === "dark";
  const label = !mounted
    ? "Toggle theme"
    : isDark
      ? "Switch to light mode"
      : "Switch to dark mode";

  return (
    <button
      type="button"
      onClick={() => mounted && setTheme(isDark ? "light" : "dark")}
      aria-label={label}
      title={label}
      className="relative size-7 rounded-sm flex items-center justify-center text-mca-dim border border-mca-border bg-mca-surface/60 transition-colors cursor-pointer hover:text-mca-accent hover:border-mca-accent-mid"
      suppressHydrationWarning
    >
      {mounted ? (
        isDark ? (
          <Moon size={14} strokeWidth={2} aria-hidden />
        ) : (
          <Sun size={14} strokeWidth={2} aria-hidden />
        )
      ) : null}
    </button>
  );
}
