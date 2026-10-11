"""The shared task workflow: Director -> Strategist -> Researcher -> Copywriter -> Designer -> Supervisor.

Every step is a persisted task with explicit dependencies. A task only starts when all
of its dependencies are `completed`; its input is built from their stored outputs.
If a task fails, every task that depends on it is marked `skipped` and the run is `failed`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import schemas
from .config import AgentSpec, Client, Settings
from .render import check_overflow, render_carousel
from .runner import AgentRunner, TaskError, TaskResult, normalize_url, run_with_retries
from .storage import Store, now

log = logging.getLogger("creative_hq.workflow")


class BudgetExceeded(Exception):
    pass


def _j(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


class Workflow:
    def __init__(self, settings: Settings, agents: dict[str, AgentSpec], store: Store):
        self.settings = settings
        self.agents = agents
        self.store = store

    # ------------------------------------------------------------------ budget
    def _budget_for(self, run_id: str, spec: AgentSpec) -> float:
        run_left = self.settings.per_run_usd - self.store.spent_run(run_id)
        day_left = self.settings.per_day_usd - self.store.spent_today()
        allowed = min(spec.max_budget_usd, run_left, day_left)
        if allowed < 0.05:
            raise BudgetExceeded(
                f"spending limit reached (run left ${run_left:.2f}, day left ${day_left:.2f}); "
                f"{spec.id} was not started"
            )
        return allowed

    # ------------------------------------------------------------------ tasks
    async def _task(self, run_id: str, task_id: str, role: str, prompt: str, workdir: Path,
                    readable_dirs: list[Path] | None = None) -> TaskResult:
        spec = self.agents[role]
        row = self.store.get_task(task_id)
        for dep in json.loads(row["depends_on"]):
            if self.store.get_task(dep)["status"] != "completed":
                raise TaskError(f"dependency {dep} is not completed")
        self.store.update_task(task_id, status="running", started_at=now(), input_json={"prompt": prompt})
        log.info("[%s] %s %s — %s", task_id.split("-")[-1], spec.id, spec.name, row["title"])
        attempts = 0

        def failed_attempt(cost: float):
            nonlocal attempts
            attempts += 1
            if cost:
                self.store.record_spend(run_id, task_id, spec.id, cost)

        try:
            res = await run_with_retries(
                self.runner, self.settings.task_retries,
                budget_fn=lambda: self._budget_for(run_id, spec),
                on_failed_attempt=failed_attempt,
                spec=spec, task_id=task_id, prompt=prompt, schema=schemas.BY_ROLE[role],
                workdir=workdir, readable_dirs=readable_dirs,
            )
        except (TaskError, BudgetExceeded) as e:
            self.store.update_task(task_id, status="failed", error=str(e), attempts=attempts + 1, finished_at=now())
            raise
        self.store.record_spend(run_id, task_id, spec.id, res.cost_usd)
        self.store.update_task(
            task_id, status="completed", output_json=res.output, cost_usd=res.cost_usd,
            attempts=attempts + 1, session_id=res.session_id, finished_at=now(),
        )
        log.info("     done: $%.3f, %d turns, %d tool calls", res.cost_usd, res.num_turns, len(res.tool_calls))
        return res

    # ------------------------------------------------------------------ main
    async def run(self, client: Client, request: str) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        out = self.settings.path("deliverables") / client.client_id / run_id
        work = out / "_work"
        work.mkdir(parents=True, exist_ok=True)
        self.runner = AgentRunner(self.settings.path("logs") / "runs" / f"{run_id}.jsonl")
        self.store.create_run(run_id, client.client_id, request)
        self.store.update_run(run_id, deliverable_dir=str(out))
        log.info("RUN %s | client=%s | request=%r", run_id, client.client_id, request)

        plan = [  # (task suffix, role, title, depends_on)
            ("T1", "director", "Interpret request, write brief, assign tasks", []),
            ("T2", "strategist", "Select non-repetitive topics", ["T1"]),
            ("T3", "researcher", "Research and fact-check topics with real sources", ["T1", "T2"]),
            ("T4", "copywriter", "Write carousel slides, captions, CTAs", ["T1", "T2", "T3"]),
            ("T5", "designer", "Visual concept, layouts, render slides", ["T1", "T4"]),
            ("T6", "supervisor", "Quality, accuracy and compliance review", ["T1", "T3", "T4", "T5"]),
        ]
        for i, (suffix, role, title, deps) in enumerate(plan, 1):
            self.store.create_task(f"{run_id}-{suffix}", run_id, i, self.agents[role].id, title,
                                   [f"{run_id}-{d}" for d in deps])
        tid = lambda s: f"{run_id}-{s}"  # noqa: E731

        try:
            history = self.store.history(client.client_id)
            ctx = (f"# CLIENT PROFILE\n{client.profile_text()}\n\n# CLIENT KNOWLEDGE BASE\n{client.knowledge_text()}\n\n"
                   f"# TODAY\n{datetime.now(timezone.utc).date().isoformat()}")

            # T1 Director
            d = (await self._task(run_id, tid("T1"), "director",
                 f"# OPERATOR REQUEST\n{request}\n\n{ctx}\n\n# CONTENT HISTORY FOR THIS CLIENT\n{_j(history) if history else 'None yet.'}\n\n"
                 f"Limit: at most {self.settings.max_deliverables} deliverables per request.", work)).output
            if d["deliverable_format"] != "instagram_carousel":
                raise TaskError(f"format '{d['deliverable_format']}' is not implemented yet (only instagram_carousel)")
            count = min(int(d["deliverable_count"]), self.settings.max_deliverables)

            # T2 Strategist
            s = (await self._task(run_id, tid("T2"), "strategist",
                 f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-003']}\n\n"
                 f"Select exactly {count} topics (topic_id T1..T{count}).\n\n{ctx}\n\n"
                 f"# FULL CONTENT HISTORY (avoid repeating)\n{_j(history) if history else 'None yet — this is the first content for this client.'}",
                 work)).output
            topics = s["topics"][:count]
            if len(topics) < count:
                raise TaskError(f"strategist returned {len(topics)} topics, {count} required")
            repeat_warnings = self._repetition_check(topics, history)

            # T3 Researcher
            r_res = await self._task(run_id, tid("T3"), "researcher",
                 f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-002']}\n\n"
                 f"# TOPICS TO RESEARCH\n{_j(topics)}\n\n# CLIENT COMPLIANCE RULES\n{_j(client.profile.get('compliance'))}\n\n"
                 "Use WebSearch (and WebFetch where possible). Number sources S1, S2, ...", work)
            research, source_report = self._verify_sources(r_res)
            for src in research["sources"]:
                self.store.add_source(run_id, client.client_id, src, verified=True)
            for src in source_report["rejected"]:
                self.store.add_source(run_id, client.client_id, src, verified=False)
            (work / "research.json").write_text(_j({"research": research, "source_report": source_report}))
            if not any(t["verified_facts"] for t in research["topics"]):
                raise TaskError("research produced no verified facts; stopping instead of writing unsupported content")

            # T4 Copywriter
            copy_prompt = (
                f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-004']}\n\n"
                f"# TOPICS\n{_j(topics)}\n\n# VERIFIED RESEARCH (the only facts you may use)\n{_j(research)}\n\n"
                f"Write {count} carousels with {d['slides_per_carousel']} slides each (slide 1 = hook, last = CTA).\n\n{ctx}")
            copy = (await self._task(run_id, tid("T4"), "copywriter", copy_prompt, work)).output

            review, design, pngs = None, None, {}
            for round_no in range(self.settings.max_revision_rounds + 1):
                design_tid = tid("T5") if round_no == 0 else tid(f"T5r{round_no}")
                review_tid = tid("T6") if round_no == 0 else tid(f"T6r{round_no}")

                # T5 Designer + renderer
                design = (await self._task(run_id, design_tid, "designer",
                          f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-005']}\n\n"
                          f"# BRAND KIT\n{_j(client.profile['brand'])}\n\n# CAROUSEL COPY\n{_j(copy['carousels'])}", work)).output
                pngs, overflow = await asyncio.to_thread(self._render, client, copy, design, out)

                # T6 Supervisor (looks at the actual PNGs with the Read tool)
                review = await self._supervise(run_id, review_tid, client, d, research, copy, design, pngs, overflow,
                                               repeat_warnings, source_report, out)

                blocking = [i for i in review["issues"] if i["blocking"]]
                if review["verdict"] == "approved" and not blocking:
                    break
                if round_no == self.settings.max_revision_rounds:
                    log.warning("Supervisor still requires revisions after %d round(s); sending to operator with notes", round_no)
                    break
                # Revision: Copywriter fixes, then Designer/Supervisor run again.
                rev_tid = tid(f"T4r{round_no + 1}")
                self.store.create_task(rev_tid, run_id, 10 + round_no * 3, self.agents["copywriter"].id,
                                       f"Revision round {round_no + 1}", [review_tid])
                self.store.create_task(tid(f"T5r{round_no + 1}"), run_id, 11 + round_no * 3, self.agents["designer"].id,
                                       f"Re-design after revision {round_no + 1}", [rev_tid])
                self.store.create_task(tid(f"T6r{round_no + 1}"), run_id, 12 + round_no * 3, self.agents["supervisor"].id,
                                       f"Re-review after revision {round_no + 1}", [rev_tid, tid(f"T5r{round_no + 1}")])
                copy = (await self._task(run_id, rev_tid, "copywriter",
                        copy_prompt + f"\n\n# YOUR PREVIOUS VERSION\n{_j(copy['carousels'])}\n\n"
                        f"# QUALITY SUPERVISOR ISSUES TO FIX\n{_j(review['issues'])}", work)).output

            self._package(client, run_id, request, d, topics, research, source_report, copy, design, review, pngs, out)
            status = "pending_approval"
            self.store.update_run(run_id, status=status)
            for t in topics:
                self.store.add_history(client.client_id, run_id, "instagram_carousel", t["title"], t["angle"], str(out))
            log.info("RUN %s finished: %s | total $%.3f | %s", run_id, status, self.store.spent_run(run_id), out)
        except (TaskError, BudgetExceeded) as e:
            self._fail(run_id, str(e))
        except Exception as e:  # unexpected bug: record it, never report success
            log.exception("unexpected error")
            self._fail(run_id, f"{type(e).__name__}: {e}")
        return run_id

    async def rereview(self, client: Client, run_id: str) -> str:
        """Re-renders a finished run's final copy/design (e.g. after a renderer or brand-kit fix)
        and has the Quality Supervisor review it again as a new, real task."""
        run = self.store.get_run(run_id)
        if not run or run["status"] not in ("pending_approval", "rejected"):
            raise SystemExit(f"run {run_id} is not a finished, unapproved run")
        out = Path(run["deliverable_dir"])
        self.runner = AgentRunner(self.settings.path("logs") / "runs" / f"{run_id}.jsonl")
        done = [t for t in self.store.tasks_for_run(run_id) if t["status"] == "completed"]
        last = lambda agent: json.loads([t for t in done if t["agent_id"] == agent][-1]["output_json"])  # noqa: E731
        d, topics_all = last("AG-001"), last("AG-003")["topics"]
        copy, design = last("AG-004"), last("AG-005")
        topics = [t for t in topics_all if t["topic_id"] in {c["topic_id"] for c in copy["carousels"]}]
        if (out / "_work" / "research.json").exists():
            research, source_report = json.loads((out / "_work" / "research.json").read_text()).values()
        else:  # runs made before research.json existed: rebuild from the stored source verification
            research = last("AG-002")
            rows = self.store.db.execute("SELECT source_ref, verified, url FROM sources WHERE run_id=?", (run_id,)).fetchall()
            ok = {r["source_ref"] for r in rows if r["verified"]}
            research["sources"] = [s for s in research["sources"] if s["id"] in ok]
            source_report = {"rejected": [{"url": r["url"]} for r in rows if not r["verified"]], "dropped_facts": []}

        n = sum(1 for t in self.store.tasks_for_run(run_id) if "-R" in t["id"]) + 1
        review_tid = f"{run_id}-R{n}"
        self.store.create_task(review_tid, run_id, 100 + n, self.agents["supervisor"].id,
                               f"Re-review after re-render #{n}", [])
        self.store.update_run(run_id, status="running")
        log.info("RE-REVIEW %s (#%d)", run_id, n)
        try:
            pngs, overflow = await asyncio.to_thread(self._render, client, copy, design, out)
            review = await self._supervise(run_id, review_tid, client, d, research, copy, design, pngs, overflow, [],
                                           source_report, out)
            self._package(client, run_id, run["request"], d, topics, research, source_report, copy, design, review, pngs, out)
            self.store.update_run(run_id, status="pending_approval")
            log.info("RE-REVIEW %s done: verdict %s | run total $%.3f", run_id, review["verdict"], self.store.spent_run(run_id))
        except (TaskError, BudgetExceeded) as e:
            self.store.update_run(run_id, status="pending_approval", error=f"re-review failed: {e}")
            log.error("re-review failed: %s (previous package left unchanged)", e)
        return run_id

    async def _supervise(self, run_id, task_id, client, d, research, copy, design, pngs, overflow, repeat_warnings,
                         source_report, out: Path) -> dict:
        image_list = "\n".join(f"- {p}" for ps in pngs.values() for p in ps)
        ctx = f"# CLIENT PROFILE\n{client.profile_text()}\n\n# CLIENT KNOWLEDGE BASE\n{client.knowledge_text()}"
        return (await self._task(run_id, task_id, "supervisor",
                f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-006']}\n\n"
                f"Requested carousels: {min(int(d['deliverable_count']), self.settings.max_deliverables)}, "
                f"slides each: {d['slides_per_carousel']}\n\n"
                f"# VERIFIED RESEARCH\n{_j(research)}\n\n# COPY\n{_j(copy['carousels'])}\n\n# DESIGN SPEC\n{_j(design)}\n\n"
                f"# RENDERER CAPABILITIES\nThe renderer draws text, bullets, one highlight, footnotes and a CTA button only; "
                f"icons, cards, timelines and logos are not supported and are not expected.\n\n"
                f"# RENDERED SLIDE IMAGES (open them with Read)\n{image_list}\n\n"
                f"# AUTOMATED CHECKS\n- Text overflow detected on slides: {_j(overflow)}\n"
                f"- Topic repetition warnings: {_j(repeat_warnings)}\n- Sources rejected as unverifiable: {len(source_report['rejected'])}\n"
                f"- Facts dropped for lacking verified sources: {_j(source_report['dropped_facts'])}\n\n{ctx}",
                out / "_work", readable_dirs=[out])).output

    def _fail(self, run_id: str, error: str) -> None:
        for t in self.store.tasks_for_run(run_id):
            if t["status"] in ("pending", "running"):
                self.store.update_task(t["id"], status="skipped" if t["status"] == "pending" else "failed",
                                       error=t["error"] or "not run: an earlier task failed")
        self.store.update_run(run_id, status="failed", error=error)
        log.error("RUN %s FAILED: %s (spent $%.3f)", run_id, error, self.store.spent_run(run_id))

    # ------------------------------------------------------------------ checks
    @staticmethod
    def _repetition_check(topics: list[dict], history: list[dict]) -> list[str]:
        warnings = []
        for t in topics:
            for h in history:
                ratio = SequenceMatcher(None, t["title"].lower(), h["topic"].lower()).ratio()
                if ratio > 0.75:
                    warnings.append(f"'{t['title']}' is {ratio:.0%} similar to past topic '{h['topic']}'")
        return warnings

    @staticmethod
    def _verify_sources(res: TaskResult) -> tuple[dict, dict]:
        """Keeps only sources whose URL really appeared in this task's WebSearch/WebFetch results."""
        research = res.output
        seen = res.search_urls | res.fetched_urls
        kept, rejected = [], []
        for src in research["sources"]:
            u = normalize_url(src["url"])
            if u in res.fetched_urls:
                src["accessed_via"] = "WebFetch"
                kept.append(src)
            elif u in seen:
                src["accessed_via"] = "WebSearch"
                kept.append(src)
            else:
                rejected.append(src)
        valid_ids = {s["id"] for s in kept}
        dropped = []
        for topic in research["topics"]:
            good = []
            for f in topic["verified_facts"]:
                f["source_ids"] = [i for i in f["source_ids"] if i in valid_ids]
                if f["source_ids"]:
                    good.append(f)
                else:
                    dropped.append(f["fact"])
                    topic["unverified_claims"].append(f["fact"] + " [dropped: no verified source]")
            topic["verified_facts"] = good
        research["sources"] = kept
        if rejected:
            log.warning("     rejected %d source(s) not found in real tool results: %s",
                        len(rejected), [s["url"] for s in rejected])
        log.info("     sources verified: %d kept, %d rejected; %d searches, %d fetches",
                 len(kept), len(rejected),
                 sum(c["name"] == "WebSearch" for c in res.tool_calls), len(res.fetched_urls))
        return research, {"rejected": rejected, "dropped_facts": dropped}

    # ------------------------------------------------------------------ output
    def _render(self, client: Client, copy: dict, design: dict, out: Path) -> tuple[dict, list]:
        designs = {c["topic_id"]: c for c in design["carousels"]}
        pngs, overflow = {}, []
        for i, car in enumerate(copy["carousels"], 1):
            folder = out / f"carousel-{i}"
            pngs[car["topic_id"]] = render_carousel(car, designs.get(car["topic_id"], {}), client.profile["brand"],
                                                    client.name, folder)
            overflow += [f"carousel-{i} slide {n}" for n in check_overflow(folder / "slides.html")]
        log.info("     rendered %d slide images%s", sum(len(v) for v in pngs.values()),
                 f"; overflow on {overflow}" if overflow else "")
        return pngs, overflow

    def _package(self, client, run_id, request, d, topics, research, source_report, copy, design, review, pngs, out: Path):
        src_by_id = {s["id"]: s for s in research["sources"]}
        for i, car in enumerate(copy["carousels"], 1):
            folder = out / f"carousel-{i}"
            lines = [f"# Carousel {i}: {car['title']}", "", f"Client: {client.name}  |  Status: PENDING YOUR APPROVAL", ""]
            for sl in sorted(car["slides"], key=lambda x: x["n"]):
                lines += [f"## Slide {sl['n']} ({sl['role']})", f"**{sl['headline']}**", "", sl["body"], ""]
            lines += ["## Caption", "", car["caption"], "", " ".join(car["hashtags"]), "", "## Sources for claims", ""]
            for ref in car["fact_refs"]:
                urls = ", ".join(src_by_id[s]["url"] for s in ref["source_ids"] if s in src_by_id) or "NO VERIFIED SOURCE"
                lines.append(f"- Slide {ref['slide']}: {ref['claim']} — {urls}")
            (folder / "copy.md").write_text("\n".join(lines) + "\n")
            (folder / "copy.json").write_text(_j(car))

        src_lines = ["# Research sources (verified against real tool results)", ""]
        for s in research["sources"]:
            src_lines.append(f"- **{s['id']}** [{s['title']}]({s['url']}) — {s['publisher']} (via {s['accessed_via']})\n  > {s['relevant_excerpt']}")
        if source_report["rejected"]:
            src_lines += ["", "## Rejected (URL not found in tool results — not used)", ""]
            src_lines += [f"- {s['url']}" for s in source_report["rejected"]]
        src_lines += ["", "## Facts per topic", ""]
        for t in research["topics"]:
            src_lines.append(f"### {t['topic_id']}")
            src_lines += [f"- {f['fact']} [{', '.join(f['source_ids'])}] (confidence {f['confidence']}{', may change' if f['may_change'] else ''})" for f in t["verified_facts"]]
            src_lines += [f"- UNVERIFIED: {c}" for c in t["unverified_claims"]]
            src_lines += [f"- Caveat: {c}" for c in t["caveats"]]
        (out / "sources.md").write_text("\n".join(src_lines) + "\n")

        qa = [f"# Quality review — verdict: {review['verdict'].upper()}", "", review["summary"], "", "## Checks", ""]
        qa += [f"- [{'x' if c['passed'] else ' '}] {c['check']}: {c['notes']}" for c in review["checks"]]
        qa += ["", f"Images reviewed by the Supervisor: {review['images_reviewed']}", "", "## Issues", ""]
        qa += [f"- {'BLOCKING' if x['blocking'] else 'minor'} | {x['topic_id']} slide {x['slide']}: {x['issue']} → {x['fix']}" for x in review["issues"]] or ["- none"]
        (out / "qa_report.md").write_text("\n".join(qa) + "\n")

        tasks = self.store.tasks_for_run(run_id)
        readme = [
            f"# Deliverables — {client.name}", "", f"Run: `{run_id}`  ", f"Request: {request}  ",
            "Status: **PENDING YOUR APPROVAL** — nothing has been published or sent.", "",
            f"Approve: `python -m creative_hq approve {run_id}`  ", f"Reject: `python -m creative_hq reject {run_id} --note \"...\"`", "",
            "## Brief (AG-001)", "", d["interpretation"], "", d["brief"], "",
            "## Topics (AG-003)", "",
        ]
        readme += [f"- **{t['title']}** — {t['angle']}" for t in topics]
        readme += ["", "## Contents", "", "- `carousel-N/slide-XX.png` — final slide images (1080x1350)",
                   "- `carousel-N/slides.html` — editable slide source", "- `carousel-N/copy.md` — slide text, caption, hashtags, claim sources",
                   "- `sources.md` — research sources and verified facts", "- `qa_report.md` — Quality Supervisor report", "",
                   "## Task log", "", "| Task | Agent | Status | Cost |", "|---|---|---|---|"]
        readme += [f"| {t['title']} | {t['agent_id']} | {t['status']} | ${t['cost_usd'] or 0:.3f} |" for t in tasks]
        readme += ["", f"Total cost: ${self.store.spent_run(run_id):.3f}"]
        (out / "README.md").write_text("\n".join(readme) + "\n")
