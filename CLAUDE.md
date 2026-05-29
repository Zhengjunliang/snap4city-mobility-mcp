# snap4city-mobility-mcp - AI Agent System Prompt

## 1. Project Overview
**Langgraph MCP client** for referente's remote Snap4City mobility advisor server (UNIFI Sistemi Distribuiti elaborato Tipo A). 真实 MCP server 归 referente 部署在内网 (VPN+SSH tunnel 访问), 本项目只交付 **client + Langgraph orchestrator + CLI glue**。User asks a trip question → Langgraph agent calls remote MCP server's tools → returns multi-modal options for the Snap4City dashboard widget to render.
- **Stack**: Python 3.10+ + FastMCP 2.x **Client** + Langgraph 1.x (StateGraph orchestrator)
- **Transport**: HTTP Streamable → referente dashboard at `http://localhost:8000` (VPN+SSH tunnel 前提, 见 §5)
- **Frontend**: N/A (rendering by Snap4City dashboard widgets)
- **Database**: N/A (stateless client)
- **External**: Snap4City remote MCP server (referente-managed, VPN-only), Snap4City Agentic LLM (Langgraph integration, referente-managed)

## 2. Agent Execution Rules

1. **Before coding**: Read 相关现有文件理解代码风格再动手, 禁凭印象写。
2. **Schema 对齐**: 修改类型/模型时同步检查上下游一致 (DB ↔ 后端 model ↔ 前端 enum/type)。
3. **大型任务分阶段**: 全栈改动拆 DB / Backend / Frontend 独立 Stage, 禁单次对话跨 3 层全部代码。
4. **大文件拆分**: 超 250 行的单文件 (SFC / 模块) 先改逻辑层再改视图层, 禁一次性两层全改。
5. **Language**: 中文交流; 变量名/函数名英文; 注释英文。

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
| 装环境 | `uv sync` |
| VPN 连 | FortiClient 连 UNIFI Ateneo (见 `Istruzioni_VPNAteneo_Win_V1.0_2020.pdf`) |
| SSH tunnel 前提 | `ssh -L 8000:192.168.1.117:8000 zheng@150.217.15.125` (另一窗口保持) |
| Dashboard 自检 | `Invoke-RestMethod http://localhost:8000/apps.json \| ConvertTo-Json -Depth 8` |
| CLI 跑 orchestrator | `uv run snap4city-mobility-cli "<orig>" "<dest>" [foot_shortest\|car\|public_transport\|foot_quiet]` |

- **venv 不必激活**: `uv run` 自动指向 `.venv/`. 激活仅为省 `uv run` 前缀.
- **Tool 名前缀**: dashboard 多 server 模式下 FastMCP 给每个 tool 加 server 前缀 (例: `snap4agentic_advisor_native_<toolname>`), `call_tool` 时记得带前缀, 见 memory [[project-referente-endpoint]].
- **优先 native, 别用 legacy**: dashboard `Advisor Legacy` 警告会被删, 长期路径用 `snap4agentic_advisor_native`.
