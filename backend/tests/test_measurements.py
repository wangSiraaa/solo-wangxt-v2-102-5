"""测量批次能力的验收测试：

1) 有效扫频导入 -> 叠加图理论/实测差异 -> 保守包络复核规划；
2) 坏行 / 非单调频率 / 缺失校准整批拒绝，场景不变；
3) 重复导入幂等、乱序旧批次不产生重复点也不覆盖新结论；
4) 校准修订与实测越限：旧报告可追溯、新计划需重评估；导出/导入后状态一致。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main, database
from app.db import (Base, CalibrationVersion, CarrierRow, MaskRow, Scenario)
from app.seed import DEMO_CARRIERS, DEMO_POLICY
from app.services import measurements as M
from app.services.masks import MASKS
import numpy as np


def _make_client(monkeypatch):
    """独立内存 SQLite 的 TestClient（每次调用是全新数据库）。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for m in MASKS.values():
            s.add(MaskRow(name=m.name, points=[list(p) for p in m.points],
                          span_mhz=m.span_mhz, description=m.description))
        s.add(CalibrationVersion(version="cal-v1",
                                 points=[[70.0, 0.0], [230.0, 0.0]],
                                 description="t", is_active=True))
        s.add(Scenario(name="教学演示场景", description="t", band_low_mhz=80,
                       band_high_mhz=220, guard_required_mhz=1.0,
                       leakage_limit_dbm=-45.0, reuse_policy=DEMO_POLICY,
                       carriers=[CarrierRow(**kw) for kw in DEMO_CARRIERS]))
        s.commit()
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    return TestClient(main.app), engine


@pytest.fixture()
def client(monkeypatch):
    c, _ = _make_client(monkeypatch)
    with c:
        yield c


def _demo_rules():
    return {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
            "reuse_policy": DEMO_POLICY}


def _sweep_csv(batch_ref, carrier="C1", sampled="2026-09-30T10:15:00+08:00",
               cal="cal-v1", excess_db=0.0, f_step=0.5, jitter=None):
    """按当前理论掩模生成扫频：measured = theory + excess_db（逐点）。"""
    cr = next((c for c in DEMO_CARRIERS if c["name"] == carrier), None)
    if cr is None:  # 未知载波：给一份任意合法扫频，由后端在入库前拒绝
        lines = [f"# batch: {batch_ref}", f"# carrier: {carrier}",
                 f"# sampled_at: {sampled}", f"# calibration: {cal}",
                 "f_mhz,power_dbm_hz", "90.0,-100", "91.0,-101"]
        return "\n".join(lines)
    span = MASKS[cr["mask_name"]].span_mhz
    f = np.arange(cr["center_mhz"] - span, cr["center_mhz"] + span + 1e-9, f_step)
    theory = M.theory_psd(cr["mask_name"], f, cr["center_mhz"],
                          cr["bandwidth_mhz"], cr["power_dbm"])
    lines = [f"# batch: {batch_ref}", f"# carrier: {carrier}",
             f"# sampled_at: {sampled}", f"# calibration: {cal}",
             "f_mhz,power_dbm_hz"]
    for x, t in zip(f, theory):
        p = float(t) + excess_db
        if jitter is not None:
            p += float(np.random.default_rng(0).normal(0, jitter))
        lines.append(f"{x:.3f},{p:.3f}")
    return "\n".join(lines)


def _import(client, ref, **kw):
    return client.post("/api/scenarios/1/measurements/import-text",
                       json={"text": _sweep_csv(ref, **kw), "format": "csv"})


def _confirm(client, batch_id):
    return client.post(f"/api/measurements/batches/{batch_id}/confirm")


# ---- 解析层：坏文件在入库前被拒 --------------------------------------------

def test_parser_rejects_bad_rows_nonmonotonic_missing_meta():
    good = _sweep_csv("B1")
    p = M.parse_sweep_csv(good)
    assert p.batch_ref == "B1" and len(p.points) >= 2

    # 坏行（功率不是数字）
    bad = good.replace("98.000,", "98.000,xx,", 1)
    with pytest.raises(M.MeasurementFormatError):
        M.parse_sweep_csv(bad)

    # 非单调频率
    lines = good.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("99.000"))
    lines[idx] = "97.500,-60.0"
    with pytest.raises(M.MeasurementFormatError):
        M.parse_sweep_csv("\n".join(lines))

    # 缺校准元数据
    with pytest.raises(M.MeasurementFormatError):
        M.parse_sweep_csv("\n".join(l for l in good.splitlines()
                                    if not l.startswith("# calibration")))

    # 空文件
    with pytest.raises(M.MeasurementFormatError):
        M.parse_sweep_csv("  \n")


# ---- 验收 2：坏文件整批拒绝，场景不变 ---------------------------------------

def test_bad_file_whole_rejected_no_half_batch(client):
    before = client.get("/api/scenarios/1/measurements/batches").json()
    assert before == []

    # 坏行
    bad = _sweep_csv("BAD1").replace("98.000,", "98.000,nope,", 1)
    r = client.post("/api/scenarios/1/measurements/import-text",
                    json={"text": bad, "format": "csv"})
    assert r.status_code == 400 and "数字" in r.json()["detail"]

    # 非单调
    txt = _sweep_csv("BAD2").splitlines()
    txt[6] = "80.000,-90"
    r = client.post("/api/scenarios/1/measurements/import-text",
                    json={"text": "\n".join(txt), "format": "csv"})
    assert r.status_code == 400 and "单调" in r.json()["detail"]

    # 缺失校准版本
    r = _import(client, "BAD3", cal="cal-unknown")
    assert r.status_code == 400 and "缺失校准" in r.json()["detail"]

    # 未知载波
    r = _import(client, "BAD4", carrier="NOPE")
    assert r.status_code == 400

    # 没有任何批次落库；场景/载波不受影响
    assert client.get("/api/scenarios/1/measurements/batches").json() == []
    sc = client.get("/api/scenarios/1").json()
    assert len(sc["carriers"]) == len(DEMO_CARRIERS)


# ---- 验收 1：有效导入 -> 差异叠加 -> 包络复核 -------------------------------

def test_valid_import_overlay_and_envelope_postcheck(client):
    r = _import(client, "B-OK", excess_db=0.0)
    assert r.status_code == 200
    bid = r.json()["batch"]["id"]
    assert r.json()["batch"]["status"] == "imported"
    assert r.json()["batch"]["has_violation"] is False

    # 原始记录与校准后曲线持久化（cal-v1 为 0 dB，校准值≈原始读数）
    detail = client.get(f"/api/measurements/batches/{bid}").json()
    assert len(detail["raw_points"]) == len(detail["calibrated_curve"])
    assert len(detail["deviations"]) == len(detail["raw_points"])
    assert abs(detail["max_excess_db"]) <= 0.6  # 与理论重合
    assert detail["calibration_version"] == "cal-v1"

    # 未确认前叠加页没有测量曲线
    ov = client.get("/api/scenarios/1/measurements/overlay").json()
    c1 = next(c for c in ov["carriers"] if c["carrier_name"] == "C1")
    assert c1["measured_dbm_hz"] is None

    # 确认后：理论/实测/保守包络三者齐备
    assert _confirm(client, bid).status_code == 200
    ov = client.get("/api/scenarios/1/measurements/overlay").json()
    c1 = next(c for c in ov["carriers"] if c["carrier_name"] == "C1")
    assert c1["measured_dbm_hz"] is not None
    assert c1["confirmed_batch"]["id"] == bid
    # 保守包络逐点不低于理论与实测
    for t, m, e in zip(c1["theory_dbm_hz"], c1["measured_dbm_hz"],
                       c1["envelope_dbm_hz"]):
        assert e >= t - 1e-9
        if m is not None:
            assert e >= m - 1e-9

    # 用经确认测量包络规划并 post-check（零越限）
    sc = client.get("/api/scenarios/1").json()
    pr = client.post("/api/plan-runs", json={
        "carriers": sc["carriers"], "rules": _demo_rules(),
        "band_low_mhz": 80, "band_high_mhz": 300,
        "mode": "mask_aware", "scenario_id": 1,
        "use_measured_envelope": True})
    assert pr.status_code == 200, pr.text
    body = pr.json()
    assert body["post_check_basis"] == "confirmed_measured_envelope"
    assert body["envelope_batch_ids"] == [bid]
    assert body["post_check"]["counts"]["error"] == 0


def test_violating_measurement_envelope_forces_more_spacing(client):
    # 测量比理论高 12 dB：包络复核应比纯理论掩模感知更保守（占用跨度更大）
    r = _import(client, "B-HIGH", excess_db=12.0)
    bid = r.json()["batch"]["id"]
    assert r.json()["batch"]["has_violation"] is True
    _confirm(client, bid)

    sc = client.get("/api/scenarios/1").json()
    common = {"carriers": sc["carriers"], "rules": _demo_rules(),
              "band_low_mhz": 80, "band_high_mhz": 400, "mode": "mask_aware"}
    theory_plan = client.post("/api/plan-runs", json=common).json()
    env_plan = client.post("/api/plan-runs",
                           json={**common, "scenario_id": 1,
                                 "use_measured_envelope": True}).json()
    assert env_plan["post_check_basis"] == "confirmed_measured_envelope"
    assert env_plan["occupied_span_mhz"] >= theory_plan["occupied_span_mhz"] - 1e-9
    assert env_plan["post_check"]["counts"]["error"] == 0


def test_envelope_plan_without_confirmation_rejected(client):
    r = client.post("/api/plan-runs", json={
        "carriers": client.get("/api/scenarios/1").json()["carriers"],
        "rules": _demo_rules(), "band_low_mhz": 80, "band_high_mhz": 300,
        "mode": "mask_aware", "scenario_id": 1, "use_measured_envelope": True})
    assert r.status_code == 400 and "确认" in r.json()["detail"]


# ---- 验收 3：幂等重放 + 乱序迟到保护 ----------------------------------------

def test_replay_idempotent_no_duplicate_points(client):
    txt = _sweep_csv("B-DUP")
    r1 = client.post("/api/scenarios/1/measurements/import-text",
                     json={"text": txt})
    r2 = client.post("/api/scenarios/1/measurements/import-text",
                     json={"text": txt})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["batch"]["id"] == r2.json()["batch"]["id"]
    assert r2.json()["replayed"] is True
    batches = client.get("/api/scenarios/1/measurements/batches").json()
    assert len(batches) == 1
    detail = client.get(f"/api/measurements/batches/{r1.json()['batch']['id']}").json()
    # 没有重复点
    freqs = [p[0] for p in detail["raw_points"]]
    assert len(freqs) == len(set(freqs))

    # 同批次标识 + 不同内容 -> 冲突拒绝
    other = _sweep_csv("B-DUP", sampled="2026-09-29T00:00:00+08:00")
    r3 = client.post("/api/scenarios/1/measurements/import-text",
                     json={"text": other})
    assert r3.status_code == 409
    assert len(client.get("/api/scenarios/1/measurements/batches").json()) == 1


def test_late_old_batch_cannot_override_newer_conclusion(client):
    # 先到较新的批次并确认
    new = _import(client, "B-NEW", sampled="2026-09-30T12:00:00+08:00").json()
    _confirm(client, new["batch"]["id"])
    # 迟到的旧批次：允许归档，但标记 arrived_late
    old = _import(client, "B-OLD", sampled="2026-09-28T08:00:00+08:00").json()
    assert old["batch"]["arrived_late"] is True
    # 旧批次不能确认覆盖新结论
    r = _confirm(client, old["batch"]["id"])
    assert r.status_code == 409 and "迟到" in r.json()["detail"]
    # 当前确认结论仍是新批次
    batches = {b["batch_ref"]: b for b in
               client.get("/api/scenarios/1/measurements/batches").json()}
    assert batches["B-NEW"]["status"] == "confirmed"
    assert batches["B-OLD"]["status"] == "imported"
    ov = client.get("/api/scenarios/1/measurements/overlay").json()
    c1 = next(c for c in ov["carriers"] if c["carrier_name"] == "C1")
    assert c1["confirmed_batch"]["batch_ref"] == "B-NEW"


def test_out_of_order_arrival_supersedes_old_when_newer_confirmed(client):
    # 乱序到达：旧的先确认，新的后到再确认 -> 旧的变 superseded
    old = _import(client, "B-OLD2", sampled="2026-09-28T08:00:00+08:00").json()
    _confirm(client, old["batch"]["id"])
    new = _import(client, "B-NEW2", sampled="2026-09-30T12:00:00+08:00").json()
    assert new["batch"]["arrived_late"] is False
    _confirm(client, new["batch"]["id"])
    batches = {b["batch_ref"]: b for b in
               client.get("/api/scenarios/1/measurements/batches").json()}
    assert batches["B-OLD2"]["status"] == "superseded"
    assert batches["B-NEW2"]["status"] == "confirmed"
    assert batches["B-NEW2"]["supersedes_batch_id"] == old["batch"]["id"]
    # 被取代的批次不能重新确认
    assert _confirm(client, old["batch"]["id"]).status_code == 409
    # 对同一批次重复确认是幂等的
    assert _confirm(client, new["batch"]["id"]).status_code == 200


# ---- 验收 4：校准修订 / 越限失效 / 历史可追溯 -------------------------------

def test_calibration_revision_versioned_and_old_report_reproducible(client):
    bid = _import(client, "B-CAL").json()["batch"]["id"]
    detail_before = client.get(f"/api/measurements/batches/{bid}").json()
    assert detail_before["calibration_version"] == "cal-v1"

    # 规划一次（绑定 cal-v1）
    sc = client.get("/api/scenarios/1").json()
    plan_resp = client.post("/api/plan-runs", json={
        "carriers": sc["carriers"], "rules": _demo_rules(),
        "band_low_mhz": 80, "band_high_mhz": 300, "scenario_id": 1,
        "mode": "mask_aware"})
    pid = plan_resp.json()["plan_record_id"]
    assert plan_resp.json()["calibration_version"] == "cal-v1"

    # 修订校准：新版本生效，关联计划全部过期
    r = client.post("/api/calibrations", json={
        "version": "cal-v2", "points": [[70.0, -2.0], [230.0, -2.0]],
        "description": "全线 -2 dB 修正"})
    assert r.status_code == 200
    assert r.json()["plans_marked_stale"] >= 1
    cals = client.get("/api/calibrations").json()
    active = [c for c in cals if c["is_active"]]
    assert [c["version"] for c in active] == ["cal-v2"]

    # 旧批次仍按原校准（cal-v1）复现：校准后曲线与快照不变
    detail_after = client.get(f"/api/measurements/batches/{bid}").json()
    assert detail_after["calibration_version"] == "cal-v1"
    assert detail_after["calibrated_curve"] == detail_before["calibrated_curve"]
    assert detail_after["calibration_points_snapshot"] == [[70.0, 0.0], [230.0, 0.0]]

    # 旧规划记录：内容快照不变，只是 stale
    rec = client.get(f"/api/plan-runs/{pid}").json()
    assert rec["stale"] is True
    assert rec["stale_reason"] == "calibration_revised"
    assert rec["result"]["feasible"] == plan_resp.json()["feasible"]
    assert rec["calibration_version"] == "cal-v1"

    # 版本号不能重复（修订必须新版本）
    dup = client.post("/api/calibrations", json={
        "version": "cal-v2", "points": [[70.0, 0.0], [230.0, 0.0]]})
    assert dup.status_code == 409

    # 新计划按新校准记录
    new_plan = client.post("/api/plan-runs", json={
        "carriers": sc["carriers"], "rules": _demo_rules(),
        "band_low_mhz": 80, "band_high_mhz": 300, "scenario_id": 1,
        "mode": "mask_aware"}).json()
    assert new_plan["calibration_version"] == "cal-v2"
    assert client.get(f"/api/plan-runs/{new_plan['plan_record_id']}").json()["stale"] is False


def test_violation_expires_plans_without_tampering_history(client):
    sc = client.get("/api/scenarios/1").json()
    plan_resp = client.post("/api/plan-runs", json={
        "carriers": sc["carriers"], "rules": _demo_rules(),
        "band_low_mhz": 80, "band_high_mhz": 300, "scenario_id": 1,
        "mode": "mask_aware"}).json()
    pid = plan_resp["plan_record_id"]
    snapshot = client.get(f"/api/plan-runs/{pid}").json()

    # 导入越限测量（未确认即触发，保守处理）：关联计划过期
    r = _import(client, "B-VIOL", excess_db=15.0)
    assert r.json()["batch"]["has_violation"] is True
    rec = client.get(f"/api/plan-runs/{pid}").json()
    assert rec["stale"] is True
    assert rec["stale_reason"] == "measurement_violation"
    # 历史结果不被篡改
    assert rec["result"]["assignments"] == snapshot["result"]["assignments"]
    assert rec["result"]["post_check"] == snapshot["result"]["post_check"]


def test_envelope_update_expires_envelope_plans(client):
    bid1 = _import(client, "B-E1", excess_db=0.0).json()["batch"]["id"]
    _confirm(client, bid1)
    sc = client.get("/api/scenarios/1").json()
    body = {"carriers": sc["carriers"], "rules": _demo_rules(),
            "band_low_mhz": 80, "band_high_mhz": 400, "scenario_id": 1,
            "mode": "mask_aware", "use_measured_envelope": True}
    p1 = client.post("/api/plan-runs", json=body).json()
    assert client.get(f"/api/plan-runs/{p1['plan_record_id']}").json()["stale"] is False

    # 更新的确认包络（仍合规、0.3 dB 差异不触发越限）-> 使用旧包络的计划过期
    bid2 = _import(client, "B-E2", sampled="2026-10-01T09:00:00+08:00",
                   excess_db=0.3).json()["batch"]["id"]
    b2 = client.get(f"/api/measurements/batches/{bid2}").json()
    assert b2["has_violation"] is False
    _confirm(client, bid2)
    rec = client.get(f"/api/plan-runs/{p1['plan_record_id']}").json()
    assert rec["stale"] is True
    assert rec["stale_reason"] == "envelope_revised"


# ---- 验收 4：导出 / 导入后批次状态一致 --------------------------------------

def _make_second_scenario(client):
    payload = {"name": "副本场景", "description": "d", "band_low_mhz": 80,
               "band_high_mhz": 220, "guard_required_mhz": 1.0,
               "leakage_limit_dbm": -45.0, "reuse_policy": DEMO_POLICY,
               "carriers": [{"name": c["name"], "center_mhz": c["center_mhz"],
                             "bandwidth_mhz": c["bandwidth_mhz"],
                             "power_dbm": c["power_dbm"],
                             "polarization": c["polarization"],
                             "mask_name": c["mask_name"]} for c in DEMO_CARRIERS]}
    return client.post("/api/scenarios", json=payload).json()["id"]


def test_export_import_bundle_restores_state(client, monkeypatch):
    # 源场景：一个已确认批次 + 一个迟到归档批次 + 一个越限批次
    keep = _import(client, "B-KEEP", sampled="2026-09-30T12:00:00+08:00",
                   excess_db=0.0).json()
    _confirm(client, keep["batch"]["id"])
    _import(client, "B-LATE", sampled="2026-09-28T08:00:00+08:00")
    _import(client, "B-VIOL2", sampled="2026-10-01T08:00:00+08:00",
            excess_db=12.0)

    bundle = client.get("/api/scenarios/1/measurements/export").json()
    assert bundle["format"].startswith("spectrum-workbench-measurements")
    assert {c["version"] for c in bundle["calibrations"]} == {"cal-v1"}
    refs = {b["batch_ref"]: b for b in bundle["batches"]}
    assert refs["B-KEEP"]["status"] == "confirmed"

    # 恢复到一个全新的工作台数据库（模拟课堂导出/离线导入）
    restore_c, _ = _make_client(monkeypatch)
    with restore_c:
        r = restore_c.post("/api/scenarios/1/measurements/import-bundle",
                           json={"bundle": bundle})
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 3 and r.json()["replayed"] == 0

        # 状态一致：确认关系恢复，迟到批次仍为归档
        got = {b["batch_ref"]: b for b in
               restore_c.get("/api/scenarios/1/measurements/batches").json()}
        assert got["B-KEEP"]["status"] == "confirmed"
        assert got["B-LATE"]["status"] == "imported" and got["B-LATE"]["arrived_late"]
        assert got["B-VIOL2"]["status"] == "imported"
        ov = restore_c.get("/api/scenarios/1/measurements/overlay").json()
        c1 = next(c for c in ov["carriers"] if c["carrier_name"] == "C1")
        assert c1["confirmed_batch"]["batch_ref"] == "B-KEEP"

        # 整包再导一次：幂等，不产生重复点
        r2 = restore_c.post("/api/scenarios/1/measurements/import-bundle",
                            json={"bundle": bundle}).json()
        assert r2["replayed"] == 3
        assert len(restore_c.get("/api/scenarios/1/measurements/batches").json()) == 3


def test_bundle_rejected_into_other_scenario_same_db(client):
    # 同一工作台内：批次标识全局唯一，不能把同批次写入另一场景
    _import(client, "B-UNIQ")
    bundle = client.get("/api/scenarios/1/measurements/export").json()
    sid2 = _make_second_scenario(client)
    r = client.post(f"/api/scenarios/{sid2}/measurements/import-bundle",
                    json={"bundle": bundle})
    assert r.status_code == 409
    # 目标场景仍为空（回滚，无半个批次）
    assert client.get(f"/api/scenarios/{sid2}/measurements/batches").json() == []


def test_import_bundle_atomic_on_bad_payload(client):
    r = client.post("/api/scenarios/1/measurements/import-bundle",
                    json={"bundle": {"format": "wrong", "batches": []}})
    assert r.status_code == 400
    assert client.get("/api/scenarios/1/measurements/batches").json() == []


def test_bundle_with_new_calibration_registers_and_freezes_pointer(client, monkeypatch):
    # 构造含新校准的包：恢复库缺失该校准 -> 按原版本号补登记，不抢占当前版本
    _import(client, "B-X")
    bundle = client.get("/api/scenarios/1/measurements/export").json()
    bundle["calibrations"].append({
        "id": 999, "version": "cal-lab2",
        "points": [[70.0, 1.5], [230.0, 1.5]],
        "description": "实验室校准", "is_active": True, "created_at": None})
    bundle["batches"][0]["calibration_version"] = "cal-lab2"

    restore_c, _ = _make_client(monkeypatch)
    with restore_c:
        r = restore_c.post("/api/scenarios/1/measurements/import-bundle",
                           json={"bundle": bundle})
        assert r.status_code == 200, r.text
        cals = {c["version"]: c for c in restore_c.get("/api/calibrations").json()}
        assert cals["cal-lab2"]["is_active"] is False
        assert cals["cal-v1"]["is_active"] is True
        b = restore_c.get("/api/scenarios/1/measurements/batches").json()[0]
        assert b["calibration_version"] == "cal-lab2"

