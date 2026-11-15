#!/usr/bin/env python3
"""复制应用事务与告警/确认顺序的单元测试（无需源库）：

  python3 -I tests/test_flow.py

验证：
  - 数据、告警、续采位置在同一个 SQLite 事务里提交；
  - 写入/重算失败时不 send_feedback（源库重发），且三者一起回滚；
  - 成功时先提交再确认；
  - 源库重送同一事务：目录不变、告警仍在、first_seen 不回退。
"""
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

# 本测试不连接源库：仅导入 replicator 的消息处理路径，
# 没有 psycopg2 时用最小桩模块占位，保持“标准库即可运行”。
try:
    import psycopg2  # noqa: F401
except ImportError:
    import types
    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.LogicalReplicationConnection = object
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

from replicator import Replicator             # noqa: E402
from store import Store                       # noqa: E402
import zeroreport                             # noqa: E402


def row(day, eid):
    return {
        "kind": "insert", "schema": "public", "table": "seismic_events",
        "columnnames": ["event_id", "origin_time", "latitude", "longitude", "magnitude"],
        "columntypes": ["text", "timestamp with time zone", "numeric", "numeric", "numeric"],
        "columnvalues": [eid, f"{day} 12:00:00+00", 34.0, 100.0, 4.0],
    }


class FakeMsg:
    def __init__(self, payload, lsn):
        self.payload = payload
        self.data_start = lsn
        self.reply_requested = False


class FakeRepCursor:
    def __init__(self):
        self.feedback = []

    def send_feedback(self, flush_lsn=None, reply=False):
        self.feedback.append(flush_lsn)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "mirror.db"))
        self.rep = Replicator({}, "slot", self.store)
        self.cur = FakeRepCursor()

    def tearDown(self):
        self.tmp.cleanup()

    def seed_week(self, today_text, end_day):
        from changes import apply_transaction
        base = date.fromisoformat(end_day)
        for off in range(1, 8):
            d = date.fromordinal(base.toordinal() - off).isoformat()
            with self.store.write_txn() as cur:
                payload = {"change": [row(d, f"S-{d}-{i}") for i in range(3)]}
                _counts, days = apply_transaction(cur, payload)
                zeroreport.evaluate_affected_days(
                    cur, days, today=date.fromisoformat(today_text)
                )
        with self.store.write_txn() as cur:
            zeroreport.full_rebuild(cur, today=date.fromisoformat(today_text))
            Store.set_meta(cur, "load_state", "loading")
            Store.set_meta(cur, "last_applied_lsn", "0/0")

    def test_success_commits_before_ack(self):
        self.seed_week("2026-11-09", "2026-11-08")
        msg = FakeMsg(
            '{"change": [' + ",".join(
                f'{{"kind":"insert","schema":"public","table":"seismic_events",'
                f'"columnnames":["event_id","origin_time","latitude","longitude","magnitude"],'
                f'"columntypes":["text","timestamp with time zone","numeric","numeric","numeric"],'
                f'"columnvalues":["N-{i}","2026-11-09 01:00:00+00",30,100,4]}}'
                for i in range(3)
            ) + "]}",
            100,
        )
        self.rep._on_message(self.cur, msg, None)
        self.assertEqual(self.cur.feedback, [100])       # 提交后才确认
        self.assertEqual(self.store.get_meta("last_applied_lsn"), "0/64")

    def test_failure_rolls_back_and_does_not_ack(self):
        self.seed_week("2026-11-09", "2026-11-08")
        before_rows = self.store.read("SELECT count(*) FROM events")[0][0]
        before_alerts = self.store.read("SELECT count(*) FROM zero_report_alerts")[0][0]
        before_lsn = self.store.get_meta("last_applied_lsn")

        original = zeroreport.evaluate_affected_days
        zeroreport.evaluate_affected_days = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("模拟告警写入失败")
        )
        try:
            msg = FakeMsg(
                '{"change": [{"kind":"insert","schema":"public","table":"seismic_events",'
                '"columnnames":["event_id","origin_time","latitude","longitude","magnitude"],'
                '"columntypes":["text","timestamp with time zone","numeric","numeric","numeric"],'
                '"columnvalues":["FAIL-1","2026-11-09 01:00:00+00",30,100,4]}]}',
                200,
            )
            with self.assertRaises(RuntimeError):
                self.rep._on_message(self.cur, msg, None)
        finally:
            zeroreport.evaluate_affected_days = original

        self.assertEqual(self.cur.feedback, [])          # 失败绝不确认
        self.assertEqual(self.store.read("SELECT count(*) FROM events")[0][0], before_rows)
        self.assertEqual(
            self.store.read("SELECT count(*) FROM zero_report_alerts")[0][0], before_alerts
        )
        self.assertEqual(self.store.get_meta("last_applied_lsn"), before_lsn)

    def test_replay_keeps_alert_and_first_seen(self):
        self.seed_week("2026-11-09", "2026-11-08")
        alerts_before = self.store.read(
            "SELECT alert_date, prior_counts, first_seen FROM zero_report_alerts ORDER BY alert_date"
        )
        # 重送种子事务（同一批变更按原 LSN 再来一遍）
        for off in range(1, 8):
            d = date.fromordinal(
                date(2026, 11, 8).toordinal() - off
            ).isoformat()
            msg = FakeMsg(
                '{"change": [' + ",".join(
                    f'{{"kind":"insert","schema":"public","table":"seismic_events",'
                    f'"columnnames":["event_id","origin_time","latitude","longitude","magnitude"],'
                    f'"columntypes":["text","timestamp with time zone","numeric","numeric","numeric"],'
                    f'"columnvalues":["S-{d}-{i}","{d} 12:00:00+00",34,100,4]}}'
                    for i in range(3)
                ) + "]}",
                50 + off,
            )
            self.rep._on_message(self.cur, msg, None)
        alerts_after = self.store.read(
            "SELECT alert_date, prior_counts, first_seen FROM zero_report_alerts ORDER BY alert_date"
        )
        self.assertEqual(alerts_after, alerts_before)
        self.assertEqual(len(self.cur.feedback), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
