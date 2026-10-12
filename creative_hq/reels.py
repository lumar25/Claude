"""Daily Reels script bank: social trend research -> topics -> fact verification -> scripts -> QA.

Agents (same six team members, different assignment):
  AG-001 Director   -> daily brief (reads the registry, weekly review on Mondays / first run of the week)
  AG-002 Researcher -> (a) recent TikTok/Instagram reference videos  (b) financial fact verification
  AG-003 Strategist -> picks N varied topics, assigns references, lists claims to verify
  AG-004 Copywriter -> writes the scripts (in parallel batches)
  AG-006 Supervisor -> checks every script, marks ready/pending, ranks the top 5

Code-level checks (not left to the model):
  - reference URLs must be direct video URLs AND appear in that task's real WebSearch results
  - TikTok publish date is decoded from the video ID (documented as "derived from ID")
  - quoted captions are checked against the raw search result text
  - financial sources must appear in real tool results; facts without a valid source are dropped
  - spoken duration is computed from word count; digits in spoken text are flagged
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Client
from .runner import TaskError, normalize_url
from .storage import now
from .workflow import BudgetExceeded, Workflow, _j

log = logging.getLogger("creative_hq.reels")

STR = {"type": "string"}
STRS = {"type": "array", "items": STR}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


DIRECTOR_SCHEMA = _obj({
    "brief": STR,
    "weekly_review_es": STR,          # "n/a" when not the first delivery of the week
    "assignments": _obj({"AG-002": STR, "AG-003": STR, "AG-004": STR, "AG-006": STR}),
})

SOCIAL_SCHEMA = _obj({
    "references": {"type": "array", "items": _obj({
        "ref_id": STR,                                   # V01, V02...
        "platform": {"type": "string", "enum": ["TikTok", "Instagram"]},
        "creator": STR,
        "url": STR,
        "caption_or_snippet": STR,                       # exactly as shown in search results
        "topic": STR,
        "hook_or_opening_if_visible": STR,               # "not visible" if unknown
        "narrative_mechanism": STR,                      # what device we can adapt
        "audience_signal": STR,                          # questions/interest visible, or "not visible"
        "cannot_verify": STR,
    })},
    "search_notes": STR,
})

STRATEGY_SCHEMA = _obj({
    "topics": {"type": "array", "items": _obj({
        "script_id": STR,                               # R01..R20 (date prefix added by system)
        "topic": STR,
        "audience": STR,
        "problem": STR,
        "goal": {"type": "string", "enum": ["discovery", "retention", "shares", "saves", "qualified_conversations"]},
        "structure": {"type": "string", "enum": ["direct_statement", "scenario", "comparison", "question",
                                                 "practical_explainer", "checklist", "myth_vs_reality", "objection"]},
        "reference_ref_id": STR,                        # V.. or "original"
        "claims_to_verify": STRS,
        "not_repetitive_because": STR,
    })},
    "notes": STR,
})

FACTS_SCHEMA = _obj({
    "sources": {"type": "array", "items": _obj({
        "id": STR, "url": STR, "title": STR, "publisher": STR,
        "accessed_via": {"type": "string", "enum": ["WebSearch", "WebFetch"]},
        "relevant_excerpt": STR,
    })},
    "claims": {"type": "array", "items": _obj({
        "script_id": STR,
        "claim": STR,
        "claim_type": {"type": "string", "enum": ["program_rule", "lender_varies", "general_preparation"]},
        "verdict": {"type": "string", "enum": ["verified", "partially_verified", "not_verified"]},
        "source_ids": STRS,
        "correct_wording": STR,
    })},
})

SCRIPTS_SCHEMA = _obj({
    "scripts": {"type": "array", "items": _obj({
        "script_id": STR,
        "topic": STR,
        "audience": STR,
        "problem": STR,
        "goal": STR,
        "amount_context": STR,          # which USD figure is meant (financing / revenue / price) or "none"
        "hook": STR,
        "script": STR,                  # full spoken script incl. hook, amounts in words
        "cta": STR,
        "cta_type": {"type": "string", "enum": ["save", "share", "reply", "consult"]},
        "structure": STR,
        "recording_notes_es": STR,
        "reference_ref_id": STR,        # V.. or "original"
        "adaptation_note_es": STR,
        "fact_refs": {"type": "array", "items": _obj({
            "claim": STR,
            "claim_type": {"type": "string", "enum": ["program_rule", "lender_varies", "general_preparation", "hypothetical"]},
            "source_ids": STRS,
        })},
        "why_selected_es": STR,
        "what_to_measure_es": STR,
    })},
})

QA_SCHEMA = _obj({
    "summary_es": STR,
    "scripts": {"type": "array", "items": _obj({
        "script_id": STR,
        "status": {"type": "string", "enum": ["ready_to_record", "pending"]},
        "issues_es": STRS,
    })},
    "top5": {"type": "array", "items": _obj({"script_id": STR, "rank": {"type": "integer"}, "reason_es": STR})},
})

VIDEO_URL = re.compile(r"^https?://(www\.)?(tiktok\.com/@[\w.\-]+/video/\d{15,22}|instagram\.com/(reel|reels|p)/[\w\-]+)/?(\?.*)?$")

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS reel_scripts (
    id TEXT PRIMARY KEY, run_id TEXT, client_id TEXT, delivery_date TEXT, topic TEXT, audience TEXT,
    goal TEXT, structure TEXT, hook TEXT, cta TEXT, cta_type TEXT, reference_url TEXT, words INTEGER,
    est_seconds REAL, qa_status TEXT, priority INTEGER, status TEXT NOT NULL DEFAULT 'propuesto',
    metrics_json TEXT, learnings TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reel_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ref_id TEXT, platform TEXT, creator TEXT, url TEXT,
    caption TEXT, published_at TEXT, published_basis TEXT, consulted_at TEXT, caption_verified INTEGER,
    mechanism TEXT, cannot_verify TEXT
);
CREATE TABLE IF NOT EXISTS reel_weekly (
    week_start TEXT PRIMARY KEY, run_id TEXT, review TEXT, created_at TEXT
);
"""
STATUSES = ("propuesto", "seleccionado", "grabado", "publicado", "descartado")


def tiktok_date(url: str) -> datetime | None:
    m = re.search(r"/video/(\d{15,22})", url)
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)) >> 32, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class ReelsWorkflow(Workflow):
    def __init__(self, settings, agents, store):
        super().__init__(settings, agents, store)
        store.db.executescript(REGISTRY_SCHEMA)
        store.db.commit()
        cfg = settings.raw["reels"]
        self.cfg = cfg
        self.run_cap = float(cfg["per_run_usd"])
        self.tz = timezone(timedelta(hours=float(cfg["timezone_offset_hours"])), "ART")

    # ----------------------------------------------------------------- registry
    def registry(self, client_id: str) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT id, delivery_date, topic, audience, structure, hook, cta_type, reference_url, status, priority,"
            " metrics_json, learnings FROM reel_scripts WHERE client_id=? ORDER BY id", (client_id,)).fetchall()
        return [dict(r) for r in rows]

    def _registry_digest(self, client_id: str) -> str:
        reg = self.registry(client_id)
        if not reg:
            return "Registry empty: this is the first delivery."
        lines = [f"{r['id']} | {r['status']} | {r['structure']} | {r['topic']} | hook: {r['hook'][:90]}"
                 + (f" | metrics: {r['metrics_json']}" if r["metrics_json"] else "")
                 + (f" | learnings: {r['learnings']}" if r["learnings"] else "") for r in reg[-200:]]
        return f"{len(reg)} scripts in registry (latest 200 shown):\n" + "\n".join(lines)

    # ----------------------------------------------------------------- run
    async def run_daily(self, client: Client) -> str:
        local_now = datetime.now(self.tz)
        day = local_now.date().isoformat()
        n = int(self.cfg["scripts_per_day"])
        run_id = local_now.strftime("%Y%m%d-%H%M%S-reels-") + uuid.uuid4().hex[:4]
        out = self.settings.path("deliverables") / client.client_id / "reels" / day
        if out.exists():
            out = out.with_name(f"{day}-{run_id[-4:]}")
        work = out / "_work"
        work.mkdir(parents=True, exist_ok=True)
        from .runner import AgentRunner
        self.runner = AgentRunner(self.settings.path("logs") / "runs" / f"{run_id}.jsonl")
        request = f"Daily Reels script bank ({n} scripts) for {client.name}, {day}"
        self.store.create_run(run_id, client.client_id, request)
        self.store.update_run(run_id, deliverable_dir=str(out))
        log.info("REELS RUN %s | %s | %d scripts | cap $%.2f", run_id, day, n, self.run_cap)

        plan = [("T1", "AG-001", "Daily brief + weekly review", []),
                ("T2", "AG-002", "Research recent TikTok/Instagram reference videos", ["T1"]),
                ("T3", "AG-003", f"Select {n} varied, non-repetitive topics", ["T1", "T2"]),
                ("T4", "AG-002", "Verify financial claims with primary sources", ["T3"])]
        batches = [list(range(i, min(i + 5, n))) for i in range(0, n, 5)]
        for b in range(len(batches)):
            plan.append((f"T5{chr(97 + b)}", "AG-004", f"Write scripts batch {b + 1}", ["T3", "T4"]))
        plan.append(("T6", "AG-006", "QA all scripts, rank top 5", [f"T5{chr(97 + b)}" for b in range(len(batches))]))
        for i, (suffix, agent, title, deps) in enumerate(plan, 1):
            self.store.create_task(f"{run_id}-{suffix}", run_id, i, agent, title, [f"{run_id}-{d}" for d in deps])
        tid = lambda s: f"{run_id}-{s}"  # noqa: E731

        ctx = (f"# CLIENT PROFILE\n{client.profile_text()}\n\n# CLIENT KNOWLEDGE BASE\n{client.knowledge_text()}\n\n"
               f"# TODAY (America/Buenos_Aires)\n{local_now.strftime('%A %Y-%m-%d %H:%M')}")
        week_start = (local_now.date() - timedelta(days=local_now.weekday())).isoformat()
        first_of_week = self.store.db.execute("SELECT 1 FROM reel_weekly WHERE week_start=?", (week_start,)).fetchone() is None
        registry = self._registry_digest(client.client_id)
        R = self.agents["researcher"]
        try:
            # T1 Director
            d = (await self._task(run_id, tid("T1"), "director",
                 f"# JOB\nToday's delivery: {n} complete Reels scripts for Kevin Martinez (spoken in English, 35-60 s each), "
                 f"based on recent TikTok/Instagram research, to increase discovery, retention, shares and qualified "
                 f"conversations about business financing. Never promise virality; views are not leads.\n\n"
                 f"# REGISTRY OF PREVIOUS SCRIPTS\n{registry}\n\n"
                 f"# WEEKLY REVIEW\n{'This is the FIRST delivery of the week starting ' + week_start + ': write weekly_review_es in Spanish (patterns observed, thematic priorities, narrative devices to test, topics covered and gaps). Base it only on the registry; if there is no data yet, say so plainly.' if first_of_week else 'Not the first delivery of the week: weekly_review_es = n/a.'}\n\n{ctx}",
                 work, schema=DIRECTOR_SCHEMA)).output
            if first_of_week and d["weekly_review_es"].strip().lower() != "n/a":
                self.store.db.execute("INSERT OR REPLACE INTO reel_weekly VALUES (?,?,?,?)",
                                      (week_start, run_id, d["weekly_review_es"], now()))
                self.store.db.commit()

            # T2 Researcher: social references
            social_spec = replace(R, max_turns=45, max_budget_usd=2.5)
            social = await self._task(run_id, tid("T2"), "researcher",
                f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-002']}\n\n"
                "# TASK A — REFERENCE VIDEOS\nFind specific, recent TikTok and Instagram videos about business financing, "
                "SBA loans, buying a business, franchise acquisition/financing, buyer readiness and deal structure "
                "(NOT personal finance, trading, crypto or get-rich content). Use about 15-25 WebSearch queries, e.g. "
                "`site:tiktok.com/@ \"SBA loan\"`, `site:tiktok.com/@ \"buy a business\"`, `site:instagram.com/reel franchise`, "
                "recent phrasing (2026, this week). Prefer the newest results.\n"
                "Return ONLY direct video URLs (tiktok.com/@creator/video/ID or instagram.com/reel/ID) that appeared in "
                "your search results; never construct or guess a URL. Copy caption_or_snippet exactly as shown in the "
                "results. You cannot open the videos (the network blocks TikTok/Instagram pages), so: hook only if the "
                "caption reveals it, otherwise 'not visible'; no metrics, dates, comments or transcripts unless they are "
                "literally in the search result text. State what you cannot verify. Aim for 15-30 relevant references.\n\n"
                f"{ctx}", work, schema=SOCIAL_SCHEMA, spec=social_spec)
            refs, ref_report = self._verify_refs(social, run_id)
            (work / "references.json").write_text(_j({"references": refs, "report": ref_report}))

            # T3 Strategist
            s = (await self._task(run_id, tid("T3"), "strategist",
                 f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-003']}\n\n"
                 f"# VERIFIED REFERENCE VIDEOS (only these may be used as references)\n{_j(refs)}\n\n"
                 f"# REGISTRY (avoid repeating topics, hooks and structures)\n{registry}\n\n"
                 f"Select exactly {n} topics, script_id R01..R{n:02d}. They must differ in problem, audience, angle and "
                 "resolution; mix structures (no structure more than 4 times). Prioritise concrete buyer/owner situations, "
                 "decision-changing mistakes, belief-vs-reality, simple explanations, hypothetical examples, real audience "
                 "questions, processes/checklists and objections. No fear-mongering or artificial controversy. "
                 "reference_ref_id = a V.. id whose mechanism genuinely fits, otherwise 'original'. List every financial claim "
                 f"the script will need verified (program rules, limits, eligibility) in claims_to_verify.\n\n{ctx}",
                 work, schema=STRATEGY_SCHEMA)).output
            topics = s["topics"][:n]
            if len(topics) < n:
                raise TaskError(f"strategist returned {len(topics)} topics, {n} required")
            valid_refs = {r["ref_id"] for r in refs}
            for t in topics:
                if t["reference_ref_id"] not in valid_refs:
                    t["reference_ref_id"] = "original"

            # T4 Researcher: fact verification
            facts_spec = replace(R, max_turns=45, max_budget_usd=2.5)
            claims = [{"script_id": t["script_id"], "claims": t["claims_to_verify"]} for t in topics]
            f_res = await self._task(run_id, tid("T4"), "researcher",
                f"# TASK B — FINANCIAL FACT VERIFICATION\nVerify these claims with primary sources (SBA.gov, eCFR, "
                "Federal Register, FTC Franchise Rule, IRS) first, reputable secondary sources only if needed. Classify each as "
                "program_rule (true for the SBA program), lender_varies (depends on the lender) or general_preparation "
                "(good practice, not a rule). Never present a lender's condition as a universal SBA rule. Give correct, "
                "precise wording, with program year/date when figures may change. Number sources S1, S2, ... Be efficient: "
                "group related claims into shared searches (about 15-25 searches total).\n\n"
                f"# CLAIMS\n{_j(claims)}", work, schema=FACTS_SCHEMA, spec=facts_spec)
            facts = self._verify_fact_sources(f_res, run_id, client)
            (work / "facts.json").write_text(_j(facts))

            # T5 Copywriter (parallel batches)
            wps = float(self.cfg["words_per_second"])
            lo, hi = int(self.cfg["min_seconds"] * wps), int(self.cfg["max_seconds"] * wps)

            async def write_batch(b: int, idx: list[int]):
                return await self._write_scripts(run_id, tid(f"T5{chr(97 + b)}"), d, [topics[i] for i in idx],
                                                 refs, facts, ctx, work)

            parts = await asyncio.gather(*(write_batch(b, idx) for b, idx in enumerate(batches)), return_exceptions=True)
            errors = [p for p in parts if isinstance(p, BaseException)]
            if errors:
                raise errors[0] if isinstance(errors[0], (TaskError, BudgetExceeded)) else TaskError(str(errors[0]))
            scripts = [sc for p in parts for sc in p]
            checks = self._auto_checks(scripts, lo, hi, wps, facts)

            # T6 Supervisor
            qa = (await self._task(run_id, tid("T6"), "supervisor",
                  f"# YOUR ASSIGNMENT\n{d['assignments']['AG-006']}\n\n"
                  "# REVIEW THESE REELS SCRIPTS (no images this time; do not use Read)\n"
                  "For each script decide ready_to_record or pending. Pending if: any financial claim not supported by the "
                  "verified claims/sources, a lender condition presented as an SBA rule, a promise of approval/speed/results, "
                  "invented case/testimonial/Kevin experience, USD figure without its context, a CTA promising something "
                  "that doesn't exist, duration outside 35-60 s, digits in spoken text, copied creator phrasing, or "
                  "recording notes that include editing/overlays/B-roll. Also check variety across the set. "
                  "Rank the 5 most promising for Kevin's goals (relevance to potential clients over generic trends). "
                  "Write issues and reasons in Spanish.\n\n"
                  f"# AUTOMATED CHECKS (computed by code)\n{_j(checks)}\n\n# VERIFIED CLAIMS\n{_j(facts['claims'])}\n\n"
                  f"# SOURCES\n{_j(facts['sources'])}\n\n# REFERENCES\n{_j(refs)}\n\n# SCRIPTS\n{_j(scripts)}\n\n{ctx}",
                  work, schema=QA_SCHEMA)).output

            self._save(client, run_id, day, d, refs, ref_report, topics, facts, scripts, checks, qa, out,
                       d["weekly_review_es"] if first_of_week else None)
            self.store.update_run(run_id, status="pending_approval")
            log.info("REELS RUN %s finished | total $%.3f | %s", run_id, self.store.spent_run(run_id), out)
        except (TaskError, BudgetExceeded) as e:
            self._fail(run_id, str(e))
        except Exception as e:
            log.exception("unexpected error")
            self._fail(run_id, f"{type(e).__name__}: {e}")
        return run_id

    async def _write_scripts(self, run_id, task_id, d, bt, refs, facts, ctx, work) -> list[dict]:
        wps = float(self.cfg["words_per_second"])
        lo, hi = int(self.cfg["min_seconds"] * wps), int(self.cfg["max_seconds"] * wps)
        ref_by_id = {r["ref_id"]: r for r in refs}
        bref = [ref_by_id[t["reference_ref_id"]] for t in bt if t["reference_ref_id"] in ref_by_id]
        bclaims = [c for c in facts["claims"] if c["script_id"] in {t["script_id"] for t in bt}]
        out = (await self._task(run_id, task_id, "copywriter",
            f"# DIRECTOR BRIEF\n{d['brief']}\n\n# YOUR ASSIGNMENT\n{d['assignments']['AG-004']}\n\n"
            f"# WRITE {len(bt)} REELS SCRIPTS (spoken by Kevin to camera)\n"
            f"- Natural, conversational American English; calm, clear, consultative, no guru language.\n"
            f"- `script` = ONLY the words Kevin says out loud, INCLUDING the hook, {lo}-{hi} words (35-60 s at a natural "
            "pace). No timecodes, labels, on-screen text, visual notes, captions, hashtags or risk notes in it. Count your words.\n"
            f"- Use exactly these script_id values: {[t['script_id'] for t in bt]}.\n"
            "- Write every amount and number in words (e.g. 'three hundred thousand dollars', 'ten percent').\n"
            "- If you mention ~USD 300,000, say whether it is financing requested, annual revenue or purchase price; "
            "amount_context must state which and add '(provisional)'.\n"
            "- Use ONLY claims verified/partially_verified below, with their correct_wording; mark hypothetical "
            "examples as hypothetical in the spoken text. Drop anything unverified. No sources in the spoken text.\n"
            "- Never promise approval, fast funding or guaranteed results; never 'anyone can get this loan'. No "
            "invented cases, testimonials or Kevin experience.\n"
            "- CTA by goal: save / share with a partner or buyer / reply to a specific question / talk to Kevin via "
            "DM (the only approved channel). No links, freebies, comment automations or services that don't exist.\n"
            "- Vary hooks: statements, scenarios, comparisons, questions, practical explanations; no off-camera "
            "interviewer unless the structure needs it.\n"
            "- recording_notes_es: brief Spanish notes ONLY on how Kevin should record and act (framing, energy, "
            "pauses, gestures, where to look). No editing, overlays or B-roll.\n"
            "- Do not copy other creators' scripts, phrases or personal stories; adapt only the mechanism "
            "(adaptation_note_es, in Spanish). If reference_ref_id is 'original', say so.\n"
            "- why_selected_es / what_to_measure_es in Spanish (e.g. retention, shares, saves, profile visits, "
            "qualified DMs; never treat views as leads).\n\n"
            f"# TOPICS\n{_j(bt)}\n\n# REFERENCES\n{_j(bref)}\n\n# VERIFIED CLAIMS\n{_j(bclaims)}\n\n"
            f"# SOURCES\n{_j(facts['sources'])}\n\n{ctx}", work, schema=SCRIPTS_SCHEMA)).output["scripts"]
        ids = [t["script_id"] for t in bt]
        for i, sc in enumerate(out[:len(ids)]):   # the system, not the model, owns the IDs
            if sc["script_id"] not in ids:
                sc["script_id"] = ids[i]
        return out[:len(ids)]

    async def redo(self, client: Client, run_id: str, script_ids: list[str]) -> None:
        """Rewrites selected scripts of a finished delivery (new Copywriter + Supervisor tasks) and re-saves it."""
        run = self.store.get_run(run_id)
        out = Path(run["deliverable_dir"])
        work = out / "_work"
        from .runner import AgentRunner
        self.runner = AgentRunner(self.settings.path("logs") / "runs" / f"{run_id}.jsonl")
        out_of = lambda suffix: json.loads(self.store.get_task(f"{run_id}-{suffix}")["output_json"])  # noqa: E731
        d, topics = out_of("T1"), out_of("T3")["topics"]
        saved = json.loads((out / "scripts.json").read_text())
        refs_file = json.loads((work / "references.json").read_text())
        refs, ref_report = refs_file["references"], refs_file["report"]
        facts = json.loads((work / "facts.json").read_text())
        redo_topics = [t for t in topics if t["script_id"] in script_ids]
        if not redo_topics:
            raise SystemExit(f"none of {script_ids} found in this delivery")
        n = sum(1 for t in self.store.tasks_for_run(run_id) if "-T5r" in t["id"]) + 1
        w_tid, q_tid = f"{run_id}-T5r{n}", f"{run_id}-T6r{n}"
        self.store.create_task(w_tid, run_id, 50 + 2 * n, "AG-004", f"Rewrite scripts {script_ids}", [])
        self.store.create_task(q_tid, run_id, 51 + 2 * n, "AG-006", f"Re-review rewritten scripts {script_ids}", [w_tid])
        day = run["deliverable_dir"].rstrip("/").split("/")[-1][:10]
        ctx = (f"# CLIENT PROFILE\n{client.profile_text()}\n\n# CLIENT KNOWLEDGE BASE\n{client.knowledge_text()}\n\n"
               f"# DELIVERY DATE\n{day}")
        log.info("REELS REDO %s: %s", run_id, script_ids)
        try:
            new = await self._write_scripts(run_id, w_tid, d, redo_topics, refs, facts, ctx, work)
            wps = float(self.cfg["words_per_second"])
            lo, hi = int(self.cfg["min_seconds"] * wps), int(self.cfg["max_seconds"] * wps)
            checks = self._auto_checks(new, lo, hi, wps, facts)
            others = [{"script_id": x["script_id"], "topic": x["topic"], "qa_status": x["qa_status"]}
                      for x in saved["scripts"] if x["script_id"] not in script_ids]
            qa_new = (await self._task(run_id, q_tid, "supervisor",
                f"# REVIEW THESE REWRITTEN REELS SCRIPTS (same criteria as the daily QA; do not use Read)\n"
                "Mark each ready_to_record or pending (unsupported claims, lender condition presented as SBA rule, promises, "
                "invented cases, USD figure without context, nonexistent CTA resources, duration outside 35-60 s, digits in "
                "spoken text, copied creator phrasing, editing/overlay/B-roll notes). Then re-rank the 5 most promising "
                "scripts of the WHOLE delivery (rewritten + others below). Spanish for issues and reasons.\n\n"
                f"# PREVIOUS TOP 5\n{_j(saved['qa']['top5'])}\n\n# OTHER SCRIPTS IN THIS DELIVERY\n{_j(others)}\n\n"
                f"# AUTOMATED CHECKS\n{_j(checks)}\n\n# VERIFIED CLAIMS\n{_j(facts['claims'])}\n\n"
                f"# SOURCES\n{_j(facts['sources'])}\n\n# REWRITTEN SCRIPTS\n{_j(new)}\n\n{ctx}",
                work, schema=QA_SCHEMA)).output
        except (TaskError, BudgetExceeded) as e:
            log.error("redo failed: %s (previous delivery left unchanged)", e)
            return
        topic_ids = {t["script_id"] for t in topics}
        # keep untouched scripts (dropping any with malformed IDs, which are the ones being rewritten) + new ones
        scripts = [x for x in saved["scripts"] if x["script_id"] in topic_ids and x["script_id"] not in script_ids] + new
        qa = saved["qa"]
        qa_by = {q["script_id"]: q for q in qa["scripts"]}
        qa_by.update({q["script_id"]: q for q in qa_new["scripts"]})
        qa = {"summary_es": qa["summary_es"] + f" | Reescritos {script_ids}: " + qa_new["summary_es"],
              "scripts": list(qa_by.values()), "top5": qa_new["top5"]}
        all_checks = {x["script_id"]: [i for i in x.get("qa_issues", []) if i.startswith(("duration", "digits", "claims"))]
                      for x in saved["scripts"] if x["script_id"] not in script_ids}
        all_checks.update(checks)
        for x in scripts:   # reset fields recomputed by _save
            for k in ("qa_status", "qa_issues", "full_id", "priority"):
                x.pop(k, None)
        row = self.store.db.execute("SELECT review FROM reel_weekly WHERE run_id=?", (run_id,)).fetchone()
        self.store.db.execute("DELETE FROM reel_scripts WHERE run_id=?", (run_id,))
        self._save(client, run_id, day, d, refs, ref_report, topics, facts, scripts, all_checks, qa, out,
                   row["review"] if row else None)
        log.info("REELS REDO done | run total $%.3f", self.store.spent_run(run_id))

    # ----------------------------------------------------------------- verification
    def _verify_refs(self, res, run_id: str) -> tuple[list[dict], dict]:
        consulted = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M ART")
        blob = _norm(" ".join(res.search_texts))
        kept, rejected, seen = [], [], set()
        for r in res.output["references"]:
            url = r["url"].strip()
            nu = normalize_url(url.split("?")[0])
            reason = None
            if not VIDEO_URL.match(url):
                reason = "not a direct video URL"
            elif nu not in res.search_urls and normalize_url(url) not in res.search_urls:
                reason = "URL not present in real search results"
            elif nu in seen:
                reason = "duplicate"
            if reason:
                rejected.append({"url": url, "reason": reason})
                continue
            seen.add(nu)
            cap = _norm(r["caption_or_snippet"])[:80]
            r["caption_verified"] = bool(cap) and cap[:60] in blob
            pub = tiktok_date(url) if r["platform"] == "TikTok" else None
            if pub:
                r["published_at"] = pub.astimezone(self.tz).strftime("%Y-%m-%d")
                r["published_basis"] = "derived from TikTok video ID"
                r["age_days"] = (datetime.now(timezone.utc) - pub).days
            else:
                r["published_at"], r["published_basis"], r["age_days"] = "unknown", "not verifiable", None
            r["consulted_at"] = consulted
            r["metrics"] = "not available (video pages cannot be opened from this environment)"
            kept.append(r)
        kept.sort(key=lambda x: x["age_days"] if x["age_days"] is not None else 10**6)
        for i, r in enumerate(kept, 1):
            r["ref_id"] = f"V{i:02d}"
            self.store.db.execute(
                "INSERT INTO reel_references (run_id, ref_id, platform, creator, url, caption, published_at, published_basis,"
                " consulted_at, caption_verified, mechanism, cannot_verify) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, r["ref_id"], r["platform"], r["creator"], r["url"], r["caption_or_snippet"], r["published_at"],
                 r["published_basis"], r["consulted_at"], int(r["caption_verified"]), r["narrative_mechanism"], r["cannot_verify"]))
        self.store.db.commit()
        ages = [r["age_days"] for r in kept if r["age_days"] is not None]
        report = {"kept": len(kept), "rejected": rejected,
                  "within_72h": sum(a <= 3 for a in ages), "within_7d": sum(a <= 7 for a in ages),
                  "within_30d": sum(a <= 30 for a in ages), "older_or_unknown": len(kept) - sum(a <= 30 for a in ages),
                  "searches": sum(c["name"] == "WebSearch" for c in res.tool_calls), "search_notes": res.output["search_notes"]}
        log.info("     references: %d kept (72h: %d, 7d: %d, 30d: %d), %d rejected",
                 len(kept), report["within_72h"], report["within_7d"], report["within_30d"], len(rejected))
        return kept, report

    def _verify_fact_sources(self, res, run_id: str, client: Client) -> dict:
        out = res.output
        seen = res.search_urls | res.fetched_urls
        kept, rejected = [], []
        for src in out["sources"]:
            u = normalize_url(src["url"])
            if u in seen:
                src["accessed_via"] = "WebFetch" if u in res.fetched_urls else "WebSearch"
                kept.append(src)
            else:
                rejected.append(src)
        ok = {s["id"] for s in kept}
        for c in out["claims"]:
            c["source_ids"] = [i for i in c["source_ids"] if i in ok]
            if not c["source_ids"] and c["verdict"] != "not_verified":
                c["verdict"] = "not_verified"
                c["correct_wording"] += " [downgraded: no source found in real tool results]"
        for s in kept:
            self.store.add_source(run_id, client.client_id, s, verified=True)
        for s in rejected:
            self.store.add_source(run_id, client.client_id, s, verified=False)
        out["sources"], out["rejected_sources"] = kept, rejected
        log.info("     fact sources: %d kept, %d rejected; claims verified: %d/%d", len(kept), len(rejected),
                 sum(c["verdict"] != "not_verified" for c in out["claims"]), len(out["claims"]))
        return out

    @staticmethod
    def _auto_checks(scripts: list[dict], lo: int, hi: int, wps: float, facts: dict) -> dict:
        valid_sources = {s["id"] for s in facts["sources"]}
        result = {}
        for sc in scripts:
            words = len(re.findall(r"[A-Za-z0-9'’\-]+", sc["script"]))
            sc["words"], sc["est_seconds"] = words, round(words / wps)
            issues = []
            if not lo <= words <= hi:
                issues.append(f"duration ~{sc['est_seconds']} s ({words} words) outside 35-60 s")
            if re.search(r"\d", sc["script"]):
                issues.append("digits in spoken text (amounts must be written in words)")
            bad = [r["claim"] for r in sc["fact_refs"] if r["claim_type"] != "hypothetical"
                   and not set(r["source_ids"]) & valid_sources]
            if bad:
                issues.append(f"claims without a verified source: {bad}")
            result[sc["script_id"]] = issues
        return result

    # ----------------------------------------------------------------- output
    def _save(self, client, run_id, day, d, refs, ref_report, topics, facts, scripts, checks, qa, out: Path, weekly):
        qa_by = {q["script_id"]: q for q in qa["scripts"]}
        rank = {t["script_id"]: t["rank"] for t in qa["top5"]}
        ref_by = {r["ref_id"]: r for r in refs}
        src_by = {s["id"]: s for s in facts["sources"]}
        for sc in scripts:
            q = qa_by.get(sc["script_id"], {"status": "pending", "issues_es": ["Sin revisión del Supervisor"]})
            if checks.get(sc["script_id"]):
                q["status"] = "pending" if any("without a verified source" in i or "digits" in i for i in checks[sc["script_id"]]) else q["status"]
            sc["qa_status"], sc["qa_issues"] = q["status"], q["issues_es"] + checks.get(sc["script_id"], [])
            sc["full_id"] = f"KM-{day.replace('-', '')}-{sc['script_id']}"
            sc["priority"] = rank.get(sc["script_id"])
        scripts.sort(key=lambda x: x["script_id"])

        L = [f"# Banco de Reels — Kevin Martinez (Livetrust Financial) — {day}", "",
             f"Ejecución: `{run_id}` · Estado: **PROPUESTO, pendiente de tu revisión** (nada se ha grabado ni publicado).", ""]
        if weekly:
            L += ["## Revisión semanal", "", weekly, ""]
        L += ["## A. Resumen de hallazgos verificables", "",
              f"- Referencias de vídeo comprobadas (URL directa presente en resultados reales de búsqueda): **{ref_report['kept']}** "
              f"({ref_report['searches']} búsquedas). Rechazadas por el sistema: {len(ref_report['rejected'])}.",
              f"- Antigüedad (fecha deducida del ID en TikTok): ≤72 h: {ref_report['within_72h']} · ≤7 días: {ref_report['within_7d']} · "
              f"≤30 días: {ref_report['within_30d']} · más antiguas o sin fecha: {ref_report['older_or_unknown']}.",
              "- Métricas, comentarios y transcripciones: **no disponibles** (este entorno no puede abrir TikTok/Instagram). "
              "Ningún vídeo se describe como 'en crecimiento'; solo hay captions indexados.",
              f"- Afirmaciones financieras verificadas: {sum(c['verdict'] != 'not_verified' for c in facts['claims'])}/{len(facts['claims'])}, "
              f"con {len(facts['sources'])} fuentes comprobadas (rechazadas: {len(facts['rejected_sources'])}).",
              f"- Notas del investigador: {ref_report['search_notes']}", "",
              f"Resumen del Supervisor: {qa['summary_es']}", "",
              "## B. Tabla de guiones", "", "| ID | Tema | Hook | Audiencia | Duración | Estado QA | Prioridad |", "|---|---|---|---|---|---|---|"]
        for sc in scripts:
            L.append(f"| {sc['full_id']} | {sc['topic']} | {sc['hook']} | {sc['audience']} | ~{sc['est_seconds']} s | "
                     f"{'✅ lista' if sc['qa_status'] == 'ready_to_record' else '⏳ pendiente'} | {sc['priority'] or '—'} |")
        L += ["", "## C. Guiones completos", ""]
        for sc in scripts:
            ref = ref_by.get(sc["reference_ref_id"])
            L += [f"### {sc['full_id']} — {sc['topic']}", "",
                  f"**1. ID / fecha / tema:** {sc['full_id']} · {day} · {sc['topic']}  ",
                  f"**2. Audiencia y problema:** {sc['audience']} — {sc['problem']}  ",
                  f"**3. Objetivo:** {sc['goal']} · Estructura: {sc['structure']} · Cifra: {sc['amount_context']}  ",
                  f"**4. Hook:** \"{sc['hook']}\"  ", "", "**5. Guion completo (inglés):**", "", "> " + sc["script"].replace("\n", "\n> "), "",
                  f"Duración estimada: ~{sc['est_seconds']} s ({sc['words']} palabras a {self.cfg['words_per_second']} palabras/s)", "",
                  f"**6. CTA:** \"{sc['cta']}\" ({sc['cta_type']})  ", "",
                  f"**7. Cómo grabarlo (Kevin):** {sc['recording_notes_es']}", ""]
            if ref:
                L.append(f"**8. Referencia:** [{ref['platform']} · {ref['creator']}]({ref['url']}) · publicado: {ref['published_at']} "
                         f"({ref['published_basis']}) · consultado: {ref['consulted_at']} · métricas: no disponibles · caption "
                         f"{'verificado en resultados de búsqueda' if ref['caption_verified'] else 'NO verificado literalmente'}. "
                         f"Qué adaptamos: {sc['adaptation_note_es']}")
            else:
                L.append(f"**8. Referencia:** Guion ORIGINAL — sin vídeo de referencia comprobado. {sc['adaptation_note_es']}")
            L += ["", "**9. Fuentes de las afirmaciones financieras:**"]
            if sc["fact_refs"]:
                for fr in sc["fact_refs"]:
                    urls = ", ".join(src_by[i]["url"] for i in fr["source_ids"] if i in src_by) or (
                        "ejemplo hipotético" if fr["claim_type"] == "hypothetical" else "SIN FUENTE VERIFICADA")
                    L.append(f"- [{fr['claim_type']}] {fr['claim']} — {urls}")
            else:
                L.append("- Sin afirmaciones financieras específicas.")
            L += ["", f"**10. Por qué y qué medir:** {sc['why_selected_es']} Medir: {sc['what_to_measure_es']}", "",
                  f"**Revisión:** {'✅ lista para grabar' if sc['qa_status'] == 'ready_to_record' else '⏳ PENDIENTE — no grabar todavía'}"
                  + ("".join(f"\n- {i}" for i in sc["qa_issues"]) if sc["qa_issues"] else ""), "", "---", ""]
        L += ["## D. Los cinco recomendados para grabar primero", ""]
        sc_by = {s["script_id"]: s for s in scripts}
        for t in sorted(qa["top5"], key=lambda x: x["rank"]):
            s = sc_by.get(t["script_id"])
            if s:
                L.append(f"{t['rank']}. **{s['full_id']}** — {s['topic']}: {t['reason_es']}")
        tasks = self.store.tasks_for_run(run_id)
        L += ["", "## E. Registro y limitaciones", "",
              f"- Registro actualizado: {len(scripts)} guiones añadidos con estado **propuesto** (tabla `reel_scripts`).",
              "- Cambiar estado: `python -m creative_hq reel-status <ID> seleccionado|grabado|publicado|descartado`.",
              "- Añadir métricas reales: `python -m creative_hq reel-metrics <ID> views=... retention=... shares=... saves=... "
              "profile_visits=... qualified_dms=... --note \"...\"`.",
              "- Limitaciones: no se pueden abrir vídeos, métricas, comentarios ni transcripciones de TikTok/Instagram desde este "
              "entorno; los Reels de Instagram casi no están indexados por el buscador; las páginas de SBA.gov tampoco se pueden "
              "abrir, así que las fuentes se comprueban por resultados de búsqueda (título y fragmento). Para crecimiento real hacen "
              "falta métricas en distintos momentos (enlaces o capturas que tú aportes).", "",
              "| Tarea | Agente | Estado | Costo |", "|---|---|---|---|"]
        L += [f"| {t['title']} | {t['agent_id']} | {t['status']} | ${t['cost_usd'] or 0:.3f} |" for t in tasks]
        L += ["", f"Costo total: ${self.store.spent_run(run_id):.3f}"]
        (out / "entrega.md").write_text("\n".join(L) + "\n")
        (out / "scripts.json").write_text(_j({"scripts": scripts, "references": refs, "facts": facts, "qa": qa}))

        for sc in scripts:
            ref = ref_by.get(sc["reference_ref_id"])
            self.store.db.execute(
                "INSERT OR REPLACE INTO reel_scripts (id, run_id, client_id, delivery_date, topic, audience, goal, structure, hook,"
                " cta, cta_type, reference_url, words, est_seconds, qa_status, priority, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'propuesto',?,?)",
                (sc["full_id"], run_id, client.client_id, day, sc["topic"], sc["audience"], sc["goal"], sc["structure"],
                 sc["hook"], sc["cta"], sc["cta_type"], ref["url"] if ref else "original", sc["words"], sc["est_seconds"],
                 sc["qa_status"], sc["priority"], now(), now()))
        self.store.db.commit()
