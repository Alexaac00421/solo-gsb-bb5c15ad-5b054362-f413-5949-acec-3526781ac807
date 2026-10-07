#!/usr/bin/env python3
"""周期按版本恢复（POST /devices/{id}/cycles/{cycle}/restore）最小示例。

现场误改某个未封存周期（锚点被整体替换、声段被误删/误录）后，凭**已发布
版本**把该周期的锚点与声段本地起止、备注恢复为冻结内容：提交设备编号、
周期编号、版本号与**当前周期详情中的内容指纹**；服务在同一写事务内核对
当前指纹与封存状态后整体恢复，并按现有映射重算当前时间轴。历史版本未
记录空周期（既无锚点也无声段）或版本声段采集编号现被其他设备/周期占用
时，整次拒绝、不留部分写入；只改目标周期，其他周期、采集包重传回执与
已发布版本保持原样。

本脚本自动建设备/锚点/声段并演示：

  1) 发布 v1 后误改 default 周期，再凭 v1 恢复（前后指纹、数量、定位结果）；
  2) 恢复后当前时间轴回到发布时状态，已发布版本保持原快照；
  3) 明确拒绝：指纹过期、周期已封存、版本不存在、版本中无该周期记录
     （既无锚点也无声段，绝不清除当前数据）、采集编号被其他周期占用。

用法：
    1) 先启动服务：  python3 -m app
    2) 再运行本例：  python3 examples/restore_example.py [base_url]
默认连接 http://127.0.0.1:8000，仅依赖标准库。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def fingerprint(base: str, device_id: str, cycle_id: str = "default") -> str:
    _, body = call(base, "GET", f"/devices/{device_id}/cycles/{cycle_id}")
    return body["cycle"]["fingerprint"]


def restore(base, device_id, cycle_id, version_no, fp=None):
    return call(base, "POST",
                f"/devices/{device_id}/cycles/{cycle_id}/restore",
                {"version_no": version_no,
                 "fingerprint": fp or fingerprint(base, device_id, cycle_id)})


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

    # 1. 建设备 RS-D1：两只锚点 + 一段声段，发布 v1（冻结现场已核对内容）
    call(base, "POST", "/devices", {"device_id": "RS-D1", "name": "录音笔-甲"})
    call(base, "POST", "/devices/RS-D1/anchors",
         {"local_ms": 1000, "utc_ms": 1700000000000})
    call(base, "POST", "/devices/RS-D1/anchors",
         {"local_ms": 61000, "utc_ms": 1700000060000})
    call(base, "POST", "/segments",
         {"capture_id": "RS-C1", "device_id": "RS-D1",
          "local_start_ms": 5000, "local_end_ms": 20000, "note": "鸟鸣"})
    show("发布 v1（冻结已核对内容）", *call(base, "POST", "/versions",
                                            {"note": "现场核对完成"}))

    # 2. 现场误改：整体替换锚点（+250ms）、误删 RS-C1、误录 RS-C2
    call(base, "PUT", "/devices/RS-D1/anchors", {"anchors": [
        {"local_ms": 1000, "utc_ms": 1700000000250},
        {"local_ms": 61000, "utc_ms": 1700000060250}]})
    call(base, "DELETE", "/segments/RS-C1")
    call(base, "POST", "/segments",
         {"capture_id": "RS-C2", "device_id": "RS-D1",
          "local_start_ms": 30000, "local_end_ms": 40000, "note": "误录"})
    show("误改后的当前时间轴", *call(base, "GET", "/timeline/current"))

    # 3. 凭 v1 恢复：携带当前周期详情中的内容指纹
    show("凭 v1 恢复 default 周期", *restore(base, "RS-D1", "default", 1))
    show("恢复后的当前时间轴（回到发布时状态）",
         *call(base, "GET", "/timeline/current"))
    show("已发布 v1 保持原快照", *call(base, "GET", "/versions/1"))

    # 4. 再次误改后演示各类明确拒绝（均整次拒绝、不留部分写入）
    call(base, "PUT", "/devices/RS-D1/anchors", {"anchors": [
        {"local_ms": 1000, "utc_ms": 1700000000500},
        {"local_ms": 61000, "utc_ms": 1700000060500}]})
    stale_fp = fingerprint(base, "RS-D1")
    call(base, "POST", "/segments",
         {"capture_id": "RS-C3", "device_id": "RS-D1",
          "local_start_ms": 70000, "local_end_ms": 80000})  # 晚到记录
    show("指纹过期 -> 409 restore_fingerprint_stale",
         *restore(base, "RS-D1", "default", 1, fp=stale_fp))
    show("版本不存在 -> 404 version_not_found",
         *restore(base, "RS-D1", "default", 999))

    # 版本中无该周期记录（既无锚点也无声段）-> 拒绝，绝不清除当前数据
    call(base, "POST", "/devices/RS-D1/cycles", {"cycle_id": "boot-2"})
    call(base, "POST", "/devices/RS-D1/anchors",
         {"cycle_id": "boot-2", "local_ms": 1000, "utc_ms": 1700010000000})
    call(base, "POST", "/devices/RS-D1/anchors",
         {"cycle_id": "boot-2", "local_ms": 61000, "utc_ms": 1700010060000})
    show("版本中无该周期记录 -> 409 restore_cycle_absent",
         *restore(base, "RS-D1", "boot-2", 1))

    # 版本声段采集编号现被其他周期占用 -> 拒绝
    call(base, "DELETE", "/segments/RS-C1")
    call(base, "POST", "/segments",
         {"capture_id": "RS-C1", "device_id": "RS-D1", "cycle_id": "boot-2",
          "local_start_ms": 5000, "local_end_ms": 20000})
    show("采集编号被其他周期占用 -> 409 restore_capture_conflict",
         *restore(base, "RS-D1", "default", 1))
    call(base, "DELETE", "/segments/RS-C1")  # 释放占用，便于继续演示

    # 周期已封存 -> 拒绝（先恢复可定位状态再封存）
    call(base, "POST", "/segments",
         {"capture_id": "RS-C1", "device_id": "RS-D1",
          "local_start_ms": 5000, "local_end_ms": 20000, "note": "鸟鸣"})
    call(base, "DELETE", "/segments/RS-C3")
    seal_fp = fingerprint(base, "RS-D1")
    call(base, "POST", "/devices/RS-D1/cycles/default/seal",
         {"fingerprint": seal_fp})
    show("周期已封存 -> 409 cycle_sealed",
         *restore(base, "RS-D1", "default", 1, fp=seal_fp))


if __name__ == "__main__":
    main()
