"use client";

import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { Moon, Sun } from "lucide-react";

export function ThemeToggle() {
  const [mounted, setMounted] = useState(false);
  const { resolvedTheme, setTheme } = useTheme();

  useEffect(() => setMounted(true), []);

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
      className="relative size-7 rounded-sm flex items-center justify-center text-jgd-dim border border-jgd-border bg-jgd-surface/60 transition-colors cursor-pointer hover:text-jgd-accent hover:border-jgd-accent-mid"
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
