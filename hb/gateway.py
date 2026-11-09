"""Local gateway that every contestant model call and research call passes through.

It is the one place where these hold identically for an in-process loop and for a third-party
framework running as a child process:
  * every generative call is the pinned model, with no fallback
  * B, C and D share one search backend and one page reader
  * the per-run dollar cap covers all subagents and retries, and money is RESERVED before a
    request is dispatched (conservative input estimate + the full max_tokens at output price),
    then reconciled to actual usage
  * every attempted call is traced and costed, including refused and failed ones

  POST /v1/messages                 Anthropic Messages proxy (stream and non-stream)
  POST /v1/messages/count_tokens    passthrough, logged, unbilled
  POST /tools/search  /tools/fetch  shared research tools
  GET  /tools/gptr                  GPT Researcher custom-retriever contract on the same tools

The contestant holds only a per-run token. Real keys stay in this process. A request is forwarded
unchanged, refused with a logged policy violation, or changed by a named shim that is logged on
that call (pin_effort, clamp_max_tokens).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .util import append_jsonl, canon, now, sha256_text, write_json

ANTHROPIC_URL = "https://api.anthropic.com"
EXA_URL = "https://api.exa.ai"
SAMPLING = ("temperature", "top_p", "top_k")


def _err(status: int, msg: str, etype: str = "invalid_request_error") -> JSONResponse:
    # Anthropic error envelope: SDKs surface the message and do not retry a 400.
    return JSONResponse({"type": "error", "error": {"type": etype, "message": msg}}, status_code=status)


@dataclass
class Ledger:
    run_dir: Path
    policy: dict
    pricing: dict
    token: str = field(default_factory=lambda: "hb-run-" + secrets.token_hex(16))
    started: float = field(default_factory=now)
    llm_calls: int = 0
    llm_errors: int = 0
    usage: dict = field(default_factory=lambda: dict(input_tokens=0, output_tokens=0,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0))
    cost_usd: float = 0.0
    reserved_usd: float = 0.0       # model spend reserved for in-flight calls
    inflight: int = 0
    max_committed_usd: float = 0.0  # high-water mark of spent + reserved; must never exceed the cap
    tool_calls: dict = field(default_factory=lambda: dict(search=0, fetch=0))
    tool_errors: int = 0
    tool_cost_usd: float = 0.0
    tool_reserved_usd: float = 0.0
    models_seen: dict = field(default_factory=dict)
    efforts_seen: dict = field(default_factory=dict)
    violations: list = field(default_factory=list)
    seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    cond: asyncio.Condition | None = None

    def add_usage(self, u: dict) -> float:
        p = self.pricing
        cost = (u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]
                + u.get("cache_creation_input_tokens", 0) * p["cache_write"]
                + u.get("cache_read_input_tokens", 0) * p["cache_read"]) / 1e6
        with self.lock:
            for k in self.usage:
                self.usage[k] += int(u.get(k, 0) or 0)
            self.cost_usd += cost
        return cost

    def wall_exceeded(self) -> bool:
        w = self.policy.get("budget", {}).get("max_wall_seconds")
        return w is not None and now() - self.started >= w

    def research_closed(self) -> bool:
        """Research tools stop early so the remaining money can pay for the final report."""
        b = self.policy.get("budget", {})
        cap = b.get("max_cost_usd")
        return cap is not None and self.cost_usd + self.reserved_usd >= cap - b.get("final_reserve_usd", 0.0)

    def near_limit(self) -> bool:
        b = self.policy.get("budget", {})
        f = b.get("soft_fraction", 0.6)
        checks = []
        if b.get("max_cost_usd"):
            checks.append(self.cost_usd / (b["max_cost_usd"] - b.get("final_reserve_usd", 0.0)))
        if b.get("max_tool_calls"):
            checks.append(sum(self.tool_calls.values()) / b["max_tool_calls"])
        if b.get("max_tool_cost_usd"):
            checks.append(self.tool_cost_usd / b["max_tool_cost_usd"])
        return any(c >= f for c in checks)

    def violation(self, kind: str, detail: str) -> None:
        v = dict(ts=now(), kind=kind, detail=detail)
        with self.lock:
            self.violations.append(v)
        append_jsonl(self.run_dir / "trace.jsonl", dict(type="violation", **v))

    def summary(self) -> dict:
        return dict(llm_calls=self.llm_calls, llm_errors=self.llm_errors, usage=dict(self.usage),
                    cost_usd=round(self.cost_usd, 6), max_committed_usd=round(self.max_committed_usd, 6),
                    tool_calls=dict(self.tool_calls), tool_errors=self.tool_errors, tool_cost_usd=round(self.tool_cost_usd, 6),
                    models_seen=dict(self.models_seen), efforts_seen=dict(self.efforts_seen),
                    violations=list(self.violations), gateway_seconds=round(now() - self.started, 2))


class Gateway:
    def __init__(self, cfg: dict, anthropic_key: str | None, tool_key: str | None, mock: bool = False):
        self.cfg, self.mock = cfg, mock
        self.anthropic_key, self.tool_key = anthropic_key, tool_key
        self.ledgers: dict[str, Ledger] = {}
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self.app = Starlette(routes=[
            Route("/v1/messages", self.messages, methods=["POST"]),
            Route("/v1/messages/count_tokens", self.count_tokens, methods=["POST"]),
            Route("/tools/search", self.search, methods=["POST"]),
            Route("/tools/fetch", self.fetch, methods=["POST"]),
            Route("/tools/gptr", self.gptr, methods=["GET"]),
        ])

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "Gateway":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(200):
            if self._server.started and self._server.servers:
                self.port = self._server.servers[0].sockets[0].getsockname()[1]
                return self
            time.sleep(0.05)
        raise RuntimeError("gateway failed to start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def open_run(self, run_dir: Path, policy: dict) -> Ledger:
        led = Ledger(run_dir=run_dir, policy=policy, pricing=self.cfg["pricing"]["contestant"])
        self.ledgers[led.token] = led
        return led

    def close_run(self, led: Ledger) -> dict:
        self.ledgers.pop(led.token, None)  # the token is dead: late calls from stray subprocesses are refused
        return led.summary()

    def _ledger(self, request: Request) -> Ledger | None:
        tok = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return self.ledgers.get(tok)

    # ------------------------------------------------------------ policy
    def _check(self, led: Ledger, body: dict, betas: str) -> str | None:
        pol = led.policy
        want = pol["model"]
        if body.get("model") != want:
            led.violation("model", f"requested {body.get('model')!r}, only {want!r} allowed")
            return f"HB_POLICY model {body.get('model')!r} not allowed; this study pins {want!r} with no fallback"
        if "fallbacks" in body or "server-side-fallback" in betas:
            led.violation("fallback", "request asked for server-side model fallback")
            return "HB_POLICY model fallbacks are disabled in this study"
        if body.get("speed"):
            led.violation("speed", "fast mode changes price and is not part of the frozen settings")
            return "HB_POLICY speed overrides are not allowed"
        tools = body.get("tools") or []
        if tools and not pol.get("allow_tools", True):
            led.violation("tools", f"closed-book run sent {len(tools)} tools")
            return "HB_POLICY tools are not allowed in the closed-book configuration"
        for t in tools:
            ttype = t.get("type") or "custom"
            if ttype != "custom":
                led.violation("server_tool", f"{ttype} bypasses the shared research backend")
                return f"HB_POLICY server tool {ttype!r} is not allowed; use the shared search/fetch tools"
        if body.get("mcp_servers"):
            led.violation("server_tool", "mcp_servers connector")
            return "HB_POLICY the MCP connector is not allowed"
        return None

    @staticmethod
    def _flags(body: dict) -> list[str]:
        f = [k for k in SAMPLING if k in body]
        th = body.get("thinking") or {}
        if th.get("type") in ("enabled", "disabled") or "budget_tokens" in th:
            f.append(f"thinking:{th.get('type')}")
        if (body.get("tool_choice") or {}).get("type") in ("any", "tool"):
            f.append("forced_tool_choice")
        msgs = body.get("messages") or []
        if msgs and msgs[-1].get("role") == "assistant":
            f.append("assistant_prefill")
        return f

    async def _reserve(self, led: Ledger, n_bytes: int, requested: int) -> tuple[int, float] | str:
        """Reserve the worst-case charge for one call. Returns (granted max_tokens, reserved USD) or a refusal."""
        enf, p = self.cfg["budget"]["enforcement"], led.pricing
        cap = led.policy.get("budget", {}).get("max_cost_usd")
        est_in = n_bytes / enf["bytes_per_token"] * max(p["input"], p["cache_write"]) / 1e6
        if led.cond is None:
            led.cond = asyncio.Condition()
        deadline = now() + enf["reservation_wait_s"]
        async with led.cond:
            while True:
                if led.wall_exceeded():
                    return "HB_BUDGET_EXHAUSTED (wall_time); no further model calls are possible in this run"
                if cap is None:
                    granted = requested
                    break
                room = cap - led.cost_usd - led.reserved_usd - est_in
                afford = int(room / p["output"] * 1e6)
                if afford >= min(enf["min_output_tokens"], requested):
                    granted = min(requested, afford)
                    break
                if led.inflight == 0 or now() >= deadline:
                    return (f"HB_BUDGET_EXHAUSTED (cost: ${led.cost_usd:.2f} spent of ${cap:.2f}); "
                            "no further model calls are possible in this run")
                try:  # other calls of this run are in flight: their reservations shrink to actual cost when they finish
                    await asyncio.wait_for(led.cond.wait(), timeout=max(0.1, deadline - now()))
                except asyncio.TimeoutError:
                    pass
            reserve = est_in + granted * p["output"] / 1e6
            led.reserved_usd += reserve
            led.inflight += 1
            led.max_committed_usd = max(led.max_committed_usd, led.cost_usd + led.reserved_usd)
        return granted, reserve

    async def _release(self, led: Ledger, reserve: float) -> None:
        async with led.cond:
            led.reserved_usd = max(0.0, led.reserved_usd - reserve)
            led.inflight -= 1
            led.cond.notify_all()

    # ------------------------------------------------------------ LLM proxy
    async def messages(self, request: Request) -> Response:
        led = self._ledger(request)
        if led is None:
            return _err(401, "HB_POLICY unknown or closed run token", "authentication_error")
        raw = await request.body()
        try:
            body = json.loads(raw)
        except Exception:
            return _err(400, "HB malformed JSON body")
        betas = request.headers.get("anthropic-beta", "")
        with led.lock:
            led.seq += 1
            seq = led.seq
            led.llm_calls += 1  # every attempt counts, including refused and failed ones
        rec = dict(type="llm", seq=seq, ts=now(), requested_model=body.get("model"), stream=bool(body.get("stream")),
                   requested_max_tokens=body.get("max_tokens"), requested_effort=(body.get("output_config") or {}).get("effort"),
                   n_messages=len(body.get("messages") or []), tools=[t.get("name") for t in body.get("tools") or []],
                   compat_flags=self._flags(body), shims=[], betas=betas, body_sha256=sha256_text(raw.decode("utf-8", "replace")))
        if self.cfg["trace"].get("store_bodies", True):
            (led.run_dir / "llm").mkdir(parents=True, exist_ok=True)
            (led.run_dir / "llm" / f"{seq:05d}.req.json").write_bytes(raw)
        reserve = [0.0]

        def finish(status: int, resp: dict | None, err: str | None = None) -> None:
            usage = (resp or {}).get("usage") or {}
            rec.update(status=status, latency_s=round(now() - rec["ts"], 3), response_model=(resp or {}).get("model"),
                       stop_reason=(resp or {}).get("stop_reason"), usage=usage, error=err, reserved_usd=round(reserve[0], 6),
                       request_id=(resp or {}).get("_request_id"))
            if not usage and status == 599 and rec.get("dispatched"):
                # The request reached the provider but its usage never came back (dropped or unreadable stream).
                # It may have been billed, so the whole reservation is counted as spent.
                with led.lock:
                    led.cost_usd += reserve[0]
                rec["cost_usd"], rec["cost_basis"] = round(reserve[0], 6), "full_reservation_usage_unknown"
            if usage:  # billed whenever the provider reports usage, including interrupted streams
                rec["cost_usd"] = round(led.add_usage(usage), 6)
                if rec["cost_usd"] > reserve[0] + 1e-6 and led.policy.get("budget", {}).get("max_cost_usd") is not None:
                    led.violation("reservation_underestimate", f"call {seq}: cost {rec['cost_usd']} > reserved {reserve[0]:.6f}")
            if status == 200 and resp:
                m = resp.get("model") or "?"
                with led.lock:
                    led.models_seen[m] = led.models_seen.get(m, 0) + 1
                    led.efforts_seen[str(rec.get("effective_effort"))] = led.efforts_seen.get(str(rec.get("effective_effort")), 0) + 1
                if m != led.policy["model"]:
                    led.violation("served_model", f"response served by {m!r}")
                if self.cfg["trace"].get("store_bodies", True):
                    write_json(led.run_dir / "llm" / f"{seq:05d}.resp.json", resp)
            else:
                with led.lock:
                    led.llm_errors += 1
            append_jsonl(led.run_dir / "trace.jsonl", rec)

        why = self._check(led, body, betas)
        if why:
            finish(400, None, why)
            return _err(400, why)

        # Named, logged shims only.
        if "pin_effort" in led.policy.get("shims", []) and (body.get("output_config") or {}).get("effort") != led.policy["effort"]:
            body.setdefault("output_config", {})["effort"] = led.policy["effort"]
            rec["shims"].append("pin_effort")
        rec["effective_effort"] = (body.get("output_config") or {}).get("effort") or "default(high)"
        requested = int(body.get("max_tokens") or 0)
        got = await self._reserve(led, len(raw), requested)
        if isinstance(got, str):
            finish(400, None, got)
            return _err(400, got)
        granted, reserve[0] = got
        if granted < requested:  # output allowance lowered to what the remaining budget can pay for
            body["max_tokens"] = granted
            rec["shims"].append("clamp_max_tokens")
        rec["effective_max_tokens"] = granted
        payload = json.dumps(body).encode() if rec["shims"] else raw

        if self.mock:
            from .mock import mock_message, sse_bytes
            resp = mock_message(body)
            finish(200, resp)
            await self._release(led, reserve[0])
            if body.get("stream"):
                return Response(sse_bytes(resp), media_type="text/event-stream")
            return JSONResponse(resp)

        # identity: the stream is relayed byte for byte and parsed for usage, so it must not be compressed
        headers = {"x-api-key": self.anthropic_key or "", "content-type": "application/json", "accept-encoding": "identity",
                   "anthropic-version": request.headers.get("anthropic-version", "2023-06-01")}
        if betas:
            headers["anthropic-beta"] = betas
        timeout = httpx.Timeout(self.cfg["execution"]["llm_request_timeout_s"], connect=30)
        rec["dispatched"] = True
        if not body.get("stream"):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.post(ANTHROPIC_URL + "/v1/messages", content=payload, headers=headers)
            except httpx.HTTPError as e:
                finish(599, None, f"upstream transport: {e!r}")
                await self._release(led, reserve[0])
                return _err(529, "HB upstream transport error", "overloaded_error")
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            data["_request_id"] = r.headers.get("request-id")
            finish(r.status_code, data if r.status_code == 200 else None,
                   None if r.status_code == 200 else json.dumps(data.get("error"))[:500])
            await self._release(led, reserve[0])
            data.pop("_request_id", None)
            return JSONResponse(data, status_code=r.status_code,
                                headers={k: v for k, v in r.headers.items() if k in ("retry-after", "request-id")})

        client = httpx.AsyncClient(timeout=timeout)
        try:
            up = await client.send(client.build_request("POST", ANTHROPIC_URL + "/v1/messages", content=payload,
                                                        headers=headers), stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            finish(599, None, f"upstream transport: {e!r}")
            await self._release(led, reserve[0])
            return _err(529, "HB upstream transport error", "overloaded_error")
        if up.status_code != 200:
            data = await up.aread()
            await up.aclose(); await client.aclose()
            finish(up.status_code, None, data.decode("utf-8", "replace")[:500])
            await self._release(led, reserve[0])
            return Response(data, status_code=up.status_code, media_type="application/json",
                            headers={k: v for k, v in up.headers.items() if k == "retry-after"})

        async def relay():
            buf = bytearray()
            try:
                async for chunk in up.aiter_bytes():
                    buf.extend(chunk)
                    yield chunk
                resp = parse_sse(bytes(buf))
                resp["_request_id"] = up.headers.get("request-id")
                finish(200, resp, None if resp.get("stop_reason") else "stream ended without message_delta")
            except BaseException as e:  # client disconnect or upstream drop: what was produced is still billed
                resp = parse_sse(bytes(buf))
                finish(200 if resp.get("stop_reason") else 599, resp or None, f"stream interrupted: {e!r}")
                raise
            finally:
                await self._release(led, reserve[0])
                await up.aclose(); await client.aclose()

        return StreamingResponse(relay(), media_type="text/event-stream")

    async def count_tokens(self, request: Request) -> Response:
        led = self._ledger(request)
        if led is None:
            return _err(401, "HB_POLICY unknown or closed run token", "authentication_error")
        raw = await request.body()
        append_jsonl(led.run_dir / "trace.jsonl", dict(type="count_tokens", ts=now()))
        if self.mock:
            return JSONResponse({"input_tokens": max(1, len(raw) // 4)})
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(ANTHROPIC_URL + "/v1/messages/count_tokens", content=raw, headers={
                "x-api-key": self.anthropic_key or "", "content-type": "application/json",
                "anthropic-version": request.headers.get("anthropic-version", "2023-06-01")})
        return Response(r.content, status_code=r.status_code, media_type="application/json")

    # ------------------------------------------------------------ research tools
    async def _tool_call(self, led: Ledger, kind: str, args: dict) -> dict:
        tcfg, b = self.cfg["tools"], led.policy.get("budget", {})
        with led.lock:
            led.seq += 1
            seq = led.seq
            led.tool_calls[kind] += 1  # attempts count, including refused ones
            total = sum(led.tool_calls.values())
            res = tcfg[f"{kind}_reserve_usd"]
            cap = b.get("max_tool_cost_usd")
            refuse = None
            if b.get("max_tool_calls") is not None and total > b["max_tool_calls"]:
                refuse = "HB_RESEARCH_BUDGET_USED: no research calls remain. Write your final report now with the evidence you have."
            elif cap is not None and led.tool_cost_usd + led.tool_reserved_usd + res > cap:
                refuse = "HB_RESEARCH_BUDGET_USED: the search allowance is spent. Write your final report now with the evidence you have."
            elif led.research_closed() or led.wall_exceeded():
                refuse = "HB_RESEARCH_BUDGET_USED: the remaining budget is reserved for writing. Write your final report now with the evidence you have."
            else:
                led.tool_reserved_usd += res  # reserved before the backend is called
        rec = dict(type="tool", seq=seq, ts=now(), tool=kind, args=args, reserved_usd=0.0 if refuse else res)
        cost = 0.0
        if refuse:
            out: dict = {"error": refuse}
        else:
            try:
                out, cost = await (self._search(args, tcfg) if kind == "search" else self._fetch(args, tcfg))
            except Exception as e:  # explicit tool failure, visible to the model and in the trace
                out = {"error": f"tool backend failure: {type(e).__name__}: {str(e)[:200]}"}
            with led.lock:
                led.tool_reserved_usd = max(0.0, led.tool_reserved_usd - res)
                led.tool_cost_usd += cost
        if "error" in out:
            with led.lock:
                led.tool_errors += 1
        elif led.near_limit():
            out["notice"] = "Most of this run's research budget is used. Start writing with the evidence you have."
        rec.update(latency_s=round(now() - rec["ts"], 3), error=out.get("error"), cost_usd=round(cost, 6), refused=bool(refuse),
                   result_sha256=sha256_text(canon(out)), result_chars=len(canon(out)),
                   urls=[r.get("url") for r in out.get("results", [])] or ([out["url"]] if out.get("url") else []))
        append_jsonl(led.run_dir / "trace.jsonl", rec)
        if self.cfg["trace"].get("store_bodies", True):
            write_json(led.run_dir / "tools" / f"{seq:05d}.{kind}.json", dict(args=args, result=out))
        return out

    async def _tool(self, request: Request, kind: str) -> Response:
        led = self._ledger(request)
        if led is None:
            return JSONResponse({"error": "HB_POLICY unknown or closed run token"}, status_code=401)
        if not led.policy.get("allow_tools", True):
            led.violation("tools", f"closed-book run called {kind}")
            return JSONResponse({"error": "HB_POLICY tools are not available in this configuration"}, status_code=403)
        return JSONResponse(await self._tool_call(led, kind, await request.json()))

    async def search(self, request: Request) -> Response:
        return await self._tool(request, "search")

    async def fetch(self, request: Request) -> Response:
        return await self._tool(request, "fetch")

    async def gptr(self, request: Request) -> Response:
        """GPT Researcher `RETRIEVER=custom` contract: GET ?query= -> [{url, raw_content}].

        One logged search plus one logged page read per result, so GPT Researcher draws on the same
        backend, block list, reader, content limit and budget as the other research arms.
        """
        led = self.ledgers.get(request.query_params.get("token", ""))
        if led is None or not led.policy.get("allow_tools", True):
            return JSONResponse([], status_code=401)
        sr = await self._tool_call(led, "search", {"query": request.query_params.get("query", "")})
        out = []
        for r in sr.get("results", []):
            page = await self._tool_call(led, "fetch", {"url": r["url"]})
            if "content" in page:
                out.append({"url": r["url"], "raw_content": page["content"]})
        return JSONResponse(out)

    def _blocked(self, url: str, tcfg: dict) -> bool:
        host = (urlparse(url).hostname or "").lower()
        u = url.lower()
        return any(host == d or host.endswith("." + d) for d in tcfg.get("blocked_domains", [])) or \
            any(s.lower() in u for s in tcfg.get("blocked_url_substrings", []))

    async def _exa(self, path: str, payload: dict, tcfg: dict) -> dict:
        async with httpx.AsyncClient(timeout=tcfg["timeout_s"]) as c:
            r = await c.post(EXA_URL + path, headers={"x-api-key": self.tool_key or "", "content-type": "application/json"}, json=payload)
            r.raise_for_status()
            return r.json()

    async def _search(self, args: dict, tcfg: dict) -> tuple[dict, float]:
        q = str(args.get("query", "")).strip()
        if not q:
            return {"error": "query is required"}, 0.0
        n = max(1, min(int(args.get("max_results") or tcfg["search_max_results"]), tcfg["search_max_results"]))
        if self.mock:
            from .mock import mock_search
            results, cost = mock_search(q, n), 0.012
        elif tcfg["backend"] == "exa":
            # Raw retrieval only: page-text snippets. No highlights, summary or answer (those are model-generated).
            j = await self._exa("/search", {"query": q, "type": tcfg["search_type"], "numResults": n,
                                            "contents": {"text": {"maxCharacters": tcfg["snippet_chars"]}}}, tcfg)
            results = [dict(title=x.get("title"), url=x.get("url"), published=x.get("publishedDate"), snippet=x.get("text"))
                       for x in j.get("results", [])]
            cost = float((j.get("costDollars") or {}).get("total") or 0.0)
        else:
            raise NotImplementedError(f"search backend {tcfg['backend']!r}")
        kept = [x for x in results if not self._blocked(x.get("url") or "", tcfg)]
        return {"query": q, "results": kept, "filtered_benchmark_sources": len(results) - len(kept)}, cost

    async def _fetch(self, args: dict, tcfg: dict) -> tuple[dict, float]:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return {"error": "url must be http(s)"}, 0.0
        if self._blocked(url, tcfg):
            return {"error": "this source is excluded in this study (benchmark material)"}, 0.0
        cap = tcfg["fetch_max_chars"]
        if self.mock:
            from .mock import mock_page
            text, cost = mock_page(url), 0.001
        elif tcfg["backend"] == "exa":
            j = await self._exa("/contents", {"urls": [url], "text": {"maxCharacters": cap + 1}}, tcfg)
            cost = float((j.get("costDollars") or {}).get("total") or 0.0)
            st = (j.get("statuses") or [{}])[0]
            res = j.get("results") or []
            if st.get("status") == "error" or not res or not res[0].get("text"):
                tag = (st.get("error") or {}).get("tag") or "no text returned"
                return {"error": f"page could not be read ({tag})", "url": url}, cost
            text = res[0]["text"]
        else:
            raise NotImplementedError(f"fetch backend {tcfg['backend']!r}")
        if len(text) > cap:  # visible, uniform truncation; never silent
            text = text[:cap] + f"\n\n[truncated: the page continues beyond {cap} characters]"
        return {"url": url, "content": text}, cost


def parse_sse(raw: bytes) -> dict:
    """Reconstruct model, stop_reason and usage from an Anthropic SSE stream."""
    out: dict = {"usage": {}, "content": []}
    blocks: dict[int, dict] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        if ev.get("type") == "content_block_start":
            blocks[ev["index"]] = dict(ev.get("content_block") or {}, _json="")
            out["content"] = [blocks[i] for i in sorted(blocks)]
        elif ev.get("type") == "content_block_delta" and ev.get("index") in blocks:
            b, d = blocks[ev["index"]], ev.get("delta") or {}
            if d.get("type") == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif d.get("type") == "input_json_delta":
                b["_json"] += d.get("partial_json", "")
        elif ev.get("type") == "content_block_stop" and ev.get("index") in blocks:
            b = blocks[ev["index"]]
            js = b.pop("_json", "")
            if js:
                try:
                    b["input"] = json.loads(js)
                except Exception:
                    b["input_raw"] = js
        elif ev.get("type") == "message_start":
            m = ev.get("message", {})
            out["model"], out["id"] = m.get("model"), m.get("id")
            out["usage"].update({k: v for k, v in (m.get("usage") or {}).items() if isinstance(v, int)})
        elif ev.get("type") == "message_delta":
            out["stop_reason"] = (ev.get("delta") or {}).get("stop_reason")
            out["usage"].update({k: v for k, v in (ev.get("usage") or {}).items() if isinstance(v, int)})
        elif ev.get("type") == "error":
            out["error"] = ev.get("error")
    return out


def run_async(coro):
    return asyncio.run(coro)
