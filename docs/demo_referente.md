# Demo per referente: query selezionate (foot / car / tpl)

Dimostrazione dello stato attuale: il client + l'orchestrator Langgraph + la catena di
tool MCP **funzionano end-to-end** sul JupyterHub Snap4City.

正文中文说明, query 用 Italian (advisor 按用户语言回复, Italian query 出 Italian 答, 适合给 referente 看)。
每条标 ✅ (能跑通) 或 ⚠️ (已知服务端限制, 诚实降级)。

## 运行步骤 (JupyterHub, s4c env)

1. `python chat.py`
2. 逐条贴下方 query (一行一条), 回车看 `✦` 回复。
3. **同组多轮 (follow-up) 之间不要空行退出**: 连续贴才复用对话历史。
4. 空行退出。每轮全量 widget JSON (`status` / `request_type` / `data` 含 WKT / `messages`)
   追加到 `outputs.txt`, 给 referente 看 payload 形状。
5. 工具级诊断 (geocode 选点坐标、routing 原始 payload) 在 `debug.log`。

> 状态 (2026-06-15 实测 + 探针):
> - ✅ **foot route** (中心城区)、**car route**、**tpl 发现** (lines/routes/stops)、**意图分类** 全通。
> - ⚠️ **PT 短途退化步行** (出 journey 但无真公交段)、**远程 foot 空**、**stop 时刻表空** = 服务端待修,
>   client 无误 (见 `docs/lessons.md`)。Group E 演示这些的诚实降级 (无编造、无乱报 ZTL)。

---

## Group A: Foot route ✅ (中心城区点到点步行; 出距离 + ETA + 地图 WKT)

```
Da Piazza del Duomo a Santa Croce a piedi
```
```
Come arrivo a piedi da Santa Maria Novella al Duomo?
```
```
Vorrei andare a piedi da Piazza del Duomo a Campo di Marte
```

**多轮 follow-up** (连续贴这两条, 中间不退出, 证明复用上轮 origin + 首轮问候裁剪):
```
Da Piazza del Duomo a Santa Croce a piedi
```
```
E se invece vado fino a Campo di Marte?
```

预期: 每条出 km + ETA; `outputs.txt` 的 `data.wkt` 是完整 LINESTRING。第二条 follow-up 复用
Duomo 起点、只改终点。follow-up 轮回复不以 `Ciao` 开头、不带 "non esitare a chiedere" 收尾客套。

---

## Group B: Car route ✅ (2026-06-15 服务端已修好; 出真车速路线)

```
In auto da Sesto Fiorentino a Scandicci
```

预期: 出完整路线, `distance_km ≈ 19`, `duration ≈ 00:12`, 经 Autostrada del Sole / FI-PI-LI
(~95 km/h = 真车速, 非步行)。`outputs.txt` 有 `data.wkt`。

---

## Group C: TPL 公交发现 ✅ (出线路 / 路线 / 站点列表)

tpl_lines:
```
Che linee di autobus ci sono a Firenze?
```
tpl_routes:
```
Quali sono i percorsi della linea 6?
```
tpl_stops:
```
Quali fermate fa la linea 6?
```

预期: lines 出一批线名 (回复说明用的 agency = *Autolinee Toscane - Urbano Area Metropolitana
Fiorentina*, 共 174 条); routes 出 line 6 的多条路线; stops 出 line 6 前 2 个方向的站点 (一向 36、
一向 34)。`outputs.txt` 里 `data.lines` / `data.routes` / `data.stops` 有内容。

---

## Group D: 已知限制 ⚠️ (诚实降级; 全部服务端待修, client 无误)

每条故意触发已知空/退化, 重点看 client 诚实措辞、不编造数字、不乱说 ZTL。

PT 短途, **退化成步行** (PT 不再返空, 但短途 OD 的 journey 只有一段 foot, 无真公交):
```
Con i mezzi pubblici da Piazza del Duomo a Campo di Marte
```
> 预期: 出 journey, 但 `outputs.txt` 里 `data.legs[0].transport == "foot"` (无 bus/tram 段)。
> (长途 PT 是否出真公交 → 见文末附录诊断, 未确认前不在 demo。)

远程/非中心 foot, 覆盖外:
```
A piedi dalla stazione di Rifredi a Piazza Dalmazia
```
> 预期: `data.route_error` 形如 `routing failed: route not found (code=-2)`; 回复诚实说没算出路线、
> 不给任何数字、请用更精确地址。不乱说 ZTL (foot 失败不触发 ZTL hint)。

stop 时刻表, 链通但无发车时刻 (探针已证服务端定论):
```
A che ora passa la linea 6 alla fermata San Marco?
```
> 预期: 站名匹配到 "Museo Di San Marco", 列出服务该站的线路 (6 / 14 / 23 / 31 / 32) + 明说
> "当前没有发车时刻"。**绝不编造时间**。
> 注: 探针 (`scripts/probe_timetable.py`) 证实 `tpl_stop_timeline` 线上 schema **只有 `stop` 参数**
> (无 datetime), `timetable`/`realtime` 恒空 → **服务端未加载时刻, client 无法修**, 交 referente。

---

## Group E: 范围 / 健壮性 ✅

unsupported (other intent):
```
Che tempo fa domani a Firenze?
```
> 预期: 友好说明只答出行/公交相关问题, 请换个问法 (status `unsupported`)。

缺 origin (missing slot):
```
Vorrei andare a Santa Croce
```
> 预期: 追问起点 (status `missing_place`), **不**说 unsupported。

---

## 自验清单

1. `python chat.py`, 按 Group A → E 逐条贴。
2. A/B/C/E: `✦` 有实质内容、非 `✗`; follow-up 轮不以 `Ciao` 开头、不以客套收尾; car 回复是**意大利语**。
3. `cat outputs.txt`: A/B 有 `data.wkt` (B 距离≈19km); C 有 `data.lines/routes/stops`。
4. D: PT 短途 `data.legs[0].transport == "foot"`; foot 远程是 `data.route_error` 无数字; 时刻表列线路无时刻、**无编造**。
5. 终端回复截图 + `outputs.txt` 给 referente。

---

## 附录: PT 长途诊断 (可选; 不属正式 demo, 未确认前别给 referente)

短途 PT 退化步行。长途选城际走廊, 看 `data.legs` 有无 `transport` 非 `foot` 的段
(真 bus/tram)。连续贴, 看 `outputs.txt`:

```
Con i mezzi pubblici da Santa Maria Novella a Scandicci
```
```
Con i mezzi pubblici da Piazza del Duomo a Sesto Fiorentino
```

> 注: 机场案例 (`aeroporto di Firenze`) 已移除, geocode 把它选成市中心 "Piazza di San Firenze"
> (debug.log 实证), POI/机场类地名 geocode 弱, 待单独 feature 任务。

> **判定**: 某轮 `data.legs` 出现 `"transport": "bus"` / `"tram"` → PT 真能出公交段, 反馈给我后升级进
> Group E/正式 demo。若仍全是 `foot` 段 → PT 引擎对所有 OD 退化步行, 仍报 referente。
> (`group_arc_legs` 见 `mcp_tools.py`; leg 的 `transport`/`provider` 来自服务端 arc。)
