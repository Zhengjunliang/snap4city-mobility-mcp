# Next Phase Tracker — Snap4City Mobility Advisor MCP

> **Purpose**: starting context for a **new Claude Code conversation** after onboarding (Phase 1-3) and Phase 4. Self-contained — a fresh session should pick up cold from this file + `README.md` + `CLAUDE.md`.

## TL;DR for the new conversation

UNIFI Sistemi Distribuiti **elaborato Tipo A** (4-week mini-project): build a **FastMCP client + Langgraph orchestrator + CLI glue** that calls DISIT's Snap4City Agentic LLM tools. The MCP server itself is referente-managed (deployed on the intranet, reached directly from Snap4City JupyterHub at `192.168.1.117:8000`) — this project does not ship a server. User asks a trip question → Langgraph agent calls the remote tools → returns multimodal options → Snap4City dashboard widget renders.

**Phase 4 完成 (2026-05-25)**, 进入 **Phase 5**。Phase 4 的本地 stand-in (`server.py` + `_helpers.py`) 已在 Phase 5 §2 退役, 真实存活的 Phase 4 deliverable = `orchestrator.py` + `cli.py`。CLI 现走 HTTP Streamable transport 直打 referente dashboard, 不再 spawn stdio 子进程。

**2026-06-03 更新**: Phase 5 §2 (切远程 server) 完成。运行环境确定为 **Snap4City JupyterHub** (referente 要求 Python 开发在专用 Jupyter 跑; JupyterHub 直连内网 MCP `192.168.1.117:8000` 不用 SSH tunnel; orchestrator 靠 `S4C_DASHBOARD_URL` env 切本地/Jupyter)。**Llama4 LLM 已接入** (Phase 5 §3 client 部分): endpoint `llama4-agentic-inference` (OpenAI 兼容 + 原生 tool calling), client = `src/snap4city_mobility_mcp/llm.py` 的 `Llama4Client`, JupyterHub 实测通 (0.33s)。详见 memory [[project-jupyterhub-runtime]] + `docs/lessons.md` L9/L10。

## Phase 4 deliverables (closed)

> Phase 5 §2 已退役 stand-in 资产 — 下面标 ~~retired~~ 的项目当时确实交付了, 现已删除。真实存活的 Phase 4 输出 = orchestrator.py + cli.py。

- ~~**MCP tools** (`src/snap4city_mobility_mcp/server.py`)~~ — **retired Phase 5 §2** (stand-in 本地 server 删除, 远程 referente server 接管):
  - ~~`locations` — 模糊地址搜索, wraps `/location/?excludePOI=true` (Tuscany-locked)~~
  - ~~`shortestpath` — km4city graph routing, wraps `/shortestpath`, 4 种 route_type~~
- **Orchestrator** (`src/snap4city_mobility_mcp/orchestrator.py`) — **存活, transport 已切**:
  - Langgraph `StateGraph` 4 节点链 (`resolve_origin → resolve_destination → compute_route → format_output`), 错误短路到 `format_output`
  - ~~`PythonStdioTransport` + `sys.executable` 显式钉死~~ → Phase 5 §2 改为 `StreamableHttpTransport` 直打 referente dashboard
- **CLI** (`src/snap4city_mobility_mcp/cli.py` + `pyproject.toml [project.scripts]`) — **存活**:
  - Console script `snap4city-mobility-cli "<origin>" "<destination>" [route_type]`
  - 不是 MCP tool, 是顶层 Langgraph 程序; 内部以 HTTP Streamable client 直打远程 server 跑双 tool 链
- **Docs**:
  - `docs/snap4city-api-notes.md` §1 (`locations` field-by-field) + §2 (`shortestpath` 真实响应 vs OpenAPI spec 偏差) — 仍保留作为 km4city 后端字段参考; referente server 大概率包同后端, 但工具名 + 入参签名见 §3 (Phase 5 §2 R0 探针产出)
  - `docs/lessons.md` ~~L1 (FastMCP Client 相对 import 陷阱)~~ (Phase 5 §2 删) + L2 (OpenAPI spec vs 真实响应) + L3 (km4city short-window stale routes) + L4 (`[project.scripts]` 改后必重装启动器)
- **Verification matrix**: V1-V8 全通 (happy walk 0.68 km, src==dst empty routes, 乱码 geocode HTTP 500 origin 短路, car-ZTL empty routes, public_transport empty routes, …), ruff 干净 — Phase 5 §2 R4 用 CLI 端到端重跑同一矩阵 (走远程 server) 验证一致性

## Phase 5 outline (Post-MVP)

按重排后的依赖 / 优先级:

1. ~~**切到 referente 远程 MCP server**~~ ✅ **完成 (2026-05-28)**. VPN+SSH tunnel 通, dashboard at `http://localhost:8000`, native server 25 工具全可达。本 stage 同时**彻底退役本地 stand-in** (`server.py` / `_helpers.py` 已删, `pyproject` server script 已删, `Sample tool.py` 移到 `docs/reference/`)。R0 探针 / R1 transport (StreamableHttp via Client(cfg)) / R2 签名适配 (`address_search_location` + `routing` + 坐标拆 float) / R3 删 stand-in / R4 端到端 (happy 0.68 km foot 通 / src==dst 正确捕 -2 / car-ZTL 揭露 referente wrapper bug → L8) / R5 文档全同步。沉淀: lessons L5 (apps.json 内网 IP) + L6 (FastMCP prefix policy) + L7 (envelope error_code=0 vs error_message=successful) + L8 (car-ZTL wrapper bug)。
2. **pytest 单测网** (`pytest-asyncio` + `httpx.MockTransport`) — **首项的 §1 之后立刻接**。原放第 1 位; 切远程优先级压过, 因 stand-in 删完后 client 侧 mock 目标才稳定 (不再是 server 内部 tool 函数, 而是 transport 层 / Client.call_tool 返回结构)。覆盖目标: `orchestrator._resolve_endpoint` / `_compute_route` 各错误分支, `_first_coord` GeoJSON 顺序边界, transport mock 模拟 referente envelope。
3. **接 LLM** — ✅ **client 完成 (2026-06-03)**. 模型已定 = Snap4City **Llama4** (`llama4-agentic-inference`, OpenAI 兼容 + tool calling), 不是 Claude/GPT。`llm.py` `Llama4Client.chat()` 就绪 + mock 测过。**剩余 = 接进 Langgraph**, 两条路待定 (用户表示先把现有确定性链在 JupyterHub 跑通再决定):
   - **A. 确定性链不变 + LLM 只做 NLU**: 自然语言 "我要从 Duomo 走到 Santa Croce" → 拆 `origin`/`destination`/`route_type` 喂现有 4 节点 graph; LLM 另做输出叙述。简单可控。
   - **B. 全 agentic loop**: 把 MCP geocode/routing 包成 OpenAI function schema 喂 LLM, LLM 自己决定调哪个工具 (`tool_choice="auto"` → `tool_calls` → 执行 → `role:"tool"` 回填)。灵活, 更贴 "agentic", 但更难控。
4. **接 Snap4City dashboard** — 聊天界面调用 LLM agent, 地图组件渲染 `journey.routes[0].wkt` LINESTRING。Widget URL 模式待 referente 确认。
5. ~~**替换 `_helpers.py` 为 referente 真版**~~ — **删项**: Phase 5 §1 (切远程) 同步把 `_helpers.py` 一起删了, 无对象可替换; `Sample tool.py` 已移到 `docs/reference/` 作为 referente 参考代码归档, 不抽取为 MCP tool (本项目 client-only)。

## Phase 6 — Integration & polish (报告 / 交付)

- Hand server URL + tool manifest to referente for Langgraph integration test
- 写最终报告 — 从 `README.md` (运营 walkthrough) + 本文件 phase 历史 + per-phase git commit + dashboard / CLI 截图 拼装
- 按 disit.org/5986 生成最终 ZIP / RAR (代码 + 报告 + 截图)

## Living open questions (carry forward, update weekly)

1. Mid-term checkpoint expected by Prof. Nesi?
2. Report language: it / en / zh?
3. ~~Transport preference (stdio vs HTTP/SSE) for Langgraph integration?~~ — 已定 HTTP Streamable (Phase 5 §1 R1 切完)
4. **referente server 是否要 auth token?** — `apps.json` 看到的 `auth` 字段可能空可能有要求, 部分 server 进 dashboard 时可能需要 token (memory [[project-referente-endpoint]] 第 2 条)。R0 探针出来如发现 401 / unauthorized, 要跟 referente 问 token 怎么发。
5. ZIP deliverable structure per disit.org/5986?
6. Dashboard widget URL pattern for embedding rendered routes?
7. ~~`Sample tool.py` 是 reference 代码还是要抽取为 tpl_* MCP tool?~~ — 已定: 移到 `docs/reference/Sample tool.py` 作 referente 参考归档, 不抽取 (本项目 client-only)
8. **legacy vs native server 在 referente 那的去除时间表?** — dashboard warning 说 legacy 上线版会删, 我们已锁 native; 但 native 的 endpoint path 看起来不是标准 FastMCP `/mcp` mount (`/tool/search`), 要跟 referente 确认是否长期稳定

## How a new Claude Code conversation should start

Paste this prompt into a fresh session to bootstrap context:

> 这是 UNIFI Sistemi Distribuiti elaborato Tipo A 的延续会话. 项目: snap4city-mobility-mcp (**Langgraph MCP client** for referente's remote Snap4City server). Phase 1-4 完成, Phase 5 §1-2 (切远程 server) 完成; 运行环境已迁到 **Snap4City JupyterHub**, Llama4 agentic LLM client (`llm.py`) 已加。请按顺序读: `CLAUDE.md` (尤其 §5.1 运行模式) → `README.md` → `docs/next-phase.md` → `docs/lessons.md` (尤其 L9/L10) → `src/snap4city_mobility_mcp/orchestrator.py` → `src/snap4city_mobility_mcp/llm.py` → `src/snap4city_mobility_mcp/cli.py`。然后 `git log -5` + `git status`。准备好后告诉我下一步 (LLM 接进 Langgraph 的 A/B 方案 / pytest / dashboard), 我们决定本 stage 范围。

运行环境 = **Snap4City JupyterHub** (CLAUDE.md §5.1): 浏览器登录, 内网直连不用 VPN/SSH tunnel; conda 3.11 env (kernel `s4c`, 见 lessons L9); 跑前设 `S4C_USERNAME` / `S4C_PASSWORD` (MCP endpoint orchestrator 默认已指 `192.168.1.117:8000`, 改动用 `S4C_DASHBOARD_URL`)。LLM (Llama4) 真跑。

JupyterHub 内自检:

```bash
curl -s http://192.168.1.117:8000/apps.json | python -m json.tool | head
```

Expected: JSON with `mcpServers` listing `snap4agentic_advisor_native` / `_legacy` / `_experimental`. 然后跑一发 CLI smoke:

```bash
snap4city-mobility-cli "Piazza Duomo Firenze" "Piazza Santa Croce Firenze" foot_shortest
```

Expected: `ok=true`, `summary.distance_km ≈ 0.68`. 偶发 `ok=false (no route found)` 间隔 ≥ 5s 重跑, 见 `docs/lessons.md` L3。
