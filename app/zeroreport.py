"""零报日质检：从持续修订的震例目录中找出疑似采集断档的 UTC 日。

判定规则（全部按 UTC 日历日、按 origin_time 归类）：

  某个**已结束**的 UTC 日 D 没有震例（计数为 0），且此前连续 7 天
  （D-7 … D-1）每天至少 3 笔震例 => D 是零报日告警。
  当天（UTC 今日）尚未结束，不参与判定。

结果保存为派生表 ``zero_report_alerts``，每行记录告警日与它前七日的计数：

  * 源事务应用时，把该事务改到的每一个历史日期（旧值日期 + 新值日期）
    连同其后 7 天放在**同一个 SQLite 事务**里重判 —— 迟报、跨日更正、
    删除都会按整笔源事务完成后的目录刷新受影响日及其后七日的告警；
  * 镜像数据、告警表、续采位置（meta.last_applied_lsn）同事务提交，
    失败则一起回滚、不向源库确认；源库重送时按当前目录重算，结果幂等，
    告警行的 first_seen 不会被重送回退；
  * 即使源库没有新事务，``MidnightRefresher`` 也会在跨过 UTC 午夜后
    把新结束的那一天纳入判定（并补齐应用停机期间漏掉的日期）。

本模块只依赖标准库 / SQLite，便于单元测试。
"""
import json
import threading
from datetime import date, datetime, timedelta, timezone

WINDOW_DAYS = 7              # 前七日
MIN_EVENTS_PER_DAY = 3       # 每日至少三笔
FULL_REBUILD_MAX_GAP_DAYS = 31

ALERTS_TABLE = "zero_report_alerts"


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def as_date(value) -> date:
    """date / datetime / ISO 文本 -> date（datetime 先换算到 UTC）。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def day_of_ts(ts) -> str:
    """规范化后的 origin_time 文本 -> UTC 日期字符串 'YYYY-MM-DD'。"""
    return as_date(str(ts)[:10]).isoformat()


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _get_meta(cur, key, default=""):
    row = cur.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


def _set_meta(cur, key, value):
    cur.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _counts_between(cur, first_day: date, last_day: date):
    """[first_day, last_day] 内每个 UTC 日的事件数（走 origin_time 索引）。"""
    start = first_day.isoformat() + "T00:00:00"
    end_exclusive = (last_day + timedelta(days=1)).isoformat() + "T00:00:00"
    rows = cur.execute(
        "SELECT substr(origin_time, 1, 10) AS d, count(*) FROM events "
        "WHERE origin_time >= ? AND origin_time < ? GROUP BY d",
        (start, end_exclusive),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _prior_counts(day: date, counts):
    out = []
    for offset in range(WINDOW_DAYS, 0, -1):
        d = day - timedelta(days=offset)
        out.append({"date": d.isoformat(), "count": int(counts.get(d.isoformat(), 0))})
    return out


def _alert_if_matches(day: date, counts):
    """按目录计数判断 day 是否零报日；是则返回告警行，否则 None。"""
    if int(counts.get(day.isoformat(), 0)) != 0:
        return None
    prior = _prior_counts(day, counts)
    if not all(item["count"] >= MIN_EVENTS_PER_DAY for item in prior):
        return None
    return {
        "date": day.isoformat(),
        "day_count": 0,
        "prior_counts": prior,
        "prior_total": sum(item["count"] for item in prior),
    }


def _upsert_alert(cur, row):
    """写入/更新告警行；first_seen 只在首次插入时写，重送/重算不回退。"""
    prior_json = json.dumps(
        row["prior_counts"], ensure_ascii=False, separators=(",", ":")
    )
    existing = cur.execute(
        "SELECT day_count, prior_counts, first_seen, updated_at FROM zero_report_alerts "
        "WHERE alert_date=?",
        (row["date"],),
    ).fetchone()
    if existing and int(existing[0]) == row["day_count"] and existing[1] == prior_json:
        # 内容未变：不动 first_seen/updated_at，重放源事务不会制造“变化”
        row["first_seen"] = existing[2]
        row["updated_at"] = existing[3]
        return row
    now = _now_text()
    if existing:
        cur.execute(
            "UPDATE zero_report_alerts SET day_count=?, prior_counts=?, updated_at=? "
            "WHERE alert_date=?",
            (row["day_count"], prior_json, now, row["date"]),
        )
        row["first_seen"] = existing[2]
        row["updated_at"] = now
        return row
    cur.execute(
        "INSERT INTO zero_report_alerts"
        "(alert_date, day_count, prior_counts, first_seen, updated_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (row["date"], row["day_count"], prior_json, now, now),
    )
    row["first_seen"] = now
    row["updated_at"] = now
    return row


def _delete_alert(cur, day: str):
    cur.execute("DELETE FROM zero_report_alerts WHERE alert_date=?", (day,))


def evaluate_day(cur, day, today=None):
    """在一个已开启的写事务里重判单日并更新告警表。

    day >= today（当天未结束或未来）时不做判定，并清掉可能存在的旧行。
    返回告警行 dict 或 None。
    """
    day_obj = as_date(day)
    today_obj = as_date(today) if today is not None else utc_today()
    if day_obj >= today_obj:
        _delete_alert(cur, day_obj.isoformat())
        return None
    counts = _counts_between(
        cur, day_obj - timedelta(days=WINDOW_DAYS), day_obj
    )
    row = _alert_if_matches(day_obj, counts)
    if row is None:
        _delete_alert(cur, day_obj.isoformat())
        return None
    return _upsert_alert(cur, row)


def evaluate_affected_days(cur, affected_days, today=None):
    """整笔源事务应用完成后，重判每个受影响日及其后七日。

    受影响日可能很多（台网重新测定一笔事务改多行），这里只对整个区间做
    一次计数查询，再在内存里逐日判定。
    """
    today_obj = as_date(today) if today is not None else utc_today()
    targets = set()
    for value in affected_days or ():
        try:
            base = as_date(value)
        except (TypeError, ValueError):
            continue
        for offset in range(WINDOW_DAYS + 1):       # 受影响日 + 其后 7 天
            targets.add(base + timedelta(days=offset))
    targets = {d for d in targets if d < today_obj}
    if not targets:
        return []

    first = min(targets) - timedelta(days=WINDOW_DAYS)
    last = max(targets)
    counts = _counts_between(cur, first, last)
    changed = []
    for day in sorted(targets):
        row = _alert_if_matches(day, counts)
        if row is None:
            _delete_alert(cur, day.isoformat())
        else:
            changed.append(_upsert_alert(cur, row))
    return changed


def full_rebuild(cur, today=None):
    """按当前完整目录重建全部告警（初始装载完成 / 需要整体修复时）。"""
    today_obj = as_date(today) if today is not None else utc_today()
    completed = today_obj - timedelta(days=1)
    rows = cur.execute(
        "SELECT substr(origin_time, 1, 10) AS d, count(*) FROM events GROUP BY d"
    ).fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}

    cur.execute(f"DELETE FROM {ALERTS_TABLE} WHERE alert_date > ?", (completed.isoformat(),))
    inserted = 0
    valid = set()
    if counts:
        first = min(date.fromisoformat(d) for d in counts)
        day = first
        while day <= completed:
            row = _alert_if_matches(day, counts)
            if row is not None:
                valid.add(day.isoformat())
                _upsert_alert(cur, row)          # 内容未变时保留 first_seen
                inserted += 1
            day += timedelta(days=1)
    # 清掉不再成立的告警（仍成立的保留 first_seen）
    keep = {
        r[0] for r in cur.execute(f"SELECT alert_date FROM {ALERTS_TABLE}").fetchall()
    }
    for stale in sorted(keep - valid):
        _delete_alert(cur, stale)

    _set_meta(cur, "alerts_evaluated_through", completed.isoformat())
    _set_meta(cur, "alerts_ready", "1")
    _set_meta(cur, "alerts_updated_at", _now_text())
    return inserted


def reset_alerts(cur):
    """重建镜像时清掉旧结果并禁止供数，避免把上一代目录的告警拿出来。"""
    cur.execute(f"DELETE FROM {ALERTS_TABLE}")
    _set_meta(cur, "alerts_ready", "0")
    _set_meta(cur, "alerts_evaluated_through", "")
    _set_meta(cur, "alerts_updated_at", "")


def refresh_alerts_through(cur, today=None):
    """把判定推进到昨日；跨 UTC 午夜或应用停机数日后由调度线程调用。"""
    today_obj = as_date(today) if today is not None else utc_today()
    completed = today_obj - timedelta(days=1)
    last_raw = _get_meta(cur, "alerts_evaluated_through", "")
    try:
        last = date.fromisoformat(last_raw[:10]) if last_raw else None
    except ValueError:
        last = None

    need_full = (
        last is None
        or last > completed
        or (completed - last).days > FULL_REBUILD_MAX_GAP_DAYS
    )
    if need_full:
        n = full_rebuild(cur, today_obj)
        return {
            "mode": "full",
            "through": completed.isoformat(),
            "days": (completed - (last or completed)).days if last else 0,
            "alerts": n,
        }

    days = 0
    day = last + timedelta(days=1)
    while day <= completed:
        evaluate_day(cur, day, today_obj)
        days += 1
        day += timedelta(days=1)
    _set_meta(cur, "alerts_evaluated_through", completed.isoformat())
    _set_meta(cur, "alerts_ready", "1")
    if days:
        _set_meta(cur, "alerts_updated_at", _now_text())
    return {"mode": "incremental", "through": completed.isoformat(), "days": days}


def alert_count(cur):
    return cur.execute(f"SELECT count(*) FROM {ALERTS_TABLE}").fetchone()[0]


def _row_to_item(row):
    prior = json.loads(row[2])
    return {
        "date": row[0],
        "day_count": int(row[1]),
        "prior_counts": prior,
        "prior_total": sum(int(x["count"]) for x in prior),
        "first_seen": row[3],
        "updated_at": row[4],
    }


def list_alerts(txn, limit=50, offset=0, day=None):
    """只读事务内列出告警：按告警日倒序，日期精确筛选可选。"""
    where, params = "", []
    if day:
        where = " WHERE alert_date=?"
        params.append(as_date(day).isoformat())
    total = txn.execute(
        f"SELECT count(*) FROM {ALERTS_TABLE}{where}", params
    ).fetchone()[0]
    rows = txn.execute(
        "SELECT alert_date, day_count, prior_counts, first_seen, updated_at "
        f"FROM {ALERTS_TABLE}{where} ORDER BY alert_date DESC LIMIT ? OFFSET ?",
        params + [int(limit), int(offset)],
    ).fetchall()
    return {"total": int(total), "items": [_row_to_item(r) for r in rows]}


class MidnightRefresher(threading.Thread):
    """UTC 午夜刷新线程：源库没有新事务时也推进零报日判定。

    只在镜像 ``done`` 时工作；重建（loading/catchup）期间不读旧结果、
    也不写告警表。每个 tick 以 ``BEGIN IMMEDIATE`` 重判并提交，和镜像
    写入串行化，不会看到半笔源事务。
    """

    def __init__(self, store, interval=1.0):
        super().__init__(daemon=True, name="alert-refresher")
        self.store = store
        self.interval = max(0.2, float(interval))
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _due(self, today: date) -> bool:
        if self.store.get_meta("load_state", "idle") != "done":
            return False
        ready = self.store.get_meta("alerts_ready", "0")
        last_raw = self.store.get_meta("alerts_evaluated_through", "")
        if ready != "1" or not last_raw:
            return True
        try:
            return date.fromisoformat(last_raw[:10]) < today - timedelta(days=1)
        except ValueError:
            return True

    def tick(self, today=None):
        """检查并推进一次；返回本次动作 dict，无事可做则 None。"""
        today_obj = as_date(today) if today is not None else utc_today()
        if not self._due(today_obj):
            return None
        with self.store.write_txn() as cur:
            # 拿到写锁后再次确认：避免与「开始重建」赛跑时写旧结果
            if _get_meta(cur, "load_state", "idle") != "done":
                return None
            before = alert_count(cur)
            result = refresh_alerts_through(cur, today_obj)
            after = alert_count(cur)
            if result["mode"] == "full":
                msg = (
                    f"零报日质检：按当前目录重建告警，截至 {result['through']}，"
                    f"共 {after} 条"
                )
            else:
                msg = (
                    f"零报日质检：跨日更新，评估 {result['days']} 个已结束日"
                    f"（{before}→{after} 条告警），截至 {result['through']}"
                )
            self.store.log(msg, cur)
            result["alerts"] = after
            return result

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                 # 刷新失败不影响采集；下个 tick 再试
                try:
                    self.store.log(f"零报日质检刷新失败：{exc!r}")
                except Exception:
                    pass
            self._stop.wait(self.interval)
