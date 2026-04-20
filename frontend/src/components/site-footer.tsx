import { ExternalLink } from "lucide-react";

const REPO_URL = "https://github.com/nabinpkl/justgetdomain.com";
const ISSUES_URL = `${REPO_URL}/issues`;

export function SiteFooter() {
  return (
    <footer className="border-t border-jgd-border bg-jgd-bg text-jgd-dim text-[0.85rem] leading-[1.7]">
      <div className="max-w-[1200px] mx-auto px-6 py-10 grid gap-8 sm:grid-cols-[2fr_1fr_1fr]">
        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-jgd-text">
            About this site
          </p>
          <p>
            JustGetDomain is a{" "}
            <span className="text-jgd-text">portfolio / proof-of-concept</span>{" "}
            project. We are not a registrar, not affiliated with ICANN, and run
            no transactions. The site exists to solve one specific frustration:
            you cannot browse what short domains are still available without a
            registrar lying to you.
          </p>
          <p>
            Provided <span className="text-jgd-text">as-is</span>, with no
            warranty of any kind. Snapshot data is updated periodically and
            registrations can change at any time.
          </p>
        </div>

        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-jgd-text">
            How candidates are chosen
          </p>
          <p>
            A pre-scanned subset of the English dictionary, hand-curated to
            exclude brand collisions, auth/finance/security terms, regulated
            (gov / health / legal) namespaces, and adult or illegal content.
          </p>
          <p>
            No compound generation, no AI-suggested names, no purchase flow.
            <em className="not-italic text-jgd-text"> Best effort, not exhaustive.</em>
          </p>
        </div>

        <div className="space-y-3">
          <p className="text-[0.78rem] uppercase tracking-[2.5px] text-jgd-text">
            Your responsibility
          </p>
          <p>
            Availability does not grant the right to use a name. You are
            solely responsible for trademark clearance.
          </p>
          <ul className="space-y-1.5">
            <li>
              <a
                href="https://tmsearch.uspto.gov/"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-jgd-dim/40 hover:text-jgd-accent hover:decoration-jgd-accent transition-colors"
              >
                USPTO trademark search →
              </a>
            </li>
            <li>
              <a
                href="https://branddb.wipo.int/"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-jgd-dim/40 hover:text-jgd-accent hover:decoration-jgd-accent transition-colors"
              >
                WIPO Global Brand Database →
              </a>
            </li>
            <li>
              <a
                href={ISSUES_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-jgd-dim/40 hover:text-jgd-accent hover:decoration-jgd-accent transition-colors"
              >
                Takedown / curation requests →
              </a>
            </li>
            <li>
              <a
                href="https://docs.google.com/forms/d/e/1FAIpQLScVOEVfQqP1EOf2cES6-LjWBXxc30bBahL5xc85uAUHpgS7Jw/viewform?usp=dialog"
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-[3px] decoration-jgd-dim/40 hover:text-jgd-accent hover:decoration-jgd-accent transition-colors"
              >
                Request removal →
              </a>
            </li>
          </ul>
        </div>
      </div>

      <div className="border-t border-jgd-border">
        <div className="max-w-[1200px] mx-auto px-6 py-4 flex flex-wrap items-center justify-between gap-3 text-[0.78rem] uppercase tracking-[2px]">
          <span>© JustGetDomain, portfolio piece, not a registrar.</span>
          <a
            href={REPO_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 hover:text-jgd-accent transition-colors"
          >
            Source on GitHub
            <ExternalLink size={11} />
          </a>
        </div>
      </div>
    </footer>
  );
}
