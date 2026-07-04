# whatif-router PT performance patch

Patch for the upstream [disit/whatif-router](https://github.com/disit/whatif-router) cloned into
`whatif-local/whatif-router-src/` (which is gitignored — this folder is the tracked record of the
change and the deliverable for the referente).

## Problem

Every public-transport (`vehicle=bus` / `routing=pt`) request rebuilds the entire routing engine
from scratch, so a single route takes minutes.

In `Servlet.getRoute()` the PT branch did, **per request**:

```java
hopper = initGHGtfs(config);                 // new GraphHopperGtfs + importOrLoad()
PtRouter ptRouter = initPtRouter(config, ..);// new PtRouter + new TranslationMap().doImport()
response = getGtfsRoute(ptRouter, ...);
...
hopper.close();                              // throw it all away
```

`initGHGtfs().importOrLoad()` deserializes the whole GTFS graph-cache into the heap
(`gtfs_0.p` ≈ 477MB + edges + geometry + location index, ≈ 2GB total), then `hopper.close()`
discards it — so the next request pays the full load again. The cost is **O(load the graph)**, not
O(route a trip), and the 2GB load into a 6g heap also thrashes GC.

Measured against a local instance loaded with Tuscany GTFS (Autolinee Toscane), one PT route:

| call | time |
|------|------|
| cold (first, disk cold) | **293.7 s** |
| warm (OS page cache warm) | **125.3 s** |

The warm/cold gap is just OS page cache on the graph files — it is **not** a network/tunnel effect
(both numbers are direct localhost, no proxy involved).

## Fix

Build the PT engine (`GraphHopperGtfs` + `PtRouter` + translation map) **once** and reuse it for the
webapp's lifetime. The router is date-independent (the query date only enters the `Request` in
`getGtfsRoute`), so a shared singleton is safe.

- `Servlet.java`: cache `GraphHopperGtfs` / `PtRouter` in static fields, build lazily via a
  double-checked-locking `getPtRouter()`; PT branch uses the cache; removed `hopper.close()`.
- `PtWarmupListener.java` (new) + `web.xml`: a `ServletContextListener` calls `getPtRouter()` at
  startup, so the one-time ~2-minute graph load happens during container boot instead of on the
  first user's request.
- `pom.xml`: added `javax.servlet-api` (scope `provided`) for the listener; Tomcat supplies it at
  runtime, so it is not bundled into the war.

Result: after boot, each PT route drops from 125–293 s to sub-second.

## Applying

```bash
cd whatif-router-src            # the upstream clone
git apply ../patches/pt-router-singleton.patch
mvn clean package               # rebuild the war
```

To revert to pristine upstream: `git checkout .` inside `whatif-router-src`.
