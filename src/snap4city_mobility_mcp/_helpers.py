"""复刻 DISIT fastMCP 工具约定的最小可用 helpers (referente 未公开源码).

referente `Sample tool.py` 用了 `_safe_get` / `create_success` / `create_error`
/ `_describe_payload` 这些助手, 但没贴定义. 这里实现最小兼容版, 等 referente
公开真版直接 drop-in 替换本模块.
"""
from typing import Any

import httpx


async def _safe_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """GET → (payload, error, http_meta).

    成功: payload 是解析后的 JSON dict, error 是 None.
    失败: payload 是 None, error 是人读字符串.
    http_meta 总含 `{"url": ..., "status": ...}` 用于排错.
    """
    meta: dict[str, Any] = {"url": url, "status": None}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url, headers=headers or {})
            meta["status"] = r.status_code
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}: {r.text[:200]}", meta
            return r.json(), None, meta
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}", meta
    except ValueError as e:
        return None, f"Invalid JSON in response: {e}", meta


def create_success(
    data: Any,
    total: int | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """包装成功结果为标准信封."""
    out: dict[str, Any] = {"ok": True, "data": data}
    if total is not None:
        out["total"] = total
    if meta is not None:
        out["meta"] = meta
    return out


def create_error(message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """包装失败为标准信封."""
    out: dict[str, Any] = {"ok": False, "error": message}
    if meta is not None:
        out["meta"] = meta
    return out


def _describe_payload(payload: dict[str, Any], hint: str | None = None) -> dict[str, Any]:
    """轻量 schema 提示: 顶层 keys + 可选标签."""
    out: dict[str, Any] = {
        "keys": list(payload.keys()) if isinstance(payload, dict) else None
    }
    if hint:
        out["hint"] = hint
    return out
