"""wal2json（format-version 1）行变更 -> SQLite 镜像。

wal2json format 1：一条消息 = 一笔源事务，形如
  {"change": [ {"kind": "insert"|"update"|"delete", ...}, ... ], "xid": ..., ...}

应用规则：
- insert/update：按主键 upsert（幂等 —— 源库重发时重复应用结果不变）；
- update 若改了主键：先按 oldkeys 删旧行再 upsert 新行；
- delete：按 oldkeys 主键删除（幂等 —— 删不存在的行是无害空操作）；
- 只处理行变更：truncate 等其他消息记录并忽略。

变更只可能按 LSN 顺序被应用（单槽单消费者），且每条 wal2json 消息携带的是
该事务提交时的整行新值，因此旧值永远不会盖回新值。
"""
from canonical import norm_ts
from zeroreport import day_of_ts

UPSERT_SQL = """
INSERT INTO events(event_id, origin_time, latitude, longitude, magnitude)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(event_id) DO UPDATE SET
    origin_time = excluded.origin_time,
    latitude    = excluded.latitude,
    longitude   = excluded.longitude,
    magnitude   = excluded.magnitude
"""

DELETE_SQL = "DELETE FROM events WHERE event_id = ?"


def _old_event_id(change):
    keys = change.get("oldkeys") or {}
    names = keys.get("keynames") or []
    if "event_id" in names:
        return keys["keyvalues"][names.index("event_id")]
    return None


def _mirror_day(cur, event_id):
    """变更前镜像里该行的 UTC 日期；行不存在返回 None。

    wal2json 的 oldkeys 默认只带主键，跨日更正/删除的「旧日期」要从镜像里
    查出来，才能把受影响日及其后七日一并重判。
    """
    if event_id is None:
        return None
    row = cur.execute(
        "SELECT origin_time FROM events WHERE event_id=?", (event_id,)
    ).fetchone()
    return day_of_ts(row[0]) if row else None


def apply_change(cur, change):
    """应用一条行变更，返回 (kind 或 None, 受影响 UTC 日期集合)。"""
    kind = change.get("kind")
    if kind in ("insert", "update"):
        cols = dict(zip(change["columnnames"], change["columnvalues"]))
        days = set()
        if kind == "update":
            old_id = _old_event_id(change)
            old_day = _mirror_day(cur, old_id)
            if old_day:
                days.add(old_day)
            if old_id is not None and old_id != cols["event_id"]:
                cur.execute(DELETE_SQL, (old_id,))   # 主键被改：先删旧行
        ts = norm_ts(cols["origin_time"])
        cur.execute(
            UPSERT_SQL,
            (
                cols["event_id"],
                ts,
                float(cols["latitude"]),
                float(cols["longitude"]),
                float(cols["magnitude"]),
            ),
        )
        days.add(day_of_ts(ts))
        return kind, days
    if kind == "delete":
        old_id = _old_event_id(change)
        days = set()
        if old_id is not None:
            old_day = _mirror_day(cur, old_id)
            if old_day:
                days.add(old_day)
            cur.execute(DELETE_SQL, (old_id,))
        return kind, days
    return None, set()  # truncate 等：只处理行变更


def apply_transaction(cur, payload):
    """把一笔源事务的全部行变更应用到（调用方持有的）同一个 SQLite 事务里。

    调用方负责提交/回滚 —— 整笔源事务要么整笔可见，要么完全不可见。
    返回 (counts, affected_days)：affected_days 是这笔事务涉及的 UTC 日期
    （含更正/删除前的旧日期），调用方据此在**同一事务**里重判零报日。
    """
    counts = {"insert": 0, "update": 0, "delete": 0, "other": 0}
    affected_days = set()
    for change in payload.get("change", []):
        kind, days = apply_change(cur, change)
        counts[kind if kind else "other"] += 1
        affected_days |= days
    return counts, affected_days
