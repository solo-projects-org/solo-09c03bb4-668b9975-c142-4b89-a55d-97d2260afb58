#!/usr/bin/env python3
"""零报日质检单元测试（仅标准库）：

  python3 -I tests/test_alerts.py

覆盖：告警判定（已结束日、前七日每日>=3、当天不判定）、UTC 午夜无新事务也推进、
     迟报/跨日更正/删除按整笔事务完成后的目录重判受影响日及其后七日、
     告警与目录/续采位置同事务提交、失败不确认、重送不回退首次告警。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from changes import apply_transaction                 # noqa: E402
from store import Store                               # noqa: E402
import zeroreport                                     # noqa: E402

TZ = timezone.utc


def ts(day, hour=12):
    return f"{day} {hour:02d}:00:00.000000+00"


def change(kind, eid, when, lat=34.0, lon=100.0, mag=4.0):
    if kind == "delete":
        return {
            "kind": "delete", "schema": "public", "table": "seismic_events",
            "oldkeys": {"keynames": ["event_id"], "keytypes": ["text"], "keyvalues": [eid]},
        }
    ch = {
        "kind": kind, "schema": "public", "table": "seismic_events",
        "columnnames": ["event_id", "origin_time", "latitude", "longitude", "magnitude"],
        "columntypes": ["text", "timestamp with time zone", "numeric", "numeric", "numeric"],
        "columnvalues": [eid, when, lat, lon, mag],
    }
    if kind == "update":
        ch["oldkeys"] = {"keynames": ["event_id"], "keytypes": ["text"], "keyvalues": [eid]}
    return ch


def insert_txn(prefix, day, n=3):
    return {"change": [change("insert", f"{prefix}-{day}-{i}", ts(day)) for i in range(n)]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "mirror.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, payload, lsn="0/10", today=None):
        with self.store.write_txn() as cur:
            counts, days = apply_transaction(cur, payload)
            zeroreport.evaluate_affected_days(cur, days, today=today)
            Store.set_meta(cur, "last_applied_lsn", lsn)
        return counts, days

    def alerts(self):
        return self.store.read(
            "SELECT alert_date FROM zero_report_alerts ORDER BY alert_date"
        )

    def alert_dates(self):
        return [r[0] for r in self.alerts()]

    def seed_week(self, end_day):
        """end_day 前七日各 3 笔（end_day 本身无震例）。"""
        d = date.fromisoformat(end_day)
        for off in range(1, 8):
            self.apply(insert_txn("S", (d.fromordinal(d.toordinal() - off)).isoformat()))

    def alert_row(self, day):
        rows = self.store.read(
            "SELECT alert_date, day_count, prior_counts, first_seen FROM "
            "zero_report_alerts WHERE alert_date=?",
            (day,),
        )
        return rows[0] if rows else None


class AlertRuleTest(Base):
    def test_alert_when_ended_day_empty_and_prior_seven_dense(self):
        self.seed_week("2026-11-08")
        with self.store.write_txn() as cur:
            n = zeroreport.full_rebuild(cur, today=date(2026, 11, 9))
        self.assertEqual(n, 1)
        row = self.alert_row("2026-11-08")
        self.assertIsNotNone(row)
        prior = json.loads(row[2])
        self.assertEqual([x["count"] for x in prior], [3] * 7)
        self.assertEqual([x["date"] for x in prior], [
            "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
            "2026-11-05", "2026-11-06", "2026-11-07",
        ])
        self.assertEqual(row[1], 0)

    def test_today_not_judged(self):
        self.seed_week("2026-11-08")
        with self.store.write_txn() as cur:
            n = zeroreport.full_rebuild(cur, today=date(2026, 11, 8))
        self.assertEqual(n, 0)                       # 11-08 当天未结束
        with self.store.write_txn() as cur:
            row = zeroreport.evaluate_day(cur, "2026-11-08", today=date(2026, 11, 8))
        self.assertIsNone(row)
        self.assertEqual(self.alert_dates(), [])

    def test_prior_day_below_three_no_alert(self):
        d = date(2026, 11, 8)
        for off in range(1, 8):
            n = 2 if off == 4 else 3
            self.apply(insert_txn("S", (d.fromordinal(d.toordinal() - off)).isoformat(), n))
        with self.store.write_txn() as cur:
            self.assertEqual(zeroreport.full_rebuild(cur, today=date(2026, 11, 9)), 0)

    def test_day_with_events_is_not_alert(self):
        self.seed_week("2026-11-08")
        self.apply(insert_txn("D", "2026-11-08", 1))
        with self.store.write_txn() as cur:
            self.assertEqual(zeroreport.full_rebuild(cur, today=date(2026, 11, 9)), 0)


class RevisionTest(Base):
    def setUp(self):
        super().setUp()
        self.today = date(2026, 11, 9)
        self.seed_week("2026-11-08")
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=self.today)
        self.assertEqual(self.alert_dates(), ["2026-11-08"])

    def test_late_report_clears_alert(self):
        counts, days = self.apply(
            insert_txn("L", "2026-11-08", 1), today=self.today
        )
        self.assertIn("2026-11-08", days)
        self.assertEqual(self.alert_dates(), [])

    def test_delete_restores_alert(self):
        self.apply(insert_txn("L", "2026-11-08", 1), today=self.today)
        self.assertEqual(self.alert_dates(), [])
        # 删除这笔迟报（旧日期从镜像查得）后，告警应恢复
        self.apply({"change": [change("delete", "L-2026-11-08-0", None)]}, today=self.today)
        self.assertEqual(self.alert_dates(), ["2026-11-08"])

    def test_cross_day_correction_moves_event_and_restores_alert(self):
        self.apply(insert_txn("L", "2026-11-08", 1), today=self.today)
        self.assertEqual(self.alert_dates(), [])
        # 一笔源事务把该震例改到别的日期：旧日期 11-08 应重新成为零报日
        self.apply(
            {"change": [change("update", "L-2026-11-08-0", ts("2026-11-09"))]},
            today=self.today,
        )
        self.assertEqual(self.alert_dates(), ["2026-11-08"])

    def test_deleting_prior_day_event_clears_alert_within_seven_days(self):
        # 11-01 从 3 笔降为 2 笔 -> 其后的 11-08 不再满足“前七日每天至少三笔”
        self.apply(
            {"change": [change("delete", "S-2026-11-01-0", None)]}, today=self.today
        )
        self.assertEqual(self.alert_dates(), [])

    def test_revision_outside_seven_day_window_keeps_alert(self):
        # 11-01 再有震例（迟报/新增），仍在前七日内 -> 只更新计数，不改判定
        self.apply(insert_txn("L", "2026-11-01", 1), today=self.today)
        row = self.alert_row("2026-11-08")
        self.assertIsNotNone(row)
        prior = json.loads(row[2])
        self.assertEqual(prior[0], {"date": "2026-11-01", "count": 4})

    def test_replay_does_not_regress_first_seen(self):
        first = self.alert_row("2026-11-08")[3]
        # 源库重送早先事务（触碰 11-01，会重判 11-01..11-08）-> 告警仍在
        self.apply(insert_txn("S", "2026-11-01", 3), lsn="0/10", today=self.today)
        row = self.alert_row("2026-11-08")
        self.assertIsNotNone(row)
        self.assertEqual(row[3], first)              # 首次告警时间不回退


class AtomicityTest(Base):
    def test_failure_rolls_back_alert_and_position(self):
        """告警重算失败 -> 目录、告警、续采位置整笔回滚，不确认源库。"""
        with self.assertRaises(RuntimeError):
            with self.store.write_txn() as cur:
                counts, days = apply_transaction(
                    cur, insert_txn("S", "2026-11-01", 3)
                )
                zeroreport.evaluate_affected_days(cur, days)
                Store.set_meta(cur, "last_applied_lsn", "0/AA")
                raise RuntimeError("模拟写入失败")
        self.assertEqual(self.store.read("SELECT count(*) FROM events")[0][0], 0)
        self.assertEqual(self.alert_dates(), [])
        self.assertIsNone(self.store.get_meta("last_applied_lsn"))

    def test_alerts_commit_together_with_position(self):
        self.apply(insert_txn("S", "2026-11-01", 3), lsn="0/AA")
        self.assertEqual(self.store.get_meta("last_applied_lsn"), "0/AA")


class MidnightTest(Base):
    def test_midnight_tick_updates_without_source_transactions(self):
        # 目录在 11-08 结束后无任何新事务；把“today”推进到 11-09 即应产生告警
        self.seed_week("2026-11-08")
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=date(2026, 11, 8))
            Store.set_meta(cur, "load_state", "done")
        self.assertEqual(self.alert_dates(), [])

        refresher = zeroreport.MidnightRefresher(self.store, interval=0.05)
        self.assertIsNone(refresher.tick(today=date(2026, 11, 8)))   # 当日未结束
        result = refresher.tick(today=date(2026, 11, 9))
        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(result["days"], 1)
        self.assertEqual(self.alert_dates(), ["2026-11-08"])
        self.assertEqual(self.store.get_meta("alerts_evaluated_through"), "2026-11-08")

    def test_tick_does_nothing_while_rebuilding(self):
        with self.store.write_txn() as cur:
            Store.set_meta(cur, "load_state", "loading")
            Store.set_meta(cur, "alerts_ready", "0")
        refresher = zeroreport.MidnightRefresher(self.store, interval=0.05)
        self.assertIsNone(refresher.tick(today=date(2026, 11, 9)))
        self.assertEqual(self.alert_dates(), [])

    def test_rebuild_reset_hides_old_results(self):
        self.seed_week("2026-11-08")
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=date(2026, 11, 9))
            Store.set_meta(cur, "load_state", "done")
        self.assertEqual(self.alert_dates(), ["2026-11-08"])
        with self.store.write_txn() as cur:
            zeroreport.reset_alerts(cur)             # 开始重建：不供旧结果
        self.assertEqual(self.alert_dates(), [])
        self.assertEqual(self.store.get_meta("alerts_ready"), "0")

    def test_full_rebuild_preserves_first_seen_and_drops_stale(self):
        self.seed_week("2026-11-08")
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=date(2026, 11, 9))
        first = self.alert_row("2026-11-08")[3]
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=date(2026, 11, 9))   # 内容未变
        self.assertEqual(self.alert_row("2026-11-08")[3], first)
        # 给零报日补一笔迟报后全量重建：该告警应消失
        self.apply(insert_txn("L", "2026-11-08", 1), today=date(2026, 11, 9))
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=date(2026, 11, 9))
        self.assertEqual(self.alert_dates(), [])

    def test_large_gap_falls_back_to_full_rebuild(self):
        with self.store.write_txn() as cur:
            Store.set_meta(cur, "load_state", "done")
            Store.set_meta(cur, "alerts_ready", "1")
            Store.set_meta(cur, "alerts_evaluated_through", "2026-01-01")
        refresher = zeroreport.MidnightRefresher(self.store, interval=0.05)
        result = refresher.tick(today=date(2026, 11, 9))
        self.assertEqual(result["mode"], "full")


if __name__ == "__main__":
    unittest.main(verbosity=2)
