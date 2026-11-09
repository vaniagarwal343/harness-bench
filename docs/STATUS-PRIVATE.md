# Private status notes (not exported)

Written 2026-11-09 after the round-1 revision. `docs/AUDIT.md` is also private and is unchanged from the first pass; its DRACO, Hyperresearch and Tavily references describe the earlier plan.

## Verified so far
- Offline: 26 tests; full mock pipeline for all four configurations; grader validation and diagnostics against the real RuVerBench files with mock judges.
- Free live checks (2026-11-09): the Anthropic key is valid and sees `claude-fable-5-2`; the TypeSafe key is valid; Codex CLI is 0.162.1 and signed in with ChatGPT; the global Codex AGENTS.md is empty.
- Not verified: any account balance (none is readable with these keys), any paid call, Exa, a real Codex judging session.

## Live uncertainties, in the order the smoke test resolves them
1. Whether B, C and D finish a useful report inside $4. Earlier planning guesses (not measurements) were about $6, $8 and $11 at larger content limits. Expect the cap to bind. The gateway stops research at $3 and leaves $1 to write.
2. GPT Researcher: thinking blocks in responses may break its string parsing; its up-to-10 retries per call burn budget when a call is refused; a deep run may not reach its report step before the cap.
3. Deep Agents: its summarisation edits history, which Fable 5.2 can reject with a 400. Unlikely to trigger at $4 of context, but unproven.
4. Exa: request and response shapes were taken from the documentation, never called. `type: auto` is documented only as "balanced".
5. Codex CLI judging: the flag set is from `exec --help` and an earlier study's closed-book arm; `--ignore-user-config` plus `--ephemeral` plus the disabled feature list has not been run. Whether the event log names the model is unknown; if not, `verify` warns.
6. Astra credits: about 225 sessions planned (smoke 8 batch + about 50 one-at-a-time, validation 10 + 31 + 30, diagnostics 15, main 80). Consumption per session is unknown until the first one.
7. The input-token reservation uses 3 bytes per token. If a call ever costs more than its reservation the gateway logs it and `verify` fails; raise the margin then.
8. Jev's list endpoint shows aliases only, so `jev-1.14.0` is first confirmed by the paid preflight.

## Expected spend (planning figures)
- Anthropic: capped at $26 smoke + $260 main + $24 buffer by the ledger.
- Exa: capped at $0.60 a run, $39.60 across 66 research runs; expect roughly half.
- Jev: about 2,400 judgments at about 8k tokens, roughly $1.
