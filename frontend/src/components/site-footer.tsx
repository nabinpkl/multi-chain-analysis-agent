import { ExternalLink } from "lucide-react";

const REPO_URL = "https://github.com/nabinpkl/multi-chain-analysis-engine";
const ISSUES_URL = `${REPO_URL}/issues`;

export function SiteFooter() {
  return (
    <footer className="border-t border-mca-border bg-mca-bg text-mca-dim text-[0.85rem] leading-[1.7]">
      <div className="max-w-[1200px] mx-auto px-6 py-10 grid gap-8 sm:grid-cols-[2fr_1fr_1fr]">
        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-mca-text">
            About this site
          </p>
          <p>
            MultiChain Analysis Engine is a{" "}
            <span className="text-mca-text">portfolio / proof-of-concept</span>{" "}
            project. It ingests transactions from multiple public blockchains,
            normalizes them into a common shape, links each transaction to the
            entities and contracts it touches, and serves the result as an
            explorable graph.
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
            Runs on one Oracle free-tier VM behind Cloudflare.
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
          <span>© MultiChain Analysis Engine, portfolio piece, not financial advice.</span>
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
    </footer>
  );
}
