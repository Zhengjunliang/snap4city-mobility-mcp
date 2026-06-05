# Lessons — 架构陷阱沉淀

> 每条 `L{n} {仍生效的绑定事实 + 修复}`. 只留仍约束当前设计的坑。

---

L13 Llama4 `tool_choice="auto"` 自由调工具**不可靠**: 模型一旦想叙述 (如 routing 失败后写 "retrying with a different profile…") 就把 tool call 写成 **prosa 前缀 + pythonic 文本** `The request failed…[routing(...)]` 而非结构化 `tool_calls` → 漏成最终答案, `data` 空, 工具没真跑 (是 L11 同类问题反复发作; `recover_pythonic_tool_calls` 打补丁只治标, 且 prosa-前缀变体它也接不住)。**治本: 砍掉 `agent ⇄ tools` agentic 回路**, 改线性确定性图 `understand → execute → respond`: `understand` 用 forced `tool_choice={extract_slots}` (强制结构化, 从不走 pythonic) 只提取 `{intent, origin, destination, mode}`; `execute` 纯 Python 按 mode 跑固定流 (geocode×2 + `routing(mode)`, foot_quiet 失败确定性回退 foot_shortest); `respond` 用 `tool_choice="none"` (不传 tools) 只措辞 → LLM **永不自由调工具**, pythonic-leak 这条路彻底消失。代价: tpl_* 发现链暂不支持 (返友好提示)。`recover_pythonic_tool_calls`/`_parse_pythonic_calls` 一族 + agentic 节点已删 (单一正确实现, 历史归 git)。

L2 km4city `routing` 的 `journey.routes` 是 **list** of `{arc,distance,eta,time,wkt}` (OpenAPI spec 误写成 dict); `ok` / HTTP 200 ≠ 找到路 — `routes` 可空 (车在步行街 / 起终点同点)。必检 `len(journey.routes) > 0`; 写工具前 live-probe, 别只信 spec; 整 envelope 透传上层按真实结构挑。

L3 km4city 冷启动 / 短时间重复命中相同坐标对时偶发返**临时空 routes**, 几秒后自愈。`routing_with_retry` 隔 6s 重试 1 次遮掉。transient — 跟稳定的 L8 区分。

L4 加 / 改 `[project.scripts]` 后, `.venv` 启动器 stub **不会**因源码改动自动重生 — 跑 `uv run <name>` (自动 sync + 重建) 或 `uv pip install -e .`。(`'<name>' is not recognized` ≠ PATH 问题。)

L5 dashboard `/apps.json` 返回的 server URL **写死内网 IP** (`http://192.168.1.117:8000/...`), host 不同就连不上。[mcp_tools._build_config](../src/snap4city_mobility_mcp/mcp_tools.py) 拿到 cfg 后先把该 IP 替换成 `DASHBOARD_URL` 再喂 `Client`。

L6 FastMCP **只在 multi-server cfg** 才给 tool 名加 `<server>_` 前缀; 我们 single-native-server cfg 不加, 直接 `call_tool("routing", ...)` 裸名即通。扩到 multi 会突然加前缀, 所有 hardcoded 名崩 — `_build_config` 只挑 native 保持 single-server。

L7 km4city `routing` envelope 里 `response.error_code` 是**唯一**成功权威 (`"0"` = OK); `response.error_message` 成功时也非空 (`"successful"`)。永远别拿 `error_message` 当失败信号。

L8 referente 的**车在中心步行街** (Florence ZTL Duomo→Santa Croce car) 返裸顶层 `{"error": ""}` (wrapper bug, 没透传 km4city `-2` envelope); **稳定**, 重试不愈 (不同于 transient L3), 表现成 "empty body" → 建议改 foot/public_transport。第 3 次仍空 = L8; 隔 ≥5s 出 envelope = L3。

L9 JupyterHub 默认 kernel = Python 3.9, 太老装不了 fastmcp (要 ≥3.10), `pip install fastmcp` 报误导性的 "no matching distribution"。建 conda 3.11 env + ipykernel (`s4c`)。git 可用, uv 通常没装 (用 pip/conda)。

L12 Llama4 后端 (vLLM) context 一大就**炸/幻觉**: 网关返 HTTP **200** 但 body 包上游错误 `Failed to make POST request to ...:8080/serve/llama4-agentic-inference. Error: 500 Server Error: Internal Server Error` → 原 `_is_transient` 不认 (只列 502/503/504) → 不重试直接崩。根因常是请求太大: `tools` 节点把工具原始返回**整个塞回** messages (geocode 100 个 feature / routing 超长 wkt+逐 arc), 每轮重读越堆越大。修: (a) `_TRANSIENT_HINTS` 加 `internal server error`/`server error`/`failed to make post request` → 当 transient 重试; (b) [mcp_tools.slim_result_for_llm](../src/snap4city_mobility_mcp/mcp_tools.py): 喂模型的 **message** 瘦身 (geocode 留 top-5 + {address,city,coordinates}; routing 丢 wkt, arc→去重街道名), 而 **audit `tool_results` 留完整** → widget `data` 不受影响 ([orchestrator.tools](../src/snap4city_mobility_mcp/orchestrator.py) 分两路)。小 context = 少 500 + 少幻觉。

L11 km4city geocoder **不再 region-locked** (索引现含 Valencia/ES + 法国南部), `address_search_location("...Firenze")` 可返 100 条全西班牙/法国命中、零托斯卡纳。叠加 `excludePOI` 默认 `true` 把"Piazza del Duomo"(广场=POI)滤掉 → 只剩模糊街道匹配落到西班牙。schema 无地理约束参数。修复 ([mcp_tools.exec_tool](../src/snap4city_mobility_mcp/mcp_tools.py)): 强制 `excludePOI=false` (让地标可被找到) + 客户端按 `TUSCANY_BBOX` (lng 9.6–12.5, lat 42.2–44.5) 过滤 feature, 保留 score 序; 区内空 → 返 `{error}` 给 agent 重试。另: Llama4 偶把 tool call 写成裸 pythonic 文本而非结构化 `tool_calls`, 两种变体都会漏成最终答案 → `data` 空、routing 不真跑、答案是幻觉: (a) `;` 分隔多调用 `fn(); fn()`; (b) `[fn(...)]` 后粘聊天模板 `assistant\n\n<编造答案>`. `_parse_pythonic_calls` 已加: exec-mode 解析多语句 + `_leading_bracket_group` 只取开头平衡 `[...]` 丢尾部模板文本。**(geocode 过滤部分仍生效; pythonic 恢复部分已被 L13 取代 — agentic 回路与 `_parse_pythonic_calls` 一族删除, LLM 不再自由调工具。)**

L10 Llama4 旧 endpoint `llama4-inference` 即使 auth 成功 (200) 也返 `{"message":"Rule not found..."}` (~0.1-0.3s) — auth ≠ inference 授权。用 OpenAI 兼容的 agentic endpoint `llama4-agentic-inference` ([llm.py](../src/snap4city_mobility_mcp/llm.py) 默认), 顺带解锁原生 tool calling。响应是 OpenAI `{choices:[{message:{content,tool_calls}}]}`。
