"""Plugin-local append-only run storage for swarm-agent."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class RunStore:
    """Append-only JSONL state scoped to the uninstallable plugin.

    The store is deliberately simple and plugin-local so large swarms do not
    write hundreds of worker transcripts into Hermes' main session database.
    """

    def __init__(self, base_dir: str | Path, run_id: str):
        self.base_dir = Path(base_dir)
        self.run_id = run_id
        self.run_dir = self.base_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.worker_results_path = self.run_dir / "worker-results.jsonl"
        self.reducer_results_path = self.run_dir / "reducer-results.jsonl"
        self.final_path = self.run_dir / "final.json"
        self._lock = asyncio.Lock()

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        record = {"ts": time.time(), **payload}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    async def write_event(self, payload: dict[str, Any]) -> None:
        await self._append_jsonl(self.events_path, payload)

    async def write_worker_result(self, payload: dict[str, Any]) -> None:
        await self._append_jsonl(self.worker_results_path, payload)

    async def write_reducer_result(self, payload: dict[str, Any]) -> None:
        await self._append_jsonl(self.reducer_results_path, payload)

    async def write_final(self, payload: dict[str, Any]) -> None:
        data = json.dumps({"ts": time.time(), **payload}, ensure_ascii=False, indent=2, sort_keys=True)
        async with self._lock:
            self.final_path.write_text(data + "\n", encoding="utf-8")
