"""Persistent budget ledger: money is reserved BEFORE work is dispatched and reconciled afterwards.

One append-only JSONL file per funded provider under budget/. State is rebuilt by replaying the
file, under an exclusive file lock, so concurrent workers and separate processes see one truth.

  reserve(pool, key, amount)   refuse unless  committed(pool) + amount <= pool size
                                         and  committed(all)  + amount <= confirmed funding
  reconcile(key, actual)       replace the reservation by what was actually charged

committed = actual cost of reconciled entries + full amount of still-open reservations.
A reservation left open by a crash stays reserved (conservative) until released by hand with
`hb budget --release KEY`. Pools never borrow from each other, so debugging on the smoke or buffer
pool cannot eat into the main allocation.
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
        # No confirmed amount on file means nothing may be spent with that provider.
        self.provider, self.pools, self.confirmed = provider, dict(f["pools"]), float(cfg.get("confirmed_funding", {}).get(provider, 0.0))
        self.path = ROOT / "budget" / f"{provider}{'.mock' if mock else ''}.jsonl"
        self._tl = threading.Lock()

    # ------------------------------------------------------------ state
    def _replay(self, lines: list[str]) -> dict:
        ent: dict[str, dict] = {}
        for line in lines:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["op"] == "reserve":
                ent[e["key"]] = dict(pool=e["pool"], reserved=e["amount"], actual=None)
            elif e["op"] == "reconcile" and e["key"] in ent:
                ent[e["key"]]["actual"] = e["actual"]
            elif e["op"] == "release" and e["key"] in ent:
                ent[e["key"]]["actual"] = 0.0
        return ent

    @staticmethod
    def _committed(ent: dict, pool: str | None = None) -> float:
        return sum((e["reserved"] if e["actual"] is None else e["actual"]) for e in ent.values()
                   if pool is None or e["pool"] == pool)

    def _locked(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tl, open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                ent = self._replay(f.read().splitlines())
                out, rec = fn(ent)
                if rec:
                    f.write(json.dumps(dict(rec, ts=now()), sort_keys=True) + "\n")
                    f.flush()
                return out
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------ operations
    def reserve(self, pool: str, key: str, amount: float) -> None:
        def fn(ent):
            if key in ent:
                raise BudgetRefused(f"{self.provider}: reservation {key!r} already exists")
            pc, tc = self._committed(ent, pool), self._committed(ent)
            if pc + amount > self.pools[pool] + 1e-9:
                raise BudgetRefused(f"{self.provider}/{pool}: ${pc:.2f} committed of ${self.pools[pool]:.2f}; cannot reserve ${amount:.2f}")
            if tc + amount > self.confirmed + 1e-9:
                raise BudgetRefused(f"{self.provider}: ${tc:.2f} committed of ${self.confirmed:.2f} confirmed funding; "
                                    f"cannot reserve ${amount:.2f}. Raise {self.provider}.confirmed_usd in configs/funding.local.toml only after the money is in the account.")
            return None, dict(op="reserve", pool=pool, key=key, amount=round(amount, 6))
        self._locked(fn)

    def reconcile(self, key: str, actual: float) -> None:
        def fn(ent):
            if key not in ent:
                raise KeyError(key)
            return None, dict(op="reconcile", key=key, actual=round(actual, 6),
                              over_reservation=bool(actual > ent[key]["reserved"] + 1e-6))
        self._locked(fn)

    def release(self, key: str, why: str) -> None:
        self._locked(lambda ent: (None, dict(op="release", key=key, why=why)))

    def status(self) -> dict:
        def fn(ent):
            pools = {p: dict(size=s, spent=round(sum(e["actual"] for e in ent.values() if e["pool"] == p and e["actual"] is not None), 4),
                             open_reserved=round(sum(e["reserved"] for e in ent.values() if e["pool"] == p and e["actual"] is None), 4))
                     for p, s in self.pools.items()}
            for p in pools.values():
                p["available"] = round(p["size"] - p["spent"] - p["open_reserved"], 4)
            tc = self._committed(ent)
            return dict(provider=self.provider, confirmed_usd=self.confirmed, committed=round(tc, 4),
                        headroom_vs_confirmed=round(self.confirmed - tc, 4), pools=pools,
                        open=[k for k, e in ent.items() if e["actual"] is None],
                        over_reservation=[k for k, e in ent.items() if e["actual"] is not None and e["actual"] > e["reserved"] + 1e-6]), None
        return self._locked(fn)


def plan_check(cfg: dict) -> list[str]:
    """Static arithmetic: do the frozen counts and caps fit the pools and the funding? Returns problems."""
    cb, rs = cfg["budget"]["closed_book"]["max_cost_usd"], cfg["budget"]["research"]["max_cost_usd"]
    n_res = sum(1 for s in cfg["systems"].values() if s["budget"] == "research")
    n_cb = sum(1 for s in cfg["systems"].values() if s["budget"] == "closed_book")
    per_q = n_cb * cb + n_res * rs
    a, out = cfg["funding"]["anthropic"], []
    for pool, n in (("main", cfg["sampling"]["main"]), ("smoke", cfg["sampling"]["smoke"])):
        if n * per_q > a["pools"][pool] + 1e-9:
            out.append(f"{pool}: {n} questions x ${per_q:.2f} = ${n * per_q:.2f} exceeds the {pool} pool ${a['pools'][pool]:.2f}")
    if sum(a["pools"].values()) > a["planned_usd"] + 1e-9:
        out.append(f"pools sum to ${sum(a['pools'].values()):.2f}, above planned funding ${a['planned_usd']:.2f}")
    exa = cfg["funding"]["exa"]
    need = (cfg["sampling"]["main"] + cfg["sampling"]["smoke"]) * n_res * cfg["budget"]["research"]["max_tool_cost_usd"]
    if need > exa["pools"]["research"] + 1e-9:
        out.append(f"exa: worst case ${need:.2f} exceeds the research pool ${exa['pools']['research']:.2f}")
    return out
