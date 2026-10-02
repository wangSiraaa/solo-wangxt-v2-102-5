"""SQLAlchemy 模型：场景、载波、示例频谱掩模、校准版本、测量批次、规划记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    band_low_mhz: Mapped[float] = mapped_column(Float, default=80.0)
    band_high_mhz: Mapped[float] = mapped_column(Float, default=220.0)
    guard_required_mhz: Mapped[float] = mapped_column(Float, default=1.0)
    leakage_limit_dbm: Mapped[float] = mapped_column(Float, default=-45.0)
    # 极化复用规则，如 {"H|V": "unknown", "RHCP|V": "allowed"}
    reuse_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    carriers: Mapped[list["CarrierRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="CarrierRow.position")

    batches: Mapped[list["MeasurementBatch"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="MeasurementBatch.sampled_at")
    plans: Mapped[list["PlanRecord"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="PlanRecord.created_at")


class CarrierRow(Base):
    __tablename__ = "carriers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    center_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    bandwidth_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    power_dbm: Mapped[float] = mapped_column(Float, nullable=False)
    polarization: Mapped[str] = mapped_column(String(8), nullable=False)
    mask_name: Mapped[str] = mapped_column(String(32), nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="carriers")


class MaskRow(Base):
    """示例频谱发射掩模：名称 + 折线点 + 说明（教学示例）。"""
    __tablename__ = "masks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 一侧（非负偏移）折线点 [[offset_mhz, attenuation_db], ...]
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    span_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


class CalibrationVersion(Base):
    """频谱仪校准修订（不可变）。

    points: [[f_mhz, gain_db], ...]，按 f_mhz 严格升序；测量点经分段线性
    增益修正后得到“校准后曲线”。历史批次引用具体版本 id，修订只新增行，
    从而旧报告始终能按原校准复现。
    """
    __tablename__ = "calibration_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    batches: Mapped[list["MeasurementBatch"]] = relationship(
        back_populates="calibration")


class MeasurementBatch(Base):
    """离线扫频测量批次（一个批次对应场景中一个载波的一次扫频）。

    原子写入：导入时先完整校验（格式、严格单调频率、校准版本存在且覆盖
    全部测点），任何错误整批拒绝，不产生半个批次。
    batch_ref 全局唯一：相同批次重放幂等，不产生重复点。
    """
    __tablename__ = "measurement_batches"
    __table_args__ = (UniqueConstraint("batch_ref", name="uq_batch_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"))
    carrier_name: Mapped[str] = mapped_column(String(64), nullable=False)
    calibration_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.id"))
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    # imported（原始归档，不参与包络）/ confirmed（经确认，可用于 post-check）
    # / superseded（已被同载波更晚的确认批次取代）
    status: Mapped[str] = mapped_column(String(16), default="imported",
                                        nullable=False)
    # 导入时的快照：掩模名/版本、载波参数、当前理论掩模曲线、校准后曲线、
    # 逐点偏差、越限点与最大偏差；保证校准/场景后续变化时报告仍可复现。
    mask_name: Mapped[str] = mapped_column(String(32), nullable=False)
    carrier_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    calibration_version: Mapped[str] = mapped_column(String(64), nullable=False)
    calibration_points_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    calibrated_curve: Mapped[list] = mapped_column(JSON, default=list)
    theory_curve: Mapped[list] = mapped_column(JSON, default=list)
    deviations: Mapped[list] = mapped_column(JSON, default=list)
    violations: Mapped[list] = mapped_column(JSON, default=list)
    max_excess_db: Mapped[float] = mapped_column(Float, default=0.0)
    has_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 迟到保护：同载波已有更晚采样时刻的确认结论时为 True；
    # 这种批次可以归档导入，但永远不能确认覆盖新结论。
    arrived_late: Mapped[bool] = mapped_column(Boolean, default=False)
    supersedes_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurement_batches.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    scenario: Mapped[Scenario] = relationship(back_populates="batches")
    calibration: Mapped[CalibrationVersion] = relationship(back_populates="batches")
    points: Mapped[list["MeasurementPoint"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan",
        order_by="MeasurementPoint.position")


class MeasurementPoint(Base):
    """测量批次的原始采样点（未经校准修正的原始读数，原样持久化）。"""
    __tablename__ = "measurement_points"
    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_batch_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("measurement_batches.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    f_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    power_dbm_hz: Mapped[float] = mapped_column(Float, nullable=False)

    batch: Mapped[MeasurementBatch] = relationship(back_populates="points")


class PlanRecord(Base):
    """规划结果的历史快照（只追加，从不篡改内容）。

    校准修订或关联测量发现越限时，仅把 stale 置 True 并写明原因，
    保存的 request/result 保持不变，旧报告按原口径复现。
    """
    __tablename__ = "plan_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    used_measured_envelope: Mapped[bool] = mapped_column(Boolean, default=False)
    envelope_batch_ids: Mapped[list] = mapped_column(JSON, default=list)
    calibration_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    result_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stale_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    scenario: Mapped[Scenario | None] = relationship(back_populates="plans")
