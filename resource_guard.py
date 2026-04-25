"""Resource guardrails for swarm-agent high-concurrency runs."""

from __future__ import annotations

import math
import resource


class ResourceDecision:
    """Simple importlib-friendly decision object.

    Avoid dataclasses here because direct importlib loading in plugin tests may
    execute the module before it is present in sys.modules.
    """

    def __init__(self, requested: int, adjusted: int, fd_soft=None, fd_hard=None, reason: str = ""):
        self.requested = requested
        self.adjusted = adjusted
        self.fd_soft = fd_soft
        self.fd_hard = fd_hard
        self.reason = reason


class ResourceGuard:
    """Checks local OS limits before attempting large live swarms."""

    def __init__(self, reserve_fds: int = 128, fds_per_worker: int = 2):
        self.reserve_fds = reserve_fds
        self.fds_per_worker = fds_per_worker

    def decide_concurrency(self, requested: int) -> ResourceDecision:
        requested = max(1, int(requested))
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except Exception:
            return ResourceDecision(requested, requested, None, None, "fd limit unavailable")

        needed = self.reserve_fds + requested * self.fds_per_worker
        if soft >= needed:
            return ResourceDecision(requested, requested, soft, hard, "fd limit sufficient")

        target = needed
        if hard == resource.RLIM_INFINITY or hard >= target:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                return ResourceDecision(requested, requested, target, hard, "raised fd soft limit")
            except Exception:
                pass

        safe = max(1, math.floor((soft - self.reserve_fds) / self.fds_per_worker))
        safe = min(requested, safe)
        return ResourceDecision(
            requested,
            safe,
            soft,
            hard,
            f"fd soft limit {soft} too low for requested concurrency {requested}",
        )

    def adjust_concurrency(self, requested: int) -> int:
        return self.decide_concurrency(requested).adjusted
