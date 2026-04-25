"""Tests for the uninstallable Hermes swarm-agent plugin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_swarm_command():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")

    assert "name: swarm-agent" in manifest
    assert "kind: standalone" in manifest
    assert "swarm" in manifest
    assert "provides_commands" in manifest


def test_swarm_task_schema_has_delegate_free_300_defaults():
    tools = _load_module("tools.py", "swarm_agent_tools_test_schema")
    schema = tools.SWARM_TASK_SCHEMA
    props = schema["parameters"]["properties"]

    assert schema["name"] == "swarm_task"
    assert props["strategy"]["enum"] == ["map_reduce", "fanout"]
    assert props["mode"]["enum"] == ["llm_only"]
    assert props["max_workers"]["default"] == 300
    assert props["max_workers"]["maximum"] == 300
    assert props["max_concurrent"]["default"] == 100
    assert props["max_concurrent"]["maximum"] == 300
    assert props["dry_run"]["default"] is False


def test_build_work_items_splits_sources_and_applies_worker_limits():
    tools = _load_module("tools.py", "swarm_agent_tools_test_tasks")

    items = tools.build_work_items(
        goal="Audit these files",
        context="Repo root: /tmp/project",
        sources=["a.py", "b.py", "c.py"],
        max_workers=2,
    )

    assert len(items) == 2
    assert items[0]["worker_id"] == "worker-001"
    assert items[0]["source"] == "a.py"
    assert items[0]["context"] == "Repo root: /tmp/project"
    assert items[1]["source"] == "b.py"


def test_dry_run_returns_plan_without_parent_agent_or_delegate_task():
    tools = _load_module("tools.py", "swarm_agent_tools_test_dry_run")

    result = json.loads(
        tools.swarm_task(
            goal="Research local competitors",
            sources=["site-a", "site-b", "site-c"],
            max_workers=3,
            max_concurrent=2,
            dry_run=True,
            parent_agent=None,
        )
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["architecture"] == "plugin_native_async_llm_swarm"
    assert result["uses_delegate_task"] is False
    assert result["plan"]["total_workers"] == 3
    assert result["plan"]["max_concurrent"] == 2


def test_non_dry_run_does_not_require_parent_agent_context(monkeypatch):
    tools = _load_module("tools.py", "swarm_agent_tools_test_no_parent")

    async def fake_llm_call(*, worker_id, prompt, **kwargs):
        return {"worker_id": worker_id, "status": "ok", "content": f"done {worker_id}"}

    monkeypatch.setattr(tools, "_async_llm_call", fake_llm_call)

    result = json.loads(
        tools.swarm_task(
            goal="Do work",
            sources=["a", "b"],
            max_workers=2,
            max_concurrent=2,
            strategy="fanout",
            dry_run=False,
            parent_agent=None,
        )
    )

    assert result["success"] is True
    assert result["uses_delegate_task"] is False
    assert result["worker_results_count"] == 2


def test_swarm_task_never_dispatches_delegate_task(monkeypatch):
    tools = _load_module("tools.py", "swarm_agent_tools_test_no_delegate")

    def forbidden_dispatch(name, *args, **kwargs):
        if name == "delegate_task":
            raise AssertionError("swarm_task must not dispatch delegate_task")
        return json.dumps({"success": True})

    async def fake_llm_call(*, worker_id, prompt, **kwargs):
        return {"worker_id": worker_id, "status": "ok", "content": "ok"}

    monkeypatch.setattr(tools.registry, "dispatch", forbidden_dispatch)
    monkeypatch.setattr(tools, "_async_llm_call", fake_llm_call)

    result = json.loads(
        tools.swarm_task(
            goal="No delegate",
            sources=["a", "b", "c"],
            max_workers=3,
            max_concurrent=3,
            strategy="fanout",
        )
    )

    assert result["success"] is True
    assert result["uses_delegate_task"] is False


def test_fake_llm_scheduler_completes_300_workers(monkeypatch):
    """The scheduler should complete all 300 workers using provider-aware waves.

    With no provider specified, sweet_spot=50. So the scheduler runs waves of
    50, completing 300 workers in 6 waves. All 300 should succeed.
    """
    tools = _load_module("tools.py", "swarm_agent_tools_test_300")

    completed_workers = set()

    async def fake_llm_call(*, worker_id, prompt, **kwargs):
        completed_workers.add(worker_id)
        await asyncio.sleep(0.01)
        return {"worker_id": worker_id, "status": "ok", "content": "ok"}

    monkeypatch.setattr(tools, "_async_llm_call", fake_llm_call)

    result = json.loads(
        tools.swarm_task(
            goal="Stress fake workers",
            sources=[f"item-{i}" for i in range(300)],
            max_workers=300,
            max_concurrent=300,
            allow_300_live=True,
            strategy="fanout",
            timeout_seconds=60,
        )
    )

    assert result["success"] is True
    assert result["worker_results_count"] == 300
    assert result["observability"]["completed"] == 300
    assert result["observability"]["failed"] == 0
    assert len(completed_workers) == 300


def test_300_concurrency_requires_explicit_opt_in():
    tools = _load_module("tools.py", "swarm_agent_tools_test_opt_in")

    result = json.loads(
        tools.swarm_task(
            goal="Unsafe by default",
            sources=[f"item-{i}" for i in range(300)],
            max_workers=300,
            max_concurrent=300,
            allow_300_live=False,
            dry_run=False,
        )
    )

    assert result["success"] is False
    assert "sweet spot" in result["error"]


def test_run_store_writes_valid_jsonl_under_300_concurrent(tmp_path):
    runtime = _load_module("run_store.py", "swarm_agent_run_store_test")

    async def write_all():
        store = runtime.RunStore(tmp_path, "test-run")
        await asyncio.gather(
            *[
                store.write_worker_result({"worker_id": f"w-{i}", "status": "ok"})
                for i in range(300)
            ]
        )
        return store

    store = asyncio.run(write_all())
    lines = store.worker_results_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 300
    parsed = [json.loads(line) for line in lines]
    assert {item["worker_id"] for item in parsed} == {f"w-{i}" for i in range(300)}


def test_resource_guard_reduces_concurrency_when_fd_limit_low(monkeypatch):
    guard_mod = _load_module("resource_guard.py", "swarm_agent_resource_guard_test")

    monkeypatch.setattr(guard_mod.resource, "getrlimit", lambda _limit: (256, 256))

    guard = guard_mod.ResourceGuard()
    adjusted = guard.adjust_concurrency(300)

    assert adjusted < 300
    assert adjusted >= 1
