# Next Phase Tracker — Snap4City Mobility Advisor MCP

> 新会话起步用。架构/规则 → `CLAUDE.md §1`; 工具签名 → `docs/snap4city-api-notes.md`; 踩坑 → `docs/lessons.md` (至 L19)。本文件只跟踪**活动状态**: 做完啥 / 下一步 / 待问 referente。

## Done
- 远程 referente MCP server 接通 (HTTP Streamable, JupyterHub 内网直连)。
- Llama4 LLM client (`llm.py`, endpoint `llama4-agentic-inference`)。
- 确定性 orchestrator (`understand → execute → respond`) + 终端多轮 REPL (`chat.py`, 全量 JSON → `outputs.txt`); 本地 mock 单测 (`tests/`) 绿。
- **foot route** JupyterHub 实测通 (干净多语回复 + 全 WKT, 无 pythonic leak)。
- **car / public_transport routing 失败已定性** (`scripts/probe_routing.py` 裸调 + L19): car/PT 对**任意 OD** (含非-ZTL Campo di Marte) 全返裸 `{"error":""}`, 同 OD 的 foot_quiet 正常 → **服务端 car/PT wrapper 专门坏, 非 ZTL / 非坐标 / 非客户端**。
- respond 不再把服务端空错误谎报成 ZTL/步行区 (L19c)。
- geocode 加固: 两段式 `excludePOI` + 城市阶梯 + label 子集选点 (L17), 治跨城同名 / POI 误排。
- 会话级对象 (`_CFG`/`_LLM`) 进程级懒缓存, 删每轮 schema 拉取 (L16)。
- tpl_* 发现链已编码 (`tpl.py`, `run_tpl_flow`) — payload 形状为防御性猜测, **未 live 校准**。

## Next
1. **上报 referente**: car/PT routing 服务端返空 (附 `probe_routing.py` 输出 + L19) → 确认是 wrapper bug 还是 routetype 不支持。
2. **tpl_* live 校准** (chat.py): "quali linee ci sono?" / "percorsi/fermate della linea 6" / "orari della linea 6 alla fermata X" → 校准 `tpl.py` payload 形状假设 (raw head 进 debug.log; AtF URI 接受度)。
3. **PT route 校准**: 找能通的 quarter 跑 PT → 校准 `group_arc_legs` 临时 leg 分组键 (依赖 Next #1 先确认 PT 服务端可用)。
4. **Dashboard widget 接线**: chat UI → `run_advisor`; map widget 渲染 `data.wkt` LINESTRING (widget URL 模式问 referente)。
5. **终报 + ZIP** (disit.org/5986) — 代码 + 报告 + 截图。

## Open questions (待问 referente)
1. core 工具是否需 auth token? (目前未见)
2. widget 是否消费 `data.arcs` (逐段明细, 现注释掉省 ~90% payload)? 同样确认新字段 `data.legs` (PT) 、`data.lines/routes/stops/timeline` (tpl)。
3. dashboard widget 嵌入渲染路线的 URL 模式?
4. 报告语言: it / en / zh?
