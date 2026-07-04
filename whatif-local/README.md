# whatif-local — 本地 whatif-router 容器 + Toscana GTFS 验证

referente 要求: whatif-router 是开源项目 (github.com/disit/whatif-router)，本地起容器、灌托斯卡纳
GTFS + OSM PBF、跑测试验证数据兼容；测通后 referente 把数据放线上（修好那个线上 endpoint）。

**为什么要做**: 本项目 `bus_route` (`src/snap4city_mobility_mcp/mcp_server.py`) 调线上
`https://www.snap4city.org/whatif-router/route?vehicle=bus&...`，但线上实例**没加载托斯卡纳
GTFS** → 只返一条退化道路线，无真公交/换乘段（见 `docs/lessons.md` L31）。请求参数本就正确
（`Servlet.java`: `vehicle=bus` 即走 GTFS 公交路由）——坏的是线上没数据。

> 本目录只放 **scaffold 文件**（compose + 脚本 + 本 README）。下面所有 install/download/run/隧道
> 命令**你手动执行**。`data/`、`whatif-router-src/`、`test-output*.json` 已被 `.gitignore` 挡住不进 git。

运行环境: **Windows 本地 Docker Desktop**，内存 ≥8GB。

---

## Step by step

### 1. Docker Desktop 内存 ≥8GB
Settings → Resources → Memory ≥ 8GB（GraphHopper 用 centro PBF + GTFS 建图吃堆内存；compose 里
已设 `CATALINA_OPTS=-Xmx6g`）。

### 2. 克隆 whatif-router 源码
上游没发布镜像/预编译 war，compose 的 `whatif-router` 服务用 Maven 从源码编 war。在本目录跑:
```bash
git clone https://github.com/disit/whatif-router whatif-router-src
```

### 3. 下载数据
```bash
bash fetch-data.sh
```
下载进 `data/`（命名对齐 compose 的 `GH_GTFS_FILES`）:
- `centro-latest.osm.pbf` — OSM 路网 Italy Centro（覆盖 Toscana，~450MB，Geofabrik）
- `at.gtfs` — Autolinee Toscane GTFS（托斯卡纳主公交运营商）
- `gest.gtfs` — GEST GTFS（佛罗伦萨有轨电车）

脚本尾部会 `unzip -l` 抽验每个 GTFS 含 `stops/routes/trips/stop_times/calendar` 必需表；报 `MISS`
说明某表缺失（会导致公交图不全）。

> GEST 直链取自 dati.toscana.it CKAN；若失效，去 `dati.toscana.it/dataset/rt-oraritb` 页面
> 复制 GEST.gtfs / AUTOLINEE_TOSCANE.gtfs 的 download 直链，替换 `fetch-data.sh` 顶部的
> `AT_URL` / `GEST_URL`。

### 4. 编译 war
```bash
docker compose run --rm whatif-router
```
产出 `whatif-router-src/target/whatif-router-1.0-SNAPSHOT.war`。

### 5. 起容器
```bash
docker compose up tomcat
```
**首跑会建 `data/graph-cache`（数分钟~十几分钟，日志静默属正常，别当 hang）**。日志出现
"loaded GTFS"/graph 就绪类字样后可测。
- 若启动崩报 traffic/typical_ttt 相关错: `GH_TYPICAL_TTT_PATH` 指的是空目录 `data/typical_ttt`，
  某些版本会强读 → 看日志，必要时放一个占位 json 或在 compose 去掉该 env（不影响公交路由）。

### 6. 测试
```bash
bash test-route.sh
```
打佛罗伦萨市内 OD（Duomo → Campo di Marte，都在 AT 城网），`vehicle=bus` + 对照 `vehicle=foot`。
结果存 `test-output.json`（bus）和 `test-output-foot.json`（foot）。

**成功判据**: `paths[0].instructions` 里出现**真公交段**（trip/agency/line/stop 字段），且 bus 的
几何/距离与纯步行不同。若 bus == foot 或只有街道 turn-by-turn → GTFS 没加载，回查容器日志 +
`fetch-data.sh` 输出。

### 7. 报告 referente
把以下发给 referente:
- 测试 OD（Duomo → Campo di Marte）
- `test-output.json`（bus）与 `test-output-foot.json`（foot）对比
- instructions 里的真公交段（trip/agency/line）
- 结论: "GTFS 数据兼容，请放线上"

---

## Step 8（可选）— 用真实 dashboard 端到端测本地容器（隧道）

浏览器**从不**直连 whatif-router；链路是
`dashboard(browser) → api.py 桥(JupyterHub) → orchestrator → mcp_server(JupyterHub) → whatif-router`。
只有 `mcp_server`（JupyterHub 侧）调 router。但容器跑在你 Windows 本地，JupyterHub 够不到
`localhost:8080`（NAT）→ 用隧道把本地 :8080 暴露成公网 URL。

**用 cloudflared，不用 ngrok**: 调用方是 `mcp_server` 的 httpx 服务端 GET（非浏览器）。ngrok 免费版
可能对无 `ngrok-skip-browser-warning` header 的请求返 HTML 拦截页 → `bus_route` 解析 JSON 失败；
且 ngrok 需账号+token。cloudflared quick tunnel 无账号、无拦截页、返干净 JSON。

```bash
# Windows 装
winget install --id Cloudflare.cloudflared
# 容器 (:8080) 起好后，另开一个终端跑（进程要一直开着，关终端即断）
cloudflared tunnel --url http://localhost:8080
# 打印 https://<随机词>.trycloudflare.com → 你的 router endpoint = 该 URL + /whatif-router/route
```

然后在 **JupyterHub** 的 `mcp_server` / `api.py` 进程环境里设 env 并重启这两个进程:
```bash
export S4C_WHATIF_ROUTER_URL=https://<随机词>.trycloudflare.com/whatif-router/route
python -m snap4city_mobility_mcp.mcp_server   # 终端1
uvicorn api:app --host 0.0.0.0 --port 8010    # 终端2
```
先自检:
```bash
curl -s -X POST localhost:8010/advise -H "Content-Type: application/json" \
  -d '{"query":"da Duomo a Campo di Marte in autobus","history":[]}'
```
返真公交 route（非退化步行）后，去真实 Snap4City dashboard 聊天框问同句 → 前端画橙公交线。

测完: 清掉 `S4C_WHATIF_ROUTER_URL`（回默认线上）、停隧道、停容器。quick tunnel URL 每次重启会变，
变了要同步更新 env 并重启 mcp_server+api.py。

---

## 附: PBF 太大想减小（可选）
用 osmium 从 centro 切托斯卡纳 bbox 子集（更小更快建图）:
```bash
# bbox: 大致覆盖 Toscana (left,bottom,right,top)
osmium extract -b 9.6,42.2,12.4,44.5 data/centro-latest.osm.pbf -o data/toscana.osm.pbf
# 然后把 compose 的 GH_MAP_PBF 改成 /data/toscana.osm.pbf
```
