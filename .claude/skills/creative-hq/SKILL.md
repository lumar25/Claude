---
name: creative-hq
description: Operate the CREATIVE HQ multi-agent creative agency. Use whenever the user asks (in any language) for creative work for an agency client, e.g. Instagram carousels, captions or content for Kevin / Livetrust Financial, or asks about the status, approval, rejection or history of agency jobs, or asks to check that the agents work.
---

# Operating CREATIVE HQ for the user

The user is not a programmer. They describe work in plain language (often Spanish); you run the
agency's real multi-agent workflow for them. Explain results in the user's language, simply.

## Commands (run from the repository root)

| User intent | Command |
|---|---|
| New creative job ("crea un carrusel para Kevin sobre…") | `python -m creative_hq run "<request>"` — run in the background (5-12 min), then report |
| Check the six agents really work | `python -m creative_hq check --live` (≈ $0.15) |
| List jobs / see one job | `python -m creative_hq status` / `python -m creative_hq status <run_id>` |
| Re-render slides + new Supervisor review | `python -m creative_hq review <run_id>` |
| Approve / reject | `python -m creative_hq approve <run_id> --note "..."` / `python -m creative_hq reject <run_id> --note "..."` |
| Topics already used | `python -m creative_hq history` |
| Daily Reels script bank (20 scripts, research + verification) | `python -m creative_hq reels` — background, 15-30 min, then report `deliverables/<client>/reels/<date>/entrega.md` |
| Reels registry / change status / add real metrics | `python -m creative_hq reels-registry` · `reel-status <ID> seleccionado\|grabado\|publicado\|descartado` · `reel-metrics <ID> views=.. shares=.. --note ".."` |

## Rules

1. **Pass the user's request to the Director as-is.** Translate it into English only if needed for clarity, and keep every detail they gave (topic, count, client). Do not write the content yourself: the agents produce it.
2. **Never fake results.** Only report what the run actually produced: `status`, the files in `deliverables/<client>/<run_id>/`, and `logs/runs/<run_id>.jsonl`. If a run fails, show the error and help fix it.
3. **Approve or reject only when the user explicitly tells you to** for that specific run. Approving records a decision; it never publishes anything. Nobody publishes or sends anything from this system.
4. **After a job finishes, report:**
   - which agents ran, with their costs;
   - the topic(s);
   - the slide copy and caption, from `carousel-N/copy.md`;
   - the Supervisor's verdict and its main issues, from `qa_report.md`;
   - the sources, from `sources.md`;
   - any errors.

   Show slide images with the Read tool if useful.
5. **Mind the cost.** One job usually costs $1.5–4. The limits in `config/settings.yaml` stop the run before going over them.
6. **Commit and push the run's outputs** (deliverables, database, logs) after each finished job, so they are kept.
7. **Configuration changes go in YAML or Markdown only**, never in Python, unless the user asks for a feature:
   - clients: `clients/<id>/profile.yaml` and `clients/<id>/knowledge/*.md`;
   - agent instructions: `agents/*.md`;
   - models and budgets: `config/settings.yaml`.
