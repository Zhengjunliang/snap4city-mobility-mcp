# snap4city-mobility-mcp - AI Agent System Prompt

## 1. Project Overview
**Langgraph MCP client** for referente's remote Snap4City mobility advisor server (UNIFI Sistemi Distribuiti elaborato Tipo A). 真实 MCP server 归 referente 部署在内网 (Snap4City JupyterHub 内网直连访问), 本项目只交付 **client + Langgraph orchestrator + CLI glue**。User asks a trip/transport question → Langgraph **agentic graph** (understand → agent ⇄ tools → format) drives Llama4 to call the remote MCP tools → returns widget JSON for the Snap4City dashboard to render.
- **Stack**: Python 3.10+ + FastMCP 2.x **Client** + Langgraph 1.x (StateGraph orchestrator)
- **Transport**: HTTP Streamable → referente dashboard (JupyterHub 内网直连 `192.168.1.117:8000`, 见 §5)
- **Frontend**: N/A (rendering by Snap4City dashboard widgets)
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
| CLI 跑 advisor | `snap4city-mobility-cli "<自然语言问题>"` (无参 = 多轮 REPL) |
| 本地 mock 测 | `uv run pytest -q` (不需 LLM/MCP) |

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
