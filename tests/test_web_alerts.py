#!/usr/bin/env python3
"""网页层自测（需 fastapi/httpx，无需源库）：
   python3 -I tests/test_web_alerts.py
用临时 SQLite 镜像构造告警，验证页面渲染、筛选、CSV、就绪前 503。
"""
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from fastapi.testclient import TestClient

TMP = tempfile.TemporaryDirectory()
os.environ["MIRROR_DB"] = os.path.join(TMP.name, "mirror.db")

import main                      # noqa: E402
import zeroreport                # noqa: E402
from changes import apply_transaction  # noqa: E402
from store import Store          # noqa: E402

TODAY = date(2026, 11, 9)


def row(day, i):
    return {
        "kind": "insert", "schema": "public", "table": "seismic_events",
        "columnnames": ["event_id", "origin_time", "latitude", "longitude", "magnitude"],
        "columntypes": ["text", "timestamp with time zone", "numeric", "numeric", "numeric"],
        "columnvalues": [f"S-{day}-{i}", f"{day} 12:00:00+00", 34.0, 100.0, 4.0],
    }


def seed(store):
    for off in range(1, 8):
        d = date.fromordinal(TODAY.toordinal() - off - 1).isoformat()
        with store.write_txn() as cur:
            apply_transaction(cur, {"change": [row(d, i) for i in range(3)]})
    with store.write_txn() as cur:
        zeroreport.reset_alerts(cur)
        zeroreport.full_rebuild(cur, today=TODAY)
        Store.set_meta(cur, "load_state", "done")


def main_():
    seed(main.store)
    with TestClient(main.app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200, r.status_code

        r = client.get("/alerts")
        assert r.status_code == 200, r.text
        assert "2026-11-08" in r.text, r.text[:2000]
        assert "零报日质检" in r.text
        # 页面展示前七日计数（7 个 3）
        assert r.text.count(">3</td>") >= 7, r.text

        r = client.get("/alerts?day=2026-11-08")
        assert r.status_code == 200 and "2026-11-08" in r.text
        r = client.get("/alerts?day=2026-11-07")
        assert r.status_code == 200 and "没有零报日告警" in r.text
        r = client.get("/alerts?day=bad-date")
        assert r.status_code == 400, r.status_code

        r = client.get("/alerts.csv")
        assert r.status_code == 200
        lines = r.text.strip().splitlines()
        assert lines[0].startswith("alert_date_utc,day_count,day_7_count")
        assert len(lines) == 2 and lines[1].startswith("2026-11-08,0,3,3,3,3,3,3,3,"), lines[1]

        r = client.get("/events")
        assert r.status_code == 200 and "查询震例" in r.text
        r = client.get("/events.csv")
        assert r.status_code == 200 and "event_id,origin_time_utc" in r.text

        r = client.get("/api/status")
        st = r.json()
        assert st["serving"] is True and st["alerts_ready"] is True, st
        assert st["alerts_count"] == 1, st

        # 重建期间：不供旧结果
        with main.store.write_txn() as cur:
            Store.set_meta(cur, "load_state", "loading")
            zeroreport.reset_alerts(cur)
        r = client.get("/alerts")
        assert r.status_code == 503, r.status_code
        r = client.get("/alerts.csv")
        assert r.status_code == 503, r.status_code
        r = client.get("/events")
        assert r.status_code == 503, r.status_code

    print("WEB_OK")


if __name__ == "__main__":
    main_()
