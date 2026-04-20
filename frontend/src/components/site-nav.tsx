"use client";

import Link from "next/link";
import { ThemeToggle } from "@/components/theme-toggle";
import { usePathname } from "next/navigation";

export function SiteNav() {
  const pathname = usePathname();
  return (
    <nav
      role="navigation"
      aria-label="Main"
      className="sticky top-0 z-50 h-14 flex justify-between items-center px-6 sm:px-8 text-[0.75rem] tracking-[1.5px] uppercase backdrop-blur-[16px] bg-jgd-nav border-b border-jgd-border relative"
    >
      <Link
        href="/"
        className="flex items-center gap-1.5 text-jgd-accent font-bold transition-opacity hover:opacity-80"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 512 512"
          className="w-8 h-8 shrink-0"
          aria-hidden
        >
          <text
            x="105"
            y="340"
            fontFamily="sans-serif"
            fontWeight="700"
            fontSize="280"
            fill="currentColor"
            letterSpacing="-15"
          >
            &gt;_
          </text>
          <circle cx="385" cy="375" r="20" fill="currentColor" />
        </svg>
        <span>
          JustGetDomain<span className="text-jgd-accent">.</span>
        </span>
      </Link>

      <p
        className="hidden md:block absolute left-1/2 -translate-x-1/2 text-[0.68rem] normal-case tracking-[0.5px] text-jgd-muted max-w-[440px] text-center leading-tight"
        role="note"
      >
        Proof of concept - real data, but{" "}
        <span className="text-jgd-dim">availability is not guaranteed</span>.
      </p>

      <div className="flex items-center gap-4">
        <Link
          href="/explore"
          className={`text-[0.75rem] tracking-[1.5px] uppercase transition-opacity hover:opacity-100 ${pathname === "/explore" ? "text-jgd-accent" : "text-jgd-dim opacity-80"}`}
        >
          Explore Domains
        </Link>
        <ThemeToggle />
      </div>
    </nav>
  );
}
