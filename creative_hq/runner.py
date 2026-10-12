"""Executes a single agent task with the Claude Agent SDK.

Each task is a real, separate Claude agent session with:
- the agent's own system prompt (its instructions file),
- only the tools that agent is allowed to use (enforced twice: tool list + permission callback),
- a hard spending cap (max_budget_usd),
- a structured-output schema, so results can be validated and passed to the next agent,
- full capture of tool calls/results (used to verify research sources).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .config import AgentSpec

log = logging.getLogger("creative_hq.runner")

URL_RE = re.compile(r"https?://[^\s\"'<>)\]}]+")
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

FOOTER = """

## System rules (CREATIVE HQ)
- You are one agent in a team. Your output is passed to other agents and finally to a human operator for approval.
- Never claim to have done something you did not actually do with a tool. Never fabricate sources, data or results.
- You cannot publish, post, send messages or modify any external client asset. Do not attempt to.
- Finish by returning your result through the StructuredOutput tool, matching the schema exactly.
"""


class TaskError(Exception):
    def __init__(self, message: str, retryable: bool = False, cost: float = 0.0):
        super().__init__(message)
        self.retryable = retryable
        self.cost = cost


@dataclass
class TaskResult:
    output: dict
    cost_usd: float
    session_id: str
    num_turns: int
    tool_calls: list[dict] = field(default_factory=list)
    search_urls: set[str] = field(default_factory=set)    # URLs returned by WebSearch
    fetched_urls: set[str] = field(default_factory=set)   # URLs successfully opened by WebFetch
    search_texts: list[str] = field(default_factory=list)  # raw WebSearch result text (to check quoted captions)
    denied_tools: list[str] = field(default_factory=list)


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


def normalize_url(url: str) -> str:
    return url.strip().rstrip(".,;:").rstrip("/").lower().replace("http://", "https://").replace("://www.", "://")


class AgentRunner:
    def __init__(self, event_log: Path):
        self.event_log = event_log
        event_log.parent.mkdir(parents=True, exist_ok=True)

    def _event(self, task_id: str, agent_id: str, kind: str, data: Any) -> None:
        with open(self.event_log, "a") as f:
            f.write(json.dumps({"task": task_id, "agent": agent_id, "kind": kind, "data": data}, ensure_ascii=False, default=str) + "\n")

    async def run(
        self,
        spec: AgentSpec,
        task_id: str,
        prompt: str,
        schema: dict,
        budget_usd: float,
        workdir: Path,
        readable_dirs: list[Path] | None = None,
    ) -> TaskResult:
        allowed = set(spec.tools) | {"StructuredOutput"}
        readable = [d.resolve() for d in (readable_dirs or [])]
        denied: list[str] = []

        async def can_use_tool(name: str, tool_input: dict, ctx: ToolPermissionContext):
            if name not in allowed:
                denied.append(name)
                self._event(task_id, spec.id, "permission_denied", {"tool": name, "input": tool_input})
                return PermissionResultDeny(message=f"{spec.id} is not allowed to use {name}.")
            if name == "Read":
                target = Path(tool_input.get("file_path", "")).resolve()
                if not any(target.is_relative_to(d) for d in readable):
                    denied.append(name)
                    self._event(task_id, spec.id, "permission_denied", {"tool": name, "input": tool_input})
                    return PermissionResultDeny(message="Read is limited to this run's deliverable folder.")
            return PermissionResultAllow()

        options = ClaudeAgentOptions(
            system_prompt=spec.instructions + FOOTER,
            model=spec.model,
            tools=list(spec.tools),
            can_use_tool=can_use_tool,
            max_turns=spec.max_turns,
            max_budget_usd=round(budget_usd, 4),
            output_format={"type": "json_schema", "schema": schema},
            setting_sources=[],          # do not load any local Claude settings or CLAUDE.md
            cwd=str(workdir),
            stderr=lambda line: log.debug("[%s cli] %s", spec.id, line.rstrip()),
        )

        result = TaskResult(output={}, cost_usd=0.0, session_id="", num_turns=0, denied_tools=denied)
        pending: dict[str, dict] = {}
        final: ResultMessage | None = None
        self._event(task_id, spec.id, "start", {"model": spec.model, "budget_usd": budget_usd, "tools": spec.tools})

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                call = {"id": block.id, "name": block.name, "input": block.input}
                                pending[block.id] = call
                                if block.name != "StructuredOutput":
                                    result.tool_calls.append(call)
                                    log.info("    %s -> %s %s", spec.id, block.name, json.dumps(block.input)[:160])
                                self._event(task_id, spec.id, "tool_call", call)
                            elif isinstance(block, TextBlock) and block.text.strip():
                                self._event(task_id, spec.id, "text", block.text[:2000])
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                self._collect_evidence(result, pending.get(block.tool_use_id), block)
                                self._event(task_id, spec.id, "tool_result", {
                                    "tool_use_id": block.tool_use_id,
                                    "is_error": block.is_error,
                                    "content": _result_text(block.content)[:3000],
                                })
                    elif isinstance(msg, ResultMessage):
                        final = msg
        except (CLIConnectionError, ProcessError) as e:
            raise TaskError(f"agent process error: {e}", retryable=True) from e

        if final is None:
            raise TaskError("agent ended without a result", retryable=True)

        result.cost_usd = float(final.total_cost_usd or 0.0)
        result.session_id = final.session_id
        result.num_turns = final.num_turns
        self._event(task_id, spec.id, "result", {
            "subtype": final.subtype, "is_error": final.is_error, "cost_usd": result.cost_usd,
            "turns": final.num_turns, "errors": final.errors, "api_error_status": final.api_error_status,
            "usage": final.usage, "model_usage": final.model_usage, "duration_api_ms": final.duration_api_ms,
        })

        if final.subtype == "error_max_budget_usd":
            raise TaskError(f"{spec.id} hit its spending limit (${budget_usd:.2f}) before finishing", cost=result.cost_usd)
        if final.subtype == "error_max_turns":
            raise TaskError(f"{spec.id} hit its turn limit ({spec.max_turns}) before finishing", retryable=True, cost=result.cost_usd)
        if final.is_error or final.subtype != "success":
            status = final.api_error_status
            raise TaskError(
                f"{spec.id} failed ({final.subtype}, status={status}): {final.errors or final.result}",
                retryable=status in RETRYABLE_STATUS if status else True,
                cost=result.cost_usd,
            )
        if not isinstance(final.structured_output, dict):
            raise TaskError(f"{spec.id} returned no structured result", retryable=True, cost=result.cost_usd)

        result.output = final.structured_output
        return result

    @staticmethod
    def _collect_evidence(result: TaskResult, call: dict | None, block: ToolResultBlock) -> None:
        if not call or block.is_error:
            return
        text = _result_text(block.content)
        if call["name"] == "WebSearch":
            result.search_urls.update(normalize_url(u) for u in URL_RE.findall(text))
            result.search_texts.append(text)
        elif call["name"] == "WebFetch":
            url = call["input"].get("url", "")
            if url:
                result.fetched_urls.add(normalize_url(url))


async def run_with_retries(runner: AgentRunner, retries: int, budget_fn, on_failed_attempt, **kwargs) -> TaskResult:
    """Retries transient failures.

    `budget_fn()` returns the dollars this attempt may spend (re-checked before every attempt,
    raises when the run/day limit is exhausted). `on_failed_attempt(cost)` records spend of failed attempts.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await runner.run(budget_usd=budget_fn(), **kwargs)
        except TaskError as e:
            on_failed_attempt(e.cost)
            if not e.retryable or attempt > retries:
                raise
            wait = 5 * attempt
            log.warning("    attempt %d failed (%s); retrying in %ds", attempt, e, wait)
            await asyncio.sleep(wait)
