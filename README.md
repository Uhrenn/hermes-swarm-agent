# 🐝 hermes-swarm-agent

**Uninstallable swarm plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — run up to 300 lightweight LLM workers concurrently via the `/swarm` slash command.**

> User-controlled only — the agent cannot call this on its own. Only `/swarm` triggers it.

---

## Why Swarm?

Hermes already has `delegate_task` for spawning subagents. So why add a swarm?

### The Problem with `delegate_task` at Scale

`delegate_task` spawns full `AIAgent` instances — each one gets its own model client, system prompt, tool registry, session tracking, callbacks, and SQLite writes. That's great for 3–10 complex subtasks. But try 100 and you hit:

- **Thread overhead** — each child is a full agent with model client, system prompt, tool registry, callbacks, session tracking
- **File conflicts** — 100 agents writing to the same repo = merge chaos
- **SQLite contention** — 100 concurrent session DB writes bottleneck
- **API rate limits** — 100 full agents = 100× the token cost (system prompts, tool schemas, context)
- **No retry logic** — if a child hits a 429, it just fails
- **Memory pressure** — 100 full agent states in RAM

### What Swarm Does Differently

Swarm strips away the overhead. Each worker is a single LLM call — no tools, no session state, no file access, no SQLite writes. That means:

| | `delegate_task | `/swarm` |
|-|----------------|----------|
| **Concurrency** | 3–10 practical | **100–300 proven** |
| **Cost per worker** | Full system prompt + tool schemas (~2000 tokens overhead) | Single focused prompt (~200 tokens overhead) |
| **300 workers cost** | ~600K overhead tokens wasted on system prompts | ~60K overhead tokens — **10× cheaper** |
| **Speed** | Minutes per worker (tool loops, retries) | Seconds per worker (single LLM call) |
| **300 workers time** | Hours | **3.7 minutes** |
| **Retry on 429** | None — worker just fails | Auto-retry with adaptive backoff |
| **Rate limit handling** | None — blast all 300 at once | Provider-aware sweet spots, wave-based execution |
| **File conflicts** | 100 agents editing files = chaos | Zero — workers are LLM-only |
| **Session DB** | 100 concurrent SQLite writes | Zero — plugin-local JSONL files |
| **Uninstallable** | Core feature | `rm -rf ~/.hermes/plugins/swarm-agent` |

### Real Numbers

Tested on Ollama Cloud (nemotron-3-nano:30b):

```
300 workers completed:   100% success rate
Wall time:               3.7 minutes
Throughput:              1.4 workers/sec
Total LLM calls:         344 (300 workers + 44 auto-recovered retries)
Estimated cost:          $0.00 (Ollama Cloud free tier)
```

### When to Use Each

**Use `delegate_task` when:**
- You need agents to use tools (file, terminal, browser)
- Tasks are complex, multi-step, and need agent reasoning loops
- You need 3–10 specialized subagents with different toolsets
- Tasks require file editing, code execution, or web browsing

**Use `/swarm` when:**
- You need broad parallel research across many sources
- Tasks are independent and don't need tool access
- You want 50–300 workers evaluating different things simultaneously
- You need fast, cheap, high-throughput analysis
- You want map-reduce synthesis across hundreds of inputs
- You want auto-retry on rate limits with provider-aware scheduling

**Use both together:**
- `/swarm` to research 300 sources in parallel
- `delegate_task` to take the synthesis and implement changes with tools

---

## What It Does

`/swarm` runs many lightweight LLM workers concurrently over independent work items, stores results in plugin-local JSONL files, and optionally reduces outputs into a final synthesis with verifiers.

```text
User objective
  → 300 async LLM workers (provider-aware scheduling, adaptive retry)
  → Reducer tree (fan-in groups of 10)
  → Final synthesizer
  → Optional verifiers
```

---

## Install

```bash
# Clone into Hermes plugins directory
git clone https://github.com/Uhrenn/hermes-swarm-agent.git ~/.hermes/plugins/swarm-agent

# Enable plugin
hermes plugins enable swarm-agent

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

Users trigger the swarm directly:

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
| `workers:<N>` | Total workers | 300 |
| `concurrent:<N>` | Max concurrent | 100 |
| `strategy:<type>` | `map_reduce` or `fanout` | `map_reduce` |
| `verifiers:<N>` | Verifier count | 0 |
| `timeout:<N>` | Global timeout (seconds) | 900 |
| `dry_run` | Plan only, don't execute | false |

---

## Parameters

| Parameter | Type | Default | Max | Description |
|-----------|------|---------|-----|-------------|
| `goal` | string | **required** | — | Overall objective for the swarm |
| `context` | string | `""` | — | Shared background context for all workers |
| `sources` | string[] | auto-generated | 300 | Independent items to assign to workers |
| `mode` | string | `"llm_only"` | — | Current mode. `delegate_task` remains separate |
| `strategy` | string | `"map_reduce"` | — | `fanout` (raw results) or `map_reduce` (with synthesis) |
| `max_workers` | int | 300 | 300 | Total lightweight LLM workers |
| `max_concurrent` | int | 100 | 300 | Maximum simultaneous coroutines per wave |
| `verifier_count` | int | 0 | 5 | Optional verifiers to critique synthesis |
| `timeout_seconds` | int | 900 | 3600 | Global swarm timeout |
| `worker_timeout_seconds` | int | 180 | 600 | Per-worker LLM call timeout |
| `provider` | string | auto | — | Provider override (`ollama-cloud`, `xiaomi`, etc.) |
| `model` | string | auto | — | Model override |
| `allow_300_live` | bool | false | — | Required for tool-initiated calls with high concurrency |
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

## Production Test: 300 Workers on Ollama Cloud

### Configuration
- **Provider:** Ollama Cloud (nemotron-3-nano:30b)
- **Workers:** 300
- **Max concurrent:** 100 (provider-aware scheduling)
- **Strategy:** fanout
- **Retry logic:** Adaptive (auto-recover 429s with backoff)

### Results

| Metric | Value |
|--------|-------|
| Workers completed | **300/300 (100%)** |
| Failed | **0** |
| Initial concurrent | 100 (provider sweet spot) |
| Peak concurrency | 100 |
| Waves executed | 5 |
| Auto-retries recovered | 44 |
| Wall time | **3.7 minutes** |
| Throughput | 1.4 workers/sec |
| Total LLM calls | 344 |
| Total tokens | ~190K |
| **GPU time** | **~12.5 minutes** |
| **Cost** | **Subscription-based** (see below) |

### Cost — Ollama Cloud Subscription

Ollama Cloud charges by **GPU time**, not per token:

| Plan | Price | 300-Worker Swarm Impact |
|------|-------|------------------------|
| **Free** | $0/mo | ⚠️ Likely exhausts session |
| **Pro** | $20/mo | ⚠️ Significant session usage |
| **Max** | $100/mo | ✅ Well within limits |

- Avg 2.17s GPU time per call × 344 calls = **~12.5 min total GPU time**
- ~552 tokens per worker (98 prompt + 454 completion)
- Session limits reset every 5 hours, weekly limits every 7 days
- Per-token add-on pricing coming soon
- Check usage: https://ollama.com/settings/usage

---

## Architecture

### Files

```
swarm-agent/
├── plugin.yaml          # Plugin metadata
├── __init__.py          # Entrypoint — registers /swarm command (no tool)
├── tools.py             # Core runtime: scheduler, LLM calls, reducers, provider sweet spots
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
/swarm <goal>
  ├── _parse_swarm_args()          # Parse key:value options
  ├── swarm_task()
  │    ├── build_work_items()      # Split sources into worker items
  │    ├── ResourceGuard           # Check OS fd limits
  │    ├── RunStore                # Create plugin-local run directory
  │    └── _run_swarm_async()
  │         ├── _run_workers()     # Provider-aware wave execution
  │         │    ├── Wave 1: min(max_concurrent, sweet_spot) workers
  │         │    ├── Wave 2: next batch
  │         │    ├── ...auto-retry 429s with backoff...
  │         │    └── Wave N: remaining workers
  │         └── _reduce_results()  # map_reduce only
  │              ├── Reducer tree (fan-in groups of 10)
  │              ├── Final synthesizer
  │              └── Verifiers (optional)
  └── Returns formatted text to user
```

### Provider-Aware Scheduling

The scheduler starts at the provider's known sweet spot, not at `max_concurrent`:

```python
PROVIDER_SWEET_SPOTS = {
    "ollama-cloud": 100,   # tested: 100/100 ok
    "xiaomi": 50,          # tested: 50/50 ok
    "openrouter": 80,      # estimated
    "minimax": 0,          # unusable
}
```

This avoids the wasteful "launch 300, get 150 429s, retry" pattern. Instead: launch 100, all succeed, launch next 100, all succeed, done.

### Retry Logic

When workers hit 429 rate-limit errors:

1. Failed workers are collected for retry
2. Concurrency reduced by 1/3
3. Jittered backoff applied (2^n seconds + random 0-2s)
4. Failed items re-queued
5. After a clean wave (0 failures), concurrency bumps back up

### Run Storage

Each run creates a plugin-local directory:

```
~/.hermes/plugins/swarm-agent/runs/<run_id>/
  ├── events.jsonl           # Run lifecycle events
  ├── worker-results.jsonl   # One line per worker result
  ├── reducer-results.jsonl  # Reducer outputs
  └── final.json             # Final synthesis + verifier results
```

---

## Safety Model

### Concurrency Limits

| Setting | Default | Hard Max | Notes |
|---------|---------|----------|-------|
| `max_workers` | 300 | 300 | Total workers across all waves |
| `max_concurrent` | 100 | 300 | Simultaneous coroutines per wave |
| `allow_300_live` | false | — | Required for tool-initiated calls above provider sweet spot |

### Resource Guard

Before launching, the plugin checks the OS file descriptor limit:

```python
needed = 128 + requested_concurrent × 2
if soft_fd_limit < needed:
    safe = (soft_fd_limit - 128) / 2
```

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
- ✅ Fake LLM scheduler completes all 300 workers via waves
- ✅ 300 live concurrency requires explicit opt-in
- ✅ JSONL run store survives 300 concurrent writes
- ✅ Resource guard reduces concurrency when fd limit is too low
- ✅ Plugin registers `/swarm` command (not a tool)
- ✅ Manifest declares `provides_commands` (not `provides_tools`)

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
