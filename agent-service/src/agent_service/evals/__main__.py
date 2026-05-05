"""Entry point shim so `python -m agent_service.evals <suite>` works."""

from agent_service.evals.cli import main

raise SystemExit(main())
