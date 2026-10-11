"""CREATIVE HQ command line.

    python -m creative_hq check                         # verify environment and configuration
    python -m creative_hq run "Create two Instagram carousels for Kevin"
    python -m creative_hq status [RUN_ID]               # list runs, or show one run's tasks
    python -m creative_hq review RUN_ID                 # re-render + new Supervisor review
    python -m creative_hq approve RUN_ID [--note ...]   # record your approval (does NOT publish)
    python -m creative_hq reject RUN_ID --note ...
    python -m creative_hq history [CLIENT_ID]           # content history used to avoid repetition
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys

from .config import ROOT, list_clients, load_agents, load_client, load_settings
from .storage import Store

# Words in a request that select a client. Add aliases here when you onboard new clients.
CLIENT_ALIASES = {"kevin": "livetrust-financial", "livetrust": "livetrust-financial"}


def setup_logging(settings) -> None:
    logs = settings.path("logs")
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger("creative_hq")
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    file = logging.FileHandler(logs / "creative_hq.log")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root.addHandler(console)
    root.addHandler(file)


def resolve_client(settings, request: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    text = request.lower()
    for word, cid in CLIENT_ALIASES.items():
        if word in text:
            return cid
    clients = list_clients(settings)
    if len(clients) == 1:
        return clients[0]
    raise SystemExit(f"Could not tell which client this is for. Use --client one of {clients}")


def cmd_check(settings) -> int:
    ok = True

    def line(passed: bool, label: str, detail: str = ""):
        nonlocal ok
        ok &= passed
        print(f"  [{'OK' if passed else '!!'}] {label}{(' — ' + detail) if detail else ''}")

    print("CREATIVE HQ environment check")
    line(sys.version_info >= (3, 10), "Python >= 3.10", sys.version.split()[0])
    try:
        import claude_agent_sdk
        line(True, "claude-agent-sdk installed", getattr(claude_agent_sdk, "__version__", ""))
    except ImportError:
        line(False, "claude-agent-sdk installed", "run: pip install -r requirements.txt")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        line(True, "Playwright + Chromium (slide rendering)")
    except Exception as e:
        line(False, "Playwright + Chromium", f"run: python -m playwright install chromium ({e.__class__.__name__})")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    host_auth = bool(os.environ.get("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"))
    line(has_key or host_auth or bool(shutil.which("claude")), "Claude credentials",
         "ANTHROPIC_API_KEY set" if has_key else ("provided by Claude Code cloud session" if host_auth
                                                  else "set ANTHROPIC_API_KEY (see README)"))
    try:
        agents = load_agents(settings)
        line(True, "6 agent definitions", ", ".join(f"{a.id}({a.model})" for a in agents.values()))
    except Exception as e:
        line(False, "agent definitions", str(e))
    for cid in list_clients(settings):
        c = load_client(settings, cid)
        line(True, f"client '{cid}'", f"{len(c.knowledge)} knowledge file(s)")
    print(f"  Budgets: ${settings.per_run_usd:.2f}/run, ${settings.per_day_usd:.2f}/day")
    print("All checks passed." if ok else "Some checks failed — see above.")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="creative_hq", description="CREATIVE HQ multi-agent creative agency")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("request")
    r.add_argument("--client")
    s = sub.add_parser("status")
    s.add_argument("run_id", nargs="?")
    rv = sub.add_parser("review")
    rv.add_argument("run_id")
    for name in ("approve", "reject"):
        a = sub.add_parser(name)
        a.add_argument("run_id")
        a.add_argument("--note", default="", required=(name == "reject"))
    h = sub.add_parser("history")
    h.add_argument("client_id", nargs="?", default="livetrust-financial")
    args = p.parse_args()

    os.chdir(ROOT)
    settings = load_settings()
    if args.cmd == "check":
        return cmd_check(settings)

    store = Store(settings.path("database"))
    if args.cmd == "run":
        setup_logging(settings)
        from .workflow import Workflow
        client = load_client(settings, resolve_client(settings, args.request, args.client))
        run_id = asyncio.run(Workflow(settings, load_agents(settings), store).run(client, args.request))
        run = store.get_run(run_id)
        print(f"\nRun {run_id}: {run['status']}")
        if run["status"] == "pending_approval":
            print(f"Deliverables waiting for your approval in: {run['deliverable_dir']}")
        else:
            print(f"Error: {run['error']}")
        return 0 if run["status"] == "pending_approval" else 1

    if args.cmd == "review":
        setup_logging(settings)
        from .workflow import Workflow
        run = store.get_run(args.run_id)
        if not run:
            raise SystemExit("unknown run id")
        asyncio.run(Workflow(settings, load_agents(settings), store).rereview(load_client(settings, run["client_id"]), args.run_id))
        run = store.get_run(args.run_id)
        print(f"\nRun {args.run_id}: {run['status']}" + (f" ({run['error']})" if run["error"] else ""))
        return 0

    if args.cmd == "status":
        if not args.run_id:
            for run in store.list_runs():
                print(f"{run['id']}  {run['status']:<17} ${store.spent_run(run['id']):.3f}  {run['client_id']}  {run['request'][:60]}")
            return 0
        run = store.get_run(args.run_id)
        if not run:
            raise SystemExit("unknown run id")
        print(f"Run {run['id']} — {run['status']} — {run['request']}")
        for t in store.tasks_for_run(run["id"]):
            print(f"  {t['id'].split('-')[-1]:<6} {t['agent_id']}  {t['status']:<10} ${t['cost_usd'] or 0:.3f}  {t['title']}"
                  + (f"  [{t['error']}]" if t["error"] else ""))
        if run["error"]:
            print(f"Error: {run['error']}")
        return 0

    if args.cmd in ("approve", "reject"):
        run = store.get_run(args.run_id)
        if not run:
            raise SystemExit("unknown run id")
        if run["status"] not in ("pending_approval", "approved", "rejected"):
            raise SystemExit(f"run is '{run['status']}', nothing to approve")
        status = "approved" if args.cmd == "approve" else "rejected"
        store.update_run(args.run_id, status=status, approval_note=args.note)
        store.set_history_status(args.run_id, status)
        print(f"Run {args.run_id} marked {status}. Nothing was published or sent — "
              "publishing is a separate manual step that CREATIVE HQ does not perform.")
        return 0

    if args.cmd == "history":
        for h in store.history(args.client_id):
            print(f"{h['created_at'][:10]}  {h['status']:<17} {h['format']}  {h['topic']}  ({h['angle']})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
