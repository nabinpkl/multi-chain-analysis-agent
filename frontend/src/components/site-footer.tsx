import { ExternalLink } from "lucide-react";

const REPO_URL = "https://github.com/nabinpkl/multi-chain-analysis-agent";
const ISSUES_URL = `${REPO_URL}/issues`;

// Author attribution. Both unset by default so a fork does not ship
// the upstream author's name. Set both to render a "Built by <name>"
// link in the footer. URL detection: linkedin.com gets the LinkedIn
// icon, anything else gets the generic external-link icon.
const BUILT_BY_NAME = process.env.NEXT_PUBLIC_BUILT_BY_NAME;
const BUILT_BY_URL = process.env.NEXT_PUBLIC_BUILT_BY_URL;
const builtByIsLinkedIn = BUILT_BY_URL?.includes("linkedin.com") ?? false;

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

export function SiteFooter() {
  return (
    <footer className="border-t border-mca-border bg-mca-bg text-mca-dim text-[0.85rem] leading-[1.7]">
      <div className="max-w-[1200px] mx-auto px-6 py-10 grid gap-8 sm:grid-cols-[2fr_1fr_1fr]">
        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-mca-text">
            About this site
          </p>
          <p>
            MultiChain Analysis Agent is an{" "}
            <span className="text-mca-text">open-source agent-design exercise</span>.{" "}
            An LLM analyst answers questions about live wallet behavior on Solana
            mainnet, grounded in a real-time on-chain graph. The chain is the
            pressure environment: real public high-volume data forces clean
            ingest, idempotent writes, and grounded narrative.
          </p>
          <p>
            Provided <span className="text-mca-text">as-is</span>, with no
            warranty of any kind. On-chain data is indexed continuously but may
            lag finality, reorg, or miss events during backfill windows.
          </p>
        </div>

        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-mca-text">
            How the pipeline works
          </p>
          <p>
            A single Rust service subscribes to chain RPCs, decodes logs and
            calldata, resolves addresses to known labels, and writes normalized
            edges into the graph store.
          </p>
          <p>
            The full stack runs end-to-end via{" "}
            <code className="text-mca-text">docker compose up</code>.
            <em className="not-italic text-mca-text"> Best effort, not a block explorer.</em>
          </p>
        </div>

        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-mca-text">
            Your responsibility
          </p>
          <p>
            Graph data is for analysis and research. Do not treat it as legal,
            financial, or compliance evidence without verifying on-chain.
          </p>
          <ul className="space-y-1.5">
            <li>
              <a
                href="https://etherscan.io/"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-mca-dim/40 hover:text-mca-accent hover:decoration-mca-accent transition-colors"
              >
                Verify on Etherscan →
              </a>
            </li>
            <li>
              <a
                href="https://solscan.io/"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-mca-dim/40 hover:text-mca-accent hover:decoration-mca-accent transition-colors"
              >
                Verify on Solscan →
              </a>
            </li>
            <li>
              <a
                href={ISSUES_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-mca-dim/40 hover:text-mca-accent hover:decoration-mca-accent transition-colors"
              >
                Report bad data / label errors →
              </a>
            </li>
          </ul>
        </div>
      </div>

      <div className="border-t border-mca-border">
        <div className="max-w-[1200px] mx-auto px-6 py-4 flex flex-wrap items-center justify-between gap-3 text-[0.78rem] uppercase tracking-[2px]">
          <span>© MultiChain Analysis Agent. Open source. Not financial advice.</span>
          <div className="flex items-center gap-5">
            {BUILT_BY_NAME && BUILT_BY_URL && (
              <a
                href={BUILT_BY_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1.5 hover:text-mca-accent transition-colors"
              >
                {builtByIsLinkedIn ? (
                  <LinkedInIcon className="w-3 h-3" />
                ) : (
                  <ExternalLink size={11} />
                )}
                Built by {BUILT_BY_NAME}
              </a>
            )}
            <a
              href={REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1.5 hover:text-mca-accent transition-colors"
            >
              Source on GitHub
              <ExternalLink size={11} />
            </a>
          </div>
        </div>
      </div>
    </footer>
  );
}
