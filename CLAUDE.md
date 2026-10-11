# CREATIVE HQ

A multi-agent AI creative agency built on the Claude Agent SDK (Python). It has six agents
(AG-001 to AG-006), one per file in `agents/`. They run a fixed, persisted workflow in
`creative_hq/workflow.py` and produce deliverables that wait for human approval.

When the user asks for creative work for a client, or about agency jobs, use the
`creative-hq` skill (`.claude/skills/creative-hq/SKILL.md`). Run the real workflow; never write
or simulate the agents' output yourself.

The operator is not a programmer. Reply in their language (usually Spanish), explain simply,
and keep configuration in YAML/Markdown files rather than Python.
