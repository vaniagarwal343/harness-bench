"""Persistent spending ledger with one cumulative ceiling per provider.

There are no per-run dollar caps. The only spending limit is the provider's cumulative ceiling for
the round (and, below it, the amount confirmed to be in the account). Every model call is appended
to the ledger as it is metered, so the record survives a crash, and a call is only dispatched if
its worst-case charge still fits under the ceiling (hb/gateway.py holds that reservation in memory
while the call is in flight).

One append-only JSONL file per provider under budget/. Entries:
  spend      category, key, amount         one metered charge (a model call, a search, a judgment)
  reserve / reconcile / release            entries written by the earlier per-run-cap scheme; still counted

Categories separate what the money was for (smoke, main, preflight, infrastructure_bug, grading).
They are labels for reporting, not sub-limits.
"""
from __future__ import annotations

import fcntl
import json
import threading
from pathlib import Path

from .util import ROOT, now


class BudgetRefused(Exception):
    pass


class Budget:
    def __init__(self, provider: str, cfg: dict, mock: bool = False):
        f = cfg["funding"][provider]
        self.provider, self.ceiling = provider, float(f["ceiling_usd"])
        # No confirmed amount on file means nothing may be spent with that provider.
        self.confirmed = float(cfg.get("confirmed_funding", {}).get(provider, 0.0))
        self.limit = min(self.ceiling, self.confirmed)
        self.path = ROOT / "budget" / f"{provider}{'.mock' if mock else ''}.jsonl"
        self._tl = threading.Lock()

    def _entries(self, lines: list[str]) -> tuple[dict, list]:
        legacy: dict[str, dict] = {}
        spends = []
        for line in lines:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["op"] == "spend":
                spends.append(e)
            elif e["op"] == "reserve":
                legacy[e["key"]] = dict(category=e.get("pool", "legacy"), reserved=e["amount"], actual=None)
            elif e["op"] == "reconcile" and e["key"] in legacy:
                legacy[e["key"]]["actual"] = e["actual"]
            elif e["op"] == "release" and e["key"] in legacy:
                legacy[e["key"]]["actual"] = 0.0
        return legacy, spends

    def _locked(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tl, open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                legacy, spends = self._entries(f.read().splitlines())
                out, rec = fn(legacy, spends)
                if rec:
                    f.write(json.dumps(dict(rec, ts=now()), sort_keys=True) + "\n")
                    f.flush()
                return out
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @staticmethod
    def _committed(legacy: dict, spends: list) -> float:
        return sum((e["reserved"] if e["actual"] is None else e["actual"]) for e in legacy.values()) + sum(s["amount"] for s in spends)

    def committed(self) -> float:
        return self._locked(lambda l, s: (self._committed(l, s), None))

    def remaining(self) -> float:
        return self.limit - self.committed()

    def spend(self, category: str, key: str, amount: float, note: str = "") -> None:
        """Record a charge that has already happened. Never refuses: what was spent is recorded."""
        if amount:
            self._locked(lambda l, s: (None, dict(op="spend", category=category, key=key, amount=round(amount, 6), note=note)))

    def status(self) -> dict:
        def fn(legacy, spends):
            cats: dict[str, float] = {}
            for e in legacy.values():
                cats[e["category"]] = cats.get(e["category"], 0.0) + (e["reserved"] if e["actual"] is None else e["actual"])
            for s in spends:
                cats[s["category"]] = cats.get(s["category"], 0.0) + s["amount"]
            tc = self._committed(legacy, spends)
            return dict(provider=self.provider, ceiling_usd=self.ceiling, confirmed_usd=self.confirmed, committed=round(tc, 4),
                        remaining_vs_ceiling=round(self.ceiling - tc, 4), remaining_vs_confirmed=round(self.confirmed - tc, 4),
                        by_category={k: round(v, 4) for k, v in sorted(cats.items())},
                        open=[k for k, e in legacy.items() if e["actual"] is None]), None
        return self._locked(fn)


class Funds:
    """In-process guard in front of a Budget: holds worst-case reservations for calls in flight."""

    def __init__(self, budget: Budget):
        self.b, self.reserved, self.lock = budget, 0.0, threading.Lock()

    def try_reserve(self, amount: float) -> bool:
        with self.lock:
            if self.b.remaining() - self.reserved < amount:
                return False
            self.reserved += amount
            return True

    def settle(self, reserved: float, actual: float, category: str, key: str, note: str = "") -> None:
        with self.lock:
            self.reserved = max(0.0, self.reserved - reserved)
        self.b.spend(category, key, actual, note)
