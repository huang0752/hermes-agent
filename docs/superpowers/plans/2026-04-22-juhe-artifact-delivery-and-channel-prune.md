# Juhe Artifact Delivery And Channel Prune Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an artifact-first Juhe delivery flow so certificate ZIP packages are sent as native files instead of local paths or download links, then prune non-Juhe chat adapters in a separate gated phase.

**Architecture:** Phase 1 makes certificate delivery artifact-native. Source tools materialize the ZIP locally, Hermes captures the raw artifact in a hidden delivery sidecar before sanitizing tool output, and the gateway injects only native file attachments into Juhe. Phase 2 physically shrinks Hermes to Juhe-first messaging only, while explicitly keeping `api_server.py` and `Platform.LOCAL` because they are not disposable chat adapters.

**Tech Stack:** Python 3, aiohttp, httpx, pytest, FastMCP, Node.js, built-in `node:test`, Hermes gateway adapters, certificate workflow CLI.

---

## Decision Summary

- Phase 1 starts with a controlled preflight: snapshot the current Juhe worktree, stop the gateway, back up runtime state, and clear old session/cache/artifact data so verification is not polluted by stale Juhe history.
- Phase 1 is the approval gate. Do not delete other chat adapters until the Juhe artifact flow is passing targeted tests end-to-end.
- Do not solve this with hard blocking rules. Solve it by changing the data path from “URL/text delivery” to “local artifact delivery”.
- Keep `gateway/delivery_rules.py` as a detector/safety utility, but stop depending on it as the main behavior control.
- Do not modify `/Users/chou/code/qwsaas` in Phase 1. The leak happens before the SDK: Hermes and MCP currently expose URLs and local paths to the model. Juhe already knows how to stage a local file via temp S3 in [`/Users/chou/.hermes/hermes-agent/gateway/platforms/juhe.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/juhe.py).
- Do not touch [`/Users/chou/.hermes/hermes-agent/gateway/platforms/api_server.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/api_server.py) in this plan. It is an OpenAI-compatible inbound HTTP surface, not a chat adapter to remove.
- Treat `webhook.py` as deferred. It is an event surface, not part of the immediate “Juhe-only chat delivery” cut.

## File Structure

### Hermes files

- Create: `/Users/chou/.hermes/hermes-agent/gateway/delivery_artifacts.py`
  Responsibility: define a lightweight session-scoped artifact model, raw tool-result extractors, and model-visible sanitization helpers for certificate delivery artifacts.

- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
  Responsibility: attach `tool_complete_callback`, record hidden delivery artifacts per session, and synthesize `MEDIA:/absolute/path` tags only from the hidden artifact store instead of from visible download URLs.

- Modify: [`/Users/chou/.hermes/hermes-agent/tools/tool_result_storage.py`](/Users/chou/.hermes/hermes-agent/tools/tool_result_storage.py)
  Responsibility: sanitize model-visible certificate delivery payloads after the callback has captured raw artifacts, so the model sees safe summaries instead of `local_path`, `media_tag`, `download_url`, or `output_url`.

- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py)
  Responsibility: make the default `send_document()` fail closed so unsupported adapters do not silently send local filesystem paths as text.

- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/platforms/juhe.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/juhe.py)
  Responsibility: keep Juhe native file delivery, remove the certificate-specific hard rejection path in `send_document()`, and continue using remote materialization / temp-S3 staging as a transport detail.

- Modify: [`/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py`](/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py)
  Responsibility: remove Juhe-specific hard blocks that reject certificate render-job text or media sources before the adapter can deliver them.

- Modify: [`/Users/chou/.hermes/hermes-agent/agent/prompt_builder.py`](/Users/chou/.hermes/hermes-agent/agent/prompt_builder.py)
  Responsibility: keep Juhe prompt guidance aligned with the new artifact-first flow and the new MCP materialization tool.

### Hermes tests

- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_delivery_artifacts.py`
  Responsibility: verify artifact extraction, session-scoped storage, and payload sanitization helpers independently from the giant gateway runner.

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_media_extraction.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_media_extraction.py)
  Responsibility: stop expecting remote certificate URLs to become `MEDIA:http://signed-url`; expect local artifact media or safe link stripping instead.

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/tools/test_tool_result_storage.py`](/Users/chou/.hermes/hermes-agent/tests/tools/test_tool_result_storage.py)
  Responsibility: assert that model-visible tool results are sanitized while hidden artifact extraction still preserves delivery capability.

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_platform_base.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_platform_base.py)
  Responsibility: assert that default `send_document()` returns an error instead of echoing a filesystem path.

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py)
  Responsibility: verify Juhe still materializes remote docs and stages local artifacts, but no longer relies on certificate-specific reject branches.

- Modify: [`/Users/chou/.hermes/hermes-agent/tests/tools/test_send_message_tool.py`](/Users/chou/.hermes/hermes-agent/tests/tools/test_send_message_tool.py)
  Responsibility: remove tests that enshrine Juhe hard-block behavior and replace them with delivery-path expectations.

### Certificate backend and CLI files

- Modify: [`/Users/chou/code/certificate/backend/mcp_server/tools/render.py`](/Users/chou/code/certificate/backend/mcp_server/tools/render.py)
  Responsibility: add `materialize_render_job_artifact()` that downloads the final ZIP with a naked HTTP client and emits a local delivery artifact envelope.

- Modify: [`/Users/chou/code/certificate/backend/mcp_server/server.py`](/Users/chou/code/certificate/backend/mcp_server/server.py)
  Responsibility: register the new MCP tool.

- Modify: [`/Users/chou/code/certificate/backend/mcp_server/tests.py`](/Users/chou/code/certificate/backend/mcp_server/tests.py)
  Responsibility: cover tool behavior, tool docs, and server registration count.

- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper-lib.mjs`](/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper-lib.mjs)
  Responsibility: harden wrapper downloads so presigned object URLs are fetched without Django Bearer auth.

- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper.test.mjs`](/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper.test.mjs)
  Responsibility: lock the wrapper’s authenticated-download decision.

- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/generate.sh`](/Users/chou/code/certificate/tools/certificate-workflow-cli/generate.sh)
  Responsibility: include `materialize_render_job_artifact` in generated CLI tool coverage.

- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.ts`](/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.ts)
  Responsibility: regenerated CLI source.

- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.js`](/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.js)
  Responsibility: regenerated CLI bundle.

### Phase 2 cleanup files

- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_parallel_attachments.py`
  Responsibility: receive the Juhe-specific burst-merging test currently living in [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_telegram_photo_interrupts.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_telegram_photo_interrupts.py).

- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/config.py`](/Users/chou/.hermes/hermes-agent/gateway/config.py), [`/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py`](/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py), [`/Users/chou/.hermes/hermes-agent/hermes_cli/platforms.py`](/Users/chou/.hermes/hermes-agent/hermes_cli/platforms.py), [`/Users/chou/.hermes/hermes-agent/toolsets.py`](/Users/chou/.hermes/hermes-agent/toolsets.py)
  Responsibility: remove non-Juhe adapter enumeration after test migration and dependency audit.

## Phase 1 Acceptance Criteria

- A completed certificate render job can be materialized to a local ZIP without exposing a raw presigned URL or a local filesystem path to the user.
- Model-visible tool output contains at most filename, size, status, job id, and a safe delivery summary.
- The final Juhe reply sends the ZIP natively through `send_document()`; the visible text contains neither `download_url` nor `local_path`.
- Default `BasePlatformAdapter.send_document()` never falls back to sending a path string as text.
- No Phase 1 code changes require `qwsaas` changes.

## Phase 1 Test Matrix

- Certificate backend:
  `cd /Users/chou/code/certificate/backend && uv run python -m pytest mcp_server/tests.py -q`

- Certificate workflow CLI:
  `cd /Users/chou/code/certificate/tools/certificate-workflow-cli && node --test workflow-wrapper.test.mjs`

- Hermes targeted:
  `cd /Users/chou/.hermes/hermes-agent && source venv/bin/activate && python -m pytest tests/gateway/test_delivery_artifacts.py tests/gateway/test_media_extraction.py tests/gateway/test_platform_base.py tests/gateway/test_juhe.py tests/tools/test_tool_result_storage.py tests/tools/test_send_message_tool.py -q`

---

## Phase 1: Juhe Artifact Delivery

### Task 0: Snapshot current Juhe work and clear runtime state before Phase 1

**Files:**
- Working tree baseline: `/Users/chou/.hermes/hermes-agent` (existing modified and untracked Juhe work on `custom/juhe-platform`)
- Runtime state to back up and clear:
  `/Users/chou/.hermes/state.db`
  `/Users/chou/.hermes/sessions/`
  `/Users/chou/.hermes/juhe/`
  `/Users/chou/.hermes/memories/juhe/`
  `/Users/chou/.hermes/tmp/juhe-current-attachments/`
  `/tmp/hermes-results`
  `/tmp/certificate-render-artifacts`
- Backup destination: `/Users/chou/.hermes/backups/juhe-phase1-*`

- [ ] **Step 1: Stop the gateway and record the current worktree state**

Run:
```bash
hermes gateway stop || true
ps aux | rg "hermes.*gateway|python .*gateway" | rg -v rg || true

cd /Users/chou/.hermes/hermes-agent
git status --short > /tmp/juhe-phase1-baseline-status.txt
git status --short | wc -l
```

Expected: the gateway is stopped or already inactive, and the current changed-file count is recorded to `/tmp/juhe-phase1-baseline-status.txt` before Phase 1 edits start.

- [ ] **Step 2: Create a baseline commit for the current Juhe worktree**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
git add -A
git commit -m "wip: snapshot juhe baseline before artifact-delivery cut"
```

Expected: a WIP snapshot commit exists on `custom/juhe-platform`, so Phase 1 implementation is not mixed with the preexisting Juhe worktree state.

- [ ] **Step 3: Back up the runtime state that can pollute artifact-delivery validation**

Run:
```bash
timestamp="$(date +%Y%m%d-%H%M%S)"
backup_dir="/Users/chou/.hermes/backups/juhe-phase1-${timestamp}"
mkdir -p "$backup_dir"

if [ -e /Users/chou/.hermes/state.db ]; then
  mv /Users/chou/.hermes/state.db "$backup_dir/state.db"
fi
if [ -e /Users/chou/.hermes/sessions ]; then
  mv /Users/chou/.hermes/sessions "$backup_dir/sessions"
fi
if [ -e /Users/chou/.hermes/juhe ]; then
  mv /Users/chou/.hermes/juhe "$backup_dir/juhe-cache"
fi
if [ -e /Users/chou/.hermes/memories/juhe ]; then
  mv /Users/chou/.hermes/memories/juhe "$backup_dir/juhe-room-memory"
fi
if [ -e /Users/chou/.hermes/tmp/juhe-current-attachments ]; then
  mv /Users/chou/.hermes/tmp/juhe-current-attachments "$backup_dir/juhe-current-attachments"
fi

mkdir -p /Users/chou/.hermes/sessions
mkdir -p /Users/chou/.hermes/tmp
mkdir -p /Users/chou/.hermes/memories
```

Expected: old session transcripts, Juhe cache JSON, room memory, attachment materializations, and the SQLite state store are moved out of the live runtime tree into a timestamped backup directory.

- [ ] **Step 4: Clear temp artifact spill directories**

Run:
```bash
rm -rf /tmp/hermes-results
rm -rf /tmp/certificate-render-artifacts
```

Expected: there are no old persisted tool-result files or old render ZIPs left in the system temp area.

- [ ] **Step 5: Verify that the live runtime is clean enough for Phase 1**

Run:
```bash
test ! -e /Users/chou/.hermes/state.db
test ! -e /Users/chou/.hermes/juhe
test ! -e /Users/chou/.hermes/memories/juhe
test ! -e /Users/chou/.hermes/tmp/juhe-current-attachments
test ! -e /tmp/hermes-results
test ! -e /tmp/certificate-render-artifacts
test -z "$(find /Users/chou/.hermes/sessions -maxdepth 1 -type f -print)"
```

Expected: the live runtime has no stale SQLite session store, no Juhe cache directory, no Juhe room memory directory, no current-attachment materializations, no persisted tool-result spill files, no old render ZIP temp directory, and the recreated [`/Users/chou/.hermes/sessions`](/Users/chou/.hermes/sessions) directory is present but contains no files at all.

### Task 1: Harden wrapper downloads for presigned artifact URLs

**Files:**
- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper.test.mjs`](/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper.test.mjs)
- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper-lib.mjs`](/Users/chou/code/certificate/tools/certificate-workflow-cli/workflow-wrapper-lib.mjs)

- [ ] **Step 1: Write the failing Node test**

```js
test("downloadRenderArtifact skips Django Bearer auth for presigned object URLs", async () => {
  const originalFetch = global.fetch;
  const calls = [];
  const tempDir = await mkdtemp("/tmp/certificate-workflow-test-");

  global.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), headers: options.headers || {} });
    if (String(url).endsWith("/api/admin/auth/login")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ code: 200, data: { token: "Bearer secret-token" } }),
      };
    }
    return {
      ok: true,
      status: 200,
      headers: {
        get(name) {
          if (String(name).toLowerCase() === "content-type") {
            return "application/zip";
          }
          return null;
        },
      },
      arrayBuffer: async () => new TextEncoder().encode("zip-bytes").buffer,
    };
  };

  try {
    const result = await downloadRenderArtifact({
      downloadUrl:
        "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/2026/04/22/job-92/final.zip?X-Amz-Signature=abc",
      downloadDir: tempDir,
      filenameHint: "final",
    });

    assert.equal(result.filename, "final.zip");
    assert.equal(calls.length, 1);
    assert.equal("Authorization" in calls[0].headers, false);
  } finally {
    global.fetch = originalFetch;
  }
});
```

- [ ] **Step 2: Run the wrapper test to verify it fails**

Run:
```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
node --test workflow-wrapper.test.mjs
```

Expected: FAIL because `downloadRenderArtifact()` currently logs into Django and unconditionally sends `Authorization: Bearer secret-token` on the artifact download request.

- [ ] **Step 3: Write the minimal wrapper implementation**

```js
function shouldAttachDjangoAuthForDownload(downloadUrl) {
  const absoluteUrl = buildAbsoluteUrl(downloadUrl);
  const parsed = new URL(absoluteUrl);
  const pathname = parsed.pathname || "";
  const hasPresignedQuery =
    parsed.searchParams.has("X-Amz-Signature") ||
    parsed.searchParams.has("Signature") ||
    parsed.searchParams.has("AWSAccessKeyId") ||
    parsed.searchParams.has("X-Amz-Algorithm");
  if (hasPresignedQuery) {
    return false;
  }
  return pathname.startsWith("/api/admin/certificates/render-jobs/");
}

export async function downloadRenderArtifact({
  downloadUrl,
  downloadDir,
  filenameHint,
}) {
  const absoluteUrl = buildAbsoluteUrl(downloadUrl);
  if (!absoluteUrl) {
    throw new Error("downloadUrl is required");
  }

  let headers = {};
  if (shouldAttachDjangoAuthForDownload(absoluteUrl)) {
    const token = await loginToDjango();
    headers = token ? { Authorization: `Bearer ${token}` } : {};
  }

  const response = await fetch(absoluteUrl, { headers });
  if (!response.ok) {
    throw new Error(`Artifact download failed with status ${response.status}`);
  }

  const downloadRoot =
    cleanText(downloadDir) ||
    cleanText(process.env.CERTIFICATE_WORKFLOW_DOWNLOAD_DIR) ||
    path.join(os.tmpdir(), "certificate-workflow");
  await mkdir(downloadRoot, { recursive: true });

  const responseFilename =
    parseContentDispositionFilename(response.headers.get("content-disposition")) ||
    cleanText(filenameHint) ||
    "certificate-delivery.zip";
  const filename = ensureArtifactExtension(
    responseFilename,
    response.headers.get("content-type"),
  );
  const localPath = path.join(downloadRoot, `${Date.now()}-${filename}`);
  const content = Buffer.from(await response.arrayBuffer());
  await writeFile(localPath, content);

  return {
    localPath,
    filename,
    size: content.byteLength,
  };
}
```

- [ ] **Step 4: Run the wrapper tests again**

Run:
```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
node --test workflow-wrapper.test.mjs
```

Expected: PASS. Existing wrapper tests still pass, and the new presigned-URL test proves the wrapper does not break on MinIO object URLs.

- [ ] **Step 5: Commit**

```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
git add workflow-wrapper-lib.mjs workflow-wrapper.test.mjs
git commit -m "fix: harden certificate artifact wrapper downloads"
```

### Task 2: Add MCP `materialize_render_job_artifact` and register it

**Files:**
- Modify: [`/Users/chou/code/certificate/backend/mcp_server/tests.py`](/Users/chou/code/certificate/backend/mcp_server/tests.py)
- Modify: [`/Users/chou/code/certificate/backend/mcp_server/tools/render.py`](/Users/chou/code/certificate/backend/mcp_server/tools/render.py)
- Modify: [`/Users/chou/code/certificate/backend/mcp_server/server.py`](/Users/chou/code/certificate/backend/mcp_server/server.py)

- [ ] **Step 1: Write the failing backend tests**

```python
async def test_materialize_render_job_artifact_downloads_with_plain_http(self) -> None:
    presigned = (
        "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/"
        "2026/04/22/job-92/final.zip?X-Amz-Signature=abc"
    )
    with (
        patch.object(
            render_tools,
            "get_render_job_progress",
            new=AsyncMock(return_value={"job_id": 92, "status": "success", "download_url": presigned}),
        ),
        patch("mcp_server.tools.render.tempfile.gettempdir", return_value="/tmp"),
        patch("mcp_server.tools.render.aiohttp.ClientSession") as session_cls,
        patch("mcp_server.tools.render.time.time", return_value=1713772800.123),
    ):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.__aexit__.return_value = False
        response.raise_for_status.return_value = None
        response.read = AsyncMock(return_value=b"zip-bytes")
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = False
        session.get.return_value = response
        session_cls.return_value = session

        result = await render_tools.materialize_render_job_artifact(92)

    self.assertEqual(result["job_id"], 92)
    self.assertEqual(result["delivery"]["filename"], "final.zip")
    self.assertEqual(result["delivery"]["media_tag"], f"MEDIA:{result['delivery']['local_path']}")
    session.get.assert_awaited_once()
    self.assertEqual(session.get.await_args.kwargs.get("headers"), None)

async def test_materialize_render_job_artifact_raises_when_render_is_not_ready(self) -> None:
    with patch.object(
        render_tools,
        "get_render_job_progress",
        new=AsyncMock(return_value={"job_id": 92, "status": "running", "download_url": ""}),
    ):
        with self.assertRaisesRegex(RuntimeError, "download URL is not available"):
            await render_tools.materialize_render_job_artifact(92)

async def test_get_render_job_download_url_keeps_job_id_for_followup_materialization(self) -> None:
    with patch.object(
        render_tools.http_client,
        "get",
        new=AsyncMock(
            return_value={
                "job_id": 3,
                "status": "success",
                "download_url": "/api/admin/certificates/render-jobs/3/download",
                "output_url": "/media/render-jobs/3/output.zip",
            }
        ),
    ):
        result = await render_tools.get_render_job_download_url(3)

    self.assertEqual(
        result,
        {
            "job_id": 3,
            "download_url": "http://127.0.0.1:28000/media/render-jobs/3/output.zip",
        },
    )
```

- [ ] **Step 2: Run the backend tests to verify they fail**

Run:
```bash
cd /Users/chou/code/certificate/backend
uv run python -m pytest mcp_server/tests.py -q -k "materialize_render_job_artifact or server_registers_all"
```

Expected: FAIL because the tool does not exist yet and the server registration count is still 29.

- [ ] **Step 3: Implement the MCP tool**

```python
import aiohttp
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _render_artifact_filename(download_url: str, progress: dict) -> str:
    candidate = Path(unquote(Path(urlsplit(download_url).path).name)).name
    if candidate:
      return candidate if candidate.endswith(".zip") else f"{candidate}.zip"
    display_name = str(progress.get("zip_display_name") or f"render-job-{progress.get('job_id') or 'artifact'}").strip()
    return display_name if display_name.endswith(".zip") else f"{display_name}.zip"


async def _download_render_artifact(download_url: str, target_path: Path) -> int:
    timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(download_url) as resp:
            resp.raise_for_status()
            payload = await resp.read()
    target_path.write_bytes(payload)
    return len(payload)


async def materialize_render_job_artifact(
    job_id: Annotated[int, Field(description="渲染任务 ID。")],
    download_dir: Annotated[str, Field(description="可选下载目录；默认落到系统临时目录。")] = "",
) -> dict:
    """将渲染任务的最终 ZIP 落到本地临时文件，供 Juhe 等平台按文件发送。"""
    progress = await get_render_job_progress(job_id)
    download_url = str(progress.get("download_url") or "")
    if not download_url:
        raise RuntimeError("Render job download URL is not available yet")

    root = Path(download_dir or tempfile.gettempdir()) / "certificate-render-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    filename = _render_artifact_filename(download_url, progress)
    local_path = root / f"{int(time.time() * 1000)}-{filename}"
    size = await _download_render_artifact(download_url, local_path)

    return {
        "status": "completed",
        "job_id": job_id,
        "delivery": {
            "kind": "document",
            "filename": filename,
            "local_path": str(local_path),
            "size": size,
            "media_tag": f"MEDIA:{local_path}",
        },
    }


async def get_render_job_download_url(
    job_id: Annotated[int, Field(description="渲染任务 ID。")],
) -> dict:
    """获取下载任务的最终下载地址，并保留 job_id 供后续 materialize_render_job_artifact 使用。"""
    progress = await get_render_job_progress(job_id)
    download_url = progress.get("download_url") or ""
    if not download_url:
        raise RuntimeError("Render job download URL is not available yet")
    return {"job_id": job_id, "download_url": download_url}
```

- [ ] **Step 4: Register the tool and update registration assertions**

```python
from .tools.render import (
    create_render_job,
    create_render_job_and_wait,
    get_render_job_download_url,
    get_render_job_progress,
    list_render_jobs,
    materialize_render_job_artifact,
)

for tool in (
    get_current_user,
    list_partner_options,
    list_certificate_template_options,
    list_resource_type_options,
    list_resource_options,
    upload_file,
    upload_file_from_url,
    list_archive_companies,
    get_archive_company,
    create_archive_company,
    update_archive_company,
    list_archive_company_options,
    list_system_certificates,
    create_system_certificate,
    batch_create_system_certificates,
    update_system_certificate,
    list_honor_certificates,
    create_honor_certificate,
    batch_create_honor_certificates,
    update_honor_certificate,
    prefill_qcc,
    prefill_translate,
    prefill_archive_company,
    prefill_certificate_template,
    list_render_jobs,
    create_render_job,
    get_render_job_progress,
    get_render_job_download_url,
    create_render_job_and_wait,
    materialize_render_job_artifact,
):
    mcp.tool()(tool)
```

And update the test expectation:

```python
expected = {
    "get_current_user",
    "list_partner_options",
    "list_certificate_template_options",
    "list_resource_type_options",
    "list_resource_options",
    "upload_file",
    "upload_file_from_url",
    "list_archive_companies",
    "get_archive_company",
    "create_archive_company",
    "update_archive_company",
    "list_archive_company_options",
    "list_system_certificates",
    "create_system_certificate",
    "batch_create_system_certificates",
    "update_system_certificate",
    "list_honor_certificates",
    "create_honor_certificate",
    "batch_create_honor_certificates",
    "update_honor_certificate",
    "prefill_qcc",
    "prefill_translate",
    "prefill_archive_company",
    "prefill_certificate_template",
    "list_render_jobs",
    "create_render_job",
    "get_render_job_progress",
    "get_render_job_download_url",
    "create_render_job_and_wait",
    "materialize_render_job_artifact",
}
```

- [ ] **Step 5: Run the backend tests again**

Run:
```bash
cd /Users/chou/code/certificate/backend
uv run python -m pytest mcp_server/tests.py -q
```

Expected: PASS. The render tool suite now proves that presigned object URLs are downloaded without Bearer auth and emitted as local delivery artifacts.

- [ ] **Step 6: Commit**

```bash
cd /Users/chou/code/certificate/backend
git add mcp_server/tools/render.py mcp_server/server.py mcp_server/tests.py
git commit -m "feat: materialize certificate render artifacts in mcp"
```

### Task 3: Regenerate the workflow CLI so MCP and CLI stay in sync

**Files:**
- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/generate.sh`](/Users/chou/code/certificate/tools/certificate-workflow-cli/generate.sh)
- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.ts`](/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.ts)
- Modify: [`/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.js`](/Users/chou/code/certificate/tools/certificate-workflow-cli/certificate-workflow.js)

- [ ] **Step 1: Add the new tool to CLI generation input**

```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
```

```bash
TOOLS=(
  get_current_user
  list_partner_options
  list_certificate_template_options
  list_archive_companies
  get_archive_company
  list_archive_company_options
  list_system_certificates
  list_honor_certificates
  list_render_jobs
  list_resource_options
  prefill_qcc
  prefill_translate
  prefill_archive_company
  prefill_certificate_template
  upload_file
  upload_file_from_url
  create_archive_company
  update_archive_company
  batch_create_system_certificates
  update_system_certificate
  batch_create_honor_certificates
  update_honor_certificate
  create_render_job
  get_render_job_progress
  get_render_job_download_url
  create_render_job_and_wait
  materialize_render_job_artifact
)
```

- [ ] **Step 2: Regenerate the CLI bundle**

Run:
```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
./generate.sh
```

Expected: `certificate-workflow.ts` and `certificate-workflow.js` are regenerated successfully and contain `materialize-render-job-artifact`.

- [ ] **Step 3: Verify the generated CLI surface**

Run:
```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
node certificate-workflow.js --help | rg "materialize-render-job-artifact"
```

Expected: one matching help line for the new command.

- [ ] **Step 4: Commit**

```bash
cd /Users/chou/code/certificate/tools/certificate-workflow-cli
git add generate.sh certificate-workflow.ts certificate-workflow.js
git commit -m "chore: regenerate certificate workflow cli for artifact materialization"
```

### Task 4: Introduce Hermes hidden delivery artifacts

**Files:**
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_delivery_artifacts.py`
- Create: `/Users/chou/.hermes/hermes-agent/gateway/delivery_artifacts.py`

- [ ] **Step 1: Write focused extraction and sanitization tests**

```python
import json

from gateway.delivery_artifacts import (
    DeliveryArtifactStore,
    extract_delivery_artifacts,
    sanitize_tool_result_for_model,
)


def test_extract_delivery_artifacts_reads_wrapper_delivery_payload():
    content = json.dumps(
        {
            "mode": "execute",
            "delivery": {
                "filename": "report.zip",
                "local_path": "/tmp/report.zip",
                "size": 123,
                "media_tag": "MEDIA:/tmp/report.zip",
            },
        },
        ensure_ascii=False,
    )

    artifacts = extract_delivery_artifacts("certificate_workflow_tool", content)

    assert len(artifacts) == 1
    assert artifacts[0].local_path == "/tmp/report.zip"
    assert artifacts[0].filename == "report.zip"


def test_sanitize_tool_result_for_model_removes_paths_and_urls():
    content = json.dumps(
        {
            "status": "completed",
            "job_id": 92,
            "download_url": "https://example.com/final.zip?sig=1",
            "delivery": {
                "filename": "final.zip",
                "local_path": "/tmp/final.zip",
                "size": 2048,
                "media_tag": "MEDIA:/tmp/final.zip",
            },
        },
        ensure_ascii=False,
    )

    result = sanitize_tool_result_for_model("mcp_local_materialize_render_job_artifact", content)
    payload = json.loads(result)

    assert payload["delivery"]["filename"] == "final.zip"
    assert payload["delivery"]["size"] == 2048
    assert "local_path" not in payload["delivery"]
    assert "media_tag" not in payload["delivery"]
    assert "download_url" not in payload


def test_delivery_artifact_store_tracks_session_scoped_media_tags():
    store = DeliveryArtifactStore()
    store.record(session_key="juhe:S:1001", tool_use_id="tool-1", tool_name="certificate_workflow_tool", content=json.dumps({
        "delivery": {
            "filename": "bundle.zip",
            "local_path": "/tmp/bundle.zip",
            "media_tag": "MEDIA:/tmp/bundle.zip",
        }
    }))

    assert store.media_tags_for_session("juhe:S:1001") == ["MEDIA:/tmp/bundle.zip"]
    assert store.media_tags_for_session("juhe:S:2002") == []
```

- [ ] **Step 2: Run the new test file to verify it fails**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_delivery_artifacts.py -q
```

Expected: FAIL because `gateway.delivery_artifacts` does not exist yet.

- [ ] **Step 3: Implement the artifact helper module**

```python
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class DeliveryArtifact:
    tool_name: str
    local_path: str
    filename: str
    size: int | None
    media_tag: str


def _load_json(content: str) -> Any:
    try:
        return json.loads(content)
    except Exception:
        return None


def extract_delivery_artifacts(tool_name: str, content: str) -> list[DeliveryArtifact]:
    payload = _load_json(content)
    if not isinstance(payload, dict):
        return []
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        return []
    local_path = str(delivery.get("local_path") or "").strip()
    filename = str(delivery.get("filename") or "").strip()
    media_tag = str(delivery.get("media_tag") or "").strip()
    if not local_path or not filename or not media_tag.startswith("MEDIA:"):
        return []
    size_value = delivery.get("size")
    size = int(size_value) if isinstance(size_value, int) else None
    return [
        DeliveryArtifact(
            tool_name=tool_name,
            local_path=local_path,
            filename=filename,
            size=size,
            media_tag=media_tag,
        )
    ]


def sanitize_tool_result_for_model(tool_name: str, content: str) -> str:
    payload = _load_json(content)
    if not isinstance(payload, dict):
        return content
    delivery = payload.get("delivery")
    if isinstance(delivery, dict):
        delivery = dict(delivery)
        delivery.pop("local_path", None)
        delivery.pop("media_tag", None)
        payload["delivery"] = delivery
    payload.pop("download_url", None)
    payload.pop("output_url", None)
    return json.dumps(payload, ensure_ascii=False)


class DeliveryArtifactStore:
    def __init__(self) -> None:
        self._by_session: dict[str, list[DeliveryArtifact]] = {}

    def record(self, *, session_key: str, tool_use_id: str, tool_name: str, content: str) -> list[DeliveryArtifact]:
        artifacts = extract_delivery_artifacts(tool_name, content)
        if artifacts:
            self._by_session.setdefault(session_key, []).extend(artifacts)
        return artifacts

    def media_tags_for_session(self, session_key: str) -> list[str]:
        return [artifact.media_tag for artifact in self._by_session.get(session_key, [])]

    def clear_session(self, session_key: str) -> None:
        self._by_session.pop(session_key, None)
```

- [ ] **Step 4: Run the focused tests again**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_delivery_artifacts.py -q
```

Expected: PASS. The artifact model and helper behavior are locked before touching the runner.

- [ ] **Step 5: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add gateway/delivery_artifacts.py tests/gateway/test_delivery_artifacts.py
git commit -m "feat: add hidden delivery artifact helpers"
```

### Task 5: Hook hidden artifacts into the gateway runner and sanitize model-visible tool results

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_media_extraction.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_media_extraction.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/tools/test_tool_result_storage.py`](/Users/chou/.hermes/hermes-agent/tests/tools/test_tool_result_storage.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tools/tool_result_storage.py`](/Users/chou/.hermes/hermes-agent/tools/tool_result_storage.py)

- [ ] **Step 1: Replace the old URL-centric expectations with artifact-centric tests**

```python
def test_collect_tool_result_media_prefers_hidden_local_artifact():
    messages = [
        {
            "role": "tool",
            "tool_call_id": "cert-job",
            "content": json.dumps(
                {
                    "status": "completed",
                    "job_id": 92,
                    "delivery": {
                        "filename": "final.zip",
                        "size": 2048,
                    },
                }
            ),
        }
    ]
    hidden_media_tags = ["MEDIA:/tmp/final.zip"]

    augmented = _augment_final_response_with_tool_media(
        "压缩包已生成。",
        messages,
        set(),
        hidden_media_tags=hidden_media_tags,
    )

    assert "MEDIA:/tmp/final.zip" in augmented
    assert "download_url" not in augmented
    assert "/tmp/final.zip" in augmented


def test_certificate_render_job_result_is_sanitized_for_model():
    content = json.dumps(
        {
            "status": "completed",
            "job_id": 92,
            "download_url": "https://example.com/final.zip?sig=1",
            "delivery": {
                "filename": "final.zip",
                "local_path": "/tmp/final.zip",
                "size": 2048,
                "media_tag": "MEDIA:/tmp/final.zip",
            },
        },
        ensure_ascii=False,
    )

    result = maybe_persist_tool_result(
        content=content,
        tool_name="mcp_local_materialize_render_job_artifact",
        tool_use_id="tool-call-1",
        env=None,
        threshold=50000,
    )
    payload = json.loads(result)

    assert payload["delivery"]["filename"] == "final.zip"
    assert "local_path" not in payload["delivery"]
    assert "media_tag" not in payload["delivery"]
    assert "download_url" not in payload


def test_download_url_lookup_result_is_sanitized_for_model():
    content = json.dumps(
        {
            "job_id": 92,
            "download_url": "https://example.com/final.zip?sig=1",
        },
        ensure_ascii=False,
    )

    result = maybe_persist_tool_result(
        content=content,
        tool_name="mcp_local_get_render_job_download_url",
        tool_use_id="tool-call-2",
        env=None,
        threshold=50000,
    )
    payload = json.loads(result)

    assert payload["job_id"] == 92
    assert payload["delivery"]["status"] == "needs_materialization"
    assert "download_url" not in payload
```

- [ ] **Step 2: Run the targeted Hermes tests to verify they fail**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_media_extraction.py tests/tools/test_tool_result_storage.py -q
```

Expected: FAIL because `_augment_final_response_with_tool_media()` does not yet accept hidden media tags and `maybe_persist_tool_result()` still leaves old certificate delivery fields intact.

- [ ] **Step 3: Wire the callback and sidecar into `gateway/run.py`**

Make this change inside the existing per-turn callback wiring block in [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py), immediately next to the current `agent.tool_progress_callback = ...` assignment. Do not wire this in `AIAgent.__init__()`. The callback signature must match the runtime call order in [`/Users/chou/.hermes/hermes-agent/run_agent.py`](/Users/chou/.hermes/hermes-agent/run_agent.py): `(tool_call_id, function_name, function_args, function_result)`.

```python
from gateway.delivery_artifacts import DeliveryArtifactStore


def _augment_final_response_with_tool_media(
    final_response: str,
    messages: List[dict[str, Any]] | None,
    history_media_paths: set[str] | None,
    *,
    hidden_media_tags: list[str] | None = None,
) -> str:
    response = str(final_response or "")
    media_tags, has_voice_directive, raw_delivery_urls = _collect_tool_result_media_tags(
        messages,
        history_media_paths,
    )
    response = _strip_delivered_urls_from_response(response, raw_delivery_urls)

    extra_tags = [tag for tag in (hidden_media_tags or []) if tag not in media_tags]
    media_tags.extend(extra_tags)
    existing_media, _ = BasePlatformAdapter.extract_media(response)
    existing_paths = {path for path, _ in existing_media}
    existing_has_voice = "[[audio_as_voice]]" in response or any(is_voice for _, is_voice in existing_media)

    append_tags = [tag for tag in media_tags if tag.split("MEDIA:", 1)[1] not in existing_paths]
    if not append_tags:
        return response

    append_lines = list(append_tags)
    if has_voice_directive and not existing_has_voice:
        append_lines.insert(0, "[[audio_as_voice]]")

    if response:
        return response + "\n" + "\n".join(append_lines)
    return "\n".join(append_lines)

# inside GatewayRunner.__init__()
self._delivery_artifacts = DeliveryArtifactStore()


def _record_delivery_artifacts(self, session_key: str, tool_use_id: str, tool_name: str, content: str) -> None:
    try:
        self._delivery_artifacts.record(
            session_key=session_key,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            content=content,
        )
    except Exception:
        logger.debug("Failed to record delivery artifacts", exc_info=True)
```

Bind the callback on the main conversational agent in the same block that currently assigns `tool_progress_callback`, `step_callback`, and `stream_delta_callback`:

```python
def _tool_complete_callback(tool_call_id, function_name, function_args, function_result, *, _session_key=session_key):
    self._record_delivery_artifacts(_session_key, tool_call_id, function_name, function_result)


agent.tool_progress_callback = progress_callback if tool_progress_enabled else None
agent.tool_complete_callback = _tool_complete_callback
```

And use the hidden tags when finalizing the visible response:

```python
hidden_media_tags = self._delivery_artifacts.media_tags_for_session(session_key)
final_response = _augment_final_response_with_tool_media(
    final_response,
    result.get("messages", []),
    _history_media_paths,
    hidden_media_tags=hidden_media_tags,
)
```

- [ ] **Step 4: Replace the old certificate augmentation path with one sanitization pipeline in `tool_result_storage.py`**

Delete the legacy certificate-specific path from [`/Users/chou/.hermes/hermes-agent/tools/tool_result_storage.py`](/Users/chou/.hermes/hermes-agent/tools/tool_result_storage.py) before adding the new sanitizer. This means:

- remove `from gateway.delivery_rules import is_certificate_render_job_url`
- delete `_CERTIFICATE_RENDER_MCP_TOOLS`
- delete `_CERTIFICATE_DELIVERY_GUIDANCE`
- delete `_extract_delivery_urls()`
- delete `_strip_certificate_render_delivery_urls()`
- delete `_augment_certificate_delivery_result()`
- remove the line `content = _augment_certificate_delivery_result(content, tool_name)` from `maybe_persist_tool_result()`

Keep [`/Users/chou/.hermes/hermes-agent/gateway/delivery_rules.py`](/Users/chou/.hermes/hermes-agent/gateway/delivery_rules.py) itself for later adapter-side fallback detection, but `tool_result_storage.py` must stop importing or invoking it directly. After those deletions, wire the new sanitizer so there is exactly one model-visible sanitization path:

```python
from gateway.delivery_artifacts import sanitize_tool_result_for_model


def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
    threshold: int | float | None = None,
) -> str:
    content = sanitize_tool_result_for_model(tool_name, content)
    effective_threshold = threshold if threshold is not None else config.resolve_threshold(tool_name)
    if effective_threshold == float("inf"):
        return content
    if len(content) <= effective_threshold:
        return content

    storage_dir = _resolve_storage_dir(env)
    remote_path = f"{storage_dir}/{tool_use_id}.txt"
    preview, has_more = generate_preview(content, max_chars=config.preview_size)

    if env is not None:
        try:
            if _write_to_sandbox(content, remote_path, env):
                logger.info(
                    "Persisted large tool result: %s (%s, %d chars -> %s)",
                    tool_name,
                    tool_use_id,
                    len(content),
                    remote_path,
                )
                return _build_persisted_message(preview, has_more, len(content), remote_path)
        except Exception:
            logger.warning("Failed to persist tool result %s", tool_use_id, exc_info=True)
    return _build_persisted_message(preview, has_more, len(content), remote_path)
```

Also replace the old certificate delivery augmentation with a safe summary for URL-only render tools, including `mcp_local_create_render_job_and_wait` and `mcp_local_get_render_job_download_url`:

```python
if tool_name in {"mcp_local_create_render_job_and_wait", "mcp_local_get_render_job_download_url"}:
    payload = _try_load_json(content)
    if isinstance(payload, dict):
        payload.pop("download_url", None)
        payload.pop("output_url", None)
        payload["delivery"] = {
            "status": "needs_materialization",
            "message": "Render job ready. Call materialize_render_job_artifact with this job_id before sending the file.",
        }
        content = json.dumps(payload, ensure_ascii=False)
```

- [ ] **Step 5: Run the targeted Hermes tests again**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_delivery_artifacts.py tests/gateway/test_media_extraction.py tests/tools/test_tool_result_storage.py -q
```

Expected: PASS. Hermes can now keep artifact delivery capability without exposing paths or URLs to the model.

- [ ] **Step 6: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add gateway/run.py gateway/delivery_artifacts.py tools/tool_result_storage.py tests/gateway/test_delivery_artifacts.py tests/gateway/test_media_extraction.py tests/tools/test_tool_result_storage.py
git commit -m "feat: route certificate delivery through hidden artifacts"
```

### Task 6: Fail closed for generic document fallback and remove Juhe hard rejects

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_platform_base.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_platform_base.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/tools/test_send_message_tool.py`](/Users/chou/.hermes/hermes-agent/tests/tools/test_send_message_tool.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/base.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/platforms/juhe.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/juhe.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py`](/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py)

- [ ] **Step 1: Write the failing Hermes delivery tests**

```python
class DummyAdapter(BasePlatformAdapter):
    name = "dummy"
    async def connect(self): return True
    async def disconnect(self): return None
    async def send(self, chat_id, content, **kwargs): return SendResult(success=True, message_id="text-1")


@pytest.mark.asyncio
async def test_base_send_document_fails_closed_for_local_path():
    adapter = DummyAdapter()
    result = await adapter.send_document("room-1", "/tmp/report.zip")
    assert result.success is False
    assert "native document delivery" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_juhe_send_document_no_longer_hard_rejects_certificate_render_job_url(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr(
        adapter,
        "_materialize_remote_document_for_upload",
        AsyncMock(return_value=("/tmp/final.zip", AsyncMock())),
    )
    monkeypatch.setattr(
        adapter,
        "_stage_local_file_for_upload",
        AsyncMock(return_value=("https://temp.example.com/final.zip?sig=1", AsyncMock())),
    )
    monkeypatch.setattr(
        adapter,
        "_upload_file_from_url",
        AsyncMock(return_value={"error_code": 0, "data": {"message_id": "msg-1"}}),
    )

    result = await adapter.send_document(
        "S:1001",
        "http://49.233.103.196:9000/certificate-dev/certificate_render_jobs/2026/04/22/job-92/final.zip?X-Amz-Signature=abc",
    )

    assert result.success is True
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_platform_base.py tests/gateway/test_juhe.py tests/tools/test_send_message_tool.py -q
```

Expected: FAIL because `BasePlatformAdapter.send_document()` still echoes file paths as text and Juhe/send_message still contain certificate-specific hard-stop branches.

- [ ] **Step 3: Implement fail-closed base behavior and remove hard rejects**

Make surgical edits only. Keep the existing chunking, document-before-text ordering, image/video/voice routing, and disconnect behavior intact. In `tools/send_message_tool.py`, remove only the certificate-specific early-return guards. Do not rewrite `_send_to_platform()` or `_send_juhe()` wholesale.

```python
async def send_document(
    self,
    chat_id: str,
    file_path: str,
    caption: Optional[str] = None,
    file_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    **kwargs,
) -> SendResult:
    return SendResult(
        success=False,
        error="Native document delivery is not implemented for this platform adapter.",
    )
```

Remove the Juhe hard reject:

```python
async def send_document(
    self,
    chat_id: str,
    file_path: str,
    caption: Optional[str] = None,
    file_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    **kwargs,
) -> SendResult:
    return await self._send_file_from_source(
        chat_id=chat_id,
        source=file_path,
        file_type=5,
        caption=caption,
        reply_to=reply_to,
        metadata=kwargs.get("metadata"),
        file_name=file_name,
        caption_after_upload=True,
        materialize_remote_document=True,
    )
```

Remove only these certificate-specific guard branches from `tools/send_message_tool.py` so the adapter gets to decide how to deliver the document:

```python
# in _send_to_platform()'s Platform.JUHE branch
if contains_certificate_render_job_url(message):
    return _juhe_certificate_delivery_error()

# in _send_juhe()
if contains_certificate_render_job_url(message):
    return _juhe_certificate_delivery_error()

# in _send_juhe()'s document-media loop
if is_certificate_render_job_url(media_path):
    return _juhe_certificate_delivery_error()
```

Delete those three guards and leave the surrounding function bodies unchanged.

- [ ] **Step 4: Run the targeted tests again**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_platform_base.py tests/gateway/test_juhe.py tests/tools/test_send_message_tool.py -q
```

Expected: PASS. Hermes now fails closed on unsupported document delivery and Juhe uses generic remote-document materialization instead of certificate-specific special cases.

- [ ] **Step 5: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add gateway/platforms/base.py gateway/platforms/juhe.py tools/send_message_tool.py tests/gateway/test_platform_base.py tests/gateway/test_juhe.py tests/tools/test_send_message_tool.py
git commit -m "refactor: make juhe certificate delivery artifact-native"
```

### Task 7: Align prompts and run the Phase 1 verification suite

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/agent/prompt_builder.py`](/Users/chou/.hermes/hermes-agent/agent/prompt_builder.py)

- [ ] **Step 1: Update Juhe prompt guidance to match the new flow**

```python
"juhe": (
    "You are communicating through Juhe, an enterprise WeChat bridge. "
    "Use plain text formatting and avoid markdown-specific rendering assumptions. "
    "Use `certificate_workflow_tool` as the standard execution surface for "
    "certificate delivery. If you used MCP render tools directly, call "
    "`materialize_render_job_artifact` with the render job id before final delivery. "
    "Hermes will handle native file sending from the hidden artifact channel. "
    "Do not write local filesystem paths, MEDIA tags, or raw certificate render-job "
    "download URLs in the visible reply."
),
```

- [ ] **Step 2: Run the full Phase 1 test matrix**

Run:
```bash
cd /Users/chou/code/certificate/backend
uv run python -m pytest mcp_server/tests.py -q

cd /Users/chou/code/certificate/tools/certificate-workflow-cli
node --test workflow-wrapper.test.mjs

cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_delivery_artifacts.py tests/gateway/test_media_extraction.py tests/gateway/test_platform_base.py tests/gateway/test_juhe.py tests/tools/test_tool_result_storage.py tests/tools/test_send_message_tool.py -q
```

Expected: all three groups PASS.

- [ ] **Step 3: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add agent/prompt_builder.py
git commit -m "docs: align juhe prompt guidance with artifact delivery flow"
```

---

## Phase 2: Remove Other Chat Channels After Phase 1 Approval

## Phase 2 Guardrails

- Start Phase 2 only after Phase 1 tests pass and Claude explicitly approves the adapter-prune cut.
- Keep [`/Users/chou/.hermes/hermes-agent/gateway/platforms/api_server.py`](/Users/chou/.hermes/hermes-agent/gateway/platforms/api_server.py).
- Keep `Platform.LOCAL` because cron output still needs a sink.
- Defer `webhook.py` until a dedicated dependency audit shows it is safe to remove.
- Do not delete a non-Juhe file before migrating any Juhe-specific code or tests that landed there by accident.

### Task 8: Migrate Juhe-only tests out of non-Juhe files before deleting adapters

**Files:**
- Create: `/Users/chou/.hermes/hermes-agent/tests/gateway/test_juhe_parallel_attachments.py`
- Modify: [`/Users/chou/.hermes/hermes-agent/tests/gateway/test_telegram_photo_interrupts.py`](/Users/chou/.hermes/hermes-agent/tests/gateway/test_telegram_photo_interrupts.py)

- [ ] **Step 1: Move the Juhe burst-merging case into a Juhe-owned test file**

```python
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event


def test_merge_pending_photo_burst_keeps_juhe_parallel_attachment_fields_aligned():
    pending = {}
    first = MessageEvent(text="", message_type=MessageType.PHOTO, media_urls=["juhe://attachment/1/photo-a.jpg"])
    setattr(first, "_juhe_media_access_urls", ["https://minio.example/photo-a.jpg?X-Amz-Signature=1"])
    setattr(first, "_juhe_attachment_identities", [{"object_key": "photo-a.jpg"}])
    setattr(first, "_juhe_media_descriptions", ["图片 photo-a.jpg：营业执照第一页"])

    second = MessageEvent(text="", message_type=MessageType.PHOTO, media_urls=["juhe://attachment/2/photo-b.jpg"])
    setattr(second, "_juhe_media_access_urls", ["https://minio.example/photo-b.jpg?X-Amz-Signature=2"])
    setattr(second, "_juhe_attachment_identities", [{"object_key": "photo-b.jpg"}])
    setattr(second, "_juhe_media_descriptions", ["图片 photo-b.jpg：营业执照第二页"])

    merge_pending_message_event(pending, "juhe:S:1001", first)
    merge_pending_message_event(pending, "juhe:S:1001", second)

    merged = pending["juhe:S:1001"]
    assert merged.media_urls == [
        "juhe://attachment/1/photo-a.jpg",
        "juhe://attachment/2/photo-b.jpg",
    ]
    assert getattr(merged, "_juhe_media_access_urls") == [
        "https://minio.example/photo-a.jpg?X-Amz-Signature=1",
        "https://minio.example/photo-b.jpg?X-Amz-Signature=2",
    ]
```

- [ ] **Step 2: Remove the migrated test from the Telegram file**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway/test_juhe_parallel_attachments.py tests/gateway/test_telegram_photo_interrupts.py -q
```

Expected: PASS. The Juhe behavior is preserved and the Telegram file is cleanly Telegram-only.

- [ ] **Step 3: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add tests/gateway/test_juhe_parallel_attachments.py tests/gateway/test_telegram_photo_interrupts.py
git commit -m "test: move juhe attachment burst coverage out of telegram tests"
```

### Task 9: Remove non-Juhe adapter files, tests, and platform maps

**Files:**
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/telegram.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/discord.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/slack.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/whatsapp.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/signal.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/bluebubbles.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/qqbot.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/matrix.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/mattermost.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/homeassistant.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/dingtalk.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/feishu.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/wecom.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/wecom_callback.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/weixin.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/email.py`
- Delete: `/Users/chou/.hermes/hermes-agent/gateway/platforms/sms.py`
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/config.py`](/Users/chou/.hermes/hermes-agent/gateway/config.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py`](/Users/chou/.hermes/hermes-agent/tools/send_message_tool.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/hermes_cli/platforms.py`](/Users/chou/.hermes/hermes-agent/hermes_cli/platforms.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/toolsets.py`](/Users/chou/.hermes/hermes-agent/toolsets.py)

- [ ] **Step 1: Remove the deleted adapters from enum and tool maps**

```python
platform_map = {
    "juhe": Platform.JUHE,
}
```

And collapse platform registry defaults to Juhe plus non-chat internals:

```python
PLATFORMS = {
    "juhe": PlatformInfo(
        name="juhe",
        display_name="Juhe",
        default_toolset="hermes-juhe",
    ),
}
```

- [ ] **Step 2: Delete matching adapter tests and keep only Juhe coverage**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -c "import tools.send_message_tool; print('send_message_tool import ok')"
python -m pytest tests/gateway/test_juhe.py tests/gateway/test_juhe_parallel_attachments.py tests/tools/test_send_message_tool.py -q
```

Expected: PASS. `send_message_tool` imports cleanly even after the non-Juhe adapters are gone.

- [ ] **Step 3: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add gateway/config.py tools/send_message_tool.py hermes_cli/platforms.py toolsets.py tests/gateway tests/tools
git commit -m "refactor: prune non-juhe chat adapters"
```

### Task 10: Simplify gateway routing after the adapter prune

**Files:**
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/run.py`](/Users/chou/.hermes/hermes-agent/gateway/run.py)
- Modify: [`/Users/chou/.hermes/hermes-agent/gateway/delivery.py`](/Users/chou/.hermes/hermes-agent/gateway/delivery.py)

- [ ] **Step 1: Remove dead adapter branches while preserving `api_server` and `local`**

```python
def _init_adapter(self, platform: Platform, config: PlatformConfig):
    if platform == Platform.JUHE:
        from gateway.platforms.juhe import JuheAdapter, check_juhe_requirements
        if not check_juhe_requirements():
            logger.warning("Juhe: aiohttp/httpx not installed or JUHE credentials not set")
            return None
        return JuheAdapter(config)
    if platform == Platform.API_SERVER:
        from gateway.platforms.api_server import APIServerAdapter
        return APIServerAdapter(config)
    return None
```

And keep delivery routing conservative:

```python
if target.platform == Platform.LOCAL:
    result = self._deliver_local(content, job_id, job_name, metadata)
elif target.platform == Platform.JUHE:
    result = await self._deliver_to_platform(target, content, metadata)
else:
    raise ValueError(f"Unsupported delivery target: {target.platform.value}")
```

- [ ] **Step 2: Run the gateway-focused test pass**

Run:
```bash
cd /Users/chou/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/gateway -q
```

Expected: PASS. Remaining gateway tests cover Juhe, `api_server`, base platform behavior, and local delivery only.

- [ ] **Step 3: Commit**

```bash
cd /Users/chou/.hermes/hermes-agent
git add gateway/run.py gateway/delivery.py tests/gateway
git commit -m "refactor: simplify gateway for juhe-first routing"
```

---

## Self-Review

### Spec coverage

- “不要发送本地路径和下载链接” is covered by Task 4 and Task 5, which hide raw artifact fields from model-visible output and deliver only native attachments.
- “不要靠硬规则拦截” is covered by Task 5 and Task 6, which replace blocking with artifact capture, sanitization, and adapter-native delivery.
- “先解决文件下载，再去除其他聊天渠道” is enforced by the explicit Phase 1 gate before Tasks 8 to 10.
- “api_server.py 没必要动” is covered by the Phase 2 guardrails and Task 10 keep-list.
- “尽量不要改 qwsaas，除非有充分理由” is covered by the architecture decision to keep all transport work inside Hermes and the certificate backend, because Juhe already stages local files correctly.

### Placeholder scan

- No `TODO`, `TBD`, “implement later”, or “similar to Task N” placeholders remain.
- Every implementation task includes a code block, a test/run command, and an expected result.

### Consistency check

- The new MCP tool is consistently named `materialize_render_job_artifact`.
- The hidden Hermes module is consistently named `gateway/delivery_artifacts.py`.
- The artifact flow consistently uses `delivery.filename`, `delivery.local_path`, `delivery.size`, and `delivery.media_tag` as the raw hidden envelope, then strips sensitive fields from model-visible output.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-22-juhe-artifact-delivery-and-channel-prune.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
