# snap4city-mobility-mcp - AI Agent System Prompt

## 1. Project Overview
**Langgraph MCP client** for referente's remote Snap4City mobility advisor server (UNIFI Sistemi Distribuiti elaborato Tipo A). 真实 MCP server 归 referente 部署在内网 (Snap4City JupyterHub 内网直连访问), 本项目只交付 **client + Langgraph orchestrator + FastAPI 桥 (`api.py`) + dashboard 前端聊天框 (`frontend/`) 测试胶水**。User asks a trip question → Langgraph **deterministic graph** (understand → execute → respond) drives the flow: Llama4 只在 `understand` (forced slot 提取) 和 `respond` (措辞) 出场, **从不自由调工具**; `execute` 用 Python 确定性跑 MCP 工具流 → returns widget JSON for the Snap4City dashboard to render. 见 lesson L13 (为何砍掉 `agent ⇄ tools` agentic 回路)。支持 point-to-point **route** (foot/car/public_transport), **GPS "near" 端点解析** (L41): 前端每轮随 `/advise` 发浏览器 `gps: {lat,lng}|null` → 专名 geocode **全球无区域锁** (用户点名的城市优先 `_narrow_by_city`; 无城市时 `_pick_coord` 按 haversine 取离锚点最近的候选 — 终点锚定已解析起点、起点锚定 GPS, 见 L43; 注意: 代码不设区域限制, 但 km4city **数据实际仅 Toscana 有效** — 无 Brescia/Milano 街道, 库外查询只返模糊噪音, 测试须用 Toscana 内地点 (150km GPS 哨兵已删 2026-07-10: 用户点名异地城市是合法查询, 见 L41); origin 缺省 = GPS 坐标本身 (调一次 `coordinates_to_address` 给回复标签, 无 GPS 则 missing_place 追问); 通用类别目的地 ("farmacia più vicina", understand 的 `destination_category` slot 出英文 km4city 类别) 走 referente `service_search_near_gps_position` 半径阶梯 0.5→2→10 km 取最近, 全空退文本 geocode。只有 `other` intent 返友好 unsupported (公交线路/时刻表 reference 问题已随 tpl_* 删除, 不再支持)。**两个本地工具兜底 referente 缺口** (都在本地 MCP server `mcp_server.py`, client 经独立 single-server client 连, 避 L6 前缀): (1) **forward geocode** — referente 的 `address_search_location` 服务端坏 (L28), 本地 `address_search_location` 包公开 km4city ServiceMap (L29; 无 selection 偏置, 对排序零作用 L41); (2) **public_transport 路由** — referente 的 `routing` 的 `public_transport` 永不返真公交 (任何日期/OD 都退化纯步行或 -2, L19), 本地 `bus_route` 包 Snap4City What-If GraphHopper router (`vehicle=bus`, Gea-Night dashboard 同源, L19)。`execute` 的 PT mode 调本地 `bus_route` 出真公交线; foot/car routing、reverse、near-search 仍走 referente 远程。即本项目现交付 client + Langgraph + 桥 + 前端 + **一个本地 MCP server (geocode + bus_route)**。
- **Stack**: Python 3.10+ + FastMCP 2.x **Client** + Langgraph 1.x (StateGraph orchestrator)
- **Transport**: HTTP Streamable → referente dashboard (JupyterHub 内网直连 `192.168.1.117:8000`, 见 §5)
- **Frontend**: `frontend/mobility_advisor_dashboard.html` (CSBL HTML+JS 贴进 Snap4City widgetExternalContent, bus 画线走 widgetMap **manual** 分支直画后端切好的 per-leg 几何 (`routes[].legs`, router 只调一次, 图与字同到, 双色+站点 pin); foot/car 走 `drawMethod:"fetch"` 分支, 见 L44; 规则 9 + `frontend/README.md`)
- **Database**: N/A (stateless client)
- **External**: Snap4City remote MCP server (referente-managed, 内网 / JupyterHub 直连), Snap4City Agentic LLM (Langgraph integration, referente-managed)

## 2. Agent Execution Rules

1. **Before coding**: Read 相关现有文件理解代码风格再动手, 禁凭印象写。
2. **Schema 对齐**: 修改类型/模型时同步检查上下游一致 (DB ↔ 后端 model ↔ 前端 enum/type)。
3. **大型任务分阶段**: 全栈改动拆 DB / Backend / Frontend 独立 Stage, 禁单次对话跨 3 层全部代码。
4. **大文件拆分**: 超 250 行的单文件 (SFC / 模块) 先改逻辑层再改视图层, 禁一次性两层全改。
5. **Language**: 中文交流; 变量名/函数名英文; 注释英文。
6. **Git 归用户**: AI **永不** `git commit` / `git push`。可改文件 / `git add` / `git diff` / `git status` 给用户看, 但**提交 + 推送一律用户手动**。
7. **单一正确实现, 删噪音**: 代码库只留**一份正确实现**。重构/重设计 **原地替换**, 禁建平行/备选版本 (`advisor_graph_v2`、`*_old`、注释掉的旧逻辑 之类), 禁为"留底/留历史"保留旧代码 (历史归 git)。死代码 / 废弃路径 / 备选实现 = 噪音, 一律删干净。
8. **输出格式遵循官方, 禁自创字段**: 对外输出严格按 referente MCP server / OpenAI 标准形状, **不加自创便利字段**。LLM 回复读 `messages[-1].content` (OpenAI 标准), 禁加 `answer` 之类冗余字段。不确定某字段是否官方/widget 是否需要 → **问 referente**, 别自己塞 (例: `data.arcs` 待 referente 确认前注释掉)。
9. **前端 CSBL 文件: 单份、去行首缩进**: `frontend/` 的 widgetExternalContent HTML (CSBL) 是直接贴进 Snap4City CKEditor 的源码。CKEditor **会把行首 tab/空格当内容渲染** → 文件**不留行首缩进、不留空行** (贴入即源码可跑)。只维护 **一份** paste-ready 文件 (规则 7), 禁另存 `*.min.html` 之类平行版本。

## 3. Autopilot Workflow

触发: 消息含 "autopilot" 或 `/oh-my-claudecode:autopilot`。OMC 框架管理编排, 项目特定约束写在 §1-2。
Phase 5 收尾必做: 架构陷阱沉淀到 `docs/lessons.md` (新增编号条目, 格式: `L{n} {一句话陷阱描述} — {根因+修复策略}`)。

## 4. Lessons 文档约定

- `docs/lessons.md`: 架构陷阱沉淀 (踩过的坑 → 一次性, 避免下次重复)
- 单条 lesson ≥ 800 字时迁移到 `docs/archive/lessons_archive.md`, 主文件留索引行

## 5. Run & Verify (quick reference)

详细安装/运行 → `README.md` (用户和 referente 看的). 本节供 AI agent 后续会话速查.

| 目的 | 命令 |
|---|---|
| 装环境 (conda 3.11, 见 §5.1) | `uv sync` (或 conda env + `pip install -e .`) |
| Dashboard 自检 (JupyterHub 内) | `curl -s $S4C_DASHBOARD_URL/apps.json` |
| 起本地 MCP server (geocode + bus_route, 见 L29/L19) | `python -m snap4city_mobility_mcp.mcp_server` (:8020, 工具: `address_search_location` 包公开 ServiceMap 绕 referente 坏索引 + `bus_route` 包 What-If GraphHopper 出真公交线; 跑桥前先起它; client 经 `S4C_LOCAL_MCP_URL` 默认 `http://127.0.0.1:8020/mcp` 连) |
| 跑 advisor 桥 (dashboard 联动 + 多轮测试) | `uvicorn api:app --host 0.0.0.0 --port 8010` (JupyterHub s4c env 内; 浏览器经 jupyter-server-proxy 同源访问, 见 L27; `curl -s -X POST localhost:8010/advise -d '{"query":...,"history":[]}'` 自检; 每轮全量 JSON 写 `outputs.txt`, 诊断写 `debug.log`) |
| 本地 mock 测 | `uv run pytest -q` (不需 LLM/MCP) |

- **whatif-router URL (默认已回线上, 2026-07-10)**: `bus_route` (PT 公交线) **默认打线上 `https://www.snap4city.org/whatif-router/route`** — referente 已灌 Toscana GTFS (at+gest), 实测返真公交, 本地自托管不再必需 (运行 harness 脚本已删)。`whatif-local/` 现只留 `patches/` (给 referente 的 perf patch) + 应用/测试说明; 要测本地补丁构建才设 env `S4C_WHATIF_ROUTER_URL` 覆盖成本地 router。**注意**: 线上还没合 `pt-router-singleton` perf patch → 每 PT 请求 ~30-40s (`BUS_ROUTE_TIMEOUT_S=120` 兜住); patch 合入后可把该超时收回通用值。
- **venv 不必激活**: `uv run` 自动指向 `.venv/`. 激活仅为省 `uv run` 前缀.
- **Tool 名前缀**: dashboard 多 server 模式下 FastMCP 给每个 tool 加 server 前缀 (例: `snap4agentic_advisor_native_<toolname>`), `call_tool` 时记得带前缀, 见 memory [[project-referente-endpoint]].
- **优先 native, 别用 legacy**: dashboard `Advisor Legacy` 警告会被删, 长期路径用 `snap4agentic_advisor_native`.

### 5.1 运行环境 (2026-06-03, 见 memory [[project-jupyterhub-runtime]])

唯一运行环境 = **Snap4City JupyterHub** (referente 要求 Python 开发在专用 Jupyter 跑; 浏览器登录, 内网直连, 不用 VPN/SSH tunnel)。本地 Windows 只做编辑, git push 后在 JupyterHub `git clone` 跑 (功能账号目录, 名见 memory [[project-jupyterhub-runtime]])。

| 项 | 值 |
|---|---|
| MCP server | 内网直连 `192.168.1.117:8000` (orchestrator 默认已指这) |
| env var | `S4C_DASHBOARD_URL=http://192.168.1.117:8000` (可不设, 默认即此); creds 走文件不走 env (见下) |
| LLM | 真跑 (Llama4, 绑功能账号只能从 JupyterHub 调) |

- **登录**: snap4city.org → `Strumenti di sviluppo` → `Jupyter Hub - Python` (功能账号 + 密码见 memory [[project-jupyterhub-runtime]], 不写进 repo).
- **Llama4 LLM**: endpoint `llama4-agentic-inference` (OpenAI 兼容, 原生 tool calling + 多模态, JupyterHub 实测通 0.33s). `src/snap4city_mobility_mcp/llm.py` 的 `Llama4Client.chat(messages, tools, tool_choice)` → OpenAI `choices`; 解析用 `assistant_message()`/`tool_calls()`. 默认 `tool_choice="none"` 保证 OpenAI 格式. creds 从 `user_credentials.json` 文件读 (env 不读; 搜索序 `S4C_CREDENTIALS_FILE` → cwd → repo 根), 文件被 `.gitignore` 挡住不进 git, 手动放 repo 根目录. **只能从 JupyterHub 调** (规则绑功能账号).
- **Python**: JupyterHub 默认 kernel 3.9.7 太老 (fastmcp 要 ≥3.10) → conda 建 3.11 环境再装依赖.
