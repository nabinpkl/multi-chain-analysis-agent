function LinkedInIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden
    >
      <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
    </svg>
  );
}

export function LinkedInBlock() {
  return (
    <section className="py-16 px-6 border-t border-mca-border">
      <div className="max-w-[480px] mx-auto text-center">
        <p className="font-serif text-[1.15rem] text-mca-text mb-2">
          Built by Nabin Pokhrel
        </p>
        <p className="text-[0.85rem] text-mca-dim leading-[1.6] mb-5">
          Portfolio project exploring cross-chain transaction analysis.
        </p>
        <a
          href="https://linkedin.com/in/nabin-pokhrel"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-sm border border-mca-border bg-mca-surface/50 text-mca-dim text-[0.82rem] font-medium transition-colors hover:text-mca-accent hover:border-mca-accent/30"
        >
          <LinkedInIcon className="w-4 h-4" />
          Connect on LinkedIn
        </a>
      </div>
    </section>
  );
}
