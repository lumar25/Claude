# CREATIVE HQ — multi-agent AI creative agency

Six AI agents, built on the official **Claude Agent SDK** (Python), work together on real
creative tasks. Each one is a separate Claude session with its own instructions, its own tools,
and its own spending limit. Results move between them through a shared task workflow.
**Nothing gets published or sent.** Every deliverable waits for your approval.

```
 You: "Create two Instagram carousels for Kevin"
   │
   ▼
 T1 AG-001 Creative Director ─ reads the client profile and history, writes the brief, gives each agent its assignment
   ▼
 T2 AG-003 Content Strategist ─ picks topics and checks them against the content history so topics don't repeat
   ▼
 T3 AG-002 Research Specialist ─ runs real web searches and lists its sources ──► the system DISCARDS any source
   ▼                                                                    URL that didn't actually show up in its search results
 T4 AG-004 Copywriter ─ writes slides, caption, hashtags and CTA using verified facts only (each claim is tied to a source)
   ▼
 T5 AG-005 Graphic Designer ─ picks the visual concept and slide layouts ──► the renderer makes HTML and PNG slides
   ▼
 T6 AG-006 Quality Supervisor ─ checks accuracy, compliance and brand, and opens the PNGs to look at them
   │   └─ blocking issues? → the Copywriter revises → Designer → Supervisor (1 round by default)
   ▼
 deliverables/<client>/<run>/ ──► status PENDING APPROVAL ──► you approve or reject
```

## Folder map

| Path | What it is |
|---|---|
| `agents/AG-00X-*.md` | One file per agent: ID, tools, budget, model, and instructions. Edit these to change behaviour. |
| `clients/<client>/profile.yaml` | Client profile: brand, voice, compliance rules, disclaimer |
| `clients/<client>/knowledge/*.md` | That client's own knowledge base. Each client gets a separate folder. |
| `config/settings.yaml` | Models and spending limits (per run, per day) |
| `creative_hq/` | The engine: `workflow.py` (task graph), `runner.py` (Agent SDK sessions), `storage.py` (database), `render.py` (slides) |
| `data/creative_hq.db` | Saved history: runs, tasks with inputs/outputs, sources, content history, spend |
| `deliverables/` | Finished packages waiting for your approval |
| `logs/creative_hq.log`, `logs/runs/<run>.jsonl` | Execution logs, including every tool call each agent made |

## Safety rules built into the code (not just the prompts)

1. **No fabricated sources.** After the Researcher finishes, `workflow._verify_sources` compares each source URL with the URLs that actually came back from its WebSearch/WebFetch calls. Any source that doesn't match is rejected, and facts that relied only on rejected sources are dropped. If nothing verified is left, the run stops.
2. **No fake completion.** A task only counts as `completed` when the agent session ended successfully and returned output that matches its schema. Errors mark the task `failed`, every task that depends on it is marked `skipped`, and the run is marked `failed`.
3. **No publishing.** No agent has any tool that can post, send or edit anything outside the system. Tools are restricted twice: by the tool list, and again by a permission check (`can_use_tool`). The Supervisor's `Read` access only covers that run's own deliverable folder. `approve` only records your decision.
4. **Spending limits.** Each agent has a per-task cap (`max_budget_usd`, enforced by the SDK). The system also checks the per-run and per-day caps from `settings.yaml` before every task.
5. **Error handling.** Temporary errors (network, rate limits) get one automatic retry. Every attempt is logged, and the spend from failed attempts is counted too.

## Setting it up on your own computer

1. **Install Python 3.10+** from python.org.
2. **Get an Anthropic API key:** sign in at https://console.anthropic.com → *API Keys* → *Create key*. Add credit, and set a monthly spend limit in the Console as an extra safety net.
3. **Open a terminal in this folder and install** the dependencies (the Agent SDK, YAML and the slide renderer):
   ```bash
   python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
4. **Set your key** for the current terminal. Never put the key in a file that gets committed.
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."                  # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
   ```
5. **Check everything:** `python -m creative_hq check`

## Daily use

```bash
python -m creative_hq run "Create two Instagram carousels for Kevin"
python -m creative_hq status                 # all runs
python -m creative_hq status <RUN_ID>        # tasks, agents, cost and errors for one run
python -m creative_hq approve <RUN_ID> --note "Looks good"
python -m creative_hq reject  <RUN_ID> --note "Change slide 3"
python -m creative_hq history                # topics already used (the Strategist won't repeat them)
```

## Adding a client

Copy `clients/livetrust-financial/` to `clients/<new-id>/`, then edit `profile.yaml` and the
knowledge files. Add a name alias in `CLIENT_ALIASES` in `creative_hq/__main__.py`, or pass `--client <new-id>`.

## Current limits and next steps

- Only the Instagram carousel format runs end to end so far. Video scripts and caption sets are in the Director's schema but don't have a workflow yet.
- The Designer's output is real: editable HTML plus PNG images. The Figma and Canva connectors aren't wired into the agents yet. The design spec was built so it can drive them later.
- Livetrust's brand colours, Instagram handle and website are **placeholders** (`profile.yaml`) until Kevin confirms them.
