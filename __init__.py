"""Uninstallable swarm-agent plugin for Hermes Agent.

The plugin registers:
- Tool: ``swarm_task`` under the ``swarm`` toolset (for agent-initiated calls)
- Slash command: ``/swarm`` (for user-initiated calls)

Install location: ``~/.hermes/plugins/swarm-agent``
Uninstall: ``hermes plugins disable swarm-agent && hermes plugins remove swarm-agent``
"""

from __future__ import annotations

import json

try:
    from .tools import SWARM_TASK_SCHEMA, swarm_task
except ImportError:  # Allows direct file import from plugin loaders/tests.
    import importlib.util
    from pathlib import Path

    _tools_path = Path(__file__).with_name("tools.py")
    _spec = importlib.util.spec_from_file_location("swarm_agent_plugin_tools", _tools_path)
    if _spec is None or _spec.loader is None:  # pragma: no cover
        raise
    _tools = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tools)
    SWARM_TASK_SCHEMA = _tools.SWARM_TASK_SCHEMA
    swarm_task = _tools.swarm_task


def _parse_swarm_args(raw_args: str) -> dict:
    """Parse user-friendly slash command args into swarm_task parameters.

    Supported syntax:
        /swarm <goal>
        /swarm provider:ollama-cloud workers:100 concurrent:50 <goal>
        /swarm strategy:fanout workers:300 concurrent:100 <goal>
        /swarm dry_run <goal>
        /swarm help

    All params before the goal text are key:value pairs. Everything after
    the last key:value (or the whole string if no pairs) is the goal.
    """
    if not raw_args or not raw_args.strip():
        return {"error": "Usage: /swarm [options] <goal>\nRun /swarm help for details."}

    text = raw_args.strip()

    if text.lower() in ("help", "?"):
        return {
            "help": True,
            "text": (
                "🐝 /swarm — Run a parallel LLM research swarm\n\n"
                "Usage: /swarm [options] <your research goal>\n\n"
                "Options (key:value, before the goal):\n"
                "  provider:<name>    LLM provider (ollama-cloud, xiaomi, etc.)\n"
                "  model:<name>       Model override\n"
                "  workers:<N>        Total workers (default: 25, max: 300)\n"
                "  concurrent:<N>     Max concurrent (default: 25, max: 300)\n"
                "  strategy:<type>    map_reduce (default) or fanout\n"
                "  verifiers:<N>      Verifier count (default: 0, max: 5)\n"
                "  timeout:<N>        Global timeout in seconds (default: 900)\n"
                "  dry_run            Plan only, don't execute\n\n"
                "Examples:\n"
                "  /swarm Evaluate the top 10 AI agent frameworks\n"
                "  /swarm provider:ollama-cloud workers:100 Audit all Python files for security\n"
                "  /swarm workers:300 concurrent:100 strategy:fanout Research 300 competitors\n"
                "  /swarm dry_run What sources would you analyze for market research?\n"
            ),
        }

    # Parse key:value pairs from the front
    params = {}
    parts = text.split()
    goal_start = 0

    for i, part in enumerate(parts):
        if ":" in part:
            key, _, val = part.partition(":")
            key = key.lower().strip()
            val = val.strip()
            if key in ("provider", "model", "strategy"):
                params[key] = val
            elif key in ("workers", "concurrent", "verifiers", "timeout"):
                try:
                    params[key] = int(val)
                except ValueError:
                    # Not a valid number — treat as part of goal
                    goal_start = i
                    break
            elif key == "worker" or key == "conc":
                try:
                    params["workers" if key == "worker" else "concurrent"] = int(val)
                except ValueError:
                    goal_start = i
                    break
            else:
                # Unknown key — treat rest as goal
                goal_start = i
                break
            goal_start = i + 1
        elif part.lower() == "dry_run":
            params["dry_run"] = True
            goal_start = i + 1
        else:
            # First non-key:value word — everything from here is the goal
            goal_start = i
            break

    goal = " ".join(parts[goal_start:]).strip()
    if not goal:
        return {"error": "No goal provided. Usage: /swarm [options] <your research goal>"}

    params["goal"] = goal
    return params


def _handle_swarm_command(raw_args: str) -> str:
    """Handler for the /swarm slash command."""
    params = _parse_swarm_args(raw_args)

    if params.get("error"):
        return params["error"]

    if params.get("help"):
        return params["text"]

    result = json.loads(swarm_task(
        goal=params.get("goal", ""),
        provider=params.get("provider"),
        model=params.get("model"),
        max_workers=params.get("workers", 50),
        max_concurrent=params.get("concurrent", 50),
        strategy=params.get("strategy", "map_reduce"),
        verifier_count=params.get("verifiers", 0),
        timeout_seconds=params.get("timeout", 900),
        allow_300_live=True,  # User explicitly invoked /swarm, they know what they're doing
        dry_run=params.get("dry_run", False),
    ))

    if not result.get("success"):
        return f"Swarm failed: {result.get('error', 'unknown error')}"

    if result.get("dry_run"):
        plan = result.get("plan", {})
        lines = [
            "🐝 **Swarm Dry Run**",
            f"Strategy: {plan.get('strategy')}",
            f"Workers: {plan.get('total_workers')}",
            f"Max concurrent: {plan.get('max_concurrent')}",
            f"Waves: {plan.get('wave_count_estimate')}",
            f"Verifiers: {plan.get('verifier_count')}",
            "",
            "Preview (first 5 work items):",
        ]
        for item in result.get("work_items_preview", []):
            lines.append(f"  - {item.get('worker_id')}: {item.get('source', '')[:60]}")
        return "\n".join(lines)

    # Build human-readable output
    obs = result.get("observability", {})
    synthesis = result.get("synthesis", "")
    workers = result.get("worker_results_count", 0)
    duration = result.get("total_duration_seconds", 0)

    lines = [
        f"🐝 **Swarm Complete** — {workers} workers in {duration:.0f}s",
        f"Completed: {obs.get('completed', 0)} | Failed: {obs.get('failed', 0)} | "
        f"Waves: {obs.get('waves', 0)} | Retries: {obs.get('total_retries', 0)}",
        "",
    ]

    if synthesis:
        lines.append(synthesis)
    else:
        # fanout mode — show preview of worker results
        lines.append("**Worker Results:**")
        for wr in result.get("worker_results_preview", [])[:10]:
            status = wr.get("status", "?")
            wid = wr.get("worker_id", "?")
            content = (wr.get("content") or wr.get("error") or "")[:200]
            lines.append(f"\n**[{wid}] {status}**\n{content}")

    return "\n".join(lines)


def register(ctx) -> None:
    """Register swarm tool and /swarm slash command with Hermes."""

    # Tool — used by the agent when it decides to call swarm_task
    ctx.register_tool(
        name="swarm_task",
        toolset="swarm",
        schema=SWARM_TASK_SCHEMA,
        handler=lambda args, **kw: swarm_task(
            goal=args.get("goal"),
            context=args.get("context"),
            sources=args.get("sources"),
            mode=args.get("mode", "llm_only"),
            strategy=args.get("strategy", "map_reduce"),
            max_workers=args.get("max_workers", 25),
            max_concurrent=args.get("max_concurrent", 25),
            verifier_count=args.get("verifier_count", 0),
            timeout_seconds=args.get("timeout_seconds", 900),
            worker_timeout_seconds=args.get("worker_timeout_seconds", 180),
            provider=args.get("provider"),
            model=args.get("model"),
            allow_300_live=args.get("allow_300_live", False),
            dry_run=args.get("dry_run", False),
            parent_agent=kw.get("parent_agent"),
        ),
        emoji="🐝",
    )

    # Slash command — /swarm, user-initiated
    ctx.register_command(
        name="swarm",
        handler=_handle_swarm_command,
        description="Run a parallel LLM research swarm (up to 300 workers)",
        args_hint="[options] <goal>",
    )
