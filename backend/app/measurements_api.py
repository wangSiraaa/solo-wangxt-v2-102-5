"""测量批次、校准版本、规划记录的 HTTP 接口。

导入类接口在单事务内完成“解析 → 完整校验 → 落库”，任何错误回滚，
保证坏文件不会留下半个批次、不污染既有场景。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import measurement_io as io
from . import database

from .assemble import bands_view, build_spectrum, to_domain, to_rules, validate_masks
from .db import MeasurementBatch, PlanRecord, Scenario
from .schemas import (CalibrationIn, MeasurementBatchIn, PlanRequestV2)
from .services.analysis import Carrier, analyze
from .services import measurements as M
from .services.planner import BandLimits, plan

router = APIRouter(prefix="/api", tags=["measurements"])


# --- 校准版本 ----------------------------------------------------------------

@router.get("/calibrations")
def calibrations_list() -> list[dict]:
    with Session(database.engine) as s:
        return [io.calibration_out(c) for c in io.list_calibrations(s)]


@router.post("/calibrations")
def calibration_create(req: CalibrationIn) -> dict:
    """校准修订：只新增版本，不改写历史；关联计划全部过期。"""
    with Session(database.engine) as s:
        try:
            cal, n_stale = io.create_calibration_revision(s, req)
            s.commit()
            out = io.calibration_out(cal)
            out["plans_marked_stale"] = n_stale
            return out
        except ValueError as e:
            s.rollback()
            raise HTTPException(409, str(e))


# --- 场景测量总览（分析页叠加） ----------------------------------------------

@router.get("/scenarios/{scenario_id}/measurements/overlay")
def measurement_overlay(scenario_id: int) -> dict:
    with Session(database.engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        active = io.get_active_calibration(s)
        return {
            "scenario_id": scenario_id,
            "active_calibration_version": active.version if active else None,
            "carriers": io.carrier_overlay(s, sc),
        }


# --- 批次导入 / 查询 / 确认 ---------------------------------------------------

@router.get("/scenarios/{scenario_id}/measurements/batches")
def batches_list(scenario_id: int) -> list[dict]:
    with Session(database.engine) as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        return [io.batch_summary(b) for b in io.list_batches(s, scenario_id)]


@router.get("/measurements/batches/{batch_id}")
def batch_get(batch_id: int) -> dict:
    with Session(database.engine) as s:
        b = s.get(MeasurementBatch, batch_id)
        if b is None:
            raise HTTPException(404, "批次不存在")
        return io.batch_detail(b)


class ImportTextIn(BaseModel):
    text: str
    format: str = "csv"


@router.post("/scenarios/{scenario_id}/measurements/import-text")
def batch_import_text(scenario_id: int, req: ImportTextIn) -> dict:
    """导入课堂扫频 CSV 文本：原子事务，坏行/非单调/缺校准整体拒绝。"""
    try:
        if req.format != "csv":
            raise M.MeasurementFormatError(f"不支持的格式 {req.format!r}")
        sweep = M.parse_sweep_csv(req.text)
    except M.MeasurementFormatError as e:
        raise HTTPException(400, f"文件校验失败：{e}")
    with Session(database.engine) as s:
        try:
            batch, replayed = io.import_sweep(s, scenario_id, sweep)
            s.commit()
        except M.MeasurementFormatError as e:
            s.rollback()
            raise HTTPException(400, f"导入被拒绝：{e}")
        except ValueError as e:
            s.rollback()
            raise HTTPException(409, str(e))
        return {"replayed": replayed, "batch": io.batch_summary(batch)}


@router.post("/scenarios/{scenario_id}/measurements/batches")
def batch_import_json(scenario_id: int, req: MeasurementBatchIn) -> dict:
    """JSON 直传（与 CSV 同一套校验/幂等/迟到逻辑）。"""
    try:
        sweep = M.parse_sweep_json(req.model_dump())
    except M.MeasurementFormatError as e:
        raise HTTPException(400, f"文件校验失败：{e}")
    with Session(database.engine) as s:
        try:
            batch, replayed = io.import_sweep(s, scenario_id, sweep)
            s.commit()
        except M.MeasurementFormatError as e:
            s.rollback()
            raise HTTPException(400, f"导入被拒绝：{e}")
        except ValueError as e:
            s.rollback()
            raise HTTPException(409, str(e))
        return {"replayed": replayed, "batch": io.batch_summary(batch)}


@router.post("/measurements/batches/{batch_id}/confirm")
def batch_confirm(batch_id: int) -> dict:
    with Session(database.engine) as s:
        try:
            b = io.confirm_batch(s, batch_id)
            s.commit()
            return io.batch_summary(b)
        except ValueError as e:
            s.rollback()
            raise HTTPException(409, str(e))


# --- 规划（可选测量包络 post-check）+ 历史记录 -------------------------------

@router.post("/plan-runs")
def plan_run(req: PlanRequestV2) -> dict:
    scenario_id = req.scenario_id
    with Session(database.engine) as s:
        try:
            req.validate_band()
            validate_masks(req.carriers)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        carriers = [to_domain(c) for c in req.carriers]
        rules = to_rules(req.rules)
        envelopes: dict[str, list] = {}
        envelope_ids: dict[str, int] = {}
        active = io.get_active_calibration(s)
        cal_version = active.version if active else "uncalibrated"

        if req.use_measured_envelope:
            if scenario_id is None:
                raise HTTPException(400, "使用测量包络复核必须指定 scenario_id")
            envelopes, envelope_ids = io.build_envelopes(s, scenario_id)
            if not envelopes:
                raise HTTPException(
                    400, "该场景还没有任何经确认的测量批次，无法用测量包络复核")
            mode = "measured_envelope"
        else:
            mode = req.mode

        try:
            result = plan(carriers, rules,
                          BandLimits(req.band_low_mhz, req.band_high_mhz),
                          mode=mode, psd_overrides=envelopes)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"规划失败：{e}")

        if result["feasible"]:
            planned = [
                Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                        bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                        polarization=a["polarization"], mask_name=a["mask_name"])
                for a in result["assignments"]
            ]
            # post-check 与约束口径一致：用同一确认测量包络复核
            result["post_check"] = analyze(planned, rules,
                                           psd_overrides=envelopes or None)
            result["bands"] = bands_view(planned)
            result["spectrum"] = build_spectrum(planned, 0.05)
            result["post_check_basis"] = (
                "confirmed_measured_envelope" if envelopes else "theoretical_mask")
            result["envelope_batch_ids"] = sorted(envelope_ids.values())

        rec = io.persist_plan(
            s, scenario_id=scenario_id, mode=mode,
            used_measured_envelope=bool(envelopes),
            envelope_ids=envelope_ids,
            request_snapshot=req.model_dump(), result=result,
            calibration_version=cal_version)
        s.commit()
        return {"plan_record_id": rec.id, "calibration_version": cal_version,
                **result}


@router.get("/scenarios/{scenario_id}/plan-runs")
def plan_runs_list(scenario_id: int) -> list[dict]:
    with Session(database.engine) as s:
        rows = list(s.scalars(select(PlanRecord)
                              .where(PlanRecord.scenario_id == scenario_id)
                              .order_by(PlanRecord.created_at.desc(), PlanRecord.id.desc())))
        return [io.plan_record_out(p) for p in rows]


@router.get("/plan-runs/{record_id}")
def plan_run_get(record_id: int) -> dict:
    with Session(database.engine) as s:
        p = s.get(PlanRecord, record_id)
        if p is None:
            raise HTTPException(404, "规划记录不存在")
        return io.plan_record_out(p)


# --- 导出 / 导入测量包 -------------------------------------------------------

@router.get("/scenarios/{scenario_id}/measurements/export")
def measurements_export(scenario_id: int) -> dict:
    with Session(database.engine) as s:
        try:
            return io.export_bundle(s, scenario_id)
        except ValueError as e:
            raise HTTPException(404, str(e))


class BundleIn(BaseModel):
    bundle: dict


@router.post("/scenarios/{scenario_id}/measurements/import-bundle")
def measurements_import(scenario_id: int, req: BundleIn) -> dict:
    """整包原子导入/恢复：任何批次或校准非法则全部回滚，状态与导出时一致。"""
    with Session(database.engine) as s:
        try:
            out = io.import_bundle(s, scenario_id, req.bundle)
            s.commit()
            return out
        except M.MeasurementFormatError as e:
            s.rollback()
            raise HTTPException(400, f"测量包校验失败：{e}")
        except ValueError as e:
            s.rollback()
            raise HTTPException(409, str(e))
