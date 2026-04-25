"""Uninstallable swarm-agent plugin for Hermes Agent.

The plugin registers one tool, ``swarm_task``, under the ``swarm`` toolset.
Install location is intentionally under ``~/.hermes/plugins/swarm-agent`` so it
can be removed later with ``hermes plugins remove swarm-agent`` or by deleting
that directory.
"""

from __future__ import annotations

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


def register(ctx) -> None:
    """Register the swarm_task tool with Hermes' plugin context."""
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
