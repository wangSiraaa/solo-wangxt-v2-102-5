"""SQLAlchemy 模型：场景、载波、示例频谱掩模、校准版本、测量批次、计划记录。"""
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
    """校准版本：频率相关修正量 (dB)。不可变——修订校准即新建版本，
    历史测量批次仍引用其导入时的版本，保证历史报告可按原校准复现。"""
    __tablename__ = "calibration_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_cal_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # [[freq_mhz, offset_db], ...] 频率严格递增，分段线性、端点外持平
    factors: Mapped[list] = mapped_column(JSON, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class MeasurementBatch(Base):
    """一次离线扫频导入的测量批次（原子写入；重放幂等；迟到旧批次不改结论）。

    curves 为列式 JSON：freq_mhz / raw_dbm_hz（原始记录）/ calibrated_dbm_hz
    （校准后曲线）/ theory_dbm_hz 与 deviation_db（对当前理论掩模的偏差，
    理论无定义处为 null）/ envelope_dbm_hz（保守包络）。
    """
    __tablename__ = "measurement_batches"
    __table_args__ = (UniqueConstraint("scenario_id", "batch_key", name="uq_batch_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False)
    batch_key: Mapped[str] = mapped_column(String(128), nullable=False)
    carrier_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 采样时刻：UTC ISO-8601 字符串（固定微秒宽度，字典序即时间序）
    sampled_at: Mapped[str] = mapped_column(String(40), nullable=False)
    calibration_id: Mapped[int] = mapped_column(
        ForeignKey("calibration_versions.id"), nullable=False)
    # 规范化内容的 SHA-256：同批次重放幂等判定
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # confirmed = 该载波当前确认结论；superseded = 被更新批次取代 / 迟到旧批次
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    violation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_excess_db: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    points_count: Mapped[int] = mapped_column(Integer, nullable=False)
    curves: Mapped[dict] = mapped_column(JSON, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    calibration: Mapped[CalibrationVersion] = relationship()


class PlanRecord(Base):
    """一次规划运行的持久化报告。post_check 为历史结果，永不修改；
    测量越限或校准更新时只翻转 status（active -> expired）并记录原因。"""
    __tablename__ = "plan_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # theory / measurement_envelope
    post_check_basis: Mapped[str] = mapped_column(String(24), nullable=False,
                                                  default="theory")
    band_low_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    band_high_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    rules: Mapped[dict] = mapped_column(JSON, nullable=False)
    assignments: Mapped[list] = mapped_column(JSON, nullable=False)
    post_check: Mapped[dict] = mapped_column(JSON, nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expired_reason: Mapped[str] = mapped_column(Text, default="")
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
