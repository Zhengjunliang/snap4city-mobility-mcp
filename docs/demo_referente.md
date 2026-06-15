# Demo per referente — query selezionate (foot route + tpl)

Dimostrazione dello stato attuale: il client + l'orchestrator Langgraph + la catena di
tool MCP **funzionano end-to-end** sul JupyterHub Snap4City.

正文中文说明 + query 用 Italian (advisor 按用户语言回复, Italian query → Italian 答, 适合给 referente 看)。

## 运行步骤 (JupyterHub, s4c env)

1. `python chat.py`
2. 逐条贴下方 query (一行一条), 回车看 `✦` 回复。
3. **同组多轮 (follow-up) 之间不要空行退出** — 连续贴, 才复用对话历史。
4. 空行退出。每轮全量 widget JSON (`status` / `request_type` / `data` 含 WKT / `messages`)
   追加到 `outputs.txt` — 给 referente 看 payload 形状。
5. 工具级诊断 (geocode 选点坐标、routing 原始空 payload) 在 `debug.log`。

> 状态 (2026-06-15 实测): **car routing 已修好** (出真车速路线); **PT 短途退化步行** (不再空,
> 但短途无真公交段, 长途见 Group C-bis); 远程/非中心 foot 仍空、stop 时刻表仍空 (服务端待修,
> client 无误, 见 `docs/lessons.md` L19/L21/L22)。Group C 演示已知限制的**诚实降级**。

---

## Group A — Foot route ✅ (中心城区点到点步行; 应出距离 + ETA + 地图 WKT)

```
Da Piazza del Duomo a Santa Croce a piedi
```
```
Come arrivo a piedi da Santa Maria Novella al Duomo?
```
```
Vorrei andare a piedi da Piazza del Duomo a Campo di Marte
```

**多轮 follow-up** (连续贴这两条, 中间不退出 — 证明复用上轮 origin):
```
Da Piazza del Duomo a Santa Croce a piedi
```
```
E se invece vado fino a Campo di Marte?
```

预期: 每条出 km + ETA; `outputs.txt` 的 `data.wkt` 是完整 LINESTRING。第二条 follow-up 复用
Duomo 起点、只改终点为 Campo di Marte。

---

## Group B — TPL 公交发现 ✅ (应出线路 / 路线 / 站点列表)

tpl_lines:
```
Che linee di autobus ci sono a Firenze?
```
tpl_routes (实证 ~22 routes):
```
Quali sono i percorsi della linea 6?
```
tpl_stops:
```
Quali fermate fa la linea 6?
```

预期: lines 出一批线名 (回复会说明用的 agency = *Autolinee Toscane - Urbano Area
Metropolitana Fiorentina*); routes 出约 22 条; stops 出 line 6 前 2 个方向的站点。
`outputs.txt` 里 `data.lines` / `data.routes` / `data.stops` 有内容。

> **⚠️ timeline 站名依赖**: Group C 的 tpl_timeline 要的站名必须是 line 6 **真实服务**的站。
> **先跑上面的 tpl_stops 拿到真实站名**, 再把 timeline query 里的站名 (下方 `San Marco` 是占位)
> 换成 stops 输出里的一个, 否则 `_match_stop` 可能匹配不上。

---

## Group C — 已知限制 ⚠️ (单独分组; 向 referente 展示诚实降级, 均服务端待修)

每条**故意**触发已知空返回, 重点看 client 是否**诚实措辞、不编造数字、不乱说 ZTL**。

car (服务端空; 用非-ZTL 可驾驶 OD, 排除 "ZTL 误判"):
```
In auto da Sesto Fiorentino a Scandicci
```
> 预期: 不报距离, 说 routing 服务未返回结果、建议步行/公交; **不得**断言"目的地在 ZTL 步行区"。

public_transport — **短途退化成步行** (2026-06-15 实测: PT 不再返空, 但短途 OD 返的 journey
里 `legs` 只有一段 `transport: "foot"`, 无真公交段):
```
Con i mezzi pubblici da Piazza del Duomo a Campo di Marte
```
> 预期: 出 journey 但内容是步行 (`data.legs[0].transport == "foot"`)。短途 PT 引擎退化步行 — 用下面
> Group C-bis 的长途 OD 才可能出真 bus/tram 段。

远程/非中心 foot (覆盖外):
```
A piedi dalla stazione di Rifredi a Piazza Dalmazia
```
> 预期: 诚实说没算出路线、不给数字, 请用更具体地址。

tpl_timeline (链通但时刻表服务端空):
```
A che ora passa la linea 6 alla fermata San Marco?
```
> 预期: 列出服务该站的线路 + 明说"当前没有发车时刻", **绝不编造时间**。
> (`San Marco` 占位; 不中就换成 Group B tpl_stops 输出里的真实站名。)

---

## Group C-bis — 长途 PT 复测 🔬 (确认是否出真 bus/tram 段)

短途 PT 退化成步行, 长途选**有轨电车 T1/T2 + 城际**走廊, 看 `data.legs` 里有没有
`transport` 非 `foot` 的段 (真公交/电车)。每条连续贴, 看 `outputs.txt` 的 `data.legs`:

T2 电车走廊 (SMN ↔ 机场):
```
Con i mezzi pubblici da Santa Maria Novella all'aeroporto di Firenze
```
T1 电车走廊 (SMN ↔ Scandicci):
```
Con i mezzi pubblici da Santa Maria Novella a Scandicci
```
城际 (Firenze ↔ Sesto Fiorentino):
```
Con i mezzi pubblici da Piazza del Duomo a Sesto Fiorentino
```

> **判定**: `outputs.txt` 里该轮 `data.legs` 若出现 `"transport": "bus"` / `"tram"` (非 `foot`)
> → PT **真修好了**, 能出公交段。若仍全是 `foot` 段 → PT 引擎对所有 OD 都退化步行, 仍需报 referente。
> (`group_arc_legs` 见 `mcp_tools.py`; leg 的 `transport`/`provider` 字段直接来自服务端 arc。)

---

## Group D — 范围 / 健壮性 (可选; 展示意图分类与缺槽追问)

unsupported (other intent):
```
Che tempo fa domani a Firenze?
```
> 预期: 友好说明只答出行/公交相关问题, 请换个问法。

缺 origin (missing slot):
```
Vorrei andare a Santa Croce
```
> 预期: 追问起点, **不**说 unsupported。

---

## 自验清单

1. `python chat.py`, 按 A → B → C (→ D) 逐条贴。
2. Group A/B 每条 `✦` 有实质内容 (距离 / 列表), 不是 `✗`。
3. `cat outputs.txt`: Group A 有 `data.wkt`; Group B 有 `data.lines/routes/stops`。
4. Group C 每条回复**无编造数字、无 ZTL 误判、无假发车时刻** — 措辞诚实。
5. 终端回复截图 + `outputs.txt` 给 referente。
