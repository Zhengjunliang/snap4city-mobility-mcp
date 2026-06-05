# Lessons — 架构陷阱沉淀

> 每条 `L{n} {仍生效的绑定事实 + 修复}`. 只留仍约束当前设计的坑。

---

L2 km4city `routing` 的 `journey.routes` 是 **list** of `{arc,distance,eta,time,wkt}` (OpenAPI spec 误写成 dict); `ok` / HTTP 200 ≠ 找到路 — `routes` 可空 (车在步行街 / 起终点同点)。必检 `len(journey.routes) > 0`; 写工具前 live-probe, 别只信 spec; 整 envelope 透传上层按真实结构挑。

L3 km4city 冷启动 / 短时间重复命中相同坐标对时偶发返**临时空 routes**, 几秒后自愈。`routing_with_retry` 隔 6s 重试 1 次遮掉。transient — 跟稳定的 L8 区分。

L4 加 / 改 `[project.scripts]` 后, `.venv` 启动器 stub **不会**因源码改动自动重生 — 跑 `uv run <name>` (自动 sync + 重建) 或 `uv pip install -e .`。(`'<name>' is not recognized` ≠ PATH 问题。)

L5 dashboard `/apps.json` 返回的 server URL **写死内网 IP** (`http://192.168.1.117:8000/...`), host 不同就连不上。[mcp_tools._build_config](../src/snap4city_mobility_mcp/mcp_tools.py) 拿到 cfg 后先把该 IP 替换成 `DASHBOARD_URL` 再喂 `Client`。

L6 FastMCP **只在 multi-server cfg** 才给 tool 名加 `<server>_` 前缀; 我们 single-native-server cfg 不加, 直接 `call_tool("routing", ...)` 裸名即通。扩到 multi 会突然加前缀, 所有 hardcoded 名崩 — `_build_config` 只挑 native 保持 single-server。

L7 km4city `routing` envelope 里 `response.error_code` 是**唯一**成功权威 (`"0"` = OK); `response.error_message` 成功时也非空 (`"successful"`)。永远别拿 `error_message` 当失败信号。

L8 referente 的**车在中心步行街** (Florence ZTL Duomo→Santa Croce car) 返裸顶层 `{"error": ""}` (wrapper bug, 没透传 km4city `-2` envelope); **稳定**, 重试不愈 (不同于 transient L3), 表现成 "empty body" → 建议改 foot/public_transport。第 3 次仍空 = L8; 隔 ≥5s 出 envelope = L3。

L9 JupyterHub 默认 kernel = Python 3.9, 太老装不了 fastmcp (要 ≥3.10), `pip install fastmcp` 报误导性的 "no matching distribution"。建 conda 3.11 env + ipykernel (`s4c`)。git 可用, uv 通常没装 (用 pip/conda)。

L11 km4city geocoder **不再 region-locked** (索引现含 Valencia/ES + 法国南部), `address_search_location("...Firenze")` 可返 100 条全西班牙/法国命中、零托斯卡纳。叠加 `excludePOI` 默认 `true` 把"Piazza del Duomo"(广场=POI)滤掉 → 只剩模糊街道匹配落到西班牙。schema 无地理约束参数。修复 ([mcp_tools.exec_tool](../src/snap4city_mobility_mcp/mcp_tools.py)): 强制 `excludePOI=false` (让地标可被找到) + 客户端按 `TUSCANY_BBOX` (lng 9.6–12.5, lat 42.2–44.5) 过滤 feature, 保留 score 序; 区内空 → 返 `{error}` 给 agent 重试。另: Llama4 偶把多 tool call 写成 `;` 分隔裸 pythonic 文本 (`fn(); fn()`), `_parse_pythonic_calls` 原只认 `[...]`/单调用 → 漏成最终答案; 已加 exec-mode 解析多语句。

L10 Llama4 旧 endpoint `llama4-inference` 即使 auth 成功 (200) 也返 `{"message":"Rule not found..."}` (~0.1-0.3s) — auth ≠ inference 授权。用 OpenAI 兼容的 agentic endpoint `llama4-agentic-inference` ([llm.py](../src/snap4city_mobility_mcp/llm.py) 默认), 顺带解锁原生 tool calling。响应是 OpenAI `{choices:[{message:{content,tool_calls}}]}`。
