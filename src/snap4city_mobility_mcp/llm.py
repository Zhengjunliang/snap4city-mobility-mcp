"""Llama4 inference client — Snap4City ClearML on-demand agentic API.

Wraps the auth + inference flow from referente's reference example. Auth and
inference live behind https://www.snap4city.org, but the API rule is bound to a
function account and only authorizes requests from the Snap4City JupyterHub — so
this client returns real answers only when run there.

Endpoint `llama4-agentic-inference` is OpenAI/MCP-compatible (vLLM):
  - Send OpenAI `messages` (chat, multimodal text + image_url) via `chat()`.
  - Pass `tools` (OpenAI function schema) + `tool_choice="auto"` to let the model
    emit `tool_calls` — the basis for an agentic loop over the MCP tools. Feed
    each tool result back as a `{"role": "tool", ...}` message in the next turn.
  - Response is always an OpenAI object: `choices[0].message.{content, tool_calls}`.
    We default `tool_choice="none"`, which forces the OpenAI format even when no
    tools are passed (otherwise the endpoint would fall back to a legacy shape).

Request envelope: {access_token, endpoint, params:{messages, tools?, tool_choice, temperature?}}.

Credentials are read from a user_credentials.json file (same {"username",
"password"} shape as referente's example) — nothing sensitive lands in git (the
file is .gitignored). Search order:
    S4C_CREDENTIALS_FILE -> ./user_credentials.json -> <repo>/user_credentials.json
Optional endpoint overrides: S4C_LLM_API_URL, S4C_LLM_ENDPOINT.
TokenManager caches/refreshes the access token in token_stored.json.
"""
import asyncio
import json
import os
import pathlib
import time
from typing import Any

import httpx

from snap4city_mobility_mcp.token_manager import TokenManager

LLAMA4_API_URL = os.environ.get(
    "S4C_LLM_API_URL", "https://www.snap4city.org/apis/llama4-agentic-inference"
)
LLAMA4_ENDPOINT = os.environ.get("S4C_LLM_ENDPOINT", "llama4-agentic-inference")
# Reference example showed tens of seconds round-trip; allow generous headroom.
LLM_TIMEOUT_S = 120.0
# The Snap4City gateway returns a transient error envelope (e.g. "The upstream
# server is timing out") when the vLLM backend is slow to warm up or busy — a heavy
# agent turn (long system prompt + 7 tool schemas, tool_choice=auto) is the usual
# trigger. The backend is typically warm by the next attempt, so retry a few times.
LLM_RETRIES = 2
LLM_RETRY_BACKOFF_S = 4.0
# Substrings (case-insensitive) that mark a gateway/backend error worth retrying.
# The gateway can answer HTTP 200 while wrapping an upstream failure in the body, e.g.
# "Failed to make POST request to .../llama4-agentic-inference. Error: 500 Server Error:
# Internal Server Error" — the vLLM backend choked (often a too-large request). Treat
# those as transient: a slimmer / retried turn usually clears it.
_TRANSIENT_HINTS = (
    "timing out", "timeout", "timed out", "upstream", "temporarily unavailable",
    "overloaded", "try again", "bad gateway", "503", "502", "504",
    "internal server error", "server error", "failed to make post request",
)


class Llama4Error(RuntimeError):
    """Inference API returned an error envelope (or non-JSON) instead of `choices`."""


def _is_transient(message: str | None, status_code: int) -> bool:
    """True when a gateway/backend failure is worth retrying (vs. a hard error)."""
    if status_code in (429, 500, 502, 503, 504):
        return True
    if message:
        low = message.lower()
        return any(hint in low for hint in _TRANSIENT_HINTS)
    return False


CREDENTIALS_FILENAME = "user_credentials.json"


def _credentials_file() -> pathlib.Path | None:
    """First existing user_credentials.json (see module docstring for search order)."""
    candidates: list[pathlib.Path] = []
    env_path = os.environ.get("S4C_CREDENTIALS_FILE")
    if env_path:
        candidates.append(pathlib.Path(env_path))
    cwd = pathlib.Path.cwd()
    candidates.append(cwd / CREDENTIALS_FILENAME)
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    candidates.append(repo_root / CREDENTIALS_FILENAME)
    return next((p for p in candidates if p.is_file()), None)


def _load_credentials() -> tuple[str, str]:
    """Read username/password from a user_credentials.json file (no env fallback)."""
    path = _credentials_file()
    if path is None:
        raise Llama4Error(
            "no user_credentials.json found — set S4C_CREDENTIALS_FILE to its path, "
            "or place it in the working dir or llmagentic/"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Llama4Error(f"could not read credentials file {path}: {e}") from e
    username = data.get("username") if isinstance(data, dict) else None
    password = data.get("password") if isinstance(data, dict) else None
    if not username or not password:
        raise Llama4Error(f"{path} is missing 'username' or 'password'")
    return username, password


def assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    """`choices[0].message` from an OpenAI response ({} if absent)."""
    choices = response.get("choices") or []
    if not choices:
        return {}
    return choices[0].get("message", {}) or {}


def tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Pending `tool_calls` from the response ([] if none / final answer)."""
    return assistant_message(response).get("tool_calls") or []


class Llama4Client:
    """Snap4City Llama4 agentic client.

    TokenManager (sync, requests-based) handles auth; the inference POST uses
    httpx to stay consistent with the rest of the package. Use `achat` from
    async code (Langgraph nodes) — it offloads the blocking call to a thread.
    """

    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        if not (username and password):
            username, password = _load_credentials()
        self._tm = TokenManager(username, password)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "none",
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """OpenAI `messages` (+ optional tools) -> full OpenAI response.

        Inspect the result with `assistant_message()` / `tool_calls()`. Multimodal
        content (image_url parts) is supported inside each message's `content`
        list. `tool_choice` defaults to "none" so the response is always the
        OpenAI `choices` format; pass `tools` + `tool_choice="auto"` to let the
        model decide and emit tool calls.

        Transient gateway/backend failures (upstream timeouts, 5xx, network
        resets) are retried up to LLM_RETRIES times with linear backoff; hard
        errors (bad credentials, inactive API rule) are raised immediately.
        """
        params: dict[str, Any] = {"messages": messages, "tool_choice": tool_choice}
        if tools is not None:
            params["tools"] = tools
        if temperature is not None:
            params["temperature"] = temperature

        token = self._tm.get_token()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        body = {"access_token": token, "endpoint": LLAMA4_ENDPOINT, "params": params}

        last_exc: Llama4Error | None = None
        for attempt in range(LLM_RETRIES + 1):
            try:
                resp = httpx.post(
                    LLAMA4_API_URL, json=body, headers=headers, timeout=LLM_TIMEOUT_S
                )
            except httpx.HTTPError as e:
                # Network-level failure (read timeout, connection reset) — transient.
                last_exc = Llama4Error(f"inference request failed: {e}")
                if attempt < LLM_RETRIES:
                    time.sleep(LLM_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                raise last_exc from e

            try:
                data = resp.json()
            except ValueError as e:
                raise Llama4Error(
                    f"non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
                ) from e
            if isinstance(data, dict) and "choices" in data:
                return data
            # Error envelopes: {"message": ...} / {"detail": ...}. (A deprecated endpoint
            # or an inactive API rule shows up here as e.g. "Rule not found for this user/path".)
            msg = (data.get("message") or data.get("detail")) if isinstance(data, dict) else None
            err = Llama4Error(
                msg or f"unexpected response (HTTP {resp.status_code}): {resp.text[:200]}"
            )
            # Retry transient gateway/backend errors (upstream timeout warming up vLLM);
            # raise hard errors (bad creds, inactive rule) on the first try.
            if _is_transient(msg, resp.status_code) and attempt < LLM_RETRIES:
                last_exc = err
                time.sleep(LLM_RETRY_BACKOFF_S * (attempt + 1))
                continue
            raise err

        # The loop always returns or raises above; this only satisfies type-checkers.
        raise last_exc or Llama4Error("inference failed after retries")  # pragma: no cover

    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "none",
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Async `chat` — runs the blocking call in a worker thread."""
        return await asyncio.to_thread(
            lambda: self.chat(
                messages, tools=tools, tool_choice=tool_choice, temperature=temperature
            )
        )
