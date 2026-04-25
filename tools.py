"""Core implementation for the Hermes swarm-agent plugin.

This plugin deliberately does NOT call Hermes' ``delegate_task``.  Hermes keeps
``delegate_task`` as a separate user-facing primitive, while this plugin provides
a removable, plugin-native async LLM swarm runtime for high-throughput map/reduce
workloads.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

# Direct pytest imports from ~/.hermes/plugins can shadow Hermes' real tools
# package because this plugin file is named tools.py. Put the Hermes checkout
# first when present; normal Hermes plugin loading already has it importable.
_HERMES_AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
if _HERMES_AGENT_ROOT.exists() and str(_HERMES_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_HERMES_AGENT_ROOT))

try:
    from tools.registry import registry, tool_error, tool_result
except Exception:  # pragma: no cover - only for very isolated test contexts
    registry = None

    def tool_error(message: str) -> str:
        return json.dumps({"error": message})

    def tool_result(payload: dict[str, Any]) -> str:
        return json.dumps(payload)

try:
    from .resource_guard import ResourceGuard
    from .run_store import RunStore
except Exception:  # Allows direct file import by tests/plugin loader.
    import importlib.util

    _base = Path(__file__).resolve().parent

    def _load_neighbor(name: str):
        spec = importlib.util.spec_from_file_location(f"swarm_agent_{name}", _base / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    ResourceGuard = _load_neighbor("resource_guard").ResourceGuard
    RunStore = _load_neighbor("run_store").RunStore


# ── Provider sweet spots (from live stress tests) ────────────────────────────
# These are the concurrency levels where each provider returns 100% success.
# The scheduler uses these as starting concurrency instead of blindly launching
# at max_concurrent and hoping retry logic catches the 429s.

PROVIDER_SWEET_SPOTS: dict[str | None, int] = {
    None: 50,              # unknown provider — conservative default
    "ollama-cloud": 100,   # tested 2026-04-25: 100/100 ok, 150→97%, 200→79%
    "xiaomi": 50,          # tested 2026-04-25: 50/50 ok, 100→25%
    "minimax": 0,          # instant 429 at any concurrency — unusable
    "openrouter": 80,      # estimated from aggregator limits
    "kimi": 50,            # conservative estimate
    "deepseek": 50,        # conservative estimate
    "openai-codex": 20,    # conservative for OAuth route
}


def _get_sweet_spot(provider: str | None) -> int:
    """Return the known concurrency sweet spot for a provider."""
    if provider and provider in PROVIDER_SWEET_SPOTS:
        return PROVIDER_SWEET_SPOTS[provider]
    return PROVIDER_SWEET_SPOTS[None]


DEFAULT_MAX_WORKERS = 300
DEFAULT_MAX_CONCURRENT = 100
HARD_MAX_WORKERS = 300
HARD_MAX_CONCURRENT = 300
DEFAULT_REDUCER_FAN_IN = 10
PLUGIN_RUNS_DIR = Path.home() / ".hermes" / "plugins" / "swarm-agent" / "runs"


SWARM_TASK_SCHEMA = {
    "name": "swarm_task",
    "description": (
        "Run a plugin-native async LLM swarm over many independent work items. "
        "This does not use Hermes delegate_task. Use for wide/decomposable tasks: "
        "codebase audits, multi-source research, scenario testing, or parallel critique."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Overall objective for the swarm."},
            "context": {"type": "string", "description": "Shared background/context every worker receives."},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Independent items to assign to workers: file paths, URLs, questions, modules, scenarios, etc.",
            },
            "mode": {
                "type": "string",
                "enum": ["llm_only"],
                "default": "llm_only",
                "description": "Current plugin-native mode. High-concurrency workers are LLM-only; delegate_task remains separate.",
            },
            "strategy": {
                "type": "string",
                "enum": ["map_reduce", "fanout"],
                "default": "map_reduce",
                "description": "fanout returns worker results; map_reduce adds reducer/final synthesis passes.",
            },
            "max_workers": {
                "type": "integer",
                "minimum": 1,
                "maximum": HARD_MAX_WORKERS,
                "default": DEFAULT_MAX_WORKERS,
                "description": "Total lightweight LLM workers to run. Hard-capped at 300.",
            },
            "max_concurrent": {
                "type": "integer",
                "minimum": 1,
                "maximum": HARD_MAX_CONCURRENT,
                "default": DEFAULT_MAX_CONCURRENT,
                "description": "Maximum simultaneous LLM worker coroutines per wave. Hard-capped at 300.",
            },
            "verifier_count": {
                "type": "integer",
                "minimum": 0,
                "maximum": 5,
                "default": 0,
                "description": "Optional verifier LLM calls to critique the reduced synthesis.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 5,
                "maximum": 3600,
                "default": 900,
                "description": "Global swarm timeout.",
            },
            "worker_timeout_seconds": {
                "type": "integer",
                "minimum": 5,
                "maximum": 600,
                "default": 180,
                "description": "Per-worker LLM call timeout.",
            },
            "provider": {"type": "string", "description": "Optional provider override for LLM workers."},
            "model": {"type": "string", "description": "Optional model override for LLM workers."},
            "allow_300_live": {
                "type": "boolean",
                "default": False,
                "description": "Required for direct tool calls with concurrency above 100. /swarm command auto-sets this.",
            },
            "dry_run": {"type": "boolean", "default": False, "description": "Return the plan without calling LLMs."},
        },
        "required": ["goal"],
    },
}


def _coerce_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _clean_string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _make_generic_sources(max_workers: int) -> list[str]:
    perspectives = [
        "facts and constraints",
        "risks and failure modes",
        "implementation architecture",
        "testing and verification",
        "cost and resource analysis",
        "security and safety review",
        "user experience and operations",
        "edge cases and alternatives",
    ]
    if max_workers <= len(perspectives):
        return perspectives[:max_workers]
    return [f"worker perspective {i + 1}: {perspectives[i % len(perspectives)]}" for i in range(max_workers)]


def build_work_items(
    *,
    goal: str,
    context: str = "",
    sources: Optional[Iterable[str]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[dict[str, Any]]:
    """Build compact, independent LLM worker inputs."""
    clean_goal = str(goal or "").strip()
    clean_context = str(context or "").strip()
    source_items = _clean_string_list(list(sources) if sources is not None else None)
    if not source_items:
        source_items = _make_generic_sources(max_workers)
    selected_sources = source_items[:max_workers]
    total_sources = len(source_items)
    return [
        {
            "worker_id": f"worker-{index:03d}",
            "index": index,
            "total_workers": len(selected_sources),
            "total_sources": total_sources,
            "goal": clean_goal,
            "context": clean_context,
            "source": source,
        }
        for index, source in enumerate(selected_sources, start=1)
    ]


# Backwards-compatible helper name for older dry-run callers/tests.
def build_worker_tasks(**kwargs) -> list[dict[str, Any]]:
    return [
        {**item, "prompt": _build_worker_prompt(item)}
        for item in build_work_items(**{k: v for k, v in kwargs.items() if k in {"goal", "context", "sources", "max_workers"}})
    ]


def _build_worker_prompt(item: dict[str, Any]) -> str:
    return (
        f"Swarm worker {item['index']}/{item['total_workers']} for objective: {item['goal']}\n\n"
        f"Source {item['index']}/{item['total_sources']}: {item['source']}\n\n"
        f"Shared context:\n{item.get('context') or '(none)'}\n\n"
        "Work independently. Return concise, evidence-grounded output with:\n"
        "1. Key findings\n2. Evidence or inspected items\n3. Risks/uncertainties\n4. Recommended next action\n"
        "Do not claim tool/file access unless the prompt itself provides evidence."
    )


async def _async_llm_call(
    *,
    worker_id: str,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    timeout_seconds: int = 180,
    max_tokens: int = 700,
    temperature: float = 0.2,
    role: str = "worker",
) -> dict[str, Any]:
    """Single LLM call wrapper used by swarm workers/reducers.

    Tests monkeypatch this function, so the 300-concurrency stress test never
    burns real provider calls.
    """
    from agent.auxiliary_client import async_call_llm

    messages = [
        {
            "role": "system",
            "content": (
                "You are a lightweight worker in a Hermes swarm. Be concise, "
                "independent, evidence-grounded, and avoid unsupported claims."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    response = await asyncio.wait_for(
        async_call_llm(
            task="swarm",
            provider=provider or None,
            model=model or None,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
        ),
        timeout=timeout_seconds + 5,
    )
    msg = response.choices[0].message
    content = (getattr(msg, "content", None) or "").strip()
    return {
        "worker_id": worker_id,
        "role": role,
        "status": "ok",
        "content": content,
    }


async def _run_workers(
    *,
    work_items: list[dict[str, Any]],
    max_concurrent: int,
    provider: str | None,
    model: str | None,
    worker_timeout_seconds: int,
    run_store: RunStore,
    max_retries: int = 3,
    global_timeout: float = 900.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run workers in waves with provider-aware concurrency and adaptive retry.

    Strategy:
    1. Start at min(max_concurrent, provider_sweet_spot) — not at max_concurrent
    2. Run waves of that size until all workers complete
    3. If 429s hit, reduce concurrency and retry failed workers with jittered backoff
    4. After a clean wave (0 failures), try bumping concurrency back up
    5. Track all metrics for observability
    """
    sweet_spot = _get_sweet_spot(provider)
    # Start at the lower of user's max_concurrent and the provider sweet spot.
    # This avoids the wasteful "launch 300, get 150 429s, retry" pattern.
    initial_concurrent = min(max_concurrent, sweet_spot) if sweet_spot > 0 else min(max_concurrent, 25)
    current_concurrent = initial_concurrent

    started = time.monotonic()
    peak = 0
    active = 0
    lock = asyncio.Lock()
    all_results: list[dict[str, Any]] = []
    pending_items = list(work_items)
    wave_number = 0
    total_retries = 0
    max_concurrent_used = 0

    async def run_one(item: dict[str, Any], sem: asyncio.Semaphore) -> dict[str, Any]:
        nonlocal active, peak
        async with sem:
            async with lock:
                active += 1
                peak = max(peak, active)
            try:
                result = await asyncio.wait_for(
                    _async_llm_call(
                        worker_id=item["worker_id"],
                        prompt=_build_worker_prompt(item),
                        provider=provider,
                        model=model,
                        timeout_seconds=worker_timeout_seconds,
                        role="worker",
                    ),
                    timeout=worker_timeout_seconds + 10,
                )
            except Exception as exc:
                result = {
                    "worker_id": item["worker_id"],
                    "role": "worker",
                    "status": "failed",
                    "error": str(exc),
                }
            finally:
                async with lock:
                    active -= 1
            await run_store.write_worker_result(result)
            return result

    while pending_items and (time.monotonic() - started) < global_timeout:
        wave_number += 1
        wave_items = pending_items[:current_concurrent]
        pending_items = pending_items[current_concurrent:]
        max_concurrent_used = max(max_concurrent_used, current_concurrent)

        sem = asyncio.Semaphore(current_concurrent)
        wave_results = await asyncio.gather(*(run_one(item, sem) for item in wave_items))
        wave_ok = [r for r in wave_results if r.get("status") == "ok"]
        wave_failed = [r for r in wave_results if r.get("status") != "ok"]

        all_results.extend(wave_ok)

        if not wave_failed:
            # Clean wave — try bumping concurrency back toward max_concurrent
            if pending_items and current_concurrent < max_concurrent:
                current_concurrent = min(max_concurrent, current_concurrent + 10)
            continue

        # Some workers failed — collect for retry
        failed_ids = {r["worker_id"] for r in wave_failed}
        retry_items = [item for item in wave_items if item["worker_id"] in failed_ids]
        rate_limited = any("429" in str(r.get("error", "")) for r in wave_failed)

        if rate_limited:
            # Rate limited — reduce concurrency and back off
            current_concurrent = max(5, current_concurrent * 2 // 3)
            backoff = min(2 ** min(wave_number, 4), 16) + random.uniform(0, 2)
            await asyncio.sleep(backoff)
        else:
            # Non-rate-limit errors — small backoff, keep concurrency
            await asyncio.sleep(1)

        if total_retries < max_retries * len(work_items):
            # Stagger retries: spread them across the next wave instead of dumping all at front
            # This prevents retry stampedes
            if len(retry_items) <= current_concurrent:
                pending_items = retry_items + pending_items
            else:
                # Interleave retries with remaining items to spread load
                pending_items = retry_items + pending_items
            total_retries += len(retry_items)
        else:
            # Max retries exhausted — record as failed
            all_results.extend(wave_failed)

    # Any items still unprocessed after global timeout
    for item in pending_items:
        all_results.append({
            "worker_id": item["worker_id"],
            "role": "worker",
            "status": "timeout",
            "error": "global timeout exceeded",
        })

    observability = {
        "peak_concurrency": peak,
        "max_concurrent_used": max_concurrent_used,
        "initial_concurrent": initial_concurrent,
        "provider_sweet_spot": sweet_spot,
        "waves": wave_number,
        "total_retries": total_retries,
        "duration_seconds": round(time.monotonic() - started, 3),
        "completed": sum(1 for r in all_results if r.get("status") == "ok"),
        "failed": sum(1 for r in all_results if r.get("status") != "ok"),
    }
    return all_results, observability


def _summaries(results: list[dict[str, Any]], max_chars: int = 6000) -> list[str]:
    out = []
    for result in results:
        label = result.get("worker_id", "unknown")
        status = result.get("status", "unknown")
        body = result.get("content") or result.get("error") or ""
        out.append(f"[{label} status={status}]\n{str(body)[:max_chars]}")
    return out


async def _reduce_results(
    *,
    goal: str,
    worker_results: list[dict[str, Any]],
    provider: str | None,
    model: str | None,
    worker_timeout_seconds: int,
    run_store: RunStore,
    verifier_count: int,
) -> dict[str, Any]:
    summaries = _summaries(worker_results)
    reducer_outputs: list[dict[str, Any]] = []
    for idx, group in enumerate(_chunked(summaries, DEFAULT_REDUCER_FAN_IN), start=1):
        prompt = (
            f"Reduce swarm worker outputs for objective: {goal}\n\n"
            "Merge duplicates, preserve disagreements, flag missing evidence, and produce a compact synthesis.\n\n"
            + "\n\n---\n\n".join(group)
        )
        result = await _async_llm_call(
            worker_id=f"reducer-{idx:03d}",
            prompt=prompt,
            provider=provider,
            model=model,
            timeout_seconds=worker_timeout_seconds,
            max_tokens=1200,
            role="reducer",
        )
        await run_store.write_reducer_result(result)
        reducer_outputs.append(result)

    final_input = _summaries(reducer_outputs or worker_results)
    final = await _async_llm_call(
        worker_id="final-synthesizer",
        prompt=(
            f"Create the final answer for swarm objective: {goal}\n\n"
            "Use the reduced swarm outputs below. Be concise, practical, and explicit about confidence/uncertainty.\n\n"
            + "\n\n---\n\n".join(final_input)
        ),
        provider=provider,
        model=model,
        timeout_seconds=worker_timeout_seconds,
        max_tokens=1600,
        role="finalizer",
    )

    verifiers: list[dict[str, Any]] = []
    for idx in range(verifier_count):
        verifier = await _async_llm_call(
            worker_id=f"verifier-{idx + 1:03d}",
            prompt=(
                f"Verify this swarm synthesis for objective: {goal}\n\n"
                "Look for contradictions, unsupported claims, missing risks, and actionability.\n\n"
                f"SYNTHESIS:\n{final.get('content', '')}"
            ),
            provider=provider,
            model=model,
            timeout_seconds=worker_timeout_seconds,
            max_tokens=900,
            role="verifier",
        )
        verifiers.append(verifier)

    payload = {"reducer_results": reducer_outputs, "final_result": final, "verifier_results": verifiers}
    await run_store.write_final(payload)
    return payload


async def _run_swarm_async(
    *,
    goal: str,
    context: str,
    sources: list[str] | None,
    strategy: str,
    max_workers: int,
    max_concurrent: int,
    verifier_count: int,
    timeout_seconds: int,
    worker_timeout_seconds: int,
    provider: str | None,
    model: str | None,
    run_id: str,
) -> dict[str, Any]:
    work_items = build_work_items(goal=goal, context=context, sources=sources, max_workers=max_workers)
    store = RunStore(PLUGIN_RUNS_DIR, run_id)
    await store.write_event({"event": "started", "workers": len(work_items), "max_concurrent": max_concurrent})
    worker_results, obs = await asyncio.wait_for(
        _run_workers(
            work_items=work_items,
            max_concurrent=max_concurrent,
            provider=provider,
            model=model,
            worker_timeout_seconds=worker_timeout_seconds,
            run_store=store,
            global_timeout=float(timeout_seconds) * 0.8,
        ),
        timeout=timeout_seconds,
    )
    await store.write_event({"event": "workers_completed", **obs})

    reduced: dict[str, Any] | None = None
    synthesis: str = ""
    if strategy == "map_reduce":
        reduced = await _reduce_results(
            goal=goal,
            worker_results=worker_results,
            provider=provider,
            model=model,
            worker_timeout_seconds=worker_timeout_seconds,
            run_store=store,
            verifier_count=verifier_count,
        )
        synthesis = (reduced or {}).get("final_result", {}).get("content", "")

    return {
        "worker_results": worker_results,
        "reduction": reduced,
        "synthesis": synthesis,
        "observability": obs,
        "run_dir": str(store.run_dir),
    }


def _plan_payload(
    *,
    strategy: str,
    total_workers: int,
    max_concurrent: int,
    verifier_count: int,
    resource_decision: Any = None,
    provider_sweet_spot: int = 0,
) -> dict[str, Any]:
    effective_concurrent = min(max_concurrent, provider_sweet_spot) if provider_sweet_spot > 0 else max_concurrent
    payload = {
        "strategy": strategy,
        "mode": "llm_only",
        "total_workers": total_workers,
        "max_concurrent": max_concurrent,
        "effective_concurrent": effective_concurrent,
        "provider_sweet_spot": provider_sweet_spot,
        "wave_count_estimate": math.ceil(total_workers / max(1, effective_concurrent)),
        "verifier_count": verifier_count,
        "resource_guard": getattr(resource_decision, "__dict__", resource_decision),
    }
    return payload


def _run_coro(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Hermes tool handlers are synchronous. If this ever runs inside an existing
    # event loop, use a short-lived helper thread with its own loop.
    import threading

    box: dict[str, Any] = {}

    def runner():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def swarm_task(
    *,
    goal: Optional[str] = None,
    context: Optional[str] = None,
    sources: Optional[list[str]] = None,
    mode: str = "llm_only",
    strategy: str = "map_reduce",
    max_workers: Any = DEFAULT_MAX_WORKERS,
    max_concurrent: Any = DEFAULT_MAX_CONCURRENT,
    verifier_count: Any = 0,
    timeout_seconds: Any = 900,
    worker_timeout_seconds: Any = 180,
    provider: str | None = None,
    model: str | None = None,
    allow_300_live: Any = False,
    dry_run: Any = False,
    parent_agent=None,  # Kept for Hermes plugin handler compatibility; unused.
    **_ignored: Any,
) -> str:
    """Run a delegate-free, plugin-native async LLM swarm."""
    clean_goal = str(goal or "").strip()
    if not clean_goal:
        return tool_error("goal is required")

    selected_mode = str(mode or "llm_only").strip().lower()
    if selected_mode != "llm_only":
        return tool_error("swarm-agent currently supports mode=llm_only only; delegate_task remains separate")

    selected_strategy = str(strategy or "map_reduce").strip().lower()
    if selected_strategy not in {"map_reduce", "fanout"}:
        return tool_error("strategy must be one of: map_reduce, fanout")

    total_workers = _coerce_int(max_workers, DEFAULT_MAX_WORKERS, 1, HARD_MAX_WORKERS)
    requested_concurrent = _coerce_int(max_concurrent, DEFAULT_MAX_CONCURRENT, 1, HARD_MAX_CONCURRENT)
    requested_concurrent = min(requested_concurrent, total_workers)
    verifiers = _coerce_int(verifier_count, 0, 0, 5)
    timeout = _coerce_int(timeout_seconds, 900, 5, 3600)
    worker_timeout = _coerce_int(worker_timeout_seconds, 180, 5, 600)
    allow_high = bool(allow_300_live)
    dry = bool(dry_run)

    # Only gate when called directly as a tool (not via /swarm which sets allow_300_live=True)
    sweet_spot = _get_sweet_spot(provider)
    if not allow_high and not dry and sweet_spot > 0 and requested_concurrent > sweet_spot:
        return tool_result(
            {
                "success": False,
                "error": (
                    f"max_concurrent={requested_concurrent} exceeds provider sweet spot ({sweet_spot}). "
                    f"Set allow_300_live=true or use max_concurrent={sweet_spot}."
                ),
                "uses_delegate_task": False,
            }
        )

    guard = ResourceGuard()
    resource_decision = guard.decide_concurrency(requested_concurrent)
    concurrent = resource_decision.adjusted

    work_items = build_work_items(goal=clean_goal, context=context or "", sources=sources, max_workers=total_workers)
    plan = _plan_payload(
        strategy=selected_strategy,
        total_workers=len(work_items),
        max_concurrent=concurrent,
        verifier_count=verifiers,
        resource_decision=resource_decision,
        provider_sweet_spot=sweet_spot,
    )

    if dry:
        return tool_result(
            {
                "success": True,
                "dry_run": True,
                "architecture": "plugin_native_async_llm_swarm",
                "uses_delegate_task": False,
                "plan": plan,
                "work_items_preview": work_items[: min(5, len(work_items))],
            }
        )

    run_id = f"swarm-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    try:
        data = _run_coro(
            _run_swarm_async(
                goal=clean_goal,
                context=context or "",
                sources=sources,
                strategy=selected_strategy,
                max_workers=total_workers,
                max_concurrent=concurrent,
                verifier_count=verifiers,
                timeout_seconds=timeout,
                worker_timeout_seconds=worker_timeout,
                provider=provider,
                model=model,
                run_id=run_id,
            )
        )
    except asyncio.TimeoutError:
        return tool_result(
            {
                "success": False,
                "error": "swarm_task timed out",
                "architecture": "plugin_native_async_llm_swarm",
                "uses_delegate_task": False,
                "plan": plan,
                "run_id": run_id,
                "total_duration_seconds": round(time.monotonic() - started, 2),
            }
        )
    except Exception as exc:
        return tool_result(
            {
                "success": False,
                "error": str(exc),
                "architecture": "plugin_native_async_llm_swarm",
                "uses_delegate_task": False,
                "plan": plan,
                "run_id": run_id,
                "total_duration_seconds": round(time.monotonic() - started, 2),
            }
        )

    worker_results = data["worker_results"]
    return tool_result(
        {
            "success": True,
            "architecture": "plugin_native_async_llm_swarm",
            "uses_delegate_task": False,
            "strategy": selected_strategy,
            "synthesis": data.get("synthesis", ""),
            "plan": plan,
            "run_id": run_id,
            "run_dir": data["run_dir"],
            "worker_results_count": len(worker_results),
            "worker_results_preview": worker_results[: min(10, len(worker_results))],
            "reduction": data.get("reduction"),
            "observability": data["observability"],
            "total_duration_seconds": round(time.monotonic() - started, 2),
        }
    )
