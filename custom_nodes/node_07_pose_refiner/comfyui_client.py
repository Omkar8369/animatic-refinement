"""Minimal ComfyUI HTTP-API client for Node 7.

Node 7 runs on the RunPod pod (locked decision #13); this client is
how `pipeline.cli_node7.main` (run from the pod) drives ComfyUI's
REST API at `http://127.0.0.1:8188`. We stay on stdlib urllib so no
new Python dependency is needed.

Endpoints used:
  * POST /prompt              - submit a (parameterized) workflow
  * GET  /history/<prompt_id> - poll for completion
  * GET  /view                - download one output image

Everything else (images/uploads, interrupt, queue status, websocket
notifications) is ignored on purpose: the orchestrator's usage is
strictly "submit, poll, fetch" one workflow at a time.

Errors map to Node 7's typed hierarchy:
  * Any network failure or non-2xx response -> ComfyUIConnectionError
  * A successful submit that reports node_errors             -> RefinementGenerationError
  * A workflow that finishes with no image output            -> RefinementGenerationError
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.errors import (
    ComfyUIConnectionError,
    RefinementGenerationError,
)


DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"


@dataclass(frozen=True)
class PromptSubmission:
    """Identifier returned by ComfyUI when a prompt is accepted."""
    promptId: str
    number: int


class ComfyUIClient:
    """Thin wrapper around ComfyUI's HTTP API.

    Constructor args:
        base_url: ComfyUI root, e.g. `http://127.0.0.1:8188`. The
            orchestrator passes a CLI-configurable value; default is
            the standard ComfyUI port on `localhost` so running the
            CLI on the pod (where ComfyUI is listening locally) Just
            Works.
        timeout_seconds: per-HTTP-request timeout. Generation itself
            is polled separately via `wait_for_completion` which has
            its own total-wait budget.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_COMFYUI_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    # -----------------------------------------------------------------
    # POST /prompt
    # -----------------------------------------------------------------

    def submit_prompt(self, prompt_graph: dict[str, Any]) -> PromptSubmission:
        """Submit a ComfyUI workflow graph (the `prompt` field of the
        API payload). Returns the `prompt_id` ComfyUI assigns.

        Raises:
            ComfyUIConnectionError: network failure, non-2xx response,
                or malformed JSON body.
            RefinementGenerationError: ComfyUI accepted the request
                structurally but reported per-node validation errors
                (e.g. missing weight, invalid type).
        """
        body = json.dumps({"prompt": prompt_graph}).encode("utf-8")
        url = f"{self.base_url}/prompt"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_seconds
            ) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = _safe_read_error(e)
            raise ComfyUIConnectionError(
                f"POST {url} failed with HTTP {e.code}: {detail}"
            ) from e
        except urllib.error.URLError as e:
            raise ComfyUIConnectionError(
                f"POST {url} could not reach ComfyUI: {e.reason}. "
                "Is ComfyUI running on the pod (port 8188)?"
            ) from e

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ComfyUIConnectionError(
                f"POST {url} returned non-JSON body: {raw[:200]!r}"
            ) from e

        node_errors = parsed.get("node_errors")
        if node_errors:
            raise RefinementGenerationError(
                f"ComfyUI rejected the workflow: node_errors={node_errors}. "
                "Usually means a weight file declared in models.json "
                "is not yet on disk, or a node name in workflow.json "
                "does not match the installed custom-node version."
            )

        prompt_id = parsed.get("prompt_id")
        if not prompt_id:
            raise RefinementGenerationError(
                f"ComfyUI accepted the prompt but returned no "
                f"prompt_id. Response: {parsed!r}"
            )
        return PromptSubmission(
            promptId=str(prompt_id),
            number=int(parsed.get("number", 0)),
        )

    # -----------------------------------------------------------------
    # GET /history/<prompt_id>
    # -----------------------------------------------------------------

    def wait_for_completion(
        self,
        prompt_id: str,
        total_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        """Block until the given prompt finishes (or total_timeout
        elapses). Returns the `history[prompt_id]` payload.

        Raises:
            ComfyUIConnectionError: network failure or total timeout.
            RefinementGenerationError: prompt finished with a
                workflow-level execution error.
        """
        deadline = time.monotonic() + total_timeout_seconds
        url = f"{self.base_url}/history/{urllib.parse.quote(prompt_id)}"
        while True:
            try:
                with urllib.request.urlopen(
                    url, timeout=self.timeout_seconds
                ) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                detail = _safe_read_error(e)
                raise ComfyUIConnectionError(
                    f"GET {url} failed with HTTP {e.code}: {detail}"
                ) from e
            except urllib.error.URLError as e:
                raise ComfyUIConnectionError(
                    f"GET {url} could not reach ComfyUI: {e.reason}."
                ) from e

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ComfyUIConnectionError(
                    f"GET {url} returned non-JSON body: {raw[:200]!r}"
                ) from e

            entry = parsed.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    raise RefinementGenerationError(
                        f"Workflow execution failed for prompt_id="
                        f"{prompt_id!r}: {messages}"
                    )
                if status.get("completed", False):
                    return entry

            if time.monotonic() > deadline:
                raise ComfyUIConnectionError(
                    f"Timed out after {total_timeout_seconds}s waiting "
                    f"for prompt_id={prompt_id!r} to complete. "
                    "ComfyUI might be stuck on a model load; check "
                    "the pod's ComfyUI console output."
                )
            time.sleep(poll_interval_seconds)

    # -----------------------------------------------------------------
    # GET /view
    # -----------------------------------------------------------------

    def fetch_output_image(
        self,
        filename: str,
        subfolder: str = "",
        image_type: str = "output",
        dest_path: Path | str = "",
    ) -> Path:
        """Download one image from ComfyUI's output directory via the
        `/view` endpoint and write it to `dest_path`.
        """
        if not dest_path:
            raise RefinementGenerationError(
                "fetch_output_image() called without dest_path."
            )
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        params = urllib.parse.urlencode({
            "filename": filename,
            "subfolder": subfolder,
            "type": image_type,
        })
        url = f"{self.base_url}/view?{params}"
        try:
            with urllib.request.urlopen(
                url, timeout=self.timeout_seconds
            ) as resp, open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
        except urllib.error.HTTPError as e:
            detail = _safe_read_error(e)
            raise RefinementGenerationError(
                f"GET {url} failed with HTTP {e.code}: {detail}. "
                "The workflow reported a saved image ComfyUI cannot "
                "serve -- likely a filename mismatch between "
                "workflow.json's SaveImage node and what ComfyUI "
                "actually wrote."
            ) from e
        except urllib.error.URLError as e:
            raise ComfyUIConnectionError(
                f"GET {url} could not reach ComfyUI: {e.reason}."
            ) from e
        return dest


def extract_first_image(
    history_entry: dict[str, Any],
    save_node_id: str,
) -> tuple[str, str]:
    """Pull the first `(filename, subfolder)` from the SaveImage-ish
    node's output slot in a /history entry.

    Raises:
        RefinementGenerationError: expected node missing from outputs,
            or no images in its slot.
    """
    outputs = history_entry.get("outputs", {})
    node_out = outputs.get(save_node_id)
    if not node_out:
        raise RefinementGenerationError(
            f"Workflow finished but node_id={save_node_id!r} produced "
            f"no outputs. Available keys: {list(outputs.keys())}."
        )
    images = node_out.get("images") or []
    if not images:
        raise RefinementGenerationError(
            f"Workflow finished but node_id={save_node_id!r} emitted "
            "no images."
        )
    first = images[0]
    return str(first.get("filename", "")), str(first.get("subfolder", ""))


def _safe_read_error(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", errors="replace")[:400]
    except Exception:  # noqa: BLE001 - best-effort error detail
        return "<unreadable>"


__all__ = [
    "ComfyUIClient",
    "DEFAULT_COMFYUI_URL",
    "PromptSubmission",
    "extract_first_image",
]
