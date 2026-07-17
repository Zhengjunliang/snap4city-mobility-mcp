# snap4city-mobility-mcp - AI Agent System Prompt

## 1. Project Overview
**Langgraph MCP client** for referente's remote Snap4City mobility advisor server (UNIFI Sistemi Distribuiti elaborato Tipo A). 真实 advisor MCP server 归 referente (内网, JupyterHub 直连)。本项目交付: **client + Langgraph orchestrator + FastAPI 桥 (`api.py`) + dashboard 前端聊天框 (`frontend/`) + 一个本地 MCP server (`mcp_server.py`: geocode + route)**。

- **确定性图** (L13): `understand → execute → respond`。Llama4 只在 `understand` (forced tool call 抽 slot) 和 `respond` (措辞) 出场, **从不自由调工具**; `execute` 纯 Python 跑工具流 → 出 widget JSON 给 dashboard 画。
- **支持范围**: point-to-point route (foot/car/public_transport) + **沿途服务** (用户点名类别 "con le farmacie lungo il percorso" → understand 的 `services_category` slot, execute 沿 route 几何采样锚点并发调远程 near-search, foot/car 沿线、bus 只上下车站+步行段, 按 mode 挂 `routes[i].services`, L53)。只有 `other` intent 返友好 unsupported (公交线路/时刻表 reference 问题随 tpl_* 删除, 不再支持); 到达时刻 ("arrivare per le 9") **不支持** (What-If servlet 无 `arrive_by`)。
- **端点解析** (L41/L43/L49/L52): 前端每轮随 `/advise` 发 `gps: {lat,lng}|null`。专名 geocode **无区域锁** — 点名城市优先 (`_narrow_by_city`), 否则 `_pick_coord` 按 haversine 取离**锚点**最近的候选 (终点锚定已解析起点, 起点锚定 GPS); **含门牌号的查询先走 civic 阶梯** (civic-exact StreetNumber 命中 > 街型标签 > anchor-nearest, `_normalize_feature` 透传 `civic`/`serviceType`, L52 — 无号查询行为不变; civic 落点精度受 km4city 数据限制 — 同侧多号常堆在地块角同一坐标, 见 L55)。geocoder 纯文本排序+top-N 截断会让锚点城市自己的候选缺席 (proximity 参数全无效) → 无点名城市时 `_geocode` 以**锚点城市追加重查** ("via Roma" → "via Roma, FIRENZE"; 城市来自 GPS reverse geocode 的 `municipality` 或起点 feature 的 `city`), 增强候选须与原文共享非路型词 token (`_signal_subset`), 不过则回退原结果 — 跨城地标 ("Piazza dei Miracoli") 不受影响 (L49)。**注意 km4city 数据实际只有 Toscana 有效** (无 Brescia/Milano 街道, 库外只返噪音) → 测试必须用 Toscana 内地点; 但**不加距离哨兵** (点名异地城市是合法查询, 150km 哨兵已于 2026-07-10 删除)。origin 缺省 = GPS 坐标本身 (调一次 `coordinates_to_address` 出回复标签; 无 GPS 则 missing_place 追问)。通用类别目的地 ("farmacia più vicina" → understand 的 `destination_category` slot 出英文 km4city 类别) 走远程 `service_search_near_gps_position` 半径阶梯 0.5→2→10 km, 全空退文本 geocode。
- **工具分布**: 本地 MCP server 两个 (client 用**独立 single-server client** 连, 避 L6 前缀税) — (1) `address_search_location` 包公开 km4city ServiceMap (referente 那个服务端坏, L28/L29); (2) 统一 `route` 工具包 What-If GraphHopper router, **foot/car/bus 全部走它** (referente 的 `routing` 已整体退役, L46)。bus 乘车段几何在 `route` 内部升级为**真 GTFS shape** (`gtfs_shapes.py` 调公开 km4city tpl API 按线路号+几何评分匹配方向变体、按上下车站切段, L51; 匹配不上退回 router 的站间直线 L44)。远程只剩 3 个: reverse geocode、near-search (parking 近搜带半径阶梯 0.5→1 km, 空 rung 扩搜, L56)、parking 实时。
- **桥协议 = job + poll** (L47): `POST /advise` 只**启动**本轮并秒回 `{job_id}`, 前端每 1.5s `GET /advise/{job_id}` (202 = 还在算, 带 `stage`/`elapsed_s` 让 thinking 气泡说出在干什么, 由 `run_advisor(on_stage=...)` 上报; 200 = widget JSON 原样透传)。job_id 和 stage 只活在传输层, 不进 payload (守规则 8)。**禁改回"一次请求拿结果"**: bus 轮次 50-70s, 反代链掐 60s 以上的单请求, 而心跳字节无效 (jupyter-server-proxy 对非 SSE 响应整体 buffer)。
- **默认三模式** (L31): 不指定 mode → foot + car + public_transport **一把扁平 `asyncio.gather` 全跑全画** (墙钟 = 最慢的 bus 30-45s: 线上 router 每 `vehicle=bus` 重建 PT 图, **已接受的延迟** — perf patch 交付已放弃, 2026-07-16); 指定 mode → 只跑那一条。整轮**一次答完** ("先答 foot/car、bus 后续写"的两阶段方案已试过并整体回退 — 不值得背状态机, 等待期有 stage 进度条即可)。
- **出发时刻**: understand 抽 `departure_time` ("alle 18" → `18:00`; 今天日期注入 prompt 让它能给 `YYYY-MM-DDTHH:MM`), **只**喂 public_transport 的 `startdatetime` (GraphHopper 无时变 foot/car 模型)。
- **geocode 有进程级 LRU 缓存** (km4city 地址索引是静态的)。测试须经 conftest 的 autouse fixture 清空, 否则缓存命中会跳过 FakeClient 队列里的响应, 后续每个 pop 全部错位。
- **Stack**: Python 3.10+ + FastMCP 2.x **Client** + Langgraph 1.x (StateGraph orchestrator)
- **Transport**: HTTP Streamable → referente dashboard (JupyterHub 内网直连 `192.168.1.117:8000`, 见 §5)
- **Frontend**: `frontend/mobility_advisor_dashboard.html` (CSBL HTML+JS 贴进 Snap4City widgetExternalContent, **三种 mode 全走 widgetMap manual 分支**直画后端几何 — bus 用后端切好的 per-leg 几何 (`routes[].legs`, 双色 + Gea-Night 公交车 icon pin), foot/car 整条 wkt 单 leg; widget 零 router 外呼, 图与字同到, 见 L44/L46; 规则 9 + `frontend/README.md`)。多模式轮次在 **#advChips dock** (聊天与输入框之间的固定条) 出 picker (每 mode 一枚纯模式名 chip + "Mostra tutte", 每个带路线轮次整体替换): 点选**纯本地**重画单条 + 弹 `routes[].detail` 详情气泡 (respond 预渲染, 复用 `_format_detail`; 不重跑 bus, 见 L50) + 切换 parking pins (car 显 foot/bus 隐) + 切换沿途服务 pins (换成该 route 的 `services`, "Mostra tutte" 恢复并集, L53), 选择回写 history 保 follow-up 语境。pin 删除 = `removeSelectorPin` 按 `desc` 键控 (L52); 服务 pin 图标 = widget 按 serviceUri 自渲染类别 icon, 我方只定底色 (紫 vs parking 蓝)。
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
9. **前端 CSBL 文件: 单份、去行首缩进、CSS 必须 scoped**: `frontend/` 的 widgetExternalContent HTML (CSBL) 是直接贴进 Snap4City CKEditor 的源码。(a) CKEditor **会把行首 tab/空格当内容渲染** → 文件**不留行首缩进、不留空行** (贴入即源码可跑)。(b) 只维护 **一份** paste-ready 文件 (规则 7), 禁另存 `*.min.html` 之类平行版本。(c) 它贴进的是 **dashboard 的 DOM (非 iframe)** → `<style>` 里**只允许 `.adv`-scoped 选择器**, 禁 `html`/`body`/裸标签/`*` (会命中整个父页面: 曾导致 widget 标题栏消失 + 幽灵滚动条, 见 L37)。撑满高度靠 host 给的容器, 别动全局。

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
| 起本地 MCP server (geocode + route, 见 L29/L46) | `python -m snap4city_mobility_mcp.mcp_server` (:8020, 工具: `address_search_location` 包公开 ServiceMap 绕 referente 坏索引 + `route` 包 What-If GraphHopper 出 foot/car/bus 全部路由; 跑桥前先起它; client 经 `S4C_LOCAL_MCP_URL` 默认 `http://127.0.0.1:8020/mcp` 连) |
| 跑 advisor 桥 (dashboard 联动 + 多轮测试) | `uvicorn api:app --host 0.0.0.0 --port 8010` (JupyterHub s4c env 内; 浏览器经 jupyter-server-proxy 同源访问, 见 L27; **job+poll 协议**: `POST /advise` 秒回 `{job_id}` → `GET /advise/{job_id}` 轮询 (202=还在算, 200=widget JSON), 见 L47 — 单请求跨整轮会被反代 60s 掐, 心跳流式无效; 每轮全量 JSON 写 `outputs.txt`, 诊断写 `debug.log`) |
| 本地 mock 测 | `uv run pytest -q` (不需 LLM/MCP) |

- **whatif-router URL (默认线上, 2026-07-10)**: `route` 工具 (foot/car/bus 全部) **默认打线上 `https://www.snap4city.org/whatif-router/route`** — referente 已灌 Toscana GTFS (at+gest), 实测返真公交, 本地自托管不再必需。要测其他 router 构建才设 env `S4C_WHATIF_ROUTER_URL` 覆盖。**注意**: 线上每 `vehicle=bus` 请求重建 PT 图 ~30-45s — **已接受的延迟** (perf patch 交付已放弃并删除, 2026-07-16; `whatif-local/` 已整体移出 repo, 本地残留目录被 .gitignore 挡住), `BUS_ROUTE_TIMEOUT_S=120` 兜住, 等待期靠 stage 进度可见 (L47); **foot/car 不碰 PT 图, 实测 0.3-0.5s** (L46), 走通用超时。
- **代码更新后两个进程都要重启** (mcp_server :8020 + uvicorn 桥) — 只重启桥会跑旧 mcp_server, 症状离奇 (L54: civic 全 None 端点漂移, 其实是漏重启)。
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
