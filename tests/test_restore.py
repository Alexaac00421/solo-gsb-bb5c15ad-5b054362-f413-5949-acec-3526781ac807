"""端到端测试：周期按版本恢复（restore，v1.9）。

覆盖：
- 成功恢复：误改（锚点更正/声段增删）后凭已发布版本恢复目标周期的锚点与
  声段本地起止、备注，返回恢复前后指纹、锚点与声段数量及恢复后定位结果；
  当前时间轴按现有映射实时重算；采集编号与创建时间随冻结记录一并恢复；
- 只改目标周期：其他周期与其他设备内容指纹不变；已发布版本保持原快照；
  恢复后再发布的版本与所凭版本内容一致（无新增/移除/位移）；
- 明确拒绝且不留部分写入：指纹过期、周期已封存、版本/设备/周期不存在、
  版本中目标周期既无锚点也无声段（历史版本未记录空周期，绝不清除当前
  数据）、版本声段采集编号现被其他设备或周期占用；
- 版本只有锚点（无声段）或只有声段（无锚点）时可恢复：声段清空或保持
  不可定位；
- 恢复后同编号同内容重传幂等、异内容冲突；既有采集包重传仍返回首次结果；
- 旧格式（format_rev=1）版本未标周期的记录按 default 解释：可恢复
  default 周期，非 default 周期恢复被拒绝；
- 恢复 + 锚点更正 + 发布并发下每个版本仍是完整自洽的快照。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402

# v1（无周期、无封存列）建表脚本，仅用于构造待迁移的旧库
LEGACY_SCHEMA = """
CREATE TABLE devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE anchors (
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, local_ms)
);
CREATE TABLE segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE version_anchors (
    version_no  INTEGER NOT NULL,
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, local_ms)
);
CREATE TABLE version_segments (
    version_no      INTEGER NOT NULL,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL,
    utc_start_ms    INTEGER,
    utc_end_ms      INTEGER,
    created_at      TEXT,
    PRIMARY KEY (version_no, capture_id)
);
"""


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class RestoreApiTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 工具

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def reg_cycle(self, device_id, cycle_id, note=None):
        payload = {"cycle_id": cycle_id}
        if note is not None:
            payload["note"] = note
        return self.req("POST", f"/devices/{device_id}/cycles", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id=None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self.req("POST", f"/devices/{device_id}/anchors", payload)

    def submit(self, capture_id, device_id, start, end, cycle_id=None, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def fingerprint(self, device_id, cycle_id):
        status, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def restore(self, device_id, cycle_id, version_no, fingerprint):
        return self.req("POST", f"/devices/{device_id}/cycles/{cycle_id}/restore",
                        {"version_no": version_no, "fingerprint": fingerprint})

    def publish(self):
        status, body = self.req("POST", "/versions")
        self.assertEqual(status, 201)
        return body

    def make_published_cycle(self):
        """建 D1/default：两只锚点 + 一段可定位声段，发布为 v1，返回指纹。"""
        self.make_device("D1", "录音笔-甲")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        status, body = self.submit("CAP-1", "D1", 1100, 1200, note="鸟鸣")
        self.assertEqual(status, 201)
        created_at = body["segment"]["created_at"]
        fp = self.fingerprint("D1", "default")
        v1 = self.publish()
        self.assertEqual(v1["version_no"], 1)
        return fp, created_at

    def corrupt_default_cycle(self):
        """模拟现场误改：整体替换锚点（+50ms）、删掉 CAP-1、录入 CAP-2。"""
        status, _ = self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100050},
            {"local_ms": 2000, "utc_ms": 101050}]})
        self.assertEqual(status, 200)
        status, _ = self.req("DELETE", "/segments/CAP-1")
        self.assertEqual(status, 200)
        status, _ = self.submit("CAP-2", "D1", 1300, 1400, note="误录")
        self.assertEqual(status, 201)

    # ------------------------------------------------------------ 成功恢复

    def test_restore_success_returns_fingerprints_counts_and_locations(self):
        fp_v1, created_at = self.make_published_cycle()
        self.corrupt_default_cycle()
        fp_stale = self.fingerprint("D1", "default")
        self.assertNotEqual(fp_stale, fp_v1)

        status, body = self.restore("D1", "default", 1, fp_stale)
        self.assertEqual(status, 200)
        self.assertEqual(body["device_id"], "D1")
        self.assertEqual(body["cycle_id"], "default")
        self.assertEqual(body["version_no"], 1)
        # 恢复前后指纹：前为误改后内容，后回到发布时内容
        self.assertEqual(body["fingerprint_before"], fp_stale)
        self.assertEqual(body["fingerprint_after"], fp_v1)
        # 锚点与声段数量（误改后 2 锚点 1 声段 -> 恢复为 2 锚点 1 声段）
        self.assertEqual(body["anchor_count_before"], 2)
        self.assertEqual(body["anchor_count_after"], 2)
        self.assertEqual(body["segment_count_before"], 1)   # CAP-2
        self.assertEqual(body["segment_count_after"], 1)    # CAP-1
        # 恢复后定位结果：CAP-1 按恢复后的锚点重新定位
        (seg,) = body["segments"]
        self.assertEqual(seg["capture_id"], "CAP-1")
        self.assertEqual(seg["cycle_id"], "default")
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["local_start_ms"], 1100)
        self.assertEqual(seg["local_end_ms"], 1200)
        self.assertEqual(seg["utc_start_ms"], 100100)
        self.assertEqual(seg["utc_end_ms"], 100200)
        self.assertEqual(seg["note"], "鸟鸣")
        self.assertEqual(seg["created_at"], created_at)  # 创建时间随冻结记录恢复

        # 周期详情指纹回到发布时内容；当前时间轴实时反映恢复结果
        self.assertEqual(self.fingerprint("D1", "default"), fp_v1)
        status, body = self.req("GET", "/segments/CAP-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["segment"]["utc_start_ms"], 100100)
        self.assertEqual(body["segment"]["note"], "鸟鸣")
        status, _ = self.req("GET", "/segments/CAP-2")
        self.assertEqual(status, 404)  # 误录声段随恢复移除
        _, anchors = self.req("GET", "/devices/D1/anchors")
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors["anchors"]],
                         [(1000, 100000), (2000, 101000)])
        _, tl = self.req("GET", "/timeline/current")
        (tseg,) = tl["segments"]
        self.assertEqual(tseg["capture_id"], "CAP-1")
        self.assertEqual(tseg["utc_start_ms"], 100100)

    def test_restore_only_touches_target_cycle(self):
        fp_v1, _ = self.make_published_cycle()
        # 另一周期与另一设备各有内容（不在 v1 目标范围内）
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 500000, "boot-2")
        self.add_anchor("D1", 2000, 501000, "boot-2")
        self.submit("CAP-B2", "D1", 1100, 1200, "boot-2")
        self.make_device("D2")
        self.add_anchor("D2", 1000, 900000)
        self.add_anchor("D2", 2000, 901000)
        self.submit("CAP-D2", "D2", 1100, 1200)
        fp_b2 = self.fingerprint("D1", "boot-2")
        fp_d2 = self.fingerprint("D2", "default")

        self.corrupt_default_cycle()
        status, body = self.restore("D1", "default", 1,
                                    self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        self.assertEqual(body["fingerprint_after"], fp_v1)
        # 其他周期与其他设备内容完全不变
        self.assertEqual(self.fingerprint("D1", "boot-2"), fp_b2)
        self.assertEqual(self.fingerprint("D2", "default"), fp_d2)
        status, seg = self.req("GET", "/segments/CAP-B2")
        self.assertEqual(status, 200)
        self.assertEqual(seg["segment"]["cycle_id"], "boot-2")
        self.assertEqual(seg["segment"]["utc_start_ms"], 500100)

    def test_restore_published_versions_immutable_and_next_publish_clean(self):
        self.make_published_cycle()
        _, v1_before = self.req("GET", "/versions/1")
        self.corrupt_default_cycle()
        status, _ = self.restore("D1", "default", 1,
                                 self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        # 已发布版本保持原快照，逐字节一致
        _, v1_after = self.req("GET", "/versions/1")
        self.assertEqual(v1_after, v1_before)
        # 恢复后再发布：内容与 v1 完全一致 -> 无新增/移除/位移
        v2 = self.publish()
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["changes"]["added"], [])
        self.assertEqual(v2["changes"]["removed"], [])
        self.assertEqual(v2["changes"]["displaced"], [])
        (seg,) = v2["segments"]
        self.assertEqual(seg["capture_id"], "CAP-1")
        self.assertEqual(seg["utc_start_ms"], 100100)

    def test_restore_same_version_again_is_content_noop(self):
        fp_v1, _ = self.make_published_cycle()
        self.corrupt_default_cycle()
        status, first = self.restore("D1", "default", 1,
                                     self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        # 凭恢复后的新指纹再次恢复同一版本：内容不变，前后指纹相同
        status, second = self.restore("D1", "default", 1,
                                      self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        self.assertEqual(second["fingerprint_before"], fp_v1)
        self.assertEqual(second["fingerprint_after"], fp_v1)
        self.assertEqual(second["segments"], first["segments"])

    def test_restore_version_with_anchors_only_clears_segments(self):
        # v1 只有锚点、没有声段（周期已被记录，可恢复）
        self.make_device("D1")
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 2000, 101000)
        fp_v1 = self.fingerprint("D1", "default")
        self.publish()
        # 误改：补录声段并整体替换锚点
        self.submit("CAP-9", "D1", 1100, 1200)
        self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100070},
            {"local_ms": 2000, "utc_ms": 101070}]})
        status, body = self.restore("D1", "default", 1,
                                    self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        self.assertEqual(body["anchor_count_after"], 2)
        self.assertEqual(body["segment_count_before"], 1)
        self.assertEqual(body["segment_count_after"], 0)
        self.assertEqual(body["segments"], [])
        self.assertEqual(body["fingerprint_after"], fp_v1)
        status, _ = self.req("GET", "/segments/CAP-9")
        self.assertEqual(status, 404)  # 误录声段被清除

    def test_restore_version_with_segments_only_stays_unlocatable(self):
        # v1 只有声段、没有锚点（周期已被记录，声段不可定位）
        self.make_device("D1")
        self.submit("CAP-1", "D1", 5000, 6000, note="无锚点时期")
        fp_v1 = self.fingerprint("D1", "default")
        v1 = self.publish()
        self.assertEqual(v1["segments"][0]["status"], "unlocatable")
        # 误改：补录锚点（声段变为可定位）并删除声段
        self.add_anchor("D1", 1000, 100000)
        self.add_anchor("D1", 9000, 108000)
        self.req("DELETE", "/segments/CAP-1")
        status, body = self.restore("D1", "default", 1,
                                    self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        self.assertEqual(body["anchor_count_after"], 0)
        self.assertEqual(body["segment_count_after"], 1)
        (seg,) = body["segments"]
        self.assertEqual(seg["capture_id"], "CAP-1")
        self.assertEqual(seg["status"], "unlocatable")
        self.assertIsNone(seg["utc_start_ms"])
        self.assertEqual(self.fingerprint("D1", "default"), fp_v1)

    def test_restore_capture_still_in_target_cycle_is_overwritten(self):
        # 采集编号仍在目标周期（内容被改）-> 不构成占用冲突，直接按版本恢复
        fp_v1, _ = self.make_published_cycle()
        # 误改：删除 CAP-1 后以同编号不同内容重录到目标周期
        self.req("DELETE", "/segments/CAP-1")
        self.submit("CAP-1", "D1", 1500, 1600, note="改错")
        status, body = self.restore("D1", "default", 1,
                                    self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        (seg,) = body["segments"]
        self.assertEqual(seg["local_start_ms"], 1100)
        self.assertEqual(seg["note"], "鸟鸣")
        self.assertEqual(body["fingerprint_after"], fp_v1)

    def test_restore_segment_retransmit_rules_after_restore(self):
        self.make_published_cycle()
        self.corrupt_default_cycle()
        status, _ = self.restore("D1", "default", 1,
                                 self.fingerprint("D1", "default"))
        self.assertEqual(status, 200)
        # 恢复后同编号同内容重传 -> 幂等；异内容 -> 409 capture_conflict
        status, body = self.submit("CAP-1", "D1", 1100, 1200, note="鸟鸣")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])
        status, body = self.submit("CAP-1", "D1", 1100, 1200, note="别的")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "capture_conflict")

    def test_restore_keeps_bundle_replay_receipt(self):
        # 采集包导入 -> 发布 -> 误改 -> 恢复 -> 相同包重传仍返回首次结果
        bundle = {"bundle_id": "FIELD-REST-001",
                  "devices": [{"device_id": "BD1", "name": "离线机"}],
                  "anchors": [{"device_id": "BD1", "local_ms": 1000,
                               "utc_ms": 100000},
                              {"device_id": "BD1", "local_ms": 2000,
                               "utc_ms": 101000}],
                  "segments": [{"capture_id": "BS-1", "device_id": "BD1",
                                "local_start_ms": 1100, "local_end_ms": 1200}]}
        status, first = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 201)
        self.publish()
        self.req("PUT", "/devices/BD1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100090},
            {"local_ms": 2000, "utc_ms": 101090}]})
        status, body = self.restore("BD1", "default", 1,
                                    self.fingerprint("BD1", "default"))
        self.assertEqual(status, 200)
        status, replay = self.req("POST", "/bundles", bundle)
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["anchors"], first["anchors"])
        self.assertEqual(replay["segments"], first["segments"])

    # ------------------------------------------------------------ 明确拒绝

    def test_restore_stale_fingerprint_rejected_state_unchanged(self):
        self.make_published_cycle()
        fp_old = self.fingerprint("D1", "default")
        self.corrupt_default_cycle()  # 取到指纹后内容又变
        fp_now = self.fingerprint("D1", "default")
        status, body = self.restore("D1", "default", 1, fp_old)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_fingerprint_stale")
        self.assertEqual(body["error"]["details"]["current_fingerprint"], fp_now)
        # 状态不变：仍是误改后的内容
        self.assertEqual(self.fingerprint("D1", "default"), fp_now)
        status, seg = self.req("GET", "/segments/CAP-2")
        self.assertEqual(status, 200)

    def test_restore_sealed_cycle_rejected(self):
        self.make_published_cycle()
        self.corrupt_default_cycle()
        # 封存误改后的周期
        fp = self.fingerprint("D1", "default")
        status, _ = self.req("POST", "/devices/D1/cycles/default/seal",
                             {"fingerprint": fp})
        self.assertEqual(status, 201)
        status, body = self.restore("D1", "default", 1, fp)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "cycle_sealed")
        # 封存内容不变
        self.assertEqual(self.fingerprint("D1", "default"), fp)

    def test_restore_version_not_found(self):
        self.make_published_cycle()
        status, body = self.restore("D1", "default", 999,
                                    self.fingerprint("D1", "default"))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "version_not_found")

    def test_restore_device_and_cycle_not_found(self):
        self.make_published_cycle()
        status, body = self.restore("NOPE", "default", 1, "0" * 64)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        status, body = self.restore("D1", "ghost", 1, "0" * 64)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "cycle_not_found")

    def test_restore_cycle_absent_in_version_rejected_and_not_cleared(self):
        # v1 发布时 boot-2 尚未登记（版本中既无其锚点也无声段）：
        # 历史版本未记录空周期 -> 明确拒绝，绝不能按空周期清除当前数据
        self.make_published_cycle()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 500000, "boot-2")
        self.add_anchor("D1", 2000, 501000, "boot-2")
        self.submit("CAP-B2", "D1", 1100, 1200, "boot-2")
        fp_b2 = self.fingerprint("D1", "boot-2")
        status, body = self.restore("D1", "boot-2", 1, fp_b2)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_cycle_absent")
        self.assertEqual(body["error"]["details"]["version_no"], 1)
        # 当前数据原样保留（未被按空周期清除）
        self.assertEqual(self.fingerprint("D1", "boot-2"), fp_b2)
        _, anchors = self.req("GET", "/devices/D1/anchors?cycle_id=boot-2")
        self.assertEqual(len(anchors["anchors"]), 2)
        status, _ = self.req("GET", "/segments/CAP-B2")
        self.assertEqual(status, 200)

    def test_restore_capture_occupied_by_other_device_rejected(self):
        self.make_published_cycle()
        # CAP-1 从 D1 删除后被 D2 占用
        self.req("DELETE", "/segments/CAP-1")
        self.make_device("D2")
        self.add_anchor("D2", 1000, 900000)
        self.add_anchor("D2", 2000, 901000)
        self.submit("CAP-1", "D2", 1100, 1200)
        fp_now = self.fingerprint("D1", "default")
        status, body = self.restore("D1", "default", 1, fp_now)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_capture_conflict")
        (conflict,) = body["error"]["details"]["conflicts"]
        self.assertEqual(conflict, {"capture_id": "CAP-1", "device_id": "D2",
                                    "cycle_id": "default"})
        # 整次拒绝：双方数据都不变
        self.assertEqual(self.fingerprint("D1", "default"), fp_now)
        status, seg = self.req("GET", "/segments/CAP-1")
        self.assertEqual(status, 200)
        self.assertEqual(seg["segment"]["device_id"], "D2")

    def test_restore_capture_occupied_by_other_cycle_rejected(self):
        self.make_published_cycle()
        self.reg_cycle("D1", "boot-2")
        self.add_anchor("D1", 1000, 500000, "boot-2")
        self.add_anchor("D1", 2000, 501000, "boot-2")
        # CAP-1 从 default 删除后被同设备的 boot-2 周期占用
        self.req("DELETE", "/segments/CAP-1")
        self.submit("CAP-1", "D1", 1100, 1200, "boot-2")
        fp_now = self.fingerprint("D1", "default")
        status, body = self.restore("D1", "default", 1, fp_now)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_capture_conflict")
        (conflict,) = body["error"]["details"]["conflicts"]
        self.assertEqual(conflict["cycle_id"], "boot-2")
        self.assertEqual(self.fingerprint("D1", "default"), fp_now)
        status, seg = self.req("GET", "/segments/CAP-1")
        self.assertEqual(seg["segment"]["cycle_id"], "boot-2")

    def test_restore_validation_errors(self):
        self.make_published_cycle()
        # 缺字段 / 非法版本号 / 非法指纹 -> 400，且都不触碰数据
        fp = self.fingerprint("D1", "default")
        bad_bodies = [
            {},
            {"version_no": 1},
            {"fingerprint": fp},
            {"version_no": 0, "fingerprint": fp},
            {"version_no": -2, "fingerprint": fp},
            {"version_no": "1", "fingerprint": fp},
            {"version_no": 1.5, "fingerprint": fp},
            {"version_no": True, "fingerprint": fp},
            {"version_no": 1, "fingerprint": "xyz"},
            {"version_no": 1, "fingerprint": "A" * 64},
            {"version_no": 1, "fingerprint": "0" * 63},
        ]
        for bad in bad_bodies:
            status, body = self.req("POST", "/devices/D1/cycles/default/restore", bad)
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["error"]["code"], "validation_error", bad)
        self.assertEqual(self.fingerprint("D1", "default"), fp)

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_restore_correct_and_publish(self):
        """恢复、锚点更正与发布并发：每个版本都是完整自洽的快照。"""
        self.make_published_cycle()
        for i in range(4):
            self.submit(f"CAP-C{i}", "D1", 1100 + i * 10, 1150 + i * 10)
        self.publish()  # v2：含 CAP-1 + CAP-C0..3

        results, errors = [], []

        def restore(_i):
            try:
                fp = self.fingerprint("D1", "default")
                results.append(self.restore("D1", "default", 2, fp)[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def correct(k):
            try:
                results.append(self.req("PUT", "/devices/D1/anchors", {
                    "anchors": [{"local_ms": 1000, "utc_ms": 100000 + k * 100},
                                {"local_ms": 2000, "utc_ms": 101000 + k * 100}]})[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                results.append(self.req("POST", "/versions")[0])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=restore, args=(i,)))
            threads.append(threading.Thread(target=correct, args=(i,)))
            threads.append(threading.Thread(target=publish))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 恢复可能因并发更正而指纹过期（409），也可能成功（200）；发布均成功
        self.assertTrue(all(s in (200, 201, 409) for s in results))

        _, body = self.req("GET", "/versions")
        versions = body["versions"]
        self.assertEqual([v["version_no"] for v in versions],
                         list(range(1, len(versions) + 1)))
        for v in versions:
            _, doc = self.req("GET", f"/versions/{v['version_no']}")
            # v1 只含 CAP-1；v2 及之后含 CAP-1 + CAP-C0..3
            self.assertEqual(doc["segment_count"],
                             1 if doc["version_no"] == 1 else 5)
            anchors = {a["local_ms"]: a["utc_ms"] for a in doc["anchors"]
                       if a["cycle_id"] == "default"}
            l0, l1 = sorted(anchors)
            for seg in doc["segments"]:
                # 快照内声段 UTC 必须与同快照锚点自洽（无撕裂读）
                expected = anchors[l0] + (seg["local_start_ms"] - l0) * \
                    (anchors[l1] - anchors[l0]) // (l1 - l0)
                self.assertEqual(seg["status"], "located")
                self.assertEqual(seg["utc_start_ms"], expected)


class RestoreLegacyVersionTestCase(unittest.TestCase):
    """旧格式（format_rev=1）版本：未标周期的记录按 default 解释。"""

    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        self._seed_legacy()
        db.init_db(self.db_path)  # 在线迁移
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    def _seed_legacy(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO devices VALUES ('D1','录音笔','2026-09-01T00:00:00.000Z')")
            conn.execute("INSERT INTO anchors VALUES ('D1',1000,100000,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1',2000,101000,'t')")
            import hashlib
            import json as _json
            canon = _json.dumps(
                {"capture_id": "OLD1", "device_id": "D1",
                 "local_start_ms": 1100, "local_end_ms": 1200, "note": None},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO segments VALUES ('OLD1','D1',1100,1200,NULL,?,'t')",
                (digest,))
            conn.execute(
                "INSERT INTO versions VALUES (1,'旧版','2026-09-02T00:00:00.000Z')")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',1000,100000)")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',2000,101000)")
            conn.execute(
                "INSERT INTO version_segments VALUES (1,'OLD1','D1',1100,1200,NULL,"
                "'located',100100,100200,'t')")
            conn.commit()
        finally:
            conn.close()

    def fingerprint(self, device_id, cycle_id):
        status, body = self.req("GET", f"/devices/{device_id}/cycles/{cycle_id}")
        self.assertEqual(status, 200)
        return body["cycle"]["fingerprint"]

    def test_restore_default_cycle_from_legacy_version(self):
        # 误改迁移后的 default 周期
        self.req("PUT", "/devices/D1/anchors", {"anchors": [
            {"local_ms": 1000, "utc_ms": 100033},
            {"local_ms": 2000, "utc_ms": 101033}]})
        self.req("DELETE", "/segments/OLD1")
        fp_now = self.fingerprint("D1", "default")
        # 旧版快照未标周期 -> 按 default 解释，可恢复 default 周期
        status, body = self.req("POST", "/devices/D1/cycles/default/restore",
                                {"version_no": 1, "fingerprint": fp_now})
        self.assertEqual(status, 200)
        self.assertEqual(body["anchor_count_after"], 2)
        self.assertEqual(body["segment_count_after"], 1)
        (seg,) = body["segments"]
        self.assertEqual(seg["capture_id"], "OLD1")
        self.assertEqual(seg["cycle_id"], "default")
        self.assertEqual(seg["status"], "located")
        self.assertEqual(seg["utc_start_ms"], 100100)
        _, anchors = self.req("GET", "/devices/D1/anchors")
        self.assertEqual([(a["local_ms"], a["utc_ms"]) for a in anchors["anchors"]],
                         [(1000, 100000), (2000, 101000)])
        # 已发布旧版本查询结果仍与升级前一致（无周期字段）
        _, v1 = self.req("GET", "/versions/1")
        self.assertNotIn("format_rev", v1)
        self.assertNotIn("cycle_id", v1["segments"][0])

    def test_restore_nondefault_cycle_from_legacy_version_absent(self):
        # 旧版快照没有非 default 周期的任何记录 -> 既无锚点也无声段，拒绝
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 500000})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 2000, "utc_ms": 501000})
        fp_b2 = self.fingerprint("D1", "boot-2")
        status, body = self.req("POST", "/devices/D1/cycles/boot-2/restore",
                                {"version_no": 1, "fingerprint": fp_b2})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "restore_cycle_absent")
        self.assertEqual(self.fingerprint("D1", "boot-2"), fp_b2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
