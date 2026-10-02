"""FastAPI 频谱工作台（离线简化模型）。

不连接无线电设备、不生成发射指令；只对录入的载波数据做计算与可视化。
测量批次：离线扫频文件原子导入、幂等重放、迟到旧批次归档、校准版本化、
实测越限时让关联计划过期（历史 post_check 不篡改）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .assemble import bands_view, build_spectrum, to_domain, to_rules, validate_masks
from .config import CORS_ORIGINS, DATABASE_URL
from .db import (Base, CalibrationVersion, CarrierRow, MaskRow, MeasurementBatch,
                 PlanRecord, Scenario)
from .schemas import (AnalyzeRequest, CalibrationIn, MaskOut,
                      MeasurementBatchDetail, MeasurementBatchOut,
                      MeasurementImportIn, MeasurementImportResult, PlanRecordDetail,
                      PlanRecordOut, PlanRequest, ScenarioIn, ScenarioOut,
                      ScenarioSummary)
from .seed import seed
from .services.analysis import Carrier, analyze
from .services.measurement import (CalibrationCurve, EnvelopeCurve,
                                   compute_batch_curves)
from .services.planner import BandLimits, plan
from .services.sweepfile import ParsedSweep, parse_sweep_file

app = FastAPI(
    title="频谱工作台 API（离线教学模型）",
    version="1.1.0",
    description="载波频带冲突检查、掩模尾部泄漏、线性域功率汇总与 OR-Tools 频率规划；"
                "支持离线测量批次导入与校准版本管理。"
                "不连接无线电设备，不生成发射指令。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

EXPORT_FORMAT = "spectrum-workbench/scenario-export"
EXPORT_VERSION = 1


@app.on_event("startup")
def _startup() -> None:
    Base.metadata.create_all(engine)
    seed(engine)


# ---- 计算接口（无状态，数据由前端提交） -----------------------------------

@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        validate_masks(req.carriers)
        carriers = [to_domain(c) for c in req.carriers]
        result = analyze(carriers, to_rules(req.rules))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["bands"] = bands_view(carriers)
    result["spectrum"] = build_spectrum(carriers, req.plot_grid_mhz)
    return result


def _confirmed_envelopes(s: Session, scenario_id: int) -> dict[str, EnvelopeCurve]:
    """场景内各载波当前确认批次的校准后实测曲线（用于保守包络 post-check）。"""
    env: dict[str, EnvelopeCurve] = {}
    rows = s.scalars(select(MeasurementBatch).where(
        MeasurementBatch.scenario_id == scenario_id,
        MeasurementBatch.status == "confirmed"))
    for b in rows:
        env[b.carrier_name] = EnvelopeCurve(
            carrier_name=b.carrier_name,
            f_mhz=tuple(b.curves["freq_mhz"]),
            calibrated_dbm_hz=tuple(b.curves["calibrated_dbm_hz"]))
    return env


@app.post("/api/plan")
def plan_endpoint(req: PlanRequest) -> dict:
    try:
        req.validate_band()
        validate_masks(req.carriers)
        carriers = [to_domain(c) for c in req.carriers]
        result = plan(carriers, to_rules(req.rules),
                      BandLimits(req.band_low_mhz, req.band_high_mhz), mode=req.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    envelopes = None
    if req.post_check_basis == "measurement_envelope":
        if req.scenario_id is None:
            raise HTTPException(400, "以测量包络复核需要关联场景（scenario_id）")
        with Session(engine) as s:
            if s.get(Scenario, req.scenario_id) is None:
                raise HTTPException(404, "场景不存在")
            envelopes = _confirmed_envelopes(s, req.scenario_id)

    if result["feasible"]:
        planned = [
            Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                    bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                    polarization=a["polarization"], mask_name=a["mask_name"])
            for a in result["assignments"]
        ]
        result["post_check"] = analyze(planned, to_rules(req.rules), envelopes=envelopes)
        # 规划后的频段视图与发射谱（用于前端叠加对照）
        result["bands"] = bands_view(planned)
        result["spectrum"] = build_spectrum(planned, 0.05)
    result["post_check_basis"] = req.post_check_basis

    # 关联场景时持久化计划记录（历史报告；之后只可被置为过期，不可篡改）
    if req.scenario_id is not None:
        with Session(engine) as s:
            if s.get(Scenario, req.scenario_id) is None:
                raise HTTPException(404, "场景不存在")
            rec = PlanRecord(
                scenario_id=req.scenario_id, mode=req.mode,
                post_check_basis=req.post_check_basis,
                band_low_mhz=req.band_low_mhz, band_high_mhz=req.band_high_mhz,
                rules=req.rules.model_dump(), assignments=result["assignments"],
                post_check=result.get("post_check") or {},
                feasible=result["feasible"],
            )
            s.add(rec)
            s.commit()
            result["plan_record_id"] = rec.id
    return result


# ---- 掩模 ------------------------------------------------------------------

@app.get("/api/masks", response_model=list[MaskOut])
def list_masks() -> list[MaskRow]:
    with Session(engine) as s:
        return list(s.scalars(select(MaskRow).order_by(MaskRow.name)))


# ---- 校准版本 --------------------------------------------------------------

def _cal_out(c: CalibrationVersion) -> dict:
    return {
        "id": c.id, "name": c.name, "version": c.version, "factors": c.factors,
        "description": c.description,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _expire_plans(s: Session, scenario_id: int, reason: str) -> list[int]:
    """把场景的活动计划置为过期（只改状态与原因，历史 post_check 不动）。"""
    now = datetime.now(timezone.utc)
    ids = []
    for r in s.scalars(select(PlanRecord).where(
            PlanRecord.scenario_id == scenario_id, PlanRecord.status == "active")):
        r.status = "expired"
        r.expired_reason = reason
        r.expired_at = now
        ids.append(r.id)
    return ids


@app.post("/api/calibrations")
def create_calibration(req: CalibrationIn) -> dict:
    """注册校准版本。同名自动递增版本号；旧版本不可变，
    历史测量批次仍按其导入时的版本复现。修订会让依赖该校准的
    已确认测量所属场景的活动计划过期（需按新基准重评估）。"""
    try:
        curve = CalibrationCurve.from_factors(req.name, 0, req.factors)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    factors = [[f, o] for f, o in zip(curve.freqs, curve.offsets_db)]
    with Session(engine) as s:
        latest = s.scalar(select(CalibrationVersion)
                          .where(CalibrationVersion.name == req.name)
                          .order_by(CalibrationVersion.version.desc()))
        version = 1 if latest is None else latest.version + 1
        row = CalibrationVersion(name=req.name, version=version, factors=factors,
                                 description=req.description)
        s.add(row)
        s.flush()

        expired_plan_ids: list[int] = []
        if latest is not None:
            cal_ids = s.scalars(select(CalibrationVersion.id).where(
                CalibrationVersion.name == req.name))
            affected = s.scalars(select(MeasurementBatch.scenario_id).where(
                MeasurementBatch.calibration_id.in_(list(cal_ids)),
                MeasurementBatch.status == "confirmed").distinct())
            reason = f"校准 {req.name} 已修订至 v{version}，测量基准变化，计划需重新评估"
            for sid in affected:
                expired_plan_ids.extend(_expire_plans(s, sid, reason))
        s.commit()
        out = _cal_out(row)
        out["expired_plan_ids"] = expired_plan_ids
        return out


@app.get("/api/calibrations")
def list_calibrations() -> list[dict]:
    with Session(engine) as s:
        rows = s.scalars(select(CalibrationVersion)
                         .order_by(CalibrationVersion.name, CalibrationVersion.version))
        return [_cal_out(c) for c in rows]


@app.get("/api/calibrations/{name}/{version}")
def get_calibration(name: str, version: int) -> dict:
    """取指定校准版本（历史报告按原校准复现的依据）。"""
    with Session(engine) as s:
        row = s.scalar(select(CalibrationVersion).where(
            CalibrationVersion.name == name, CalibrationVersion.version == version))
        if row is None:
            raise HTTPException(404, f"校准 {name!r} v{version} 不存在")
        return _cal_out(row)


# ---- 测量批次 --------------------------------------------------------------

def _content_hash(parsed: ParsedSweep) -> str:
    """规范化内容的 SHA-256：同批次重放（哪怕空白不同）得到相同摘要。"""
    canon = json.dumps({
        "batch_key": parsed.batch_key,
        "carrier_name": parsed.carrier_name,
        "sampled_at": parsed.sampled_at_iso,
        "calibration": {"name": parsed.calibration_name,
                        "version": parsed.calibration_version},
        "points": [[f, p] for f, p in parsed.points],
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _batch_out(b: MeasurementBatch) -> MeasurementBatchOut:
    return MeasurementBatchOut(
        id=b.id, scenario_id=b.scenario_id, batch_key=b.batch_key,
        carrier_name=b.carrier_name, sampled_at=b.sampled_at,
        calibration_name=b.calibration.name, calibration_version=b.calibration.version,
        status=b.status, violation=b.violation, max_excess_db=b.max_excess_db,
        points_count=b.points_count,
        imported_at=b.imported_at.isoformat() if b.imported_at else None)


@app.post("/api/scenarios/{scenario_id}/measurements/import",
          response_model=MeasurementImportResult)
def import_measurement(scenario_id: int, req: MeasurementImportIn) -> MeasurementImportResult:
    """导入一份离线扫频记录。原子化：先完整解析校验，再单事务写入；
    格式错误 / 非单调频率 / 缺失校准都不会写入半个批次。
    同批次重放幂等；迟到的旧测量归档为 superseded，不覆盖更新的确认结论。
    """
    try:
        parsed = parse_sweep_file(req.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    digest = _content_hash(parsed)

    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")

        # 幂等：同批次键重放
        existing = s.scalar(select(MeasurementBatch).where(
            MeasurementBatch.scenario_id == scenario_id,
            MeasurementBatch.batch_key == parsed.batch_key))
        if existing is not None:
            if existing.content_hash == digest:
                return MeasurementImportResult(
                    outcome="duplicate", batch=_batch_out(existing),
                    message=f"批次 {parsed.batch_key!r} 已导入过（内容一致），幂等忽略")
            raise HTTPException(
                409, f"批次标识 {parsed.batch_key!r} 已存在但内容不同，拒绝覆盖")

        # 缺失校准：整体拒绝
        cal = s.scalar(select(CalibrationVersion).where(
            CalibrationVersion.name == parsed.calibration_name,
            CalibrationVersion.version == parsed.calibration_version))
        if cal is None:
            raise HTTPException(
                400, f"校准 {parsed.calibration_name!r} v{parsed.calibration_version} "
                     "不存在，请先注册该校准版本再导入")

        carrier_row = next((c for c in sc.carriers if c.name == parsed.carrier_name), None)
        if carrier_row is None:
            raise HTTPException(
                400, f"场景中不存在载波 {parsed.carrier_name!r}，测量无处关联")
        carrier = Carrier(id=None, name=carrier_row.name,
                          center_mhz=carrier_row.center_mhz,
                          bandwidth_mhz=carrier_row.bandwidth_mhz,
                          power_dbm=carrier_row.power_dbm,
                          polarization=carrier_row.polarization,
                          mask_name=carrier_row.mask_name)
        cal_curve = CalibrationCurve.from_factors(cal.name, cal.version, cal.factors)
        curves, violation, max_excess = compute_batch_curves(parsed, carrier, cal_curve)

        # 确认/归档：与该载波当前确认批次比较采样时刻（迟到旧批次不覆盖新结论）
        current = s.scalar(select(MeasurementBatch).where(
            MeasurementBatch.scenario_id == scenario_id,
            MeasurementBatch.carrier_name == parsed.carrier_name,
            MeasurementBatch.status == "confirmed"))
        status = "confirmed"
        if current is not None and \
                parsed.sampled_at <= datetime.fromisoformat(current.sampled_at):
            status = "superseded"

        batch = MeasurementBatch(
            scenario_id=scenario_id, batch_key=parsed.batch_key,
            carrier_name=parsed.carrier_name, sampled_at=parsed.sampled_at_iso,
            calibration_id=cal.id, content_hash=digest, status=status,
            violation=violation, max_excess_db=max_excess,
            points_count=len(parsed.points), curves=curves)
        s.add(batch)
        s.flush()

        superseded_ids: list[int] = []
        expired_plan_ids: list[int] = []
        if status == "confirmed":
            if current is not None:
                current.status = "superseded"
                superseded_ids.append(current.id)
            if violation:
                expired_plan_ids = _expire_plans(
                    s, scenario_id,
                    f"测量批次 {parsed.batch_key} 实测越限（最大超出 "
                    f"{max_excess} dB），计划需重新评估")
        s.commit()

        if status == "superseded":
            msg = (f"批次 {parsed.batch_key!r} 采样时刻早于当前确认结论，"
                   "已归档为 superseded，不影响现有结论")
        elif violation:
            msg = (f"批次 {parsed.batch_key!r} 已确认为当前结论；实测越限 "
                   f"{max_excess} dB，{len(expired_plan_ids)} 个关联计划已置为过期")
        else:
            msg = f"批次 {parsed.batch_key!r} 已确认为当前结论，未见越限"
        return MeasurementImportResult(
            outcome=status, batch=_batch_out(batch),
            superseded_batch_ids=superseded_ids, expired_plan_ids=expired_plan_ids,
            message=msg)


@app.get("/api/scenarios/{scenario_id}/measurements",
         response_model=list[MeasurementBatchOut])
def list_measurements(scenario_id: int) -> list[MeasurementBatchOut]:
    with Session(engine) as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        rows = s.scalars(select(MeasurementBatch)
                         .where(MeasurementBatch.scenario_id == scenario_id)
                         .order_by(MeasurementBatch.carrier_name,
                                   MeasurementBatch.sampled_at.desc()))
        return [_batch_out(b) for b in rows]


@app.get("/api/scenarios/{scenario_id}/measurements/{batch_id}",
         response_model=MeasurementBatchDetail)
def get_measurement(scenario_id: int, batch_id: int) -> MeasurementBatchDetail:
    with Session(engine) as s:
        b = s.get(MeasurementBatch, batch_id)
        if b is None or b.scenario_id != scenario_id:
            raise HTTPException(404, "测量批次不存在")
        out = _batch_out(b)
        return MeasurementBatchDetail(**out.model_dump(), curves=b.curves)


# ---- 计划记录（历史报告，只可过期不可篡改） ---------------------------------

def _plan_out(r: PlanRecord) -> PlanRecordOut:
    return PlanRecordOut(
        id=r.id, scenario_id=r.scenario_id,
        created_at=r.created_at.isoformat() if r.created_at else None,
        mode=r.mode, post_check_basis=r.post_check_basis, feasible=r.feasible,
        status=r.status, expired_reason=r.expired_reason or "",
        expired_at=r.expired_at.isoformat() if r.expired_at else None,
        counts=(r.post_check or {}).get("counts"))


@app.get("/api/scenarios/{scenario_id}/plans", response_model=list[PlanRecordOut])
def list_plan_records(scenario_id: int) -> list[PlanRecordOut]:
    with Session(engine) as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        rows = s.scalars(select(PlanRecord)
                         .where(PlanRecord.scenario_id == scenario_id)
                         .order_by(PlanRecord.id.desc()))
        return [_plan_out(r) for r in rows]


@app.get("/api/scenarios/{scenario_id}/plans/{plan_id}",
         response_model=PlanRecordDetail)
def get_plan_record(scenario_id: int, plan_id: int) -> PlanRecordDetail:
    with Session(engine) as s:
        r = s.get(PlanRecord, plan_id)
        if r is None or r.scenario_id != scenario_id:
            raise HTTPException(404, "计划记录不存在")
        return PlanRecordDetail(
            **_plan_out(r).model_dump(),
            band_low_mhz=r.band_low_mhz, band_high_mhz=r.band_high_mhz,
            rules=r.rules, assignments=r.assignments, post_check=r.post_check)


# ---- 场景持久化 ------------------------------------------------------------

def _carrier_out(c: CarrierRow):
    from .schemas import CarrierOut
    return CarrierOut(id=c.id, name=c.name, center_mhz=c.center_mhz,
                      bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                      polarization=c.polarization, mask_name=c.mask_name)


def _row_to_out(sc: Scenario) -> ScenarioOut:
    return ScenarioOut(
        id=sc.id, name=sc.name, description=sc.description,
        band_low_mhz=sc.band_low_mhz, band_high_mhz=sc.band_high_mhz,
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_dbm=sc.leakage_limit_dbm,
        reuse_policy=sc.reuse_policy or {},
        carriers=[_carrier_out(c) for c in sorted(sc.carriers, key=lambda c: c.position)],
    )


@app.get("/api/scenarios", response_model=list[ScenarioSummary])
def list_scenarios() -> list[ScenarioSummary]:
    with Session(engine) as s:
        rows = list(s.scalars(select(Scenario).order_by(Scenario.id)))
        return [ScenarioSummary(id=r.id, name=r.name, description=r.description,
                                carrier_count=len(r.carriers),
                                created_at=r.created_at.isoformat() if r.created_at else None)
                for r in rows]


@app.get("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def get_scenario(scenario_id: int) -> ScenarioOut:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        return _row_to_out(sc)


@app.post("/api/scenarios", response_model=ScenarioOut)
def create_scenario(req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        if s.scalar(select(Scenario).where(Scenario.name == req.name)) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=req.band_low_mhz, band_high_mhz=req.band_high_mhz,
            guard_required_mhz=req.guard_required_mhz,
            leakage_limit_dbm=req.leakage_limit_dbm,
            reuse_policy=dict(req.reuse_policy),
            carriers=[
                CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                           bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                           polarization=c.polarization, mask_name=c.mask_name)
                for i, c in enumerate(req.carriers)
            ],
        )
        s.add(sc)
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int) -> dict:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        s.delete(sc)
        s.commit()
        return {"deleted": scenario_id}


@app.put("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: int, req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        other = s.scalar(select(Scenario).where(
            Scenario.name == req.name.strip(), Scenario.id != scenario_id))
        if other is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        sc.name = req.name.strip()
        sc.description = req.description
        sc.band_low_mhz = req.band_low_mhz
        sc.band_high_mhz = req.band_high_mhz
        sc.guard_required_mhz = req.guard_required_mhz
        sc.leakage_limit_dbm = req.leakage_limit_dbm
        sc.reuse_policy = dict(req.reuse_policy)
        sc.carriers = [
            CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                       bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                       polarization=c.polarization, mask_name=c.mask_name)
            for i, c in enumerate(req.carriers)
        ]
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


# ---- 场景导出 / 导入（含测量批次与计划记录，状态保持一致） --------------------

def _dt_iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@app.get("/api/scenarios/{scenario_id}/export")
def export_scenario(scenario_id: int) -> dict:
    """导出场景及其校准、测量批次、计划记录（含状态），用于迁移/存档。"""
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        batches = list(s.scalars(select(MeasurementBatch).where(
            MeasurementBatch.scenario_id == scenario_id)))
        plans = list(s.scalars(select(PlanRecord).where(
            PlanRecord.scenario_id == scenario_id).order_by(PlanRecord.id)))
        cal_ids = sorted({b.calibration_id for b in batches})
        cals = [s.get(CalibrationVersion, cid) for cid in cal_ids]
        return {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "scenario": {
                "name": sc.name, "description": sc.description,
                "band_low_mhz": sc.band_low_mhz, "band_high_mhz": sc.band_high_mhz,
                "guard_required_mhz": sc.guard_required_mhz,
                "leakage_limit_dbm": sc.leakage_limit_dbm,
                "reuse_policy": sc.reuse_policy or {},
                "carriers": [{
                    "name": c.name, "center_mhz": c.center_mhz,
                    "bandwidth_mhz": c.bandwidth_mhz, "power_dbm": c.power_dbm,
                    "polarization": c.polarization, "mask_name": c.mask_name,
                } for c in sorted(sc.carriers, key=lambda c: c.position)],
            },
            "calibrations": [{
                "name": c.name, "version": c.version, "factors": c.factors,
                "description": c.description, "created_at": _dt_iso(c.created_at),
            } for c in cals],
            "measurement_batches": [{
                "batch_key": b.batch_key, "carrier_name": b.carrier_name,
                "sampled_at": b.sampled_at,
                "calibration": {"name": b.calibration.name,
                                "version": b.calibration.version},
                "content_hash": b.content_hash, "status": b.status,
                "violation": b.violation, "max_excess_db": b.max_excess_db,
                "points_count": b.points_count, "curves": b.curves,
                "imported_at": _dt_iso(b.imported_at),
            } for b in batches],
            "plan_records": [{
                "created_at": _dt_iso(p.created_at), "mode": p.mode,
                "post_check_basis": p.post_check_basis,
                "band_low_mhz": p.band_low_mhz, "band_high_mhz": p.band_high_mhz,
                "rules": p.rules, "assignments": p.assignments,
                "post_check": p.post_check, "feasible": p.feasible,
                "status": p.status, "expired_reason": p.expired_reason,
                "expired_at": _dt_iso(p.expired_at),
            } for p in plans],
        }


def _parse_dt(value, field: str):
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"导出文档中 {field} 不是合法时间: {value!r}") from None
    return dt


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


@app.post("/api/scenarios/import")
def import_scenario(doc: dict = Body(...)) -> dict:
    """导入导出文档，整体原子化：场景 + 校准 + 测量批次 + 计划记录一次写入，
    批次与计划的状态（confirmed/superseded、active/expired）保持导出时一致。"""
    from .schemas import CarrierIn
    try:
        _require(isinstance(doc, dict), "导入文档必须是 JSON 对象")
        _require(doc.get("format") == EXPORT_FORMAT,
                 f"文档 format 必须是 {EXPORT_FORMAT!r}")
        _require(doc.get("version") == EXPORT_VERSION,
                 f"不支持的导出文档版本: {doc.get('version')!r}")
        sc_doc = doc.get("scenario")
        _require(isinstance(sc_doc, dict), "缺少 scenario 对象")
        _require(isinstance(sc_doc.get("name"), str) and sc_doc["name"].strip(),
                 "scenario.name 缺失")
        carriers = [CarrierIn(**c) for c in sc_doc.get("carriers", [])]
        validate_masks(carriers)
        cal_docs = doc.get("calibrations", [])
        batch_docs = doc.get("measurement_batches", [])
        plan_docs = doc.get("plan_records", [])
        _require(isinstance(cal_docs, list) and isinstance(batch_docs, list)
                 and isinstance(plan_docs, list), "文档结构不完整")
        for b in batch_docs:
            _require(b.get("status") in ("confirmed", "superseded"),
                     f"批次 {b.get('batch_key')!r} 状态非法")
            curves = b.get("curves") or {}
            n = int(b.get("points_count") or 0)
            for key in ("freq_mhz", "raw_dbm_hz", "calibrated_dbm_hz",
                        "theory_dbm_hz", "deviation_db", "envelope_dbm_hz"):
                _require(isinstance(curves.get(key), list)
                         and len(curves[key]) == n,
                         f"批次 {b.get('batch_key')!r} 曲线 {key} 缺失或长度不符")
            _require(n >= 2, f"批次 {b.get('batch_key')!r} 点数不足")
        # 每个载波至多一个 confirmed 批次（确认结论唯一性）
        seen: set[str] = set()
        for b in batch_docs:
            if b["status"] == "confirmed":
                _require(b["carrier_name"] not in seen,
                         f"载波 {b['carrier_name']!r} 存在多个 confirmed 批次")
                seen.add(b["carrier_name"])
        for p in plan_docs:
            _require(p.get("status") in ("active", "expired"),
                     f"计划记录状态非法: {p.get('status')!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    with Session(engine) as s:
        name = sc_doc["name"].strip()
        if s.scalar(select(Scenario).where(Scenario.name == name)) is not None:
            raise HTTPException(409, f"场景名 {name!r} 已存在，请先删除或改名")

        # 校准：按 (name, version) 对齐；已存在则修正量必须一致（保证可复现）
        cal_map: dict[tuple[str, int], CalibrationVersion] = {}
        for c in cal_docs:
            try:
                curve = CalibrationCurve.from_factors(c["name"], int(c["version"]),
                                                      c["factors"])
            except (KeyError, ValueError) as e:
                raise HTTPException(400, f"校准条目非法: {e}")
            key = (curve.name, curve.version)
            row = s.scalar(select(CalibrationVersion).where(
                CalibrationVersion.name == curve.name,
                CalibrationVersion.version == curve.version))
            if row is not None:
                if [list(map(float, r)) for r in row.factors] != \
                        [list(map(float, r)) for r in c["factors"]]:
                    raise HTTPException(
                        409, f"校准 {curve.name!r} v{curve.version} 已存在且修正量不同，"
                             "拒绝覆盖（校准版本不可变）")
            else:
                row = CalibrationVersion(name=curve.name, version=curve.version,
                                         factors=[list(r) for r in c["factors"]],
                                         description=c.get("description", ""))
                s.add(row)
                s.flush()
            cal_map[key] = row

        sc = Scenario(
            name=name, description=sc_doc.get("description", ""),
            band_low_mhz=sc_doc.get("band_low_mhz", 80.0),
            band_high_mhz=sc_doc.get("band_high_mhz", 220.0),
            guard_required_mhz=sc_doc.get("guard_required_mhz", 1.0),
            leakage_limit_dbm=sc_doc.get("leakage_limit_dbm", -45.0),
            reuse_policy=dict(sc_doc.get("reuse_policy") or {}),
            carriers=[CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                                 bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                                 polarization=c.polarization, mask_name=c.mask_name)
                      for i, c in enumerate(carriers)],
        )
        s.add(sc)
        s.flush()

        for b in batch_docs:
            cal_key = (b["calibration"]["name"], int(b["calibration"]["version"]))
            cal = cal_map.get(cal_key) or s.scalar(select(CalibrationVersion).where(
                CalibrationVersion.name == cal_key[0],
                CalibrationVersion.version == cal_key[1]))
            if cal is None:
                s.rollback()
                raise HTTPException(400, f"批次 {b['batch_key']!r} 引用的校准 "
                                         f"{cal_key[0]!r} v{cal_key[1]} 不在文档中")
            s.add(MeasurementBatch(
                scenario_id=sc.id, batch_key=b["batch_key"],
                carrier_name=b["carrier_name"], sampled_at=b["sampled_at"],
                calibration_id=cal.id, content_hash=b["content_hash"],
                status=b["status"], violation=bool(b["violation"]),
                max_excess_db=float(b["max_excess_db"]),
                points_count=int(b["points_count"]), curves=b["curves"],
                imported_at=_parse_dt(b.get("imported_at"), "imported_at")
                or datetime.now(timezone.utc)))

        for p in plan_docs:
            rec = PlanRecord(
                scenario_id=sc.id, mode=p.get("mode", "guard_only"),
                post_check_basis=p.get("post_check_basis", "theory"),
                band_low_mhz=float(p.get("band_low_mhz", 0.0)),
                band_high_mhz=float(p.get("band_high_mhz", 0.0)),
                rules=p.get("rules") or {}, assignments=p.get("assignments") or [],
                post_check=p.get("post_check") or {}, feasible=bool(p.get("feasible")),
                status=p["status"], expired_reason=p.get("expired_reason", ""),
                expired_at=_parse_dt(p.get("expired_at"), "expired_at"))
            created = _parse_dt(p.get("created_at"), "created_at")
            if created is not None:
                rec.created_at = created
            s.add(rec)
        s.commit()
        return {"id": sc.id, "name": sc.name,
                "calibrations": len(cal_docs), "measurement_batches": len(batch_docs),
                "plan_records": len(plan_docs)}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": "offline simplified — no radio, no transmit commands"}
