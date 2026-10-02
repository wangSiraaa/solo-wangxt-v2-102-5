"""测量批次 API 验收测试（内存 SQLite，不依赖外部服务）。

覆盖验收标准：
1. 有效扫频导入后理论/实测偏差正确持久化，并以保守包络复核规划；
2. 坏行 / 缺失校准的文件整体拒绝，场景不被污染；
3. 重复导入幂等、乱序到达的旧批次不产生重复点也不覆盖新结论；
4. 校准修订产生新版本、历史报告按原校准复现、实测越限使计划过期、
   导出/导入后批次与计划状态一致。
"""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base, CarrierRow, MaskRow, Scenario
from app.seed import DEMO_CARRIERS, DEMO_POLICY
from app.services.masks import MASKS, get_mask, psd_on_grid

RULES = {"guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0, "reuse_policy": {}}

# 两个 strict 载波：理论泄漏低于限值，留出“实测更差才越限”的演示空间
CARRIERS = [
    {"name": "C1", "center_mhz": 150.0, "bandwidth_mhz": 4.0, "power_dbm": 20.0,
     "polarization": "H", "mask_name": "strict"},
    {"name": "C2", "center_mhz": 156.0, "bandwidth_mhz": 4.0, "power_dbm": 20.0,
     "polarization": "H", "mask_name": "strict"},
]


@pytest.fixture()
def client(monkeypatch):
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
        sc = Scenario(name="教学演示场景", description="t", band_low_mhz=80,
                      band_high_mhz=220, guard_required_mhz=1.0,
                      leakage_limit_dbm=-45.0, reuse_policy=DEMO_POLICY,
                      carriers=[CarrierRow(**kw) for kw in DEMO_CARRIERS])
        s.add(sc)
        s.commit()
    monkeypatch.setattr(main, "engine", engine)
    with TestClient(main.app) as c:
        yield c


# ---- 测试辅助 ---------------------------------------------------------------

def make_scenario(client, name="测量场景"):
    r = client.post("/api/scenarios", json={
        "name": name, "description": "", "band_low_mhz": 140, "band_high_mhz": 170,
        "guard_required_mhz": 1.0, "leakage_limit_dbm": -45.0,
        "reuse_policy": {}, "carriers": CARRIERS})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def register_cal(client, name="CAL-A", factors=((140.0, 0.0), (170.0, 0.0))):
    r = client.post("/api/calibrations",
                    json={"name": name, "factors": [list(p) for p in factors]})
    assert r.status_code == 200, r.text
    return r.json()


def sweep_json(key, carrier, sampled_at, cal="CAL-A", version=1, points=None):
    return json.dumps({
        "batch_key": key, "carrier_name": carrier, "sampled_at": sampled_at,
        "calibration": {"name": cal, "version": version},
        "points": [{"freq_mhz": f, "power_dbm_hz": p} for f, p in points],
    })


def sweep_csv(key, carrier, sampled_at, cal="CAL-A", version=1, points=None):
    lines = [f"# batch_key: {key}", f"# carrier: {carrier}",
             f"# sampled_at: {sampled_at}", f"# calibration: {cal}@{version}",
             "freq_mhz,power_dbm_hz"]
    lines += [f"{f},{p}" for f, p in points]
    return "\n".join(lines) + "\n"


def theory_plus(offset_db, freqs=None, carrier=CARRIERS[0]):
    """跟随理论谱形状、整体抬高 offset_db 的测点（跨度外给很低底噪）。"""
    if freqs is None:
        freqs = np.arange(144.0, 158.01, 1.0)
    theory = psd_on_grid(get_mask(carrier["mask_name"]), np.asarray(freqs),
                         carrier["center_mhz"], carrier["bandwidth_mhz"],
                         carrier["power_dbm"])
    raw = [round(float(t) + offset_db, 3) if np.isfinite(t) else -90.0
           for t in theory]
    return list(zip([round(float(f), 3) for f in freqs], raw))


def import_file(client, sid, content, filename="sweep.json"):
    return client.post(f"/api/scenarios/{sid}/measurements/import",
                       json={"filename": filename, "content": content})


def make_plan(client, sid, basis="theory"):
    return client.post("/api/plan", json={
        "carriers": CARRIERS, "rules": RULES,
        "band_low_mhz": 140, "band_high_mhz": 170, "mode": "mask_aware",
        "scenario_id": sid, "post_check_basis": basis})


def batches_by_key(client, sid):
    return {b["batch_key"]: b
            for b in client.get(f"/api/scenarios/{sid}/measurements").json()}


# ---- 验收 1：有效导入 + 理论/实测差异 + 保守包络复核规划 ---------------------

def test_valid_import_curves_and_envelope_postcheck(client):
    sid = make_scenario(client)
    register_cal(client)  # CAL-A v1，零修正

    # 实测整体比理论高 6 dB（span 内），导入即判越限
    r = import_file(client, sid, sweep_json(
        "SW-1", "C1", "2026-09-30T10:00:00Z", points=theory_plus(6.0)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "confirmed"
    assert body["batch"]["violation"] is True
    assert body["batch"]["max_excess_db"] == pytest.approx(6.0, abs=1e-3)
    assert body["batch"]["points_count"] == len(theory_plus(6.0))

    # 持久化的曲线：原始记录 / 校准后 / 理论 / 偏差 / 保守包络
    bid = body["batch"]["id"]
    detail = client.get(f"/api/scenarios/{sid}/measurements/{bid}").json()
    curves = detail["curves"]
    n = len(curves["freq_mhz"])
    assert n == detail["points_count"]
    for i, f in enumerate(curves["freq_mhz"]):
        theory = curves["theory_dbm_hz"][i]
        if theory is None:  # 掩模跨度之外
            continue
        assert curves["raw_dbm_hz"][i] == pytest.approx(theory + 6.0, abs=2e-3)
        assert curves["calibrated_dbm_hz"][i] == pytest.approx(
            curves["raw_dbm_hz"][i], abs=1e-9)  # 零修正校准
        assert curves["deviation_db"][i] == pytest.approx(6.0, abs=2e-3)
        assert curves["envelope_dbm_hz"][i] == pytest.approx(
            max(theory, curves["calibrated_dbm_hz"][i]), abs=1e-9)

    # 理论口径 post-check：规划后零越界
    r_theory = make_plan(client, sid, "theory").json()
    assert r_theory["feasible"]
    assert r_theory["post_check"]["basis"] == "theory"
    assert r_theory["post_check"]["counts"]["error"] == 0

    # 实测保守包络口径 post-check：实测高 6 dB -> 尾部泄漏越限被复核出来
    r_env = make_plan(client, sid, "measurement_envelope").json()
    assert r_env["feasible"]
    assert r_env["post_check"]["basis"] == "measurement_envelope"
    tails = [f for f in r_env["post_check"]["findings"]
             if f["type"] == "mask_tail" and f["carrier_a"] == "C1"]
    assert tails, "实测包络下应复核出 C1 的尾部越限"
    assert "实测保守包络" in tails[0]["message"]
    assert tails[0]["leakage_dbm"] > -45.0

    # 两次规划都持久化为计划记录，基准各自正确
    plans = client.get(f"/api/scenarios/{sid}/plans").json()
    assert [p["post_check_basis"] for p in plans] == \
        ["measurement_envelope", "theory"]
    assert all(p["status"] == "active" for p in plans)
    got = client.get(f"/api/scenarios/{sid}/plans/{plans[0]['id']}").json()
    assert got["post_check"]["basis"] == "measurement_envelope"


def test_csv_import_and_overlay_curves(client):
    sid = make_scenario(client)
    register_cal(client, factors=((140.0, -1.0), (170.0, -1.0)))  # -1 dB 修正
    pts = theory_plus(0.0)  # raw 恰等于理论 -> 校准后 = 理论 - 1，不越限
    r = import_file(client, sid, sweep_csv(
        "CSV-1", "C1", "2026-09-30T10:00:00Z", points=pts), "sweep.csv")
    assert r.status_code == 200, r.text
    assert r.json()["batch"]["violation"] is False
    detail = client.get(
        f"/api/scenarios/{sid}/measurements/{r.json()['batch']['id']}").json()
    dev = [d for d in detail["curves"]["deviation_db"] if d is not None]
    assert dev and all(d == pytest.approx(-1.0, abs=2e-3) for d in dev)


# ---- 验收 2：坏文件整体拒绝，场景不变 ---------------------------------------

def test_bad_files_rejected_atomically(client):
    sid = make_scenario(client)
    register_cal(client)
    good = sweep_json("GOOD-1", "C1", "2026-09-30T10:00:00Z",
                      points=theory_plus(0.0))
    assert import_file(client, sid, good).status_code == 200
    before = client.get(f"/api/scenarios/{sid}").json()

    bad_files = [
        # 坏行：非数值
        sweep_csv("B1", "C1", "2026-09-30T11:00:00Z",
                  points=[(148.0, -50.0), ("abc", "def"), (150.0, -49.0)]),
        # 坏行：列数不对
        "# batch_key: B2\n# carrier: C1\n# sampled_at: 2026-09-30T11:00:00Z\n"
        "# calibration: CAL-A@1\nfreq_mhz,power_dbm_hz\n148.0,-50.0,extra\n149.0,-49.0\n",
        # 非单调频率
        sweep_json("B3", "C1", "2026-09-30T11:00:00Z",
                   points=[(148.0, -50.0), (150.0, -49.0), (149.0, -48.0)]),
        # 缺失校准字段
        json.dumps({"batch_key": "B4", "carrier_name": "C1",
                    "sampled_at": "2026-09-30T11:00:00Z",
                    "points": [[148.0, -50.0], [149.0, -49.0]]}),
        # 引用不存在的校准版本
        sweep_json("B5", "C1", "2026-09-30T11:00:00Z", version=99,
                   points=[(148.0, -50.0), (149.0, -49.0)]),
        # 引用不存在的载波
        sweep_json("B6", "NOPE", "2026-09-30T11:00:00Z",
                   points=[(148.0, -50.0), (149.0, -49.0)]),
        # 前半好后半坏：同样整体拒绝
        sweep_csv("B7", "C1", "2026-09-30T11:00:00Z",
                  points=[(148.0, -50.0), (149.0, -49.0), (150.0, "oops")]),
    ]
    for content in bad_files:
        r = import_file(client, sid, content)
        assert r.status_code == 400, (r.status_code, r.text)

    # 只有最初的好批次在库中；场景本身未被污染
    batches = client.get(f"/api/scenarios/{sid}/measurements").json()
    assert [b["batch_key"] for b in batches] == ["GOOD-1"]
    assert batches[0]["status"] == "confirmed"
    after = client.get(f"/api/scenarios/{sid}").json()
    assert before == after


def test_missing_calibration_never_imported(client):
    sid = make_scenario(client)
    # 校准根本没注册过：缺失校准 -> 整体拒绝
    r = import_file(client, sid, sweep_json(
        "X1", "C1", "2026-09-30T10:00:00Z",
        points=[(148.0, -50.0), (149.0, -49.0)]))
    assert r.status_code == 400
    assert "校准" in r.json()["detail"]
    assert client.get(f"/api/scenarios/{sid}/measurements").json() == []


# ---- 验收 3：幂等重放与乱序到达 ---------------------------------------------

def test_replay_idempotent_and_late_batch_archived(client):
    sid = make_scenario(client)
    register_cal(client)
    content = sweep_json("SW-A", "C1", "2026-09-30T10:00:00Z",
                         points=theory_plus(0.0))

    r1 = import_file(client, sid, content).json()
    assert r1["outcome"] == "confirmed"
    # 相同批次重放（内容一致）：幂等，不产生重复点
    r2 = import_file(client, sid, content).json()
    assert r2["outcome"] == "duplicate"
    assert r2["batch"]["id"] == r1["batch"]["id"]
    # 语义相同、空白不同的文件同样幂等
    pretty = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
    assert import_file(client, sid, pretty).json()["outcome"] == "duplicate"
    batches = client.get(f"/api/scenarios/{sid}/measurements").json()
    assert len(batches) == 1
    detail = client.get(f"/api/scenarios/{sid}/measurements/{batches[0]['id']}").json()
    assert detail["points_count"] == len(theory_plus(0.0))

    # 同批次键但内容不同：拒绝覆盖
    conflict = sweep_json("SW-A", "C1", "2026-09-30T10:00:00Z",
                          points=theory_plus(1.0))
    assert import_file(client, sid, conflict).status_code == 409

    # 先建立一个活动计划，验证越限迟到批次不会让它过期
    plan_id = make_plan(client, sid).json()["plan_record_id"]

    # 迟到的旧批次（采样时刻更早，且越限）：归档，不覆盖新结论、不让计划过期
    late = import_file(client, sid, sweep_json(
        "SW-OLD", "C1", "2026-09-29T09:00:00Z", points=theory_plus(8.0))).json()
    assert late["outcome"] == "superseded"
    assert late["batch"]["violation"] is True
    assert late["expired_plan_ids"] == []
    states = batches_by_key(client, sid)
    assert states["SW-A"]["status"] == "confirmed"
    assert states["SW-OLD"]["status"] == "superseded"
    plan = client.get(f"/api/scenarios/{sid}/plans/{plan_id}").json()
    assert plan["status"] == "active"

    # 更新的批次（越限）到达：成为新结论，旧确认批次归档，活动计划过期
    newer = import_file(client, sid, sweep_json(
        "SW-NEW", "C1", "2026-10-01T08:00:00Z", points=theory_plus(6.0))).json()
    assert newer["outcome"] == "confirmed"
    assert newer["superseded_batch_ids"] == [r1["batch"]["id"]]
    assert newer["expired_plan_ids"] == [plan_id]
    states = batches_by_key(client, sid)
    assert states["SW-NEW"]["status"] == "confirmed"
    assert states["SW-A"]["status"] == "superseded"
    assert states["SW-OLD"]["status"] == "superseded"

    # 计划过期但历史 post_check 不被篡改
    plan_after = client.get(f"/api/scenarios/{sid}/plans/{plan_id}").json()
    assert plan_after["status"] == "expired"
    assert "SW-NEW" in plan_after["expired_reason"]
    assert plan_after["post_check"] == plan["post_check"]
    assert plan_after["assignments"] == plan["assignments"]


# ---- 验收 4：校准修订、历史复现、导出导入一致 ---------------------------------

def test_calibration_revision_traceable_and_plans_expire(client):
    sid = make_scenario(client)
    v1 = register_cal(client, factors=((140.0, -1.0), (170.0, -1.0)))
    assert v1["version"] == 1

    r = import_file(client, sid, sweep_json(
        "SW-1", "C1", "2026-09-30T10:00:00Z", points=theory_plus(0.0))).json()
    bid = r["batch"]["id"]
    plan_id = make_plan(client, sid).json()["plan_record_id"]

    # 校准修订 -> 新版本；依赖该校准的已确认测量使活动计划过期
    v2 = register_cal(client, factors=((140.0, -2.5), (170.0, -2.5)))
    assert v2["version"] == 2
    assert v2["expired_plan_ids"] == [plan_id]
    plan = client.get(f"/api/scenarios/{sid}/plans/{plan_id}").json()
    assert plan["status"] == "expired" and "CAL-A" in plan["expired_reason"]

    # 旧校准版本仍在且不可变：历史批次按原校准复现
    got_v1 = client.get("/api/calibrations/CAL-A/1").json()
    assert got_v1["factors"] == [[140.0, -1.0], [170.0, -1.0]]
    detail = client.get(f"/api/scenarios/{sid}/measurements/{bid}").json()
    dev = [d for d in detail["curves"]["deviation_db"] if d is not None]
    assert all(d == pytest.approx(-1.0, abs=2e-3) for d in dev)  # 仍是 v1 的 -1 dB

    # 新批次按 v2 校准（-2.5 dB）
    r2 = import_file(client, sid, sweep_json(
        "SW-2", "C1", "2026-10-01T10:00:00Z", version=2,
        points=theory_plus(0.0))).json()
    detail2 = client.get(
        f"/api/scenarios/{sid}/measurements/{r2['batch']['id']}").json()
    dev2 = [d for d in detail2["curves"]["deviation_db"] if d is not None]
    assert all(d == pytest.approx(-2.5, abs=2e-3) for d in dev2)
    assert detail2["calibration_version"] == 2


def test_measured_violation_expires_plans_and_export_import_consistent(client):
    sid = make_scenario(client)
    register_cal(client)
    # 干净批次 + 两个活动计划（不同基准）
    import_file(client, sid, sweep_json("SW-1", "C1", "2026-09-30T10:00:00Z",
                                        points=theory_plus(0.0)))
    p1 = make_plan(client, sid, "theory").json()["plan_record_id"]
    p2 = make_plan(client, sid, "measurement_envelope").json()["plan_record_id"]

    # 实测越限批次到达 -> 两个活动计划都过期，历史结果不变
    r = import_file(client, sid, sweep_json(
        "SW-2", "C1", "2026-10-01T10:00:00Z", points=theory_plus(6.0))).json()
    assert sorted(r["expired_plan_ids"]) == sorted([p1, p2])
    plans = {p["id"]: p for p in client.get(f"/api/scenarios/{sid}/plans").json()}
    assert plans[p1]["status"] == "expired" and plans[p2]["status"] == "expired"
    assert "SW-2" in plans[p1]["expired_reason"]

    # 刷新（重新获取）后批次状态一致
    again = client.get(f"/api/scenarios/{sid}/measurements").json()
    assert again == client.get(f"/api/scenarios/{sid}/measurements").json()
    assert batches_by_key(client, sid)["SW-2"]["status"] == "confirmed"

    # 导出 -> 改名导入 -> 批次与计划状态保持一致
    doc = client.get(f"/api/scenarios/{sid}/export").json()
    assert doc["format"] == "spectrum-workbench/scenario-export"
    doc["scenario"]["name"] = "导出副本"
    r_imp = client.post("/api/scenarios/import", json=doc)
    assert r_imp.status_code == 200, r_imp.text
    new_id = r_imp.json()["id"]

    old_batches = client.get(f"/api/scenarios/{sid}/measurements").json()
    new_batches = client.get(f"/api/scenarios/{new_id}/measurements").json()
    strip = lambda bs: sorted([{k: v for k, v in b.items()
                                if k not in ("id", "scenario_id", "imported_at")}
                               for b in bs], key=lambda b: b["batch_key"])
    assert strip(new_batches) == strip(old_batches)
    # 曲线内容也一致（含偏差与包络）
    for ob, nb in zip(strip(old_batches), strip(new_batches)):
        od = client.get(f"/api/scenarios/{sid}/measurements/"
                        f"{[b for b in old_batches if b['batch_key'] == ob['batch_key']][0]['id']}").json()
        nd = client.get(f"/api/scenarios/{new_id}/measurements/"
                        f"{[b for b in new_batches if b['batch_key'] == nb['batch_key']][0]['id']}").json()
        assert od["curves"] == nd["curves"]

    old_plans = client.get(f"/api/scenarios/{sid}/plans").json()
    new_plans = client.get(f"/api/scenarios/{new_id}/plans").json()
    key = lambda p: (p["mode"], p["post_check_basis"], p["feasible"])
    old_p = sorted([{k: v for k, v in p.items()
                     if k not in ("id", "scenario_id", "created_at")}
                    for p in old_plans], key=key)
    new_p = sorted([{k: v for k, v in p.items()
                     if k not in ("id", "scenario_id", "created_at")}
                    for p in new_plans], key=key)
    assert new_p == old_p
    assert all(p["status"] == "expired" for p in new_plans)

    # 新计划需重评估：新跑的计划是 active，不受旧过期状态影响
    p3 = make_plan(client, new_id, "measurement_envelope").json()
    assert p3["post_check"]["basis"] == "measurement_envelope"
    plans3 = client.get(f"/api/scenarios/{new_id}/plans").json()
    assert any(p["status"] == "active" for p in plans3)


def test_export_import_rejects_conflicting_calibration(client):
    sid = make_scenario(client)
    register_cal(client)
    import_file(client, sid, sweep_json("SW-1", "C1", "2026-09-30T10:00:00Z",
                                        points=theory_plus(0.0)))
    doc = client.get(f"/api/scenarios/{sid}/export").json()
    doc["scenario"]["name"] = "冲突副本"
    doc["calibrations"][0]["factors"] = [[140.0, 9.9], [170.0, 9.9]]  # 篡改修正量
    r = client.post("/api/scenarios/import", json=doc)
    assert r.status_code == 409  # 校准版本不可变，拒绝覆盖
    # 未写入半个场景
    assert "冲突副本" not in {s["name"] for s in client.get("/api/scenarios").json()}
