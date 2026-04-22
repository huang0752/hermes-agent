# Juhe Deferred Batch Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change Juhe busy-session handling from interrupt-and-overwrite to deferred batch collection so ordinary `@` follow-ups are preserved, non-`@` room context still comes through, and the next turn processes one merged follow-up instead of losing earlier messages.

**Architecture:** Keep all existing non-Juhe behavior and keep `adapter._pending_messages` untouched. Add a Juhe-only runner-owned deferred batch keyed by the existing `session_key`, append busy-session `@` events into that batch without interrupting the active turn, and when the session lock releases, preprocess each deferred `MessageEvent` against the same frozen history snapshot, then merge the processed blocks into one follow-up user input for exactly one new turn.

**Tech Stack:** Python 3, asyncio, `collections.deque`, pytest, Hermes gateway runner, Juhe adapter.

---

## Scope

- In scope:
  - Juhe ordinary `@` follow-ups received while the same Juhe session is busy
  - Preservation of every deferred `@` event in arrival order
  - Drain of the full deferred batch into a single merged follow-up turn
  - Reuse of the existing `_prepare_inbound_message_text(...)` pipeline for each deferred event
  - Cleanup of deferred state on explicit clear paths and gateway shutdown/restart
  - Limits to prevent unbounded backlog growth

- Out of scope:
  - Changing non-Juhe platform behavior
  - Changing Juhe session-key construction or `group_sessions_per_user=False`
  - Natural-language task classification or task scheduler logic
  - Natural-language “cancel this” detection in V1
  - Changing explicit slash-command bypass rules

## Decision Summary

- Only Juhe uses this behavior. Other platforms keep today’s interrupt semantics.
- Only ordinary `@` trigger events enter the deferred batch. Non-`@` room chatter is already attached to the following `@` event through `_juhe_pending_context_text`; do not enqueue non-`@` messages separately.
- Do not overload `adapter._pending_messages`. Keep it as-is and add a separate runner-owned structure such as:
  ```python
  _juhe_deferred_batch: Dict[str, deque[MessageEvent]]
  ```
- Each deferred event must be preprocessed individually through `_prepare_inbound_message_text(...)` so attachments, STT, image analysis, and document extraction still work.
- All deferred events in the same drain cycle must share the same frozen `updated_history` snapshot. Do not append the first deferred result to history before preprocessing the second.
- The merged follow-up should preserve order and sequence numbers. Do not add a second sender prefix if preprocess already added `[sender]` for a shared Juhe session.
- Batch limits for V1:
  - maximum 10 deferred `@` events per session
  - enqueue-side size estimation must include `event.text` and `_juhe_pending_context_text`
  - drain-side must also enforce a final rendered-size cap of 2000 characters after preprocess so expanded attachment text cannot silently blow up the merged follow-up
- Juhe deferred acknowledgments should be sent for every accepted deferred `@` event. Do not reuse the non-Juhe 30-second interrupt-ack debounce for this path.
- Drain should trigger when the session lock is released, not when a “successful response” is detected. Normal completion, failure, and timeout all drain the deferred batch. The only exceptions are explicit clear paths: `/stop`, `/new`, and `/reset`.

## Current State Summary

- Busy-session follow-ups currently hit single-slot pending paths and can overwrite one another.
- Juhe group sessions already share one session per group by default, so different users in the same group already land in the same `session_key`.
- Non-`@` Juhe room messages are already stored and attached to the next `@` trigger through `_juhe_pending_context_text` before the event reaches runner busy handling.
- `_prepare_inbound_message_text(...)` is async and attachment-aware, so flattening deferred events into a raw text accumulator too early would lose important semantics.

## File Structure

- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
  - Add Juhe deferred-batch state
  - Append busy-session Juhe `@` events without interrupting
  - Add helper methods for enqueue, estimate, render, drain, and cleanup
  - Drain after session-lock release
  - Clear the batch in explicit clear paths and gateway shutdown/restart

- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_deferred_batch.py`
  - Cover Juhe-only deferred batching, same-group sharing, different-group isolation, merged follow-up rendering, limit handling, and drain-on-failure behavior

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_busy_session_ack.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_busy_session_ack.py)
  - Keep non-Juhe interrupt acknowledgments unchanged
  - Add Juhe assertions for “queued for merged follow-up” acknowledgments

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_session_race_guard.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_session_race_guard.py)
  - Cover sentinel-time enqueue and explicit clear-path cleanup for the new batch

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py) only if an existing Juhe fixture is the cleanest place to lock in the “non-`@` already becomes `_juhe_pending_context_text`” assumption

## Acceptance Criteria

- While a Juhe session is busy, every ordinary `@` follow-up is preserved instead of overwriting earlier ones.
- Different Juhe groups do not share deferred state.
- The same Juhe group still shares a single deferred batch because it shares a single session.
- Busy Juhe `@` follow-ups do **not** interrupt the active turn.
- After the active turn releases the session lock, the full deferred batch is processed as one merged follow-up turn.
- Each deferred event keeps its attachment-processing behavior because preprocess still receives a real `MessageEvent`.
- Non-`@` group messages are not double-counted; they remain attached to the corresponding deferred `@` event via `_juhe_pending_context_text`.
- `/stop`, `/new`, and `/reset` clear the deferred batch instead of draining it.
- Non-Juhe platforms keep today’s behavior.

## Test Matrix

- Targeted deferred-batch suite:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest \
    tests/gateway/test_juhe_deferred_batch.py \
    tests/gateway/test_busy_session_ack.py \
    tests/gateway/test_session_race_guard.py \
    -q
  ```

- Juhe regression suite:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest \
    tests/gateway/test_juhe.py \
    tests/gateway/test_telegram_photo_interrupts.py \
    -q
  ```

- Optional broader confidence sweep:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest tests/gateway/ -q
  ```

---

### Task 1: Add Runner-Owned Juhe Deferred Batch State

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_deferred_batch.py`

- [ ] Add `self._juhe_deferred_batch` to `GatewayRunner` as a per-session container of deferred `MessageEvent` objects. Use `deque` so append/pop order is explicit.
- [ ] Keep this state entirely in `GatewayRunner`; do not change the type or meaning of `adapter._pending_messages`.
- [ ] Add helper methods in `GatewayRunner` for:
  - appending a deferred Juhe event
  - peeking whether a deferred batch exists for a session
  - popping the full current batch for one drain cycle
  - clearing the batch for one session
  - clearing all batches on global shutdown/restart
  - estimating enqueue-side batch size from `event.text` and `_juhe_pending_context_text`
- [ ] Add a unit test that proves a Juhe busy session stores multiple deferred events in order while a non-Juhe busy session does not use this structure.
- [ ] Add a unit test that proves two different Juhe group `chat_id` values use different deferred batches.
- [ ] Add a unit test that proves two different senders in the same Juhe group share the same deferred batch because they share the same `session_key`.

### Task 2: Route Busy Juhe `@` Events Into The Deferred Batch

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_busy_session_ack.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_busy_session_ack.py)
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_deferred_batch.py`

- [ ] Update `_handle_active_session_busy_message(...)` so Juhe ordinary `@` events append to `self._juhe_deferred_batch` and return handled without calling `running_agent.interrupt(...)`.
- [ ] Keep the existing command bypass behavior unchanged. These commands must continue to skip deferred batching:
  - `/stop`
  - `/approve`
  - `/deny`
  - `/new`
  - `/reset`
  - `/background`
  - `/restart`
- [ ] Keep the existing non-Juhe interrupt path unchanged, including the debounce behavior.
- [ ] Add a Juhe-specific acknowledgment that says the message was received and will be merged after the current task completes. Do not reuse the current “Interrupting current task...” wording.
- [ ] Do not debounce Juhe deferred acknowledgments. Every accepted deferred `@` event should receive its own acknowledgment so operators can tell it was preserved.
- [ ] Enforce the deferred-batch limits at enqueue time:
  - accept at most 10 deferred `@` events
  - estimate total size from `event.text + _juhe_pending_context_text`
  - if the new event would exceed policy, drop it and send a short acknowledgment that later messages were ignored and should be resent after the current task finishes
- [ ] Add tests that prove:
  - Juhe busy `@` events do not interrupt
  - Juhe busy `@` events produce the new acknowledgment text
  - non-Juhe busy events still interrupt and still use the old acknowledgment flow
  - overflow acknowledgments are sent when limits are exceeded

### Task 3: Render Deferred Events Into One Follow-Up Turn

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_deferred_batch.py`

- [ ] Add a helper that takes the full deferred batch for one session and preprocesses **each** event through `_prepare_inbound_message_text(...)` using the **same frozen** `updated_history` snapshot.
- [ ] Do not mutate `updated_history` between deferred-event preprocess calls. The entire batch is one logical follow-up, not a sequence of mini-turns.
- [ ] Merge the processed blocks into one follow-up string with sequence numbers, for example:
  ```text
  [Messages received while you were working]

  1.
  <processed block 1>

  2.
  <processed block 2>
  ```
- [ ] Do not manually prepend sender names if the processed block already includes `[sender]`. Juhe shared-session preprocess already adds the sender prefix when appropriate.
- [ ] At render time, enforce a final merged-size cap of 2000 characters based on the actual processed blocks so attachment-derived text cannot exceed the intended limit silently.
- [ ] Add tests that prove:
  - two deferred events become one merged follow-up turn
  - the merged text preserves order
  - sender prefixes are not duplicated
  - attachment-derived text from preprocess is included instead of being lost
  - the same frozen history snapshot is used for all deferred events in the batch

### Task 4: Drain On Session-Lock Release, Not On Success Detection

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_deferred_batch.py`

- [ ] Bind the drain trigger to session-lock release rather than “did we send a successful final response”.
- [ ] The Juhe deferred batch should drain after:
  - normal completion
  - handled failure
  - inactivity timeout / stale termination paths that still release the session lock
- [ ] The Juhe deferred batch should **not** drain after explicit clear commands:
  - `/stop`
  - `/new`
  - `/reset`
- [ ] Place the drain trigger in the session-release `finally` path so it runs when the active turn is ending and the session lock is being released, regardless of whether the turn completed normally or failed.
- [ ] Add tests that prove:
  - a failed Juhe turn still drains the deferred batch afterward
  - `/stop` clears the deferred batch and does not drain it
  - `/new` and `/reset` clear the deferred batch and do not drain it

### Task 5: Clear Deferred State In All Explicit Cleanup Paths

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_session_race_guard.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_session_race_guard.py)
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_deferred_batch.py`

- [ ] Clear the deferred batch in the normal `/stop` path for a real running agent.
- [ ] Clear the deferred batch in the sentinel force-stop path where `_AGENT_PENDING_SENTINEL` is released before full agent startup.
- [ ] Clear the deferred batch in `/new` and `/reset` before the new session starts.
- [ ] Clear all deferred batches on gateway shutdown/restart cleanup so no stale Juhe backlog survives a process lifecycle change.
- [ ] Add regression tests for the sentinel force-stop path because it currently has multiple cleanup branches and is easy to miss.

## Recommended Implementation Order

1. Task 1 first so the Juhe-only storage and helper surface exist.
2. Task 2 next so busy Juhe `@` events stop overwriting each other.
3. Task 3 once append behavior is stable, so merged follow-up rendering can be validated in isolation.
4. Task 4 after rendering works, so drain semantics are tied to lock release with confidence.
5. Task 5 last to harden every cleanup path.

## Risks And Mitigations

- Risk: a direct read/write to `adapter._pending_messages` accidentally gets repurposed for Juhe deferred behavior.
  - Mitigation: keep the Juhe batch entirely inside `GatewayRunner` and do not reuse adapter pending storage.

- Risk: later deferred events preprocess against a partially mutated in-batch history.
  - Mitigation: freeze `updated_history` once per drain cycle and use that same snapshot for every deferred event in the batch.

- Risk: sender names are duplicated in the merged follow-up.
  - Mitigation: merge processed blocks as-is with sequence numbers only; do not add a second sender wrapper if preprocess already added `[sender]`.

- Risk: non-`@` room context gets lost or duplicated.
  - Mitigation: rely on the existing `_juhe_pending_context_text` attachment path and test that deferred batching stores only `@` trigger events.

- Risk: a long-running active turn accumulates too much backlog.
  - Mitigation: cap the batch at 10 deferred `@` events, estimate size at enqueue time, and enforce a final rendered-size cap during drain.

## Approval Notes

Claude should review these explicit choices before implementation:

1. **Juhe-only deferred batching**
   - Recommended: keep all non-Juhe code paths unchanged in V1.

2. **Runner-owned state, not adapter pending reuse**
   - Recommended: use `GatewayRunner._juhe_deferred_batch` and leave `adapter._pending_messages` untouched.

3. **Drain on lock release**
   - Recommended: normal completion, failure, and timeout all drain; `/stop`, `/new`, and `/reset` explicitly clear instead.

4. **No task recognition in V1**
   - Recommended: merge all deferred `@` follow-ups into one next-turn input and let the agent interpret which lines are corrections, supplements, or new requests.

5. **Batch limits are policy, not silent truncation**
   - Recommended: enforce 10 deferred `@` events plus a 2000-character rendered cap, and send a user-visible acknowledgment when later messages are excluded.
