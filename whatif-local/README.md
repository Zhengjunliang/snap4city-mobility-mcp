# whatif-local — 本地 whatif-router 验证 harness + 交付 referente 的 perf patch

whatif-router 是开源项目 (github.com/disit/whatif-router), **不属本项目交付物**。本目录只是本地/JupyterHub
起一份 whatif-router、灌 Toscana GTFS、跑通 `bus_route` 端到端的验证 harness，外加要交给 referente 的
性能修复 patch。

## 背景

本项目 `bus_route` (`src/snap4city_mobility_mcp/mcp_server.py`) 调 whatif-router 出真公交线 (见
`docs/lessons.md` L19)。两个问题:

1. **线上实例没数据** — `https://www.snap4city.org/whatif-router/route` 没加载 Toscana GTFS → 只返退化
   步行线 (L31)。请求参数本就正确 (`vehicle=bus` 即走 GTFS 公交路由), 坏的是线上没数据。
2. **每请求全量重载图, 极慢** — 原版 whatif-router 每个 PT 请求都 `importOrLoad()` 反序列化 ~2GB
   graph-cache, 无缓存 → 冷调 293s。见下 `patches/`。

referente 把 GTFS 放线上 + 合入 perf patch 后, 清掉 `S4C_WHATIF_ROUTER_URL` 回默认线上即可, 不再需要
本地/JupyterHub 自托管。

## 目录内容

| 文件 | 用途 |
|---|---|
| `patches/pt-router-singleton.patch` | **交付 referente 的成品** — PT router 单例 + 启动预热, 293s → 亚秒。`git apply` 到上游 checkout。 |
| `patches/README.md` | patch 的根因 + 修法 + 验证数据说明 (给 referente 看)。 |
| `run-on-jupyterhub.sh` | 在 JupyterHub 原生 (无 Docker) 起一份带 patch 的 whatif-router 到 `localhost:8080`, 供 `mcp_server` 联网测。 |
| `.gitignore` | 挡住 `whatif-router-src/` (上游 clone)、`data/`、上传的 `whatif-router.war`、下载的 Tomcat。 |

> 上游源码 (`whatif-router-src/`)、下载数据 (`data/`)、war、Tomcat 均被 `.gitignore` 挡住不进 git。
> 本 repo 里永久留痕的只有 `patches/` (交付物) + `run-on-jupyterhub.sh` (跑 harness 的脚本)。

## JupyterHub 自托管 (端到端联网测公交)

链路: `dashboard(browser) → api.py(:8010) → orchestrator → mcp_server(:8020) → whatif-router(:8080)`。
只有 `mcp_server` (JupyterHub 侧) 调 router。router 跑在同一 JupyterHub → `mcp_server` 走 `localhost`,
不碰外网 (免费 cloudflared 隧道从 JupyterHub egress 被墙, 故不用隧道)。

1. **本地构建 war** (Windows, 需 patch 已应用到 `whatif-router-src/`):
   ```bash
   cd whatif-local/whatif-router-src && git apply ../patches/pt-router-singleton.patch
   docker compose  # 或 mvn clean package —— 产出 target/whatif-router-1.0-SNAPSHOT.war (~16MB, 自带全部依赖)
   ```
   > 也可让 referente/CI 出 war; graphhopper-core:7.0-pre2 是预发布版, 从源码构建更稳。

2. **上传 war 到 JupyterHub**: 把 `whatif-router-src/target/whatif-router-1.0-SNAPSHOT.war` 经 Jupyter
   文件浏览器传到 `whatif-local/`, 重命名 `whatif-router.war`。

3. **JupyterHub 上起 router** (s4c conda env):
   ```bash
   bash whatif-local/run-on-jupyterhub.sh          # = start: 后台常驻 daemon (setsid 脱离终端)
   ```
   自动: 装 Java8 + Tomcat9 → 下 OSM PBF + Toscana GTFS 进 `data/` → 部署 war → **setsid 后台起
   Tomcat 并等 `PtWarmupListener: PT router ready.`** (首启建图数分钟, 日志静默属正常)。
   daemon 脱离终端会话, **关终端/关标签不影响它**, bus_route 持续可用。子命令:
   ```bash
   bash whatif-local/run-on-jupyterhub.sh status   # pid + HTTP 探活
   bash whatif-local/run-on-jupyterhub.sh logs     # tail -f catalina.out
   bash whatif-local/run-on-jupyterhub.sh stop     # 优雅停 (写 MapDB clean flag), 重启/换 war 前用
   bash whatif-local/run-on-jupyterhub.sh run      # 前台跑 (调试用), 只能 Ctrl-C 停
   ```

4. **另开终端, mcp_server 指 localhost**:
   ```bash
   export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
   python -m snap4city_mobility_mcp.mcp_server
   ```

5. **测**: dashboard 问公交, 或
   `curl -s -X POST localhost:8010/advise -d '{"query":"da Duomo a Campo di Marte in autobus","history":[]}'`。
   `debug.log` 里 `bus_route took` 应 < 2s, instructions 含真公交段 (trip/agency/line/stop)。

> **停止只用 `stop` 子命令** (`catalina.sh stop` → `PtWarmupListener.contextDestroyed` 写 MapDB clean
> flag, 下次秒级加载)。daemon 模式关终端/关标签无害 (Tomcat 常驻); 只有 `run` 前台模式必须 Ctrl-C 停,
> 关标签会硬杀 JVM。三终端起法见 `README.md` §11。

> **`Wrong index checksum, store was not closed properly`**: 上次 JVM 被硬杀 (kill -9 / OOM / `run`
> 前台模式下关终端) 没写 MapDB clean-shutdown 标志 → 图缓存判为损坏 (patch 单例常驻, 只有优雅停机
> 才写标志)。修: `stop` 后 `REBUILD_GRAPH=1 bash whatif-local/run-on-jupyterhub.sh` (或手动
> `rm -rf whatif-local/data/graph-cache/*`), 重建图数分钟。daemon 默认 + `stop` 停机后不复发。

测完 referente 把数据/patch 上线后: 清 `S4C_WHATIF_ROUTER_URL` 回默认, 停 Tomcat。
