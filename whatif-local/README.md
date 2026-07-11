# whatif-local — 交付 referente 的 whatif-router perf patch

whatif-router 是开源项目 (github.com/disit/whatif-router), **不属本项目交付物**。本目录只留要交给
referente 的性能修复 patch (`patches/`), 外加应用/验证它的最小说明。

## 背景

本项目 `bus_route` (`src/snap4city_mobility_mcp/mcp_server.py`) 调 whatif-router 出真公交线 (见
`docs/lessons.md` L19)。两个问题:

1. **线上实例没数据** — ✅ **已解决 (2026-07-10)**: referente 把 Toscana GTFS (at+gest) 灌进线上
   `https://www.snap4city.org/whatif-router/route`, 实测返真公交。之前无 GTFS 只返退化步行线 (L31)。
2. **每请求全量重载图, 极慢** — 原版 whatif-router 每个 PT 请求都 `importOrLoad()` 反序列化 ~2GB
   graph-cache, 无缓存 → 冷调 293s。见下 `patches/`。**线上尚未合入** → 灌完 GTFS 后实测每请求
   仍 ~30-40s (`mcp_server` 用 `BUS_ROUTE_TIMEOUT_S=120` 兜住; 合入后可收回超时)。

`mcp_server.py` 默认已指线上, 本地/JupyterHub 自托管**不再必需** — 无需再单独跑一份 router。

## 目录内容

| 文件 | 用途 |
|---|---|
| `patches/pt-router-singleton.patch` | **交付 referente 的成品** — PT router 单例 + 启动预热, 293s → 亚秒。`git apply` 到上游 checkout。 |
| `patches/README.md` | patch 的根因 + 修法 + 验证数据说明 (给 referente 看)。 |
| `.gitignore` | 挡住重建 patch 时的上游 clone (`whatif-router-src/`)、`data/`、war、下载的 Tomcat, 不进 git。 |

## 应用 / 测试 patch (可选)

只在要验证或换 GTFS 时才需要, 日常走线上默认即可。

```bash
# 应用到上游 checkout
cd whatif-local/whatif-router-src && git apply ../patches/pt-router-singleton.patch
docker compose  # 或 mvn clean package —— 产出 target/whatif-router-1.0-SNAPSHOT.war (~16MB, 自带全部依赖)
```

起一份本地/自建 router 后, 让 `mcp_server` 的 `bus_route` 指它 (代码默认是线上, 这行 env 覆盖):

```bash
export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
```

测: dashboard 问公交, 或
`curl -s -X POST localhost:8010/advise -d '{"query":"da Duomo a Campo di Marte in autobus","history":[]}'`。
带 patch 时 `debug.log` 的 `bus_route took` 应 < 2s, instructions 含真公交段 (trip/agency/line/stop)。
测完清 `S4C_WHATIF_ROUTER_URL` 回默认 (线上实例)。
