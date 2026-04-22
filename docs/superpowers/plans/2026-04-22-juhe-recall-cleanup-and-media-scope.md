# Juhe Recall Cleanup And Media Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop stale Juhe room history and stale tool artifacts from polluting new certificate turns, while providing a safe one-shot cleanup path for already-polluted runtime data.

**Architecture:** This plan has three focused changes. First, tighten Juhe room-history recall so ordinary certificate requests no longer inject old Hermes delivery replies into the prompt. Second, add an explicit backup-and-clean utility for Juhe runtime state instead of destructive startup behavior. Third, scope attachment augmentation to the current turn only and add logging/tests so an old render artifact cannot be resent on a later turn.

**Tech Stack:** Python 3, SQLite, pytest, Hermes gateway runner, Juhe adapter, Hermes session store.

---

## Scope

- In scope:
  - Juhe room-history recall tightening
  - Juhe runtime backup-and-clean utility
  - Current-turn-only attachment augmentation and targeted observability
- Out of scope:
  - Busy-session text follow-up merge in [`/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py)
  - Non-Juhe platform pruning
  - MCP or certificate backend delivery changes already completed in Phase 1

## File Structure

- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
  - Tighten when `[Relevant room history]` is attached
  - Limit `_augment_final_response_with_tool_media()` input to current-turn tool messages using `history_offset`
  - Add low-noise media-origin debug logging

- Modify: [`/Users/chou/.hermes/hermes-agent/hermes_state.py`](/Users/chou/.hermes/hermes-agent/hermes_state.py)
  - Add one narrow filter for Juhe room-history hits so old Hermes outbound delivery replies and artifact-leak text are excluded from normal recall without adding a new abstraction layer

- Create: `/Users/chou/.hermes/hermes-agent/scripts/juhe_runtime_cleanup.py`
  - One-shot backup-and-clean utility for polluted Juhe runtime state

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py)
  - Cover recall gating and recall-hit filtering

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/test_hermes_state.py`](/Users/chou/.hermes/hermes-agent/tests/test_hermes_state.py)
  - Cover room-history search filtering and ranking behavior

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_delivery_artifacts.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_delivery_artifacts.py)
  - Cover current-turn-only media augmentation and stale-artifact regression

- Create: `/Users/chou/.hermes/hermes-agent/tests/scripts/test_juhe_runtime_cleanup.py`
  - Cover backup destination selection, dry-run output, and execute mode on a temp Hermes home

## Acceptance Criteria

- A normal Juhe certificate request no longer receives `[Relevant room history]` just because generic terms like “三体系”, “范围”, or “出证” hit old room messages.
- Explicit history questions such as “之前那个” or “上次那个” still get safe history context, but old outbound Hermes delivery replies with `structuredContent`, `localpath`, `mediatag`, or `MEDIA:` are excluded.
- A stale tool result from an earlier turn cannot cause `_augment_final_response_with_tool_media()` to append or resend an old ZIP on a later turn.
- Operators can run one explicit cleanup command that backs up and clears Juhe runtime state before verification, without adding auto-delete behavior to startup.
- This plan assumes the current branch already keeps FastMCP envelope unwrapping in [`/Users/chou/.hermes/hermes-agent/gateway/delivery_artifacts.py`](/Users/chou/.hermes/hermes-agent/gateway/delivery_artifacts.py); Task 4 is not a replacement for that earlier fix.

## Recommended Execution Order

1. Task 4: fix media augmentation scope first, so the wrong-file resend is stopped during development.
2. Task 2: tighten room-history recall to stop prompt pollution.
3. Task 3: run the cleanup utility and verify on a clean runtime.

---

### Task 2: Tighten Juhe Room-History Recall

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/hermes_state.py`](/Users/chou/.hermes/hermes-agent/hermes_state.py)
- Test: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py)
- Test: [`/Users/chou/.hermes/hermes-agent/tests/test_hermes_state.py`](/Users/chou/.hermes/hermes-agent/tests/test_hermes_state.py)

- [ ] Implement the main gate in `_attach_juhe_recall_context()` inside [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py): ordinary certificate requests must not attach room history just because `search_room_history()` found generic hits. Require explicit history intent before attaching `[Relevant room history]`.
- [ ] Add one narrow filter in [`/Users/chou/.hermes/hermes-agent/hermes_state.py`](/Users/chou/.hermes/hermes-agent/hermes_state.py) so `search_room_history()` excludes outbound Hermes delivery replies and leak-shaped previews. Keep this minimal, because `search_room_history()` is also used by [`/Users/chou/.hermes/hermes-agent/tools/juhe_tool.py`](/Users/chou/.hermes/hermes-agent/tools/juhe_tool.py).
- [ ] Keep `[Recent room context]` unchanged. This task must not remove the current burst context that Juhe already builds from `room_messages`.
- [ ] Add a regression test where a normal request like “深圳市万洁环境产业有限公司 中天三体系 26年4.12 范围全要” does not gain `[Relevant room history]` even when old Anyang and Shanxi turns exist in the same room.
- [ ] Add a regression test where an explicit history question still returns safe room-history context, but leak-shaped text containing `structuredContent`, `localpath`, `mediatag`, or `MEDIA:` is filtered out.
- [ ] Run:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest tests/gateway/test_juhe.py tests/test_hermes_state.py -q
  ```

### Task 3: Add A Safe Juhe Runtime Cleanup Utility

**Files:**
- Create: `/Users/chou/.hermes/hermes-agent/scripts/juhe_runtime_cleanup.py`
- Test: `/Users/chou/.hermes/hermes-agent/tests/scripts/test_juhe_runtime_cleanup.py`

- [ ] Create a one-shot cleanup utility that backs up, then clears, the polluted Juhe runtime areas:
  - `/Users/chou/.hermes/state.db`
  - `/Users/chou/.hermes/sessions/`
  - `/Users/chou/.hermes/juhe/`
  - `/Users/chou/.hermes/memories/juhe/`
  - `/Users/chou/.hermes/tmp/juhe-current-attachments/`
  - `/tmp/hermes-results`
  - `/tmp/certificate-render-artifacts`
- [ ] Support `--dry-run` and `--execute`. The script must default to non-destructive preview output.
- [ ] Make the backup destination explicit and timestamped under `/Users/chou/.hermes/backups/`, then print the chosen backup path at the end of the run.
- [ ] Do not add startup auto-clean. Cleanup must remain an explicit operator action so we do not silently wipe unrelated runtime state.
- [ ] Add tests that run the script logic against a temp Hermes home, verify move-vs-recreate behavior, and assert that `--dry-run` does not modify the filesystem.
- [ ] Run:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest tests/scripts/test_juhe_runtime_cleanup.py -q
  python scripts/juhe_runtime_cleanup.py --dry-run
  ```

### Task 4: Scope Attachment Augmentation To The Current Turn

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_delivery_artifacts.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_delivery_artifacts.py)

- [ ] Change the `_augment_final_response_with_tool_media()` call path in [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py) so it only scans tool messages produced in the current turn, not the entire resumed session transcript.
- [ ] Use the existing `history_offset` field from the agent result to derive the current-turn slice. Pass `result["messages"][history_offset:]` or an equivalent `new_messages` slice into `_augment_final_response_with_tool_media()`; do not infer turn boundaries by rescanning the full transcript.
- [ ] Preserve current-turn hidden sidecar delivery. The fix must not break the valid Phase 1 path where a just-materialized render artifact is delivered natively in the same turn.
- [ ] Treat the current FastMCP unwrapping in [`/Users/chou/.hermes/hermes-agent/gateway/delivery_artifacts.py`](/Users/chou/.hermes/hermes-agent/gateway/delivery_artifacts.py) as a required invariant. Do not reintroduce a path where current-turn tool payloads retain raw `structuredContent.delivery.local_path` or `media_tag`.
- [ ] Add structured debug logging that records:
  - session key prefix
  - source bucket for each media tag: `assistant_media`, `current_tool_payload`, or `hidden_artifact`
  - filename only, never raw local paths or signed URLs
- [ ] Add a regression test where the session history contains an old tool payload for `安阳宝华冶金耐材有限公司.zip`, but a later unrelated turn does not re-append or resend that ZIP.
- [ ] Add a regression test where current-turn materialization still produces exactly one delivered ZIP.
- [ ] Add a regression test where polluted user-visible history text contains `localpath` or `mediatag`, but no native file is delivered unless the current turn actually produced a new artifact.
- [ ] Run:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest tests/gateway/test_delivery_artifacts.py -q
  ```

## Final Verification

- [ ] Run the targeted suite:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python -m pytest \
    tests/gateway/test_juhe.py \
    tests/test_hermes_state.py \
    tests/gateway/test_delivery_artifacts.py \
    tests/scripts/test_juhe_runtime_cleanup.py \
    -q
  ```

- [ ] Back up and clear the live Juhe runtime before manual verification:
  ```bash
  cd /Users/chou/.hermes/hermes-agent
  source .venv/bin/activate
  python scripts/juhe_runtime_cleanup.py --execute
  ```

- [ ] Reproduce the room scenario manually after cleanup:
  - Send a burst of generic certificate messages in the Juhe room
  - Trigger a new company request
  - Confirm the new turn does not import stale `[Relevant room history]`
  - Confirm no old ZIP is resent before the new turn creates a fresh artifact

## Notes For Approval

- This plan deliberately leaves the busy-session text overwrite bug in [`/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py) untouched, because that work will be handled separately.
- The cleanup step is explicit and reversible-by-backup. It does not add surprise startup deletion behavior.
- The attachment fix is scoped to current-turn media augmentation, which is the smallest safe boundary for stopping repeated wrong-file sends without reopening the Phase 1 artifact-delivery design.
