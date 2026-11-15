# 震例目录镜像采集（逻辑复制 · wal2json · SQLite）

地震台网数据员把**持续修订中的震例目录**安全地交给重定位分析：网页一键开始建立镜像，
之后持续接收源库的**新增、更正、删除**；初次取数不停写源库；镜像与续采位置持久保存后
才向源库确认复制进度；重启后已建镜像续采、未建完重做且不供数。

## 组成

```
┌────────────┐  逻辑复制(wal2json)  ┌─────────────────────┐        ┌──────────┐
│ PostgreSQL │ ───────────────────▶ │ app: FastAPI        │ ─────▶ │ 浏览器    │
│ seismic_   │  CREATE_REPLICATION_│  · 初始装载(快照)    │  HTTP  │ 状态/查询 │
│ events     │  SLOT 导出一致性快照 │  · 流式续采          │        │ CSV 下载  │
│ (持续被修订)│ ◀── send_feedback ── │  · SQLite 镜像+位置  │        └──────────┘
└─────▲──────┘   确认复制进度        └─────────┬───────────┘
      │                                        │ 持久化(卷 appdata)
      │ 不断新增/更正/删除                        ▼
└─────┴────────┐                        ┌──────────────┐
│  simulator   │                        │ mirror.db    │
│ (源库扰动器)  │                        │ events+meta  │
└──────────────┘                        └──────────────┘
```

- **db**：PostgreSQL 16 + wal2json 插件，`wal_level=logical`；初始化脚本写入
  **30000 条历史震例**（震例号 / UTC 发震时刻 / 纬度 / 经度 / 震级）。
  新版 PostgreSQL 默认只信任 `pgoutput`/`test_decoding` 作为输出插件，compose 已用
  `output_plugin_libraries=pgoutput,wal2json` 显式白名单 wal2json（逗号后不能带空格）。
- **app**：FastAPI + psycopg2 + SQLite。后台线程做初始装载与逻辑复制续采，
  网页提供采集状态、震例查询、当前目录 CSV 下载、**零报日质检**、一致性校验。
- **simulator**：源库扰动器，持续对震例表做新增/更正/删除（含一笔事务改多行的
  “台网重新测定”），模拟目录的持续修订。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000 ，点击「开始建立镜像」
```

状态页每 3 秒自动刷新，依次经历：`初始装载中 → 追平变更中 → 已就绪（供数中）`。
装载期间 simulator 仍在写源库（约每秒一笔事务），这些变更全部经复制槽在追平阶段补齐。

## 需求实现对照

| 要求 | 实现 |
| --- | --- |
| 初次取数不停写源库 | 初始装载在 `CREATE_REPLICATION_SLOT` 导出的**一致性快照**里做 REPEATABLE READ 只读扫描，源库照常读写 |
| 完整初始镜像接上后续变更，不漏不重 | 快照与槽位 `consistent_point` 严格对齐：快照之前的都在装载里，之后的都从槽位流式到达 |
| 旧值不得盖回新值 | 单槽单消费者，变更严格按 LSN 顺序应用；wal2json 每条消息携带该事务提交时的整行新值 |
| 每笔源事务整笔可见 | wal2json `format-version 1`：一条消息=一笔源事务；其全部行变更在**同一个 SQLite 事务**里提交 |
| 镜像与续采位置持久保存后才确认 | 行变更与 `meta.last_applied_lsn` 同事务提交（WAL + `synchronous=FULL`），**提交后**才 `send_feedback` |
| 写入失败不前移 | 失败/崩溃则不确认；源库重发；镜像端 upsert/delete 幂等重放 |
| 重启后已建镜像续采 | 状态 `done` 持久在 SQLite：重启后直接从槽位已确认位置续采，不重新装载 |
| 未建完则重做且不供数 | 重启时状态为 `loading/catchup`：丢弃半成品重新建立；就绪前查询/CSV 一律 503 |
| 每次取数只读同一已提交状态 | 每个网页请求只开一个 SQLite 只读事务，计数、取页、整个 CSV 流都在同一快照里 |
| 只处理行变更 | 仅应用 insert/update/delete；truncate 等记录并忽略 |
| 零报日：已结束 UTC 日无震例、前七日日日 ≥3 | `zero_report_alerts` 由镜像派生：按 `origin_time` 的 UTC 日计数判定；UTC 今日不判定 |
| 迟报/跨日更正/删除改变历史计数 | 每条源事务记录被改到的旧日期+新日期，**同一 SQLite 事务**里重判这些日及其后七日 |
| 源库无新事务，跨过 UTC 午夜也更新 | `MidnightRefresher` 线程每秒检查，跨日后推进“已评估截至”并重判新结束日；停机多日按需全量重建 |
| 告警与目录、续采位置一起提交，失败不确认 | 数据 + 告警 + `last_applied_lsn` 同事务提交，**提交后**才 `send_feedback`；失败整笔回滚、源库重发 |
| 重送不回退告警 | 告警行带 `first_seen`，重放只重算内容、不改首次告警时间；重算内容相同则 `updated_at` 不动 |
| 重建期间不供旧结果 | `POST /mirror/rebuild` 立即把状态置 `loading` 并清空旧告警；就绪前 `/alerts`、CSV、查询一律 503 |

## 零报日质检

被删/漏采的 UTC 日往往“安静得可疑”：某天一条震例都没有，而前七天每天都有。
网页「零报日质检」按以下规则列出这些日子：

- 只判定**已结束**的 UTC 日（当天尚未结束，不判断）；
- 该日震例数为 0，且此前连续 7 天（D-7 … D-1）每天至少 3 笔；
- 每条告警展示：零报日、当日计数（0）、前七日各自的计数与日期、前七合计、首次告警时间；
- 支持按告警日筛选与 CSV 下载（`/alerts.csv`，整个文件出自同一已提交状态）。

结果如何保持正确：

1. **随源事务增量重判**：每笔源事务应用时收集受影响 UTC 日期（新值日期 + 更正/删除前的
   镜像旧日期），在同一个 SQLite 写事务里重判这些日及其后七日。于是迟报（补进零报日）、
   跨日更正、删除都会立即反映；前七日里某天被改到少于 3 笔时，后续告警会撤销。
2. **UTC 午夜刷新**：即使源库一条事务都没有，后台线程跨过 UTC 午夜后会把新结束的那一天
   纳入判定（并补齐应用停机期间漏掉的日期；间隔过长时自动全量重建）。
3. **与目录同生共死**：告警、目录、续采位置同一个事务提交；写入失败则一起回滚且不向源库
   确认，源库重发后重算，结果幂等，`first_seen` 不回退。
4. **重建期间不供旧结果**：点击「重新建立」立刻停止供数并清空旧告警，镜像就绪前
   `/alerts`、`/alerts.csv` 返回 503。

> 判定只基于目录里的 `origin_time`，不依赖源库的采集/入库时间；因此“当天没有任何震例”
> 在语义上就是“疑似采集断档”。

## 演示场景

### 1. 装载中修订（源库不停写）

点击「开始建立镜像」后（默认每批 500 行、批间停 120ms，装载约 7 秒），立刻另开终端制造修订：

```bash
docker compose exec db psql -U postgres -d quakes -c \
  "UPDATE seismic_events SET magnitude=9.9 WHERE event_id='EQH-000001';"
docker compose exec db psql -U postgres -d quakes -c \
  "INSERT INTO seismic_events VALUES('EQDEMO-1', now(), 30.0, 100.0, 5.5);"
docker compose exec db psql -U postgres -d quakes -c \
  "DELETE FROM seismic_events WHERE event_id='EQH-000002';"
```

同时 simulator 每秒都在改库。状态变为「已就绪」后到「查询震例」查
`EQH-000001`（震级 9.9）、`EQDEMO-1`（存在）、`EQH-000002`（不存在）——
装载期间发生的修订一条不漏地接上了。

### 2. 一致性校验（不漏不重）

```bash
docker compose pause simulator        # 暂停源库扰动
# 等状态页「待确认延迟」变为 0，点「一致性校验」
# => 两侧各 N 行，SHA-256 相同
docker compose unpause simulator
```

### 3. 保存后尚未确认就重启

状态「已就绪」时点击「模拟崩溃：保存后未确认」。下一笔源事务**写入镜像并持久化之后、
向源库确认之前**，进程故意退出；Compose 自动重启 app。随后可在状态页看到：

- 事件日志记录“已保存但未确认，模拟进程崩溃”；
- 短时间内「镜像已保存位置」领先「源库已确认位置」（黄色提示条）；
- 源库把该事务**重发**，镜像幂等重放 —— 行数不变、内容不重不漏，两侧位置重新对齐。

### 4. 未建完则重做，且不供数

```bash
# 在「初始装载中」阶段：
docker compose restart app
```

app 重启后发现状态是 `loading`（未建完），自动丢弃半成品重新装载；
此期间访问 `/events` 或 `/events.csv` 返回 **503 镜像未就绪，暂不供数**。

### 6. 零报日质检（含迟报、跨日更正、删除与跨午夜）

镜像就绪后打开「零报日质检」。把某个 UTC 日的全部震例删掉，且此前七日每天 ≥3 笔时，
该日立即出现在告警表里（带前七日计数）：

```bash
docker compose exec db psql -U postgres -d quakes -c \
  "DELETE FROM seismic_events WHERE (origin_time AT TIME ZONE 'UTC')::date = DATE '2024-06-19';"
```

再补一笔迟报，告警随即消失；把它改到别的日期（跨日更正）或删除，告警又恢复。
即使源库完全空闲，跨过 UTC 午夜后新结束的那一天也会自动纳入判定（页面无需任何操作）。

### 5. 已建镜像续采

```bash
# 状态「已就绪」时：
docker compose restart app
```

事件日志显示“镜像已就绪，从复制槽的已确认位置续采”——**不重新装载**，
直接从上次确认位置继续，重启期间的源库变更全部补齐。

## 网页与接口

| 路径 | 说明 |
| --- | --- |
| `GET /` | 采集状态（3s 自刷新）：状态机、行数、已保存/已确认 LSN、复制槽延迟、事件日志、操作按钮 |
| `POST /mirror/start` | 开始建立镜像（idle 时） |
| `POST /mirror/rebuild` | 丢弃当前镜像重新建立（立即停止供数、清空旧告警） |
| `POST /demo/crash-after-save` | 武装“保存后未确认就崩溃”演示 |
| `GET /events` | 查询震例：震例号前缀、发震时刻区间、最小震级、经纬度范围、分页 |
| `GET /events.csv` | 下载当前目录 CSV（支持同样筛选；整个文件出自同一已提交状态） |
| `GET /alerts` | 零报日质检：告警日、当日计数、前七日逐日计数、首次告警时间；可按告警日筛选 |
| `GET /alerts.csv` | 下载零报日告警 CSV（同一已提交状态） |
| `GET /verify` | 源库 vs 镜像全量比对（行数 + SHA-256 + 差异样例） |
| `GET /api/status` | 上述状态的 JSON 版（含零报日条数与判定截至日） |
| `GET /healthz` | 存活探针 |

## 配置（compose.yaml 环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SLOT_NAME` | `quake_mirror` | 逻辑复制槽名 |
| `LOAD_BATCH` | `500` | 初始装载每批行数 |
| `LOAD_SLEEP_MS` | `120` | 批间停顿（放大演示窗口；生产设 `0`） |
| `ALERT_TICK_SECONDS` | `1` | 零报日质检的跨日刷新检查间隔 |
| `TICK_MS` | `900` | simulator 扰动节奏基准 |

## 故障恢复矩阵

| 时刻 | 重启后行为 |
| --- | --- |
| 初始装载中 | 状态 `loading` → 重做装载，就绪前不供数 |
| 追平中 | 状态 `catchup` → 重做装载（快照已失效，必须重来），就绪前不供数 |
| 已就绪 | 状态 `done` → 从槽位已确认位置续采，不重新装载 |
| 保存后未确认就崩溃 | 源库重发未确认事务，镜像幂等重放，不重不漏 |
| 复制槽被删（源库重置） | 自动转为重新建立镜像 |
| 卷被清空（`down -v`） | 一切从零开始，等待点击「开始建立镜像」 |
| 重建时源库空闲 | 主动取消复制流，立即重建；源库没有新事务也不会卡住 |
| 跨 UTC 午夜 | 后台线程推进零报日判定，不依赖源库是否有新事务 |

## 本地测试

核心逻辑（SQLite 落库、wal2json 事务应用、LSN 换算、快照读一致性、零报日规则、
跨午夜刷新、告警与目录/确认顺序）有单元测试，无需 Docker、无需源库：

```bash
python3 -I tests/test_core.py       # 镜像落库 / wal2json / LSN / 快照读
python3 -I tests/test_alerts.py     # 零报日规则、迟报/更正/删除、跨午夜、重建不供旧结果
python3 -I tests/test_flow.py       # 复制应用：告警与目录/位置同事务、失败不确认、重送幂等
```

网页层自测需要 `fastapi`/`httpx`（可用临时 SQLite，无需源库）：

```bash
python3 tests/test_web_alerts.py    # 页面/筛选/CSV/503/状态 API
```

## 目录结构

```
compose.yaml            # 一键启动：db + app + simulator
db/                     # PostgreSQL 16 + wal2json，含 3 万条历史震例的初始化脚本
app/                    # FastAPI 网页 + 逻辑复制采集线程 + SQLite 镜像
  replicator.py         #   状态机：idle→loading→catchup→done，快照装载与流式续采
  store.py              #   SQLite 层：数据 / 告警 / 续采位置同事务持久化
  changes.py            #   wal2json 行变更 -> 幂等 upsert/delete（含受影响日期）
  zeroreport.py         #   零报日质检：规则、增量重判、UTC 午夜刷新线程
  main.py               #   网页：状态 / 查询 / CSV / 零报日 / 校验
simulator/              # 源库扰动器：持续新增/更正/删除
tests/test_core.py      # 核心逻辑单元测试
tests/test_alerts.py    # 零报日规则与刷新单元测试
tests/test_flow.py      # 复制应用与确认顺序单元测试
tests/test_web_alerts.py# 网页层自测（可选，需 fastapi/httpx）
```
