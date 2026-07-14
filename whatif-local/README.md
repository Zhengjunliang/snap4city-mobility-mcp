# whatif-local — 交付 referente 的 whatif-router perf patch

whatif-router 是开源项目 (github.com/disit/whatif-router), **不属本项目交付物**。本目录只留要交给
referente 的性能修复 patch (`patches/`), 外加应用/验证它的最小说明。

## 背景

本项目的 `route` 工具 (`src/snap4city_mobility_mcp/mcp_server.py`) 调 whatif-router 出**全部三种模式**
的路线 (foot/car/bus, 见 `docs/lessons.md` L46)。线上实例的状态:

- **数据 ✅ 已灌 (2026-07-10)**: referente 把 Toscana GTFS (at+gest) 放进线上
  `https://www.snap4city.org/whatif-router/route`, 实测返真公交 → `mcp_server.py` 默认已指线上,
  **本地自托管不再必需**。
- **perf patch ❌ 未合**: 原版每个 PT 请求都 `importOrLoad()` 反序列化 ~2GB graph-cache (无缓存,
  冷调 293s) → 灌完 GTFS 后每 `vehicle=bus` 请求仍 ~30-45s (`mcp_server` 的
  `BUS_ROUTE_TIMEOUT_S=120` 兜住; 合入后可收回超时)。**foot/car 不碰 PT 图, 实测 0.3-0.5s**, 不受影响。

## 目录内容

| 文件 | 用途 |
|---|---|
| `patches/pt-router-singleton.patch` | **交付 referente 的成品** — PT router 单例 + 启动预热 + 干净关闭, 293s → 亚秒。`git apply` 到上游 checkout。 |
| `patches/README.md` | patch 的根因 + 修法 + 验证数据 (给 referente 看)。 |
| `.gitignore` | 挡住重建 patch 时的上游 clone (`whatif-router-src/`)、`data/`、war、下载的 Tomcat。 |

## 应用 / 测试 patch (可选)

只在要验证 patch 或换 GTFS 时才需要, 日常走线上默认即可。

```bash
cd whatif-local/whatif-router-src        # 上游 clone
git apply ../patches/pt-router-singleton.patch
mvn clean package                        # 产出 target/whatif-router-1.0-SNAPSHOT.war (~16MB, 自带依赖)
```

起本地 router 后, 用 env 覆盖让 `route` 指它 (代码默认是线上):

```bash
export S4C_WHATIF_ROUTER_URL=http://localhost:8080/whatif-router/route
```

测: dashboard 问公交, 或直接打桥。带 patch 时 `debug.log` 的 `routing mode=public_transport took`
应 < 2s, 且响应含真公交段 (trip/agency/line/stop)。测完清掉 `S4C_WHATIF_ROUTER_URL` 回默认 (线上实例)。
