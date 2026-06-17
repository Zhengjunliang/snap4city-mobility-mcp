# Snap4City Mobility Advisor — 代码逐函数完全讲解（中文）

> 给自己读的完整说明：每个文件、每个函数**输入 / 输出 / 用途 / 做了什么 / 为什么这么做**，
> 一步一步把整条流程串起来。设计原因（如"为什么 temperature=0"）是重点，凡涉及
> temperature、tool_choice、required、重试、瘦身、缓存、确定性的地方都会回答"为何这样、不那样"。
>
> 配套：架构陷阱见 `docs/lessons.md`（文中以 L{n} 引用）。

---

## 0. 一句话总览

用户用自然语言问一个出行/公交问题 → 一个**线性确定性 Langgraph 图**驱动：

```
用户问句 ──▶ understand ──▶ execute ──▶ respond ──▶ END ──▶ 返回 widget JSON
            (LLM 提槽)    (纯Python跑工具)  (LLM措辞)
```

- `understand`：**LLM** 用强制工具调用，从用户最新一句里提取结构化"槽位"（意图、起点文本、终点文本、模式…）。
- `execute`：**纯 Python，无 LLM**，按意图确定性地跑固定的 MCP 工具链（route：geocode×2 + routing；tpl_*：发现链）。
- `respond`：**LLM** 不带工具，只把结构化结果"措辞"成自然语言；然后组装对外的 widget JSON。

**核心设计思想（L13）**：LLM **从不自由调工具**。它只负责两件最擅长的事——挑槽位（强制结构化）和遣词造句；工具的编排全交给 Python。这样彻底消除了 Llama4 "想叙述时把工具调用写成普通文本漏进答案"的失败模式。

### 文件清单

| 文件 | 职责 |
|---|---|
| `src/snap4city_mobility_mcp/orchestrator.py` | Langgraph 图：understand / execute / respond 三节点 + 装配 + `run_advisor` 入口 |
| `src/snap4city_mobility_mcp/tpl.py` | 公交（TPL）发现链：lines/routes/stops/timeline 的确定性流程 + 解析 + 瘦身 |
| `src/snap4city_mobility_mcp/mcp_tools.py` | 客户端 MCP 层：连服务端、执行工具调用、抹平 km4city 怪癖（geocode/routing） |
| `src/snap4city_mobility_mcp/llm.py` | Llama4 推理客户端（OpenAI 兼容 endpoint） |
| `src/snap4city_mobility_mcp/token_manager.py` | OAuth token 获取/缓存/刷新 |
| `chat.py` | 终端 REPL，多轮测试胶水 |

> **重要边界**：本项目只交付 **client + Langgraph orchestrator + 测试胶水**。真正的 MCP server（实现 `routing`/`address_search_location`/`tpl_*` 等工具）归 referente，部署在 Snap4City 内网（JupyterHub 直连 `192.168.1.117:8000`）。`mcp_tools.py` 里**没有任何工具实现**，只有"怎么调远程工具"。

---

## 1. 数据流走一遍（端到端实例）

以 **"da Piazza Duomo a Santa Croce a piedi"**（从主教座堂广场步行到圣十字）为例：

1. **understand**：`messages` 里塞进这句 user message → LLM 强制调 `extract_slots` → 返
   `{request_type:"journey", origin_text:"Piazza Duomo", destination_text:"Santa Croce", mode:"foot_shortest", …}`
   → `_request_to_intent` 折成 `intent="route"`。
2. **execute**（纯 Python）：
   - geocode "Piazza Duomo" → `address_search_location` → `_pick_coord` 选出 `[lng,lat]`。
   - geocode "Santa Croce" → 同上。
   - `routing` 用 `routetype="foot_shortest"` + 两组坐标 → 得 `journey`（含 WKT、距离、时间）。
   - 每步都记进 `tool_results` 审计列表。
3. **respond**（LLM）：
   - `_extract_data` 从审计里挖出 `{wkt, distance_km, eta, duration}`。
   - `_results_view` 造一份给 LLM 看的"瘦身版" RESULTS。
   - LLM（`temperature=0.2`，不带工具）用用户的语言写出 "Da Piazza del Duomo a Santa Croce a piedi sono circa 0,68 km, ~9 minuti…"。
   - 组装 `final = {status, request_type, data, messages}`。
4. 返回。`chat.py` 只打印 `messages[-1].content`（LLM 原话）；完整 JSON（含 WKT）写进 `outputs.txt`。

---

## 2. `orchestrator.py` — Langgraph 确定性编排

模块顶部定义了两个大 system prompt 和一个合成函数 schema，是整个 LLM 行为的"宪法"。

### 2.0 两个 system prompt 与 schema（非函数，但是关键常量）

- **`UNDERSTAND_SYSTEM`**：告诉 LLM 它是"佛罗伦萨公交顾问的意图提取阶段"，必须**只调一次** `extract_slots`，按 schema 每个字段自己的描述分类。规则要点：每个字段都要填（真没有就填 `''`）、**只提地名文本不要坐标**（坐标由工具地理编码）、忽略寒暄、跟进句复用上文起终点、服务区只限托斯卡纳。带 `<examples>` few-shot 块。
- **`RESPOND_SYSTEM`**：告诉 LLM 它是"友好的佛罗伦萨出行助手"，**永远用用户的语言**回复（语言不明默认意大利语）。硬规则：每个事实只能来自给它的 RESULTS、**绝不编造**坐标/距离/时长/线号/站名、不输出原始坐标、按 `hint` 给替代建议、`missing_place` 时问用户要缺的字段、`unsupported` 时友好解释支持范围。
- **`_EXTRACT_SLOTS_SCHEMA`**：一个 OpenAI function schema，**不是真的 MCP 工具**，纯粹用来"逼出结构化输出"。8 个字段：`request_type / info_kind / origin_text / destination_text / mode / agency_text / line_text / stop_text`，全部列进 `required`。

> **为什么所有字段都 required？** Llama4 有个实测毛病：它只填 `required` 里的参数，会**静默丢掉** optional 的（真实跑出过"提取了 origin 却把 destination 丢了"）。所以全部设成 required，用空串 `''` 来表示"用户确实没给这个槽"，而不是靠"字段缺失"表示——后者会被模型不可控地省略。

### 2.1 `AdvisorState`（状态 schema）

```python
class AdvisorState(TypedDict, total=False):
    messages: list[dict]      # 多轮对话历史（system+user+assistant）
    intent: str               # route | tpl_lines | tpl_routes | tpl_stops | tpl_timeline | other
    slots: dict               # understand 的输出（含映射后的 intent）
    tool_results: list[dict]  # 审计：[{name, args, result}] 每次工具调用
    unsupported: bool         # execute 跑不了确定性流程时为 True
    final: dict               # respond 组装的 widget JSON
```

- **用途**：Langgraph 节点之间传递的唯一通道。
- **为什么这样**（L18 教训）：LangGraph **只认 schema 里声明过的键**——节点返回一个 schema 外的键会被**静默丢弃**（不同版本甚至直接报错），而且单测 mock 节点函数时不会暴露（只有走完整 `graph.ainvoke` 才丢）。所以**派生数据不进 state**：比如"缺哪些槽"不单独建通道，而是 respond 端从已有的 `slots` 通道用 `_missing_slots` 现算。

### 2.2 `_request_to_intent(slots) -> str`

- **输入**：`slots` 字典（含 `request_type`、`info_kind`）。
- **输出**：内部 intent 字符串。
- **用途**：把 LLM 的"两轴分类"（request_type + info_kind）折叠成下游统一 dispatch 的单一 `intent`。
- **做了什么**：`journey → "route"`；`transit_info → tpl_<info_kind>`（用 `_INFO_KIND_TO_INTENT` 映射，未知/空 → `"other"`）；其它 → `"other"`。
- **为什么这样**：让 LLM 按"结构"分类（是不是起点→终点的行程）而非"词汇"分类（出现 bus/line 不一定就是查公交）。两轴分类更稳，但下游 execute/tpl/respond 只想要一个 intent，所以这里做一次收敛，下游代码全不用改。

### 2.3 `understand(state, *, llm) -> dict`  ★LLM 节点

- **输入**：`state["messages"]`（对话历史）、注入的 `llm` 客户端。
- **输出**：`{"slots": …, "intent": …}`。
- **用途**：从用户最新一句提取结构化槽位。
- **做了什么**：
  1. 从历史里筛出 user/assistant 轮次 `convo`。
  2. 调 `llm.achat`，传入 `UNDERSTAND_SYSTEM` + `convo`，`tools=[_EXTRACT_SLOTS_SCHEMA]`，`tool_choice={强制 extract_slots}`，`temperature=0`。
  3. 用 `tool_calls(resp)` 取出 LLM 的工具调用，解析 `arguments` JSON → `slots`。
  4. `slots["intent"] = _request_to_intent(parsed)`。
  5. 出错（JSON 解析失败 / Llama4Error）→ fallback `{"intent":"other"}`，并写 debug 日志记录原因。
- **为什么 `temperature=0`？** 槽位提取要**确定性、可复现**——同一句问话每次都该提出一样的槽，这里完全不需要创造性，任何随机都是噪声。
- **为什么 forced `tool_choice`（强制调那个函数）？** 强制让网关返回**结构化的 `tool_calls`**，而不是把"调用意图"写成普通文本。这正是 L13 的根因修复：自由模式下 Llama4 想叙述时会输出 pythonic 文本 `[routing(...)]` 漏进答案、工具根本没跑；强制结构化这条路就不存在了。
- **为什么 `extract_slots` 是合成函数而不是真工具？** 它只为"逼出结构化输出"存在，不会真被执行——执行工具是 `execute` 节点纯 Python 的事。

### 2.4 `_pick_coord(geocode, search) -> [lng,lat] | None`

- **输入**：一次 `address_search_location` 的结果 `geocode`、原始查询文本 `search`。
- **输出**：最佳 feature 的 `[lng, lat]`，或 `None`。
- **用途**：从地理编码的若干候选里挑出"真正对应用户说的那个地方"的坐标。
- **做了什么**：
  1. 结果非 dict 或含 `error` → `None`；没有 `features` → `None`。
  2. 把 `search` 分词（`_label_tokens`）。
  3. 遍历 features，找第一个 **label（address+name）分词 ⊆ search 分词** 的 feature（额外 token 会被拒），且排除"label 只是市政名（如 FIRENZE）"的伪匹配。
  4. 找不到匹配就用服务端排第一的（站点这类 POI 没有 address，只能信服务端排序）。
  5. 取 `geometry.coordinates`，校验是数值再返 `[float(lng), float(lat)]`。
- **为什么这样**（L17）：km4city 的地理编码器会把模糊的 POI 排在真地点前面——"Piazza Duomo" 的第一条曾是个广场西边 1.1km 的公司。所以不能盲信第一条，要按 label 与用户文本的 token 包含关系挑；"严格拒绝多余 token"保证那个公司命中永远不匹配。

### 2.5 `execute(state, *, client) -> dict`  ★纯 Python 节点（无 LLM）

- **输入**：`state`（用 `slots`）、注入的 MCP `client`。
- **输出**：`{"tool_results": …, "unsupported": …}`。
- **用途**：按意图确定性地跑工具链。
- **做了什么**：
  1. intent 属于 tpl_* → 直接交给 `tpl.run_tpl_flow(client, slots)`。
  2. intent 不是 `route` → `unsupported=True`（即 "other"）。
  3. 取 `origin_text` / `destination_text`，任一为空 → `unsupported=True`（缺槽）。
  4. 内嵌 `_geocode(search)`：调 `exec_tool(client, "address_search_location", {search})`，记审计，`_pick_coord` 取坐标。
  5. geocode 起点、终点；任一 `None` → 返回（`unsupported=False`，让 respond 解释地理编码失败）。
  6. `mode = slots.mode or "foot_shortest"`。
  7. 内嵌 `_route(routetype, attempts)`：组 `routing` 参数（注意 GeoJSON 是 `[lng,lat]`，所以 `startlatitude=origin[1], startlongitude=origin[0]`），调 `exec_tool(client,"routing",…)`，记审计。
  8. 先按用户的 `mode` 跑一次。
  9. 若失败且 mode 是 foot 系（`_FOOT_FALLBACK`），用另一个 foot profile **单次**重试。
- **为什么纯 Python、无 LLM？**（L13）这是整个架构的核心——工具编排是确定性的，不该让模型即兴决定调什么。固定流程：route 永远是 geocode×2 + routing。
- **为什么 foot 失败要换 profile 重试？**（L3/L8）两个 foot profile（quiet / shortest）走服务端不同的图路径，一个空另一个可能出。这正是原来模型想手动做的恢复动作，现在由 Python 保证一定执行。**只重试一次**：用户那个 profile 已经把完整的 L3 stale 重试梯子（约 27 秒）烧完了，瞬态早排除，这次只是探另一条图路径。
- **为什么 car/PT 失败不换 mode？** 跨语义的失败（car/PT 出不来）由 respond 去"建议替代方案"，而不是 Python 偷偷换 mode——那会改变用户的本意。
- **PT 原始 arc 探针**：当 mode 是 public_transport 且开了 DEBUG，会把前 5 个原始 arc dump 进 debug.log。因为真实 PT 的 arc 形状从没在线上观测到，需要离线校准 `group_arc_legs` 的分组键。

### 2.6 `_routetype_of(entry) -> str | None`

- **输入**：一条 routing 审计 entry。
- **输出**：它的 `routetype`，或 `None`。
- **用途**：从审计 entry 的 json `args` 里读回当时用的 routetype。
- **做了什么**：`json.loads(entry["args"]).get("routetype")`，解析失败返 `None`。
- **为什么需要**：失败时 respond 要知道"是哪个 mode 失败的"才能给合理建议（"开车进不去，试试步行"）；成功的 PT 路线也要靠它判断是否要分 legs。

### 2.7 `_extract_data(results) -> dict`

- **输入**：`tool_results` 审计列表。
- **输出**：widget 的 route data（`{wkt, distance_km, eta, duration, source_node, destination_node, [legs]}`）或 `{route_error}` 或 `{}`。
- **用途**：从审计里挖出对外 widget 要的 route 数据（**最后一个成功的工具结果胜出**）。
- **做了什么**：倒序扫审计；遇到 `routing` 且含 `journey` → 取第一条 route 的 `wkt`（**完整 LINESTRING，不截断**）/distance/eta/time + 起终点节点；若 routetype 是 public_transport，再用 `group_arc_legs` 把 arc 分成 walk/ride legs（有才加）。若全是错误，记下**最早**那个错误（= 用户真正要的 mode 的错，而非 fallback 的）。
- **为什么倒序 + "最早错误"？** 成功结果取最后一个（最新最相关）；但都失败时，要报用户原本要的那个 mode 的错误，所以倒序扫描时不断覆盖、留下最早的错误串。
- **注意**：`arcs`（逐段明细）被故意注释掉——会让 payload 膨胀约 90%，等 referente 确认 dashboard widget 是否需要再开（遵守规则 #8：不确定的字段先不塞）。

### 2.8 `_template_answer(intent, data, *, unsupported, missing) -> str`

- **输入**：intent、挖出的 data、是否 unsupported、缺哪些槽 missing。
- **输出**：一句意大利语回复。
- **用途**：**respond 的 LLM 不可用时的确定性兜底**。
- **做了什么**：missing → 问用户要那些字段；unsupported → 解释支持范围；tpl → 交 `tpl_template_answer`；有距离 → `📍 X km · ~时间 · 到达`；有 route_error → `⚠ 错误`；否则道歉。
- **为什么用意大利语？** 这是顾问的默认语言（佛罗伦萨服务）。LLM 挂了也得给个能看的回复，不能空着。

### 2.9 `_missing_slots(intent, slots) -> list[str]`

- **输入**：intent、slots。
- **输出**：缺失槽的标签列表（如 `["origin","destination"]`）。
- **用途**：判断某 intent 的必需槽哪些被提取阶段留空了。
- **做了什么**：查 `_REQUIRED_SLOTS[intent]`（route 要起点+终点；tpl 表来自 `tpl.REQUIRED_SLOTS` 单一来源），逐个看 `slots` 里对应 key 是否为空。
- **为什么在 respond 端现算而不进 state？**（L18）见 2.1——派生数据不进 state，避免 schema 外的键被静默丢弃。`tpl.REQUIRED_SLOTS` 同时被 `run_tpl_flow`（跳过链）和这里（提问）共用，单一来源不会漂。

### 2.10 `_routing_hint(routetype, result) -> str | None`

- **输入**：失败的 routing 的 routetype 和 result。
- **输出**：一个建议 enum（`service_empty_try_foot_or_later` / `car_pt_blocked_try_foot`）或 `None`。
- **用途**：对**失败**的 routing 给一个确定性的"建议方向"键。
- **做了什么**：result 里 error 串含 `"empty response from routing service"` → `service_empty_try_foot_or_later`（服务端问题，**不是** ZTL）；含 `"empty routes list"` 且 routetype 是 car/PT → `car_pt_blocked_try_foot`（多半是佛罗伦萨 ZTL/步行区）；否则 `None`。
- **为什么把这判断放 Python 而不是 prompt？**（L23，"altitude"）原来 RESPOND_SYSTEM 让 LLM 去**模式匹配 `result["error"]` 字符串**再决定建议——这是把确定性逻辑硬编码进 prompt，又脆又靠 LLM 复述字符串。而 `execute` 早就知道 routetype，错误串又是 `mcp_tools.routing` 确定性产生的，所以判断下沉到 Python，prompt 只剩一句"按 hint 给建议；没 hint 别瞎断言 ZTL"。

### 2.11 `_results_view(results, *, unsupported, missing) -> dict`

- **输入**：审计、unsupported、missing。
- **输出**：给 LLM 看的紧凑 RESULTS（`{status, results:[…]}` / `{status:"unsupported",…}` / `{status:"missing_place", missing}`）。
- **用途**：把 execute 的产物压成一份**给 respond LLM 读**的小视图（去掉巨大的 WKT）。
- **做了什么**：missing/unsupported 各走专门 status；否则逐条把结果用 `slim_tpl_result`（tpl 工具）或 `slim_result_for_llm`（其它）瘦身；routing 条目额外带上 `routetype` 和 `_routing_hint` 算出的 `hint`。
- **为什么瘦身**（L12）：Llama4 context 一大就崩/幻觉；喂它的只需要摘要。**完整数据仍在 `tool_results` 审计里**，widget 不受影响。
- **为什么带 routetype/hint**：失败时 LLM 只有知道"哪个 mode 失败 + 该往哪建议"才能给出"开车不行试步行"。
- **注意规则 #8**：`hint` 只在这份给 LLM 的中间数据里；对外 `final.data` 走独立的 `_extract_data`，不含 hint，不违反"不加自创字段"。

### 2.12 `respond(state, *, llm) -> dict`  ★LLM 节点

- **输入**：`state`（messages/intent/tool_results/unsupported/slots）、`llm`。
- **输出**：`{"final": widget JSON}`。
- **用途**：把结构化结果措辞成自然语言，并组装对外 JSON。
- **做了什么**：
  1. 挖 data：tpl → `extract_tpl_data`；否则 `_extract_data`。
  2. 算 missing（仅当 unsupported 且 intent 有必需槽表）。
  3. 取最后一句 user 问话；判断 `is_followup`（历史里有 assistant 轮 = 跟进）。
  4. `view = _results_view(...)`。
  5. 调 `llm.achat`：`RESPOND_SYSTEM` + 一条 user 消息（含 `User asked` / 是否 follow-up / `RESULTS: <json>`），`tool_choice="none"`，`temperature=0.2`。
  6. 取 `assistant_message(resp).content` 当答案；LLM 报错 → fallback `_template_answer`。
  7. 把答案 append 进 `messages`，组装 `final = {status:"success", request_type:intent, data, messages}`。
- **为什么 `temperature=0.2`（不是 0、也不是 0.7）？**（L19）在 0.7 时 Llama4 会在 RESULTS 缺数据时"热心地"编造线号、捏造运营商（ATAF）、偶尔还飘到英文。接地（绝不编造）是第一位、措辞自然是第二位，所以压到 0.2——留一点点遣词空间，又不放飞。（对比：槽位提取用 0，完全不要随机。）
- **为什么 `tool_choice="none"` 且不传 tools？** 措辞节点根本不该调工具，物理上断掉"自由调工具"的可能（L13）。同时这个参数还顺带强制 OpenAI 响应格式。
- **为什么 follow-up 标记？** RESPOND_SYSTEM 据此决定要不要打招呼——首轮可以问好，跟进轮直接答、不再寒暄。`messages` 此刻是 `[历史…, 当前user]`，当前 assistant 轮还没 append，所以"有 assistant 轮"恰好等价于"这是跟进"。
- **为什么回复放进 `messages[-1].content` 而不加 `answer` 字段？**（规则 #8）对外输出严格按 OpenAI 标准形状，读 `messages[-1].content`，不加自创便利字段。`status`(success/error)、`request_type`(服务的意图)、`data`(route payload)、`messages`(多轮历史) 就是全部。

### 2.13 `_build_graph(client, llm)`

- **输入**：MCP client、llm。
- **输出**：编译好的 Langgraph。
- **用途**：装配三节点线性图。
- **做了什么**：`StateGraph(AdvisorState)`，加三节点（用 `partial` 把 client/llm 注进去），边 `understand → execute → respond → END`，`compile()`。
- **为什么用 partial 注入？** 节点签名只能收 `state`；client/llm 是会话级依赖，用 `partial` 绑定后再注册。

### 2.14 会话级缓存 + `_session_deps()`

- **`_CFG` / `_LLM`**：进程级懒缓存——dashboard `/apps.json` 拓扑（静态）和 Llama4 客户端（持有 TokenManager）。
- **`_session_deps()`**：首轮构建 `_CFG = await _build_config()` 和 `_LLM = Llama4Client()`，之后复用。
- **为什么缓存？**（L16）原来 `run_advisor` 每轮都重建这俩，导致每个问题都重新拉一遍 `/apps.json`、重建 TokenManager（每轮一串 `[INIT]/[LOAD_TOKEN]` stderr 噪音）。token 本体早已持久化在 `token_stored.json`，浪费的是对象重建 + 网络往返。缓存后 token 刷新仍正确——`TokenManager.get_token()` 每次调用内部按需查过期。
- **为什么不用 `asyncio.Lock`？** Lock 会绑定到某个事件循环，破坏"每个请求各自 `asyncio.run()`"的调用方；两个缓存对象都不绑定循环。并发首轮可能双重构建，但是良性（后写胜出）。

### 2.15 `run_advisor(query, history=None) -> dict`  ★对外入口

- **输入**：`query`（本轮问话）、`history`（上轮返回的 `messages`）。
- **输出**：widget JSON（含更新后的 `messages`）。
- **用途**：多轮出行顾问的唯一入口。
- **做了什么**：取 `cfg, llm = _session_deps()`；`messages = history + [{"role":"user","content":query}]`；`async with Client(cfg) as client` 建图并 `ainvoke`；返 `out["final"]`（无 final 则返 error 形状）。
- **为什么 Client 每轮重连、cfg/llm 不重建？**（L16）Client 生命周期干净、内网握手便宜，每轮 `async with` 重连无所谓；而 cfg/llm 重建才是真浪费，所以它俩进程级持有。
- **多轮怎么续？**（L14）把上轮 `final["messages"]` 当 `history` 传回来即可。chat REPL 和 dashboard 都这么带状态。

---

## 3. `tpl.py` — 公交（TPL）发现链

`execute` 把 tpl_* 意图委派到这里。和 route 流一样，每步都是纯 Python 驱动远程 MCP 工具，LLM 不挑工具（L13）。四条链：

```
tpl_lines    agencies → 解析 agency → tpl_lines(agency)
tpl_routes   agencies → 解析 agency → tpl_routes_by_line(line, agency)
tpl_stops    …→ tpl_stops_by_route(route)  前 2 条路线（即两个方向）
tpl_timeline …→ 按站名 token 匹配 → tpl_stop_timeline(stop)
```

> **重要背景**：tpl 的 payload 形状从没在线上完整观测过（只有工具描述）。所以这里每个解析函数都**防御性**写法，`run_tpl_flow` 会把原始 payload 头 dump 进 debug.log，供首次 JupyterHub 跑时校准。

### 3.0 关键常量

- `TPL_INTENTS`：四个 tpl 意图。
- `REQUIRED_SLOTS`：每个 tpl 意图的必需槽（routes/stops 要 line；timeline 要 line+stop）。**为什么 timeline 也要 line？** 没有"按 GPS 找站"的工具，要把站**名**解析成站的 service URI，必须先有那条线的站列表，所以 line 也成必需。
- `STOPS_ROUTES_PROBED = 2`：一条线探前 2 条 route（通常就是两个方向）。
- `TPL_LLM_KEEPS` / `TPL_TOOL_NAMES`：给 LLM 看的各类 payload 截断上限（L12：线路可能 100+，站列表是整个 GeoJSON）。
- `TPL_DATA_KEEP=50` / `ROUTES_DATA_KEEP=10`：widget data 截断上限。
- `_FLORENCE_URBAN_TOKENS`：`{firenze, fiorentina, metropolitana}`——佛罗伦萨城区网络识别用。

### 3.1 `_is_florence_urban(name) -> bool`

- **输入**：agency 名。
- **输出**：是否佛罗伦萨城区网络。
- **做了什么**：名字分词里含 `urbano` 且与 `_FLORENCE_URBAN_TOKENS` 有交集。
- **为什么需要**（L21）：km4city 里**没有**单一的 "Autolinee Toscane" 条目，品牌被拆成约 40 个子网。佛罗伦萨城市线（含 6 路）在 "Autolinee Toscane - Urbano Area Metropolitana Fiorentina"（`…_Agency_888-48`）下。基于 token 识别能扛住名字的细微变体。

### 3.2 `_unwrap_tpl(payload)`

- **用途**：剥掉 FastMCP 对非对象输出的 `{"result":[...]}` 包装。
- **做了什么**：payload 是只含一个 `result` 键且值为 list/dict 的 dict → 返 `payload["result"]`；否则原样返。
- **为什么**：服务端把非 dict 的结构化输出包成 `{"result":...}`，而文档里的 tpl 形状是裸数组——两种都得接。

### 3.3 `_generic_list(payload) -> list`

- **用途**：取出 tpl payload 里的条目列表。
- **做了什么**：先 `_unwrap_tpl`；若是 dict 且无 error，返第一个 list 类型的值（tpl_agencies 文档说是个含数组的 dict）；本身是 list 就直接返。

### 3.4 `_first_str(item, keys) -> str | None`

- **用途**：按候选键顺序取第一个非空字符串值。
- **为什么**：tpl 各 payload 的真实键名未验证，挨个候选键试（如 uri/agency/serviceUri）。

### 3.5 `_agency_entries(payload) -> [{name, uri}]`

- **用途**：从 tpl_agencies payload 提 `[{name, uri}]`。
- **做了什么**：遍历列表，每项用 `_first_str` 取 uri（候选 uri/agency/serviceUri）和 name（候选 name/agencyName/label），有 uri 才收。

### 3.6 `_route_uris(payload) -> [str]`

- **用途**：从 tpl_routes_by_line payload 提 route URI 列表（候选键 routeUri/uri/route）。

### 3.7 `_features(obj) -> list`

- **用途**：从（可能被包裹的）类 FeatureCollection dict 里取 features。
- **做了什么**：直接有 `features` list 就返；否则下钻一层找 `{某键: {features:[...]}}`。
- **为什么下钻**（L21 探针）：线上 tpl GeoJSON 嵌在包装键下、无顶层 `type`——`tpl_stops_by_route` 是 `{"BusStops":{"features":[...]}}`，`tpl_stop_timeline` 是 `{"BusStop":{...}}`。也兼容文档里的扁平 FeatureCollection。

### 3.8 `_stop_entries(payload) -> [{name, uri}]`

- **用途**：从 tpl_stops_by_route payload 提站点 `[{name, uri}]`。
- **做了什么**：线上形状是 `[URI数组, {"BusStops":{"features":[...]}}]`。分离出 URI 数组和 GeoJSON；逐 feature 取 `properties.name`（兜底 address）和 `properties.serviceUri`（兜底：按位置和 URI 数组对齐）；没有可用 feature 就只用 URI 数组。
- **为什么这么绕**：站名/URI/坐标都在 `properties` 里，且 URI 可能只在并行数组里，要按下标对齐。

### 3.9 `_match_stop(entries, stop_text) -> entry | None`

- **用途**：把用户说的站名匹配到官方站。
- **做了什么**：精确 token 集相等优先；然后**任一方向的子集**（覆盖"用户词 ⊆ 官方名"和"用户话更啰嗦"两种）；按 route 顺序第一个命中。
- **为什么和 geocode 的 `_pick_coord` 方向相反？**（L21）站名通常**比**用户输入**长**（"San Marco" → 官方 "Museo Di San Marco"），所以"用户词 ⊆ 站名"才是有用方向；而 geocode 那边是 "label ⊆ search"。

### 3.10 `_resolve_agency(agencies, agency_text) -> uri | None`

- **用途**：把用户说的运营商解析成 agency URI，或给佛罗伦萨城区默认值。
- **做了什么**：用户给了文本 → 双向 token 匹配（`toks<=want or want<=toks`）找候选；多个候选时优先 `_is_florence_urban`，否则第一个；无候选返 `None`（让 respond 请用户从列表里选）。用户没给文本 → 直接返佛罗伦萨城区默认。
- **为什么双向 token？**（L21）品牌名（"Autolinee Toscane"，用户输入）是具体子网名的**子集**，而啰嗦的用户短语可能是**超集**——两个方向都得接。旧实现方向反了，永不匹配，导致 LLM 拿着 agency 列表去**编造假线路**。

### 3.11 `run_tpl_flow(client, slots) -> dict`  ★tpl 主流程

- **输入**：MCP client、slots。
- **输出**：`{"tool_results", "unsupported"}`（和 route 流同形，**绝不返 `missing` 键**——AdvisorState 没这通道，L18）。
- **做了什么**：
  1. 缺必需槽（按 `REQUIRED_SLOTS[intent]`）→ `unsupported=True`。
  2. 内嵌 `_call(name, args)`：`exec_tool` + 记审计 + DEBUG dump 原始头。
  3. 先 `tpl_agencies` → `_agency_entries` → `_resolve_agency`；agency 为 `None` → 返回（让 respond 请用户选 agency）。
  4. `tpl_lines`：调 `tpl_lines(agency)` 返回。
  5. 否则调 `tpl_routes_by_line(line, agency)`；`tpl_routes` 到此返回。
  6. `tpl_stops` / `tpl_timeline`：对前 2 条 route 调 `tpl_stops_by_route`；`tpl_stops` 到此返回。
  7. `tpl_timeline`：对各站 payload `_match_stop` 找到站 → `tpl_stop_timeline(stop)`，命中即停。
- **为什么 DEBUG dump 原始头？** tpl 形状没线上验证，这些 dump（含最外层是裸 list 还是 `{"result":...}`）是本模块每个假设的校准数据。dump 要足够长，否则截断会把关键第二段切掉，伪造"无数据"假象。

### 3.12 `_timeline_view(payload) -> dict`

- **用途**：把 tpl_stop_timeline payload 结构化成 `{stop, lines, [timetable], [realtime]}`。
- **做了什么**：取 `BusStop`/`BusStops` 的第一个 feature 的 `properties.name` 当 stop；从 `busLines.results.bindings` 提 `[{line, desc, uri}]`；`timetable`/`realtime` 只在**非空**时才放进去。
- **为什么 timetable/realtime 常常没有？**（L21）线上探针里 `timetable`/`realtime` 恒为空——这个站的发车时刻服务端没加载。只在有数据时才 surface，让 respond 能报"服务的线路 + 时刻暂不可用"，**绝不编造发车时间**。

### 3.13 `slim_tpl_result(name, result)`

- **用途**：给 LLM 看的 tpl 紧凑视图（计数 + 截断条目，绝不整个 GeoJSON / route WKT）。
- **做了什么**：按工具名分别处理——stops 返 `{count, stops:[名…]}`；timeline 返 `{stop, lines:[…], line_count, timetable_available}`；routes 去掉 wkt/geometry 等大字段；agencies/lines 返 `{count, key:[…]}`。
- **为什么**（L12）：同 `slim_result_for_llm`——喂模型的瘦，审计里的全。

### 3.14 `_last_ok_result(results, name)` / `extract_tpl_data(intent, results)`

- **`_last_ok_result`**：倒序找最后一个该工具名且无 error 的结果。
- **`extract_tpl_data`**：按 intent 挖 widget data——lines/routes 截断；stops 跨 route 去重合并；timeline 用 `_timeline_view`。route 条目保留 WKT 供地图 widget 画线。
- **为什么 NEW 数据键待 referente？**（规则 #8）lines/routes/stops/timeline 这些 data 键和 `data.legs`/`data.arcs` 一样，是新加的、等 referente 确认 widget 是否要。

### 3.15 `_names(items, keys)` / `tpl_template_answer(intent, data)`

- **`_names`**：从条目列表按候选键提名字（字符串项直接收）。
- **`tpl_template_answer`**：respond LLM 不可用时的意大利语兜底（对应 route 的 `_template_answer`）。lines/routes/stops 列名 + 总数；timeline 列服务线路 + "时刻暂不可用"。

---

## 4. `mcp_tools.py` — 客户端 MCP 层（不实现工具）

只做两件事：(1) 连远程 server，(2) 执行确定性图的工具调用，解包响应、抹平 km4city 已知怪癖。`exec_tool` 是唯一执行入口，**永不抛异常**——所有失败都变成 `{"error":...}`。

### 4.0 关键常量

- `ROUTING_STALE_RETRIES=2` / `_DELAY_S=6.0`：routing 冷启动空响应的重试（L3）。
- `TUSCANY_BBOX`：托斯卡纳经纬度框（lng 9.6–12.5, lat 42.2–44.5），geocode 客户端侧过滤用（L11）。
- `DASHBOARD_URL`（默认内网 `192.168.1.117:8000`，可用 `S4C_DASHBOARD_URL` 覆盖）/ `INTERNAL_DASHBOARD_URL` / `NATIVE_SERVER_ID`。
- `EXPOSED_TOOLS` / `TOOL_NAMES`：允许调的 6 个工具白名单。

### 4.1 `_build_config() -> dict`

- **用途**：拉 dashboard `/apps.json`，只留 native server，把内网 IP 改成 `DASHBOARD_URL`。
- **做了什么**：httpx GET `/apps.json` → 取 `mcpServers[native]` → 把它 `url` 里的内网 IP 替换成 `DASHBOARD_URL` → 包成单 server cfg 返回。
- **为什么替换 IP？**（L5）`/apps.json` 返回的 server URL 写死内网 IP，host 不同就连不上，要替换成实际入口。
- **为什么只留 native server？**（L6）FastMCP **只在 multi-server cfg** 才给工具名加 `<server>_` 前缀；保持单 native server，就能用裸名 `call_tool("routing",…)`，所有硬编码工具名不会突然崩。同时优先用 native（legacy 会被删）。

### 4.2 `_unwrap(result)`

- **用途**：把 `fastmcp.Client.call_tool` 的返回对象转成结构化 payload。
- **做了什么**：优先 `structured_content`；否则 `json.loads(content[0].text)`；都没有返 `None`。

### 4.3 routing 一族

- **`_call_routing_once(client, args)`**：调一次 `routing` → `{data}` 或 `{error}`（捕获所有异常变 error；非 dict payload 也算 error）。
- **`_looks_stale(data)`**：启发式判断是否 L3 冷启动空壳——没有 `journey` dict 就算 stale。
- **`routing_with_retry(client, args, *, attempts=None)`** ★：
  - **用途**：带 L3 stale 重试 + L7 error_code 校验 + L2 空 routes 检测的 routing。
  - **做了什么**：先调一次；若 stale 就隔 6s 重试至多 2 次（共 3 次）。然后三道失败检查——
    - **A**：仍无 journey → 区分瞬态 L3 没清 vs 稳定 L8 wrapper bug；原始 payload 进 debug.log，用户面给"服务端空响应，换 mode 或更精确地址"。
    - **B**（L7）：`response.error_code` 不是 `"0"` → 失败。**注意 `error_message` 成功时也非空（"successful"），只能信 error_code。**
    - **C**（L2）：envelope 像成功但 `routes` 空 → "no route found (empty routes list)"（车在步行区、起终点同点等，km4city 不返 4xx）。
  - **为什么 `attempts` 可覆盖？** foot profile 回退探针传 1——用户那个 profile 已烧完完整 stale 梯子，瞬态排除了，没必要再等。

### 4.4 geocode 一族

- **`_in_tuscany(coords)`**：`[lng,lat]` 是否落在托斯卡纳框内。
- **`_label_tokens(text)`** ★：去重音（NFKD）+ casefold + 正则取词 + 去意大利语虚词（`_LABEL_STOPWORDS`，如 di/del/la/il…）。**整个 token 匹配体系的基础**，geocode 和 tpl 都用它。
- **`_narrow_by_city(features, search)`**：城市阶梯——用户点名的城市（city 分词 ⊆ search）优先，否则 FIRENZE 默认，否则 `None`。
- **`_filter_geocode_to_tuscany(payload, search)`**（L11）：只保留落在托斯卡纳框内的 features（保 score 序）；区内全空 → 返 `{"error":"no Tuscany-area match…"}`。**为什么？** km4city 地理编码器不再锁区，现在也索引瓦伦西亚/法国南部，"Piazza del Duomo, Firenze" 可能 100 条全西班牙、零托斯卡纳。schema 无地理约束参数，只能客户端框过滤。
- **`_geocode_address_first(client, args)`** ★（L17）：两段式四级阶梯——
  1. 地址 pass（`excludePOI=true`）的 named-city/Firenze 子集；
  2. POI pass（`excludePOI=false`）的 named-city/Firenze 子集（车站/地标只在 POI 目录）；
  3. 全托斯卡纳地址命中；
  4. 全托斯卡纳 POI 命中 / `{"error":…}`。
  - **为什么地址优先？** 含 POI 时服务端把模糊目录命中排在真地点前（"Piazza Duomo" → 西边 1.1km 的公司）；纯地址条目坐在可路由街图上。但精确地址跨城同名（"PIAZZA DUOMO" 在多个托斯卡纳镇都有），所以一个 pass 只有在"用户点名的城市（或默认 Firenze）里有命中"时才算赢。
- **`geocode_with_retry(client, args)`**（L20）：`_geocode_address_first` 外面包**仅针对"零托斯卡纳"瞬态**的有界重试（2 次，1.5s）。**为什么？** referente 地理编码器**时间维度非确定性**——同一查询此刻 100 条区内、下一刻 100% 国外。两段式+框过滤只要后端返**任意一条**托斯卡纳就能捞回；唯一真失败是"返 0 条托斯卡纳"的瞬态窗口，有界重试通常能清。

### 4.5 `group_arc_legs(arcs) -> [leg]`

- **用途**：把连续的 routing arc 按"交通工具身份"分成 journey 的 legs（步行段/乘车段）。
- **做了什么**：分组键 = `(transport, transport_provider)`；键变就开新 leg。每 leg 累计 distance、记起止时间、用非 "nd" 的 desc 当起止站名。
- **为什么是 provisional（暂定）？** 携带公交线号的字段从没线上观测到（execute 首跑会 dump 原始 PT arc）；若线号其实在 `desc` 里，同站相遇的两条线会被错误合并——到时按 dump 重新校准。`desc=="nd"`（无数据）永不命名 leg 端点。

### 4.6 `slim_result_for_llm(name, result)`

- **用途**：把工具结果压成给 LLM 看的紧凑视图。
- **做了什么**：geocode → top-5 features 只留 `{address, city}`；routing → 丢 WKT，留 `{distance_km, eta, time}`，多段（PT）给 legs、单段给去重街道名列表。错误/未知形状原样透传。
- **为什么 geocode 故意不给坐标？** respond LLM 曾用 geocode 坐标在 routing 失败时**自己编**距离/ETA；视图里没坐标，就没东西可即兴发挥。widget 和 execute 读的是完整 payload，从不读这个瘦视图。
- **为什么瘦身**（L12）：context 一大 Llama4 后端就 500/幻觉。

### 4.7 `exec_tool(client, name, args, *, routing_attempts=None)` ★唯一执行入口

- **用途**：把一次工具调用转发给远程 server。**永不抛异常**，返 payload 或 `{"error":…}`。
- **做了什么**：
  1. 名字不在 `TOOL_NAMES` 白名单 → `{"error":"unknown tool"}`（不碰网络）。
  2. 剥掉 `authentication`（公开后端）。
  3. `routing` → 整理 5 个参数 → `routing_with_retry`（`routing_attempts` 控梯子）。
  4. `address_search_location` → `geocode_with_retry`。
  5. 其它 → 直透 `client.call_tool` + `_unwrap`。
  6. 任何异常 → `{"error":"<name> call failed: …"}`。
- **为什么"永不抛异常"？** 这是单一执行 seam——所有失败统一成 `{"error":…}`，上层（execute/respond）能把它喂给模型并优雅恢复，而不是让某次网络抖动炸掉整个图。

---

## 5. `llm.py` — Llama4 推理客户端

封装 referente 参考例子里的 auth + inference 流程。endpoint `llama4-agentic-inference`（OpenAI/MCP 兼容 vLLM）。**只有在 Snap4City JupyterHub 上跑才返回真答案**（API 规则绑功能账号）。

### 5.0 关键常量

- `LLAMA4_API_URL` / `LLAMA4_ENDPOINT`（可用 env 覆盖）。
- `LLM_TIMEOUT_S=120` / `LLM_RETRIES=2` / `LLM_RETRY_BACKOFF_S=4`。
- `_TRANSIENT_HINTS`：标记"值得重试的网关/后端错误"的子串（timeout/upstream/5xx/`internal server error`/`failed to make post request` 等）。

### 5.1 `_is_transient(message, status_code) -> bool`

- **用途**：判断某次失败是否值得重试。
- **做了什么**：status 是 429/5xx → True；message 含 `_TRANSIENT_HINTS` 任一子串 → True。
- **为什么**（L12）：网关会**返 HTTP 200 但 body 里包上游错误**（如 vLLM 后端 500，常因请求太大）——原来的判断只认 502/503/504，会漏判直接崩。所以扩充子串清单，把这类当瞬态重试。

### 5.2 凭据加载

- **`_credentials_file()`**：按 `S4C_CREDENTIALS_FILE` → cwd → repo 根 的顺序找第一个存在的 `user_credentials.json`。
- **`_load_credentials()`**：读 username/password（无 env 兜底，缺则抛 `Llama4Error`）。
- **为什么走文件不走 env？** 文件被 `.gitignore` 挡住，敏感信息不进 git；手动放 repo 根目录。

### 5.3 响应解析助手

- **`assistant_message(response)`**：取 `choices[0].message`（无则 `{}`）。
- **`tool_calls(response)`**：取 `message.tool_calls`（无则 `[]`）。
- **`Llama4Error`**：API 返错误 envelope / 非 JSON 时抛。

### 5.4 `Llama4Client`

- **`__init__`**：没传 user/pass 就 `_load_credentials()`，建 `TokenManager`。
- **`chat(messages, *, tools=None, tool_choice="none", temperature=None)`** ★：
  - **用途**：OpenAI messages（+可选 tools）→ 完整 OpenAI 响应。
  - **做了什么**：组 `params`（含 messages/tool_choice，可选 tools/temperature）；取 token 组 headers + body `{access_token, endpoint, params}`；POST，最多重试 `LLM_RETRIES` 次（线性退避）。网络错按瞬态重试；返回含 `choices` 即成功；错误 envelope（`message`/`detail`）按 `_is_transient` 决定重试还是立即抛。
  - **为什么默认 `tool_choice="none"`？** 强制 OpenAI `choices` 格式，即使不传 tools——否则 endpoint 会退回 legacy 形状。需要 agentic 时才传 tools + `tool_choice="auto"`（但本项目确定性图**从不用** auto，见 L13）。
  - **为什么硬错误立即抛？** 坏凭据、失效 API 规则不是瞬态，重试无意义，直接抛。
- **`achat(...)`**：`asyncio.to_thread` 把阻塞的 `chat` 卸到工作线程。**为什么？** Langgraph 节点是 async 的，不能在事件循环里跑阻塞 httpx。

> 旧 endpoint `llama4-inference` 即使 auth 成功也返 `{"message":"Rule not found"}`（auth ≠ inference 授权），所以用 agentic endpoint（L10）。

---

## 6. `token_manager.py` — OAuth token 管理

直接沿用 referente 参考实现。Keycloak（`snap4city.org/auth/.../token`）的 password / refresh_token grant。

- **`__init__`**：存 user/pass/client_id(`clearml-apis`)/store_path(`token_stored.json`)；`load_token_data()` 尝试从文件读已存 token。
- **`get_token()`** ★：
  - **热路径**：缓存 token 仍有效（`time.time() < token_expiry`）→ **静默**返回。
  - 过期/无 → 先试 refresh_token，再试 user/password；成功就 `save_token_data` 并返；都失败抛异常。
  - **为什么热路径静默？** 这是每次 LLM 调用都走的路；这里打日志会刷爆整个 chat 会话的 stderr（L16 噪音根因之一）。
- **`get_token_via_user_credentials` / `get_token_via_refresh_token`**：两种 grant 的 POST。
- **`save_token_data`**：存 token/refresh_token；`token_expiry = now + expires_in - 60`（**提前 60s 过期**，留刷新余量）；写 `token_stored.json`。
- **`load_token_data`**：从文件读；读失败/无文件则清空状态。
- **为什么诊断走 stderr（`_log`）？** 保持 stdout 干净——orchestrator/chat 要解析 JSON 输出，token 噪音不能混进去。

---

## 7. `chat.py` — 终端 REPL 测试胶水

`python chat.py` 开一个交互聊天：输入问题 → 看回复 → 接着问（跟进句如 "那坐公交呢?" 会对历史解析）→ 空行退出。

- **`_setup_debug_log()`**：把本包 DEBUG 诊断路由到 `debug.log`（`mode="w"` 每次会话刷新；只动本包 logger，httpx 等保持安静；幂等，不重复挂 handler；`propagate=False` 不回显到 notebook handler）。
- **`_reply(final) -> str`**：取要显示的回复。`status != success` → `✗ 错误`；否则倒序找最后一个非空 assistant content。**为什么不硬编码 `📍km·ETA`？**（L14）`respond` 已经把距离/ETA 措辞成自然语言（LLM 挂了也用模板兜底进同一槽），这里只显 LLM 原话，零硬编码。
- **`_log_turn(final)`**：把每轮完整输出 JSON 追加到 `outputs.txt`（**原样写 dashboard payload**，不加 query/final 包装——当前 query 已在 `messages[-2].content`）。
- **`main()`** ★：
  - 初始化日志、清空 `outputs.txt`。
  - 给 stdin/stdout `reconfigure(errors="replace")`——**为什么？** JupyterHub 终端带重音的输入/粘贴会让 `input()` 崩 `UnicodeDecodeError`，旧 cp1252 控制台编码不了 emoji 提示符；两端都用替换而非抛错。
  - `while` 循环：读输入（空行退出）；`run_advisor(query, history)`（infra 失败也保 REPL 活着）；`_log_turn`；`history = final["messages"]`（带多轮状态）；打印 `✦ 回复`。
- **为什么终端 REPL 而不是 web 前端？**（L14/L15）唯一运行环境是锁死的 JupyterHub，暴露 web 端口要 `jupyter-server-proxy` + 子路径 + server 重启，极脆（还直接触发过 L15 灾难——base env `pip install` 升级依赖把整个 singleuser server 搞崩）。终端 REPL 一跑就通。

---

## 8. 附：函数 ↔ lessons 速查

| Lesson | 绑定的代码 / 决策 |
|---|---|
| L2 | `routing_with_retry` 失败检查 C（空 routes ≠ 成功） |
| L3 | `routing_with_retry` stale 6s×2 重试；`_FOOT_FALLBACK` 单次 |
| L5 | `_build_config` 把内网 IP 替换成 DASHBOARD_URL |
| L6 | 只留单 native server → 工具名无前缀 |
| L7 | `routing_with_retry` 失败检查 B（只信 error_code） |
| L8 | `routing_with_retry` 失败检查 A（裸 `{"error":""}` wrapper bug）；foot 也中招 |
| L10 | `llm.py` 用 agentic endpoint |
| L11 | `_filter_geocode_to_tuscany` bbox 过滤 |
| L12 | `slim_result_for_llm` / `slim_tpl_result` 瘦身 |
| L13 | **确定性图**：understand forced / respond none / execute 纯 Python |
| L14 | 多轮 `messages` 复用；`chat.py` 只显 LLM 原话 |
| L15 | 终端 REPL（不碰 base env / web proxy） |
| L16 | `_session_deps` 懒缓存；Client 每轮重连；token 热路径静默 |
| L17 | `_geocode_address_first` 两段式 + `_pick_coord` label 匹配 + `_narrow_by_city` 城市阶梯 |
| L18 | 派生数据不进 state（`_missing_slots` 从 slots 现算） |
| L19 | car/PT/foot routing 现状；respond `temperature=0.2` 接地；否定式硬规则 |
| L20 | `geocode_with_retry` 零区瞬态重试 |
| L21 | tpl 链解析；`_resolve_agency` 双向 token + Firenze 默认；timetable 空 |
| L22 | foot 覆盖"中心城区绑定"，非纯距离阈值 |
| L23 | `_routing_hint`/`_results_view` 把 ZTL-vs-服务端判断下沉 Python |

---

## 9. 当前能力与已知限制（与 referente 对账口径，2026-06-15）

**已闭环可用**：
- 自然语言意图提取（journey / transit_info / other 两轴分类）。
- 步行 route（中心城区 1–2km，含 WKT/距离/时间）。
- **开车 route ✅ 已跑通**（实测 Sesto→Scandicci 出真车速路线 19.12km/12min + 完整 WKT；历史曾全空，服务端已修好，client 侧无需改）。
- 公交发现：lines / routes / stops（agency 双向解析 + Firenze 城区默认）。
- 多轮对话、确定性兜底模板、km4city 各类怪癖处理。

**已知服务端限制（client 调用正确，待 referente）**：
- **public_transport route**：不再返裸 `{"error":""}`，但短途 OD 的 journey 只有一段步行 leg（无真公交），长途是否出真 bus/tram 待复测。
- **foot 覆盖**：仅中心城区，中等距离非中心区（Rifredi）/远程（10km+）返空——服务端 `search_max_feet_km` 很小，属设计内。
- **stop timetable/realtime**：`tpl_stop_timeline` 恒返空 dict；用 ISO datetime 调通 `service_info_dev` 返 200 但 timetable/realtime 仍空 = GTFS schedule 服务端未加载，客户端无解。respond 诚实报"发车时刻暂不可用"即终态。
