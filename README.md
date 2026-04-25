# 🐝 hermes-swarm-agent

**Uninstallable swarm plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — run up to 300 lightweight LLM workers concurrently via the `/swarm` slash command.**

> User-controlled only — the agent cannot call this on its own. Only `/swarm` triggers it.

---

## What It Does

`swarm_task` runs many lightweight LLM workers concurrently over independent work items, stores results in plugin-local JSONL files, and optionally reduces outputs into a final synthesis with verifiers.

```text
User objective
  → 300 async LLM workers (with adaptive retry + backoff)
  → Reducer tree (fan-in groups of 10)
  → Final synthesizer
  → Optional verifiers
```

### Why Not `delegate_task`?

| Feature | `delegate_task` (Hermes core) | `/swarm` (this plugin) |
|---------|-------------------------------|------------------------|
| Who triggers | Agent decides | **User via `/swarm`** |
| Worker type | Full `AIAgent` instances with tools | Lightweight LLM-only coroutines |
| Tool access | Full tool loop (file, terminal, browser) | No tools (LLM calls only) |
| Max practical concurrency | 3–10 | **100–300** |
| Session storage | Hermes SQLite DB | Plugin-local JSONL files |
| Memory overhead | High (full agent state per worker) | Low (single messages array) |
| Use case | Complex multi-step subtasks | Wide parallel research/analysis |
| Uninstallable | Core feature | `rm -rf ~/.hermes/plugins/swarm-agent` |

---

## Install

```bash
# Copy plugin to Hermes plugins directory
cp -r . ~/.hermes/plugins/swarm-agent

# Enable plugin and toolset
hermes plugins enable swarm-agent
hermes tools enable swarm

# Restart Hermes or start a new session
```

## Uninstall

```bash
hermes plugins disable swarm-agent
hermes plugins remove swarm-agent

# Or manually:
rm -rf ~/.hermes/plugins/swarm-agent
```

---

## Usage

### Slash Command: `/swarm`

Users can trigger the swarm directly:

```
/swarm Evaluate the top 10 AI agent frameworks in 2026
```

With options:

```
/swarm provider:ollama-cloud workers:100 concurrent:50 Audit all Python files for security
/swarm workers:300 concurrent:100 strategy:fanout Research 300 competitors
/swarm verifiers:3 timeout:600 provider:ollama-cloud Do a deep analysis of this codebase
/swarm dry_run What sources would you analyze for market research?
/swarm help
```

Options are `key:value` pairs before the goal text:

| Option | Description | Default |
|--------|-------------|---------|
| `provider:<name>` | LLM provider | auto |
| `model:<name>` | Model override | auto |
| `workers:<N>` | Total workers | 50 |
| `concurrent:<N>` | Max concurrent | 50 |
| `strategy:<type>` | `map_reduce` or `fanout` | `map_reduce` |
| `verifiers:<N>` | Verifier count | 0 |
| `timeout:<N>` | Global timeout (seconds) | 900 |
| `dry_run` | Plan only, don't execute | false |

### Tool: `swarm_task`

The agent **cannot** call `swarm_task` on its own — it's only available internally to the `/swarm` command handler. Only users decide when to use the swarm.

```json
{
  "goal": "Evaluate each AI agent framework's architecture, strengths, and weaknesses",
  "sources": ["AutoGPT", "CrewAI", "LangGraph", "MetaGPT", "DSPy"],
  "max_workers": 5,
  "max_concurrent": 5,
  "strategy": "map_reduce",
  "provider": "ollama-cloud"
}
```

### 300-Worker Swarm (Ollama Cloud)

```json
{
  "goal": "Analyze the AI agent ecosystem in 2026",
  "sources": ["...300 independent sources..."],
  "max_workers": 300,
  "max_concurrent": 100,
  "allow_300_live": true,
  "strategy": "map_reduce",
  "provider": "ollama-cloud",
  "verifier_count": 3,
  "timeout_seconds": 900
}
```

### Dry Run (Plan Without Execution)

```json
{
  "goal": "Audit this codebase",
  "sources": ["file1.py", "file2.py", "file3.py"],
  "dry_run": true
}
```

### Fanout (No Reduce/Synthesis)

```json
{
  "goal": "Get independent evaluations of each competitor",
  "sources": ["Competitor A", "Competitor B", "Competitor C"],
  "strategy": "fanout",
  "max_concurrent": 50
}
```

---

## Parameters

| Parameter | Type | Default | Max | Description |
|-----------|------|---------|-----|-------------|
| `goal` | string | **required** | — | Overall objective for the swarm |
| `context` | string | `""` | — | Shared background context for all workers |
| `sources` | string[] | auto-generated | 300 | Independent items to assign to workers |
| `mode` | string | `"llm_only"` | — | Current mode. `delegate_task` remains separate |
| `strategy` | string | `"map_reduce"` | — | `fanout` (raw results) or `map_reduce` (with synthesis) |
| `max_workers` | int | 25 | 300 | Total lightweight LLM workers |
| `max_concurrent` | int | 25 | 300 | Maximum simultaneous coroutines |
| `allow_300_live` | bool | false | — | Required for concurrency above 50 |
| `verifier_count` | int | 0 | 5 | Optional verifiers to critique synthesis |
| `timeout_seconds` | int | 900 | 3600 | Global swarm timeout |
| `worker_timeout_seconds` | int | 180 | 600 | Per-worker LLM call timeout |
| `provider` | string | auto | — | Provider override (`ollama-cloud`, `xiaomi`, etc.) |
| `model` | string | auto | — | Model override |
| `dry_run` | bool | false | — | Return plan without executing |

---

## Provider Benchmark Results

Tested with stepwise concurrency ramp (25 → 300) to find each provider's sweet spot.

### Ollama Cloud — nemotron-3-nano:30b 🏆

| Concurrent | Success Rate | Wall Time | Throughput | Status |
|-----------|-------------|-----------|------------|--------|
| 25 | 25/25 (100%) | 19.3s | 1.3/s | ✅ Sweet spot |
| 50 | 50/50 (100%) | 22.2s | 2.3/s | ✅ Sweet spot |
| **100** | **100/100 (100%)** | **39.5s** | **2.5/s** | ✅ **Optimal** |
| 150 | 145/150 (97%) | 46.7s | 3.1/s | ⚠️ 5× 429 errors |
| 200 | 144/200 (72%) | 48.6s | 3.0/s | ❌ 56× 429 errors |
| 300 | 146/300 (49%) | 50.5s | 2.9/s | ❌ 154× 429 errors |

**Sweet spot: 100 concurrent (100% success)**

### Xiaomi MiMo v2.5-pro

| Concurrent | Success Rate | Wall Time | Throughput | Status |
|-----------|-------------|-----------|------------|--------|
| 25 | 25/25 (100%) | 5.0s | 5.0/s | ✅ Sweet spot |
| **50** | **50/50 (100%)** | **5.3s** | **9.4/s** | ✅ **Optimal** |
| 100 | 25/100 (25%) | 7.3s | 3.4/s | ❌ 75× 429 errors |
| 150 | 0/150 (0%) | 8.6s | 0.0/s | ❌ Fully blocked |

**Sweet spot: 50 concurrent (100% success)**

### MiniMax Text-01

| Concurrent | Success Rate | Status |
|-----------|-------------|--------|
| 10 | 0/10 (0%) | ❌ Rate-limited immediately |

**Not usable for swarm workloads.**

### Latency Comparison (5 calls each)

| Provider | Model | Avg Latency | Avg Tokens |
|----------|-------|-------------|------------|
| Ollama Cloud | nemotron-3-nano:30b | 1.3s | 102 |
| Ollama Cloud | gpt-oss:20b | 2.2s | 191 |
| Xiaomi | mimo-v2.5-pro | 3.1s | 127 |

---

## Production Test: 225 Workers on Ollama Cloud

### Configuration
- **Provider:** Ollama Cloud (nemotron-3-nano:30b)
- **Workers:** 225
- **Max concurrent:** 100
- **Strategy:** map_reduce
- **Verifiers:** 3
- **Retry logic:** Adaptive (halves concurrency on 429, exponential backoff)

### Results

| Metric | Value |
|--------|-------|
| Workers completed | **225/225 (100%)** |
| Failed | **0** |
| Peak concurrency | 100 |
| Waves executed | 4 |
| Auto-retries recovered | 41 |
| Wall time | **5.1 minutes** |
| Throughput | 0.7 workers/sec |
| Total output | **512,685 chars** |
| Avg per worker | 2,279 chars |
| Synthesis length | 5,817 chars |

### Cost

| Item | Count | Est. Tokens |
|------|-------|-------------|
| Worker calls (incl. retries) | 266 | ~39,900 |
| Reducer calls | 23 | ~3,450 |
| Finalizer + verifiers | 4 | ~600 |
| **Total** | **293** | **~44,000** |
| **Cost** | | **~$0.00** (Ollama Cloud free tier) |

---

## Architecture

### Files

```
swarm-agent/
├── plugin.yaml          # Plugin metadata
├── __init__.py          # Entrypoint — registers swarm_task under "swarm" toolset
├── tools.py             # Core runtime: schema, scheduler, LLM calls, reducers
├── resource_guard.py    # OS limit checks (fd limits, concurrency caps)
├── run_store.py         # Append-only JSONL run storage
├── tests/
│   └── test_swarm_agent_plugin.py
└── docs/
    ├── 300-concurrent-architecture.md
    └── provider-stress-test-results.md
```

### How It Works

```text
swarm_task()
  ├── build_work_items()          # Split sources into worker items
  ├── ResourceGuard               # Check OS fd limits, adjust concurrency
  ├── RunStore                    # Create plugin-local run directory
  └── _run_swarm_async()
       ├── _run_workers()         # Wave-based execution with retry
       │    ├── Wave 1: 100 workers → semaphore-bounded coroutines
       │    ├── Wave 2: 100 workers → auto-retry 429s with backoff
       │    ├── Wave 3: remaining workers
       │    └── Wave N: retried failed items at reduced concurrency
       └── _reduce_results()      # map_reduce only
            ├── Reducer tree (fan-in groups of 10)
            ├── Final synthesizer
            └── Verifiers (optional)
```

### Retry Logic

When workers hit 429 rate-limit errors:

1. Failed workers are collected for retry
2. Concurrency is halved (e.g., 100 → 50)
3. Exponential backoff is applied (2^n seconds, max 16s)
4. Failed items are re-queued at the front
5. Convergence to provider's sweet spot happens within 1–2 waves

This means even `max_concurrent=300` auto-converges to the provider's actual limit.

### Run Storage

Each run creates a plugin-local directory:

```
~/.hermes/plugins/swarm-agent/runs/<run_id>/
  ├── events.jsonl           # Run lifecycle events
  ├── worker-results.jsonl   # One line per worker result
  ├── reducer-results.jsonl  # Reducer outputs
  └── final.json             # Final synthesis + verifier results
```

This avoids writing hundreds of worker transcripts into Hermes' main SQLite session database.

---

## Safety Model

### Concurrency Limits

| Setting | Default | Hard Max | Notes |
|---------|---------|----------|-------|
| `max_workers` | 25 | 300 | Total workers across all waves |
| `max_concurrent` | 25 | 300 | Simultaneous coroutines |
| `allow_300_live` | false | — | Required for concurrency > 50 |

### Resource Guard

Before launching, the plugin checks the OS file descriptor limit:

```python
needed = 128 + requested_concurrent × 2
if soft_fd_limit < needed:
    # Try to raise limit, or reduce concurrency automatically
    safe = (soft_fd_limit - 128) / 2
```

On macOS with the default 256 fd limit, the guard will cap concurrency at ~64 if the limit can't be raised.

### What Workers Can't Do

Workers are LLM-only coroutines. They cannot:
- Execute terminal commands
- Read/write files
- Browse the web
- Access the filesystem
- Modify Hermes session state

---

## Testing

```bash
# Run plugin tests (uses Hermes venv)
cd ~/.hermes/hermes-agent
venv/bin/pytest ~/.hermes/plugins/swarm-agent/tests/test_swarm_agent_plugin.py -q
```

Test coverage:
- ✅ Schema supports 300 max workers/concurrency
- ✅ No parent-agent context required
- ✅ No `delegate_task` dispatch (monkeypatched)
- ✅ Fake LLM scheduler reaches 300 simultaneous coroutines
- ✅ 300 live concurrency requires explicit opt-in
- ✅ JSONL run store survives 300 concurrent writes
- ✅ Resource guard reduces concurrency when fd limit is too low
- ✅ Plugin registers correctly with Hermes plugin manager

---

## File Structure

```
hermes-swarm-agent/
├── README.md                           # This file
├── plugin.yaml                         # Hermes plugin metadata
├── __init__.py                         # Plugin entrypoint
├── tools.py                            # Core swarm runtime
├── resource_guard.py                   # OS limit guardrails
├── run_store.py                        # Append-only JSONL storage
├── tests/
│   └── test_swarm_agent_plugin.py      # 10 tests (all passing)
└── docs/
    ├── 300-concurrent-architecture.md  # Architecture deep-dive
    └── provider-stress-test-results.md # Full benchmark data
```

---

## Requirements

- Hermes Agent installed (`~/.hermes/hermes-agent`)
- Python 3.11+
- At least one LLM provider configured (Ollama Cloud, Xiaomi, OpenRouter, etc.)
- No additional Python dependencies (uses Hermes' built-in `agent.auxiliary_client`)

---

## License

MIT — same as Hermes Agent.

---

## Credits

Built as a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
