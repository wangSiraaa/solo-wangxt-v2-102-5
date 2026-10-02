"""测量批次 / 校准版本 / 规划记录的持久化业务逻辑。

关键不变量：
- 批次导入在单事务内完成“完整校验 + 落库”，任何错误都回滚，不写半个批次。
- batch_ref 全局唯一且内容带哈希：相同批次重放幂等，不产生重复点；
  同一标识对应不同内容则冲突拒绝。
- 校准只追加新版本，从不修改旧版本；历史批次/报告引用具体版本快照。
- 规划记录只追加：结果快照永不改写，失效只置 stale 标志。
- 迟到的旧测量（采样时刻早于同载波已确认结论）永远不能确认覆盖新结论。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import (CalibrationVersion, MeasurementBatch, MeasurementPoint,
                 PlanRecord, Scenario)
from .schemas import CalibrationIn
from .services.masks import MASKS
from .services import measurements as M

PLAN_STALE_REASONS = {
    "calibration_revised": "校准已修订",
    "measurement_violation": "测量发现越限",
    "envelope_revised": "确认测量包络已更新",
}


# --- 校准版本 ---------------------------------------------------------------

def list_calibrations(s: Session) -> list[CalibrationVersion]:
    return list(s.scalars(select(CalibrationVersion).order_by(CalibrationVersion.id)))


def get_active_calibration(s: Session) -> Optional[CalibrationVersion]:
    return s.scalar(select(CalibrationVersion)
                    .where(CalibrationVersion.is_active.is_(True))
                    .order_by(CalibrationVersion.id.desc()))


def get_calibration_by_version(s: Session, version: str) -> Optional[CalibrationVersion]:
    return s.scalar(select(CalibrationVersion).where(
        CalibrationVersion.version == version))


def create_calibration_revision(s: Session, req: CalibrationIn
                                ) -> tuple[CalibrationVersion, int]:
    """新增校准修订：旧版本冻结、新版本成为当前版本；关联计划全部过期。

    返回 (新版本, 被标记过期的计划数)。版本号冲突抛 ValueError。
    """
    if get_calibration_by_version(s, req.version) is not None:
        raise ValueError(f"校准版本 {req.version!r} 已存在；修订必须使用新版本号")

    old_active = get_active_calibration(s)
    if old_active is not None:
        old_active.is_active = False
    cal = CalibrationVersion(version=req.version,
                             points=[[float(x), float(y)] for x, y in req.points],
                             description=req.description, is_active=True)
    s.add(cal)
    s.flush()
    # 只有引用“被替换的当前校准”的现行计划需要按新校准重评估；
    # 绑定更旧历史校准的记录保持其原过期原因/状态，历史内容不变。
    n = _mark_plans_stale(
        s, None, "calibration_revised",
        f"校准已修订为 {req.version}（旧版本 {old_active.version if old_active else '-'} 冻结）",
        calibration_version=old_active.version if old_active else None)
    return cal, n


def _cal_domain(cal: CalibrationVersion) -> M.Calibration:
    return M.Calibration(id=cal.id, version=cal.version,
                         points=tuple((float(x), float(y)) for x, y in cal.points),
                         description=cal.description)


# --- 规划记录失效 -----------------------------------------------------------

def _mark_plans_stale(s: Session, scenario_id: Optional[int], reason: str,
                      detail: str, calibration_version: Optional[str] = None
                      ) -> int:
    """把（某场景的或全部的）仍为现行的计划标记过期；已过期的保留首因。

    calibration_version 给定时只过期引用该校准版本的记录。
    """
    stmt = select(PlanRecord).where(PlanRecord.stale.is_(False))
    if scenario_id is not None:
        stmt = stmt.where(PlanRecord.scenario_id == scenario_id)
    if calibration_version is not None:
        stmt = stmt.where(PlanRecord.calibration_version == calibration_version)
    rows = list(s.scalars(stmt))
    for p in rows:
        p.stale = True
        p.stale_reason = reason
        p.stale_detail = detail
    return len(rows)


# --- 批次导入 ---------------------------------------------------------------

def _carrier_row(sc: Scenario, carrier_name: str):
    for c in sc.carriers:
        if c.name == carrier_name:
            return c
    return None


def import_sweep(s: Session, scenario_id: int, sweep: M.ParsedSweep,
                 default_status: str = "imported",
                 confirmed_at: Optional[datetime] = None
                 ) -> tuple[MeasurementBatch, bool]:
    """原子导入一个扫频批次。返回 (批次, 是否为重放)。

    任何校验错误抛 MeasurementFormatError / ValueError；调用方在单事务内
    回滚即可保证场景不变。
    """
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise ValueError("场景不存在")

    existing = s.scalar(select(MeasurementBatch).where(
        MeasurementBatch.batch_ref == sweep.batch_ref))
    if existing is not None:
        if existing.scenario_id != scenario_id:
            raise ValueError(
                f"批次标识 {sweep.batch_ref!r} 已被场景 {existing.scenario_id} 占用，"
                f"不能写入场景 {scenario_id}")
        # 同场景幂等重放：同内容直接返回原批次，不新增任何点；不同内容则冲突
        if existing.content_hash != sweep.content_hash:
            raise ValueError(
                f"批次标识 {sweep.batch_ref!r} 已被不同内容占用，拒绝写入")
        return existing, True

    cal = get_calibration_by_version(s, sweep.calibration_version)
    if cal is None:
        raise M.MeasurementFormatError(
            f"缺失校准：版本 {sweep.calibration_version!r} 不存在，"
            f"请先登记该校准修订再导入")

    crow = _carrier_row(sc, sweep.carrier_name)
    if crow is None:
        raise M.MeasurementFormatError(
            f"批次引用的载波 {sweep.carrier_name!r} 不在场景 {sc.name!r} 中")
    if crow.mask_name not in MASKS:
        raise M.MeasurementFormatError(f"载波引用了未知理论掩模 {crow.mask_name!r}")

    cal_d = _cal_domain(cal)
    # 覆盖范围检查在校准函数内（超出即 MeasurementFormatError）
    calibrated = M.calibrate(sweep.points, cal_d)

    evaluation = M.evaluate_against_theory(
        calibrated, mask_name=crow.mask_name, center_mhz=crow.center_mhz,
        bandwidth_mhz=crow.bandwidth_mhz, power_dbm=crow.power_dbm)

    # 迟到判定：同载波是否已存在采样时刻更晚的“已确认”结论
    newer_confirmed = s.scalar(select(MeasurementBatch).where(
        MeasurementBatch.scenario_id == scenario_id,
        MeasurementBatch.carrier_name == sweep.carrier_name,
        MeasurementBatch.status == "confirmed",
        MeasurementBatch.sampled_at > sweep.sampled_at).limit(1))
    arrived_late = newer_confirmed is not None

    status = default_status
    if status == "confirmed" and arrived_late:
        # 包导入恢复时也遵守迟到保护：旧批次只能归档，不能恢复成确认态
        status = "imported"

    batch = MeasurementBatch(
        batch_ref=sweep.batch_ref, scenario_id=scenario_id,
        carrier_name=sweep.carrier_name, calibration_id=cal.id,
        sampled_at=sweep.sampled_at, status=status,
        mask_name=crow.mask_name,
        carrier_snapshot={"name": crow.name, "center_mhz": crow.center_mhz,
                          "bandwidth_mhz": crow.bandwidth_mhz,
                          "power_dbm": crow.power_dbm,
                          "polarization": crow.polarization,
                          "mask_name": crow.mask_name},
        calibration_version=cal.version,
        calibration_points_snapshot=[list(p) for p in cal_d.points],
        calibrated_curve=calibrated,
        deviations=evaluation["deviations"],
        violations=evaluation["violations"],
        max_excess_db=evaluation["max_excess_db"],
        has_violation=evaluation["has_violation"],
        content_hash=sweep.content_hash, arrived_late=arrived_late,
        confirmed_at=confirmed_at if status == "confirmed" else None,
    )
    # 理论曲线与逐点偏差同网格，一并持久化用于绘图/导出复现
    batch.theory_curve = evaluation["theory_curve"]
    s.add(batch)
    s.flush()
    s.add_all([
        MeasurementPoint(batch_id=batch.id, position=i, f_mhz=f,
                         power_dbm_hz=p)
        for i, (f, p) in enumerate(sweep.points)
    ])
    s.flush()

    # 测量发现越限：关联计划过期（历史结果不篡改）
    if batch.has_violation:
        _mark_plans_stale(s, scenario_id, "measurement_violation",
                          f"批次 {batch.batch_ref}（载波 {batch.carrier_name}）"
                          f"测得曲线高出理论掩模，最大偏差 {batch.max_excess_db} dB")

    # 恢复生命周期：若恢复为 confirmed，取代同载波更早的确认批次
    if status == "confirmed":
        _apply_confirmation(s, batch, _already_stamped=True)
    return batch, False


def confirm_batch(s: Session, batch_id: int) -> MeasurementBatch:
    """确认一个批次：取代同载波旧的确认结论；迟到旧批次拒绝确认。"""
    batch = s.get(MeasurementBatch, batch_id)
    if batch is None:
        raise ValueError("批次不存在")
    if batch.status == "confirmed":
        return batch  # 幂等
    if batch.status == "superseded":
        raise ValueError("该批次已被更新批次取代，不能重新确认")

    newer = list(s.scalars(select(MeasurementBatch).where(
        MeasurementBatch.scenario_id == batch.scenario_id,
        MeasurementBatch.carrier_name == batch.carrier_name,
        MeasurementBatch.status == "confirmed",
        MeasurementBatch.sampled_at > batch.sampled_at)))
    if newer:
        # 迟到保护：旧测量不能覆盖较新的已确认结论
        batch.arrived_late = True
        raise ValueError(
            f"该批次采样于 {batch.sampled_at.isoformat()}，早于载波 "
            f"{batch.carrier_name} 当前已确认结论 "
            f"{newer[0].sampled_at.isoformat()}（{newer[0].batch_ref}），"
            f"迟到旧测量不得覆盖新结论")

    batch.status = "confirmed"
    batch.confirmed_at = datetime.now(timezone.utc)
    _apply_confirmation(s, batch)
    return batch


def _apply_confirmation(s: Session, batch: MeasurementBatch,
                        _already_stamped: bool = False) -> None:
    """把同载波旧确认批次置 superseded，并让用到旧包络的计划过期。"""
    olds = list(s.scalars(select(MeasurementBatch).where(
        MeasurementBatch.scenario_id == batch.scenario_id,
        MeasurementBatch.carrier_name == batch.carrier_name,
        MeasurementBatch.status == "confirmed",
        MeasurementBatch.id != batch.id)))
    for old in olds:
        old.status = "superseded"
        batch.supersedes_batch_id = old.id

    # 使用过“测量包络复核”的关联计划：包络变了需要重新评估
    plans = list(s.scalars(select(PlanRecord).where(
        PlanRecord.scenario_id == batch.scenario_id,
        PlanRecord.stale.is_(False),
        PlanRecord.used_measured_envelope.is_(True))))
    for p in plans:
        p.stale = True
        p.stale_reason = "envelope_revised"
        p.stale_detail = (f"载波 {batch.carrier_name} 的确认测量包络更新为 "
                          f"{batch.batch_ref}")

    # 确认的批次本身越限：所有关联计划过期（导入时通常已标记，此处兜底）
    if batch.has_violation:
        _mark_plans_stale(s, batch.scenario_id, "measurement_violation",
                          f"批次 {batch.batch_ref}（载波 {batch.carrier_name}）"
                          f"测得越限，最大偏差 {batch.max_excess_db} dB")


# --- 查询 / 包络 ------------------------------------------------------------

def list_batches(s: Session, scenario_id: Optional[int] = None
                 ) -> list[MeasurementBatch]:
    stmt = select(MeasurementBatch).order_by(
        MeasurementBatch.scenario_id, MeasurementBatch.carrier_name,
        MeasurementBatch.sampled_at)
    if scenario_id is not None:
        stmt = stmt.where(MeasurementBatch.scenario_id == scenario_id)
    return list(s.scalars(stmt))


def confirmed_batches_by_carrier(s: Session, scenario_id: int
                                 ) -> dict[str, MeasurementBatch]:
    rows = list(s.scalars(select(MeasurementBatch).where(
        MeasurementBatch.scenario_id == scenario_id,
        MeasurementBatch.status == "confirmed")))
    return {b.carrier_name: b for b in rows}


def build_envelopes(s: Session, scenario_id: int
                    ) -> tuple[dict[str, list[tuple[float, float]]], dict[str, int]]:
    """返回 {载波名: 保守包络曲线 [(offset_mhz, dBm/Hz)]} 与 {载波名: 批次 id}。

    包络 = max(理论 PSD, 校准后测量)（dB 域取最大 = 线性域最差情况）。
    频率以相对载波中心的偏移表示：规划移动载波后，包络随载波一起平移，
    post-check 与约束反算用的是同一条“发射形状”。
    """
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise ValueError("场景不存在")
    confirmed = confirmed_batches_by_carrier(s, scenario_id)
    envelopes: dict[str, list[tuple[float, float]]] = {}
    ids: dict[str, int] = {}
    for crow in sc.carriers:
        b = confirmed.get(crow.name)
        if b is None:
            continue
        ref_center = float(b.carrier_snapshot.get("center_mhz", crow.center_mhz))
        theory_pts = [(float(f) - ref_center, float(p))
                      for f, p in (b.theory_curve or [])]
        measured = [(float(f) - ref_center, float(p))
                    for f, p in b.calibrated_curve]
        envelopes[crow.name] = M.envelope_curve([measured], fallback=theory_pts)
        ids[crow.name] = b.id
    return envelopes, ids


def carrier_overlay(s: Session, sc: Scenario, grid_step_mhz: float = 0.05) -> list[dict]:
    """组装分析页叠加数据：理论曲线 / 校准后测量 / 保守包络。"""
    confirmed = confirmed_batches_by_carrier(s, sc.id)
    out = []
    for crow in sorted(sc.carriers, key=lambda c: c.position):
        b = confirmed.get(crow.name)
        f_lo = crow.center_mhz - MASKS[crow.mask_name].span_mhz
        f_hi = crow.center_mhz + MASKS[crow.mask_name].span_mhz
        f_grid = list(np.arange(f_lo, f_hi + grid_step_mhz / 2, grid_step_mhz))
        theory = M.theory_psd(crow.mask_name, f_grid, crow.center_mhz,
                              crow.bandwidth_mhz, crow.power_dbm)
        item = {
            "carrier_name": crow.name,
            "center_mhz": crow.center_mhz, "bandwidth_mhz": crow.bandwidth_mhz,
            "power_dbm": crow.power_dbm, "polarization": crow.polarization,
            "mask_name": crow.mask_name,
            "f_mhz": [round(float(x), 4) for x in f_grid],
            "theory_dbm_hz": [round(float(x), 2) for x in theory],
            "measured_dbm_hz": None,
            "envelope_dbm_hz": [round(float(x), 2) for x in theory],
            "confirmed_batch": None,
            "has_violation": False,
            "max_excess_db": 0.0,
        }
        if b is not None:
            mf = np.asarray([p[0] for p in b.calibrated_curve], dtype=float)
            mp = np.asarray([p[1] for p in b.calibrated_curve], dtype=float)
            meas = np.interp(np.asarray(f_grid), mf, mp,
                             left=np.nan, right=np.nan)
            env = np.maximum(theory, np.where(np.isnan(meas), -np.inf, meas))
            item.update({
                "measured_dbm_hz": [None if np.isnan(x) else round(float(x), 2)
                                    for x in meas],
                "envelope_dbm_hz": [round(float(x), 2) for x in env],
                "confirmed_batch": batch_summary(b) | {"point_count": len(b.points)},
                "has_violation": b.has_violation,
                "max_excess_db": b.max_excess_db,
            })
        out.append(item)
    return out


# --- 序列化 -----------------------------------------------------------------

def batch_summary(b: MeasurementBatch) -> dict:
    return {
        "id": b.id, "batch_ref": b.batch_ref, "scenario_id": b.scenario_id,
        "carrier_name": b.carrier_name,
        "calibration_version": b.calibration_version,
        "sampled_at": b.sampled_at.isoformat() if b.sampled_at else None,
        "status": b.status, "arrived_late": b.arrived_late,
        "has_violation": b.has_violation, "max_excess_db": b.max_excess_db,
        "point_count": len(b.points),
        "supersedes_batch_id": b.supersedes_batch_id,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "confirmed_at": b.confirmed_at.isoformat() if b.confirmed_at else None,
    }


def batch_detail(b: MeasurementBatch) -> dict:
    d = batch_summary(b)
    d.update({
        "mask_name": b.mask_name,
        "carrier_snapshot": b.carrier_snapshot,
        "calibration_points_snapshot": b.calibration_points_snapshot,
        "raw_points": [[p.f_mhz, p.power_dbm_hz] for p in sorted(b.points, key=lambda x: x.position)],
        "calibrated_curve": b.calibrated_curve,
        "theory_curve": b.theory_curve,
        "deviations": b.deviations,
        "violations": b.violations,
    })
    return d


def calibration_out(c: CalibrationVersion) -> dict:
    return {
        "id": c.id, "version": c.version, "points": c.points,
        "description": c.description, "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def plan_record_out(p: PlanRecord) -> dict:
    return {
        "id": p.id, "scenario_id": p.scenario_id, "mode": p.mode,
        "used_measured_envelope": p.used_measured_envelope,
        "envelope_batch_ids": p.envelope_batch_ids or [],
        "calibration_version": p.calibration_version,
        "stale": p.stale, "stale_reason": p.stale_reason,
        "stale_label": PLAN_STALE_REASONS.get(p.stale_reason or "", p.stale_reason),
        "stale_detail": p.stale_detail or "",
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "feasible": bool(p.result_snapshot.get("feasible")),
        "result": p.result_snapshot,
    }


# --- 规划记录持久化 ----------------------------------------------------------

def persist_plan(s: Session, *, scenario_id: Optional[int], mode: str,
                 used_measured_envelope: bool, envelope_ids: dict[str, int],
                 request_snapshot: dict, result: dict,
                 calibration_version: str) -> PlanRecord:
    rec = PlanRecord(
        scenario_id=scenario_id, mode=mode,
        used_measured_envelope=used_measured_envelope,
        envelope_batch_ids=sorted(envelope_ids.values()),
        calibration_version=calibration_version,
        request_snapshot=request_snapshot, result_snapshot=result)
    s.add(rec)
    s.flush()
    return rec


# --- 导出 / 导入包 -----------------------------------------------------------

def export_bundle(s: Session, scenario_id: int) -> dict:
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise ValueError("场景不存在")
    batches = list_batches(s, scenario_id)
    cal_ids = {b.calibration_id for b in batches}
    cals = [s.get(CalibrationVersion, cid_) for cid_ in sorted(cal_ids)]
    return {
        "format": "spectrum-workbench-measurements/1",
        "scenario": {"name": sc.name, "band_low_mhz": sc.band_low_mhz,
                     "band_high_mhz": sc.band_high_mhz},
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "calibrations": [calibration_out(c) for c in cals],
        "batches": [batch_detail(b) for b in batches],
    }


def import_bundle(s: Session, scenario_id: int, bundle: dict) -> dict:
    """整包原子导入：任何错误全部回滚。恢复确认/取代生命周期与迟到标志。"""
    if not isinstance(bundle, dict) or not str(
            bundle.get("format", "")).startswith("spectrum-workbench-measurements"):
        raise M.MeasurementFormatError("不是本工作台导出的测量包（format 不匹配）")

    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise ValueError("场景不存在")

    # 1) 校准版本缺失则按原版本号补登记（不改本地当前版本指针）
    for c in bundle.get("calibrations", []):
        if get_calibration_by_version(s, c["version"]) is None:
            points = [[float(x), float(y)] for x, y in c["points"]]
            s.add(CalibrationVersion(version=c["version"], points=points,
                                     description=c.get("description", "") +
                                                 "（导入自测量包）",
                                     is_active=get_active_calibration(s) is None))

    # 2) 先全部落库（imported 态，完整校验链路由 import_sweep 保证）
    imported: list[MeasurementBatch] = []
    replayed = 0
    for bd in bundle.get("batches", []):
        sweep = M.parse_sweep_json({
            "batch_ref": bd["batch_ref"], "carrier_name": bd["carrier_name"],
            "sampled_at": bd["sampled_at"],
            "calibration_version": bd["calibration_version"],
            "points": bd["raw_points"],
        })
        batch, was_replay = import_sweep(s, scenario_id, sweep)
        replayed += int(was_replay)
        imported.append(batch)

    # 3) 按原确认时间顺序恢复 confirmed/superseded 生命周期（迟到保护仍生效）
    by_ref = {b.batch_ref: b for b in imported}
    confirm_order = sorted(
        (bd for bd in bundle["batches"] if bd["status"] == "confirmed"),
        key=lambda bd: bd.get("confirmed_at") or bd["sampled_at"])
    confirmed_now = 0
    for bd in confirm_order:
        batch = by_ref[bd["batch_ref"]]
        if batch.status == "confirmed":
            continue
        try:
            confirm_batch(s, batch.id)
            confirmed_now += 1
        except ValueError:
            # 迟到批次在原库里可能已是 imported/arrived_late；保持归档态
            pass

    # 4) 回填迟到标志：同载波存在更晚已确认结论的未确认批次都算迟到
    all_batches = list_batches(s, scenario_id)
    confirmed_latest: dict[str, datetime] = {}
    for b in all_batches:
        if b.status == "confirmed":
            cur = confirmed_latest.get(b.carrier_name)
            if cur is None or b.sampled_at > cur:
                confirmed_latest[b.carrier_name] = b.sampled_at
    for b in all_batches:
        if b.status != "confirmed":
            latest = confirmed_latest.get(b.carrier_name)
            if latest is not None and b.sampled_at < latest:
                b.arrived_late = True

    return {
        "total": len(bundle.get("batches", [])),
        "replayed": replayed,
        "confirmed": confirmed_now,
        "batches": [batch_summary(b) for b in list_batches(s, scenario_id)],
    }
