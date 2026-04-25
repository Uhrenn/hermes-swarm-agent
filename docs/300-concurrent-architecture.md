# Swarm Agent Plugin: 300-Concurrent Independent Architecture

> Design target: make `~/.hermes/plugins/swarm-agent` capable of **up to 300 concurrent swarm workers** without using Hermes core `delegate_task` and without modifying Hermes core. The core `delegate_task` primitive must stay untouched.

## Executive conclusion

300 concurrent *full Hermes `AIAgent` instances* inside one Discord/gateway request is not reliable on this machine or typical Hermes installs. A reliable 300-concurrent swarm plugin should instead use a purpose-built **async swarm runtime**:

- one process, one asyncio event loop
- bounded task scheduler, not 300 OS threads
- shared async HTTP client pool and per-provider token/request limiters
- mostly **LLM-only workers** for high concurrency
- optional restricted tool workers through a separate small tool-lane, not all 300
- append-only run state under the plugin directory, not Hermes session DB writes per worker
- reducers/verifiers/finalizer after worker completion

This architecture supports 300 concurrent worker coroutines when the upstream provider/account has enough request/token quota. It will not magically bypass provider rate limits; it should auto-throttle down when headers/errors show insufficient capacity.

## Findings from current Hermes/plugin inspection

### Current swarm plugin

File: `~/.hermes/plugins/swarm-agent/tools.py`

The existing plugin currently uses `delegate_task` explicitly:

- `_call_delegate()` calls `registry.dispatch("delegate_task", {"tasks": tasks}, parent_agent=parent_agent)`.
- `swarm_task()` rejects runtime use if `delegate_task` is not registered/enabled.
- hard caps are currently `HARD_MAX_WORKERS = 300`, `HARD_MAX_CONCURRENT = 30`.

This does not meet the new requirement. The plugin needs its own runtime and should remove all `delegate_task` dependency.

### Relevant Hermes internals that can be reused without touching `delegate_task`

Safe to reuse as library APIs:

- `agent.auxiliary_client.async_call_llm()` — centralized async LLM call path; resolves providers, auth, model, custom endpoint, Codex/Responses wrapper, Anthropic-compatible adapters, and fallback behavior.
- `agent.auxiliary_client.resolve_provider_client(..., async_mode=True)` and `_build_call_kwargs()` — lower-level provider/client helpers if direct control over response headers and streaming is needed.
- `model_tools.get_tool_definitions(enabled_toolsets=...)` — schemas for restricted tool lanes.
- `model_tools.handle_function_call(...)` or `registry.dispatch(...)` — tool execution, but only in a small bounded lane.
- `tools.registry.tool_result/tool_error` — JSON result formatting.
- `hermes_constants.get_hermes_home()` — profile-safe paths.
- `hermes_cli.config.load_config()` — plugin config block loading.
- `agent.rate_limit_tracker.parse_rate_limit_headers()` — useful if plugin makes direct client calls and can inspect response headers.

Use with caution:

- `AIAgent` construction/running. It works but is too heavyweight for 300 concurrent live agents. It creates full clients/state/session/prompt/tool-loop machinery.
- `registry.dispatch()` from 300 tasks. Many tools are sync and can use subprocess/browser/files; this would overload the host. Keep tool use in a separate low-concurrency lane.

Avoid:

- `delegate_task` and `AIAgent._dispatch_delegate_task` entirely.
- writing 300 worker transcripts into Hermes’ main SQLite session store.
- exposing unrestricted terminal/browser/file writes to all workers.

### Local machine constraints observed

Command inspected live system limits:

```text
cpu_count 10
RLIMIT_NOFILE soft=256 hard=9223372036854775807
RLIMIT_NPROC soft=2666 hard=4000
```

The soft file descriptor limit of 256 is a hard blocker for naive 300 simultaneous TCP connections. A reliable implementation must either:

1. raise `RLIMIT_NOFILE` at runtime when allowed, e.g. target 4096; and/or
2. configure connection pooling carefully and never create one HTTP client per worker.

Hermes venv has:

```text
httpx 0.28.1
openai 2.32.0
psutil not installed
```

So resource metrics should use stdlib (`resource`, `/proc` where available, `tracemalloc` optional), unless the plugin declares an optional `psutil` dependency.

## Proposed plugin architecture

```text
swarm_task tool
  ↓
SwarmRuntime
  ├─ SwarmConfig / SafetyGuard
  ├─ RunStore append-only JSONL state
  ├─ RateLimitGovernor
  ├─ AsyncWorkerScheduler
  │    ├─ 300 LLM worker coroutines max
  │    └─ small tool lane, e.g. 4–16 workers max
  ├─ ReducerTree
  ├─ VerifierPool
  └─ FinalSynthesizer
```

### File layout

Keep everything under `~/.hermes/plugins/swarm-agent` so uninstall is clean:

```text
~/.hermes/plugins/swarm-agent/
  plugin.yaml
  __init__.py
  tools.py                    # tool schema + thin wrapper only
  swarm_runtime.py            # orchestration entrypoint
  config.py                   # defaults + config.yaml reader
  llm_worker.py               # async LLM-only worker loop
  tool_worker.py              # restricted tool lane, optional
  scheduler.py                # bounded async queue + cancellation
  rate_limit.py               # semaphores/token buckets/adaptive throttling
  run_store.py                # append-only JSONL + manifest
  reducers.py                 # reducer tree/verifiers/finalizer
  prompts.py                  # compact worker/reducer prompts
  resource_guard.py           # fd/memory/time/cost guardrails
  README.md
  docs/300-concurrent-architecture.md
  tests/
    test_config.py
    test_scheduler.py
    test_rate_limit.py
    test_run_store.py
    test_swarm_task_no_delegate.py
```

### Config block

Add plugin-owned config read from `~/.hermes/config.yaml`. Hermes core need not know this schema.

```yaml
swarm_agent:
  max_workers_hard: 300
  default_workers: 25
  default_concurrent: 25
  max_concurrent_hard: 300

  # Do not start at 300 blindly. Warm up then ramp.
  ramp:
    enabled: true
    start_concurrent: 10
    step: 25
    step_interval_seconds: 10
    backoff_multiplier: 0.5

  llm:
    task: swarm              # auxiliary task name; falls back to auto if unset
    provider: null           # optional explicit provider
    model: null              # optional explicit model
    max_tokens_worker: 700
    max_tokens_reducer: 1200
    temperature_worker: 0.2
    timeout_seconds: 120

  limits:
    max_runtime_seconds: 900
    per_worker_timeout_seconds: 180
    max_retries: 2
    retry_base_delay_seconds: 1.0
    budget_usd: 0            # 0 = no plugin-side money cap because exact pricing may be unknown
    estimated_input_tokens_per_worker: 2500
    estimated_output_tokens_per_worker: 700

  rate_limits:
    requests_per_minute: 0   # 0 = infer/adaptive only
    tokens_per_minute: 0     # 0 = infer/adaptive only
    min_success_rate: 0.90
    max_429_rate: 0.02

  tools:
    enabled: false
    allowed_toolsets: [file, web]
    max_tool_concurrent: 8
    read_only: true
    deny_tools: [terminal, patch, write_file, browser_navigate]

  storage:
    keep_runs: 20
    max_worker_result_chars: 6000
```

### Public tool schema changes

`swarm_task` should expose:

- `goal` required
- `sources` / `work_items` optional
- `max_workers` max 300
- `max_concurrent` max 300
- `mode`: `llm_only`, `tool_assisted`, `audit_readonly`
- `strategy`: `fanout`, `map_reduce`
- `dry_run`
- `provider`, `model` optional override
- `budget_usd`, `timeout_seconds`
- `allow_300_live`: boolean, default false. If false, values above safe default are dry-run rejected with explanation. If true, run guarded ramp.

Important: user must explicitly opt into 300 live concurrency because it can create large cost/rate-limit spikes.

## Execution model

### Worker prompt shape

Workers are not full Hermes agents. A high-concurrency worker is a compact LLM call:

```python
messages = [
  {"role": "system", "content": SWARM_WORKER_SYSTEM},
  {"role": "user", "content": worker_prompt(goal, shared_context, item, output_schema)},
]
```

Expected worker output should be compact JSON or sectioned markdown:

```json
{
  "worker_id": "w-042",
  "status": "ok|partial|failed",
  "findings": ["..."],
  "evidence": ["..."],
  "risks": ["..."],
  "recommendations": ["..."]
}
```

This gives 300 independent evaluations without 300 full tool loops.

### Async scheduling

Use one `asyncio.Queue` and worker coroutines:

```python
async def run_swarm(spec):
    guard = ResourceGuard(spec)
    governor = RateLimitGovernor(spec)
    store = RunStore(spec.run_id)

    queue = asyncio.Queue()
    for item in work_items:
        queue.put_nowait(item)

    async def worker(slot_id):
        while not queue.empty():
            item = await queue.get()
            try:
                await guard.before_start()
                async with governor.request_slot(estimated_tokens=item.estimated_tokens):
                    result = await run_llm_worker(item)
                await store.write_result(result)
                governor.record_success(result.headers)
            except Exception as exc:
                retry_or_record_failure(...)
            finally:
                queue.task_done()

    concurrency = await governor.initial_concurrency(spec.max_concurrent)
    tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]
    await asyncio.wait_for(queue.join(), timeout=spec.max_runtime_seconds)
    for task in tasks: task.cancel()
```

However, to support ramping from 10 to 300, a scheduler loop should spawn more slot tasks gradually when success/rate-limit metrics are healthy.

### RateLimitGovernor

Minimum required behavior:

- global semaphore for current concurrency
- request-per-minute token bucket if configured
- estimated token-per-minute bucket if configured
- adaptive backoff on 429/529/503/timeouts
- parse `Retry-After` when present
- treat provider rate-limit headers as live signal when available
- circuit breaker if error rate is high

Pseudo-logic:

```python
if status == 429:
    concurrency = max(1, floor(concurrency * 0.5))
    sleep(retry_after or exponential_backoff)
elif recent_success_rate > 0.95 and recent_429_rate < 0.01:
    concurrency = min(target, concurrency + ramp_step)
```

300 should be a **target**, not an immediate launch count.

### File descriptor guard

Because this machine currently has soft `RLIMIT_NOFILE=256`, the plugin should check before allowing high concurrency:

```python
needed = 128 + 2 * max_concurrent
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if soft < needed:
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(needed, soft), hard), hard))
    except Exception:
        reject_or_reduce_concurrency(f"fd limit {soft} too low for {max_concurrent}")
```

If the plugin cannot raise the limit, it should reduce concurrency automatically, e.g. `safe_concurrent = max(1, (soft - 128) // 2)`.

### HTTP client model

Do not create 300 OpenAI/AsyncOpenAI clients. Options:

1. easiest: use `agent.auxiliary_client.async_call_llm()` and rely on its async cached client per event loop;
2. better for header-aware throttling: resolve one async provider client and call it directly with `_build_call_kwargs()`.

For highest reliability, implement a plugin wrapper that:

- creates or reuses one async client per provider/model/base_url/api_mode
- sets `httpx.Limits(max_connections=max_concurrent + 20, max_keepalive_connections=min(max_concurrent, 100))` if creating a raw client
- closes clients on run completion if owned by plugin

Caveat: Hermes `auxiliary_client._to_async_client()` currently creates `AsyncOpenAI(**async_kwargs)` without custom limits. It is okay for moderate concurrency, but for 300 the plugin may need a direct `httpx.AsyncClient(limits=...)` path for OpenAI-compatible providers. Keep this code inside the plugin, fallback to `async_call_llm()` for compatibility.

### Tool use model

For reliability, 300 concurrent tool-using agents is not the target. Use two lanes:

1. **LLM lane** — up to 300 concurrent. No tools. Fast, low local resource use.
2. **Tool lane** — 4–16 concurrent. Optional. Only read-only tools by default.

If a worker needs tools:

- the LLM worker emits a `tool_requests` list
- scheduler enqueues those into `ToolLane`
- ToolLane executes allowlisted tools via `registry.dispatch` or `model_tools.handle_function_call`
- result is fed into a second LLM call for that item

Do not let 300 workers call terminal/browser/file writes directly.

### Storage model

Write plugin-local state:

```text
~/.hermes/plugins/swarm-agent/runs/<run_id>/
  manifest.json
  events.jsonl
  worker-results.jsonl
  reducer-results.jsonl
  final.json
```

Use append-only JSONL with an `asyncio.Lock` for writes. Do not persist every worker into Hermes main session DB.

### Cancellation and safety

- `max_runtime_seconds` wraps the whole run.
- per-worker `asyncio.wait_for(..., per_worker_timeout)`.
- on timeout/cancel: cancel pending worker tasks, flush partial results, return partial synthesis if possible.
- store every terminal state: `completed`, `failed`, `cancelled`, `timeout`, `rate_limited`, `budget_exceeded`.
- never hide partial results.

### Reducer tree

For 300 workers:

```text
300 worker results
  → 30 reducers, fan-in 10
  → 5 meta-reducers, fan-in 6
  → 1 final synthesizer
  → optional 3 verifier calls
```

Reducers can use the same async LLM lane but should run after workers with lower concurrency, e.g. 10–30.

## Reliability checklist for 300 live concurrency

The plugin should refuse or reduce 300 live concurrency unless all of these pass:

- provider/account likely supports the request rate/token rate, or user explicitly overrides
- fd soft limit raised high enough or concurrency reduced
- `max_runtime_seconds` set
- per-worker timeout set
- max output tokens capped
- retry limit capped
- tool lane either disabled or low-concurrency/read-only
- run store is plugin-local and append-only
- adaptive backoff enabled
- dry-run plan shows estimated request count and token budget
- gateway response does not try to print 300 full results; return summary + run path

## Implementation phases

### Phase 1 — replace delegate_task dependency with LLM-only async runtime

- Move current `tools.py` orchestration into `swarm_runtime.py`.
- Remove `_call_delegate()` and all `delegate_task` checks.
- Implement `asyncio` scheduler using `agent.auxiliary_client.async_call_llm()`.
- Keep default concurrency conservative: 25.
- Add `allow_300_live`; without it cap at 50.
- Tests prove `registry.dispatch("delegate_task")` is never called.

### Phase 2 — adaptive 300-concurrency support

- Add `RateLimitGovernor` with token/request buckets, retry-after parsing, error-rate backoff.
- Add `ResourceGuard` for fd limit and memory checks.
- Add ramp scheduler.
- Add dry-run estimates.
- Raise hard max concurrent to 300.

### Phase 3 — tool lane

- Implement optional `ToolLane` with allowlisted toolsets and read-only default.
- Add denylist for write/destructive tools.
- Add max tool concurrency default 8.
- Feed tool observations into second LLM call per item.

### Phase 4 — production polish

- Add run listing/read tools, e.g. `swarm_status` and `swarm_result`.
- Add progress callbacks if Hermes parent agent exposes them.
- Add documentation and examples.
- Add stress tests with fake async LLM provider simulating 300 concurrent requests.

## Test plan

Use tests with a fake async LLM function; never hit real providers in tests.

Required tests:

1. `test_swarm_task_does_not_dispatch_delegate_task`
   - monkeypatch `registry.dispatch` to fail if called with `delegate_task`.

2. `test_scheduler_reaches_300_concurrent_with_fake_llm`
   - fake LLM sleeps 50ms; record max simultaneous calls; assert 300 when `allow_300_live=True`.

3. `test_fd_guard_reduces_concurrency_when_limit_low`
   - monkeypatch resource limit to 256; assert concurrency is reduced or rejected.

4. `test_rate_limit_backoff_on_429`
   - fake LLM raises RateLimitError with retry-after; assert concurrency decreases and retries are bounded.

5. `test_run_store_jsonl_is_valid_under_concurrency`
   - 300 fake workers write results; parse every line.

6. `test_tool_lane_is_bounded_and_read_only`
   - 300 tool requests but only max 8 simultaneous; deny write tools.

7. `test_timeout_returns_partial_results`
   - some fake workers hang; final result includes completed + timeout counts.

## Key tradeoff

A swarm plugin that can reliably run 300 concurrent workers should be **less like 300 Hermes agents** and more like a specialized high-throughput async inference engine with optional bounded tool use. That is the only way to get close to Kimi-style high concurrency without breaking Hermes’ gateway, session DB, file descriptors, provider quotas, or local machine resources.
