"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

POLARIZATIONS = ("H", "V", "LHCP", "RHCP")
REUSE_VALUES = ("forbidden", "allowed", "unknown")


class CarrierIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    center_mhz: float = Field(gt=0)
    bandwidth_mhz: float = Field(gt=0)
    power_dbm: float
    polarization: Literal["H", "V", "LHCP", "RHCP"]
    mask_name: str = "strict"

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("载波名不能为空")
        return v


class RulesIn(BaseModel):
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    # key 形如 "H|V"（极化名按字母排序后拼接），value forbidden/allowed/unknown
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 绘图网格步长 (MHz)
    plot_grid_mhz: float = Field(default=0.05, gt=0, le=1.0)


class PlanRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    mode: Literal["guard_only", "mask_aware"] = "guard_only"
    # 关联场景：提供时规划结果持久化为计划记录（可被测量/校准事件置为过期）
    scenario_id: Optional[int] = None
    # post-check 基准：理论掩模 / 经确认的实测保守包络
    post_check_basis: Literal["theory", "measurement_envelope"] = "theory"

    def validate_band(self) -> None:
        if self.band_high_mhz <= self.band_low_mhz:
            raise ValueError("band_high_mhz 必须大于 band_low_mhz")


class CarrierOut(BaseModel):
    id: Optional[int] = None
    name: str
    center_mhz: float
    bandwidth_mhz: float
    power_dbm: float
    polarization: str
    mask_name: str


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)
    carriers: list[CarrierIn] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    id: int
    name: str
    description: str
    carrier_count: int
    created_at: Optional[str] = None


class ScenarioOut(BaseModel):
    id: int
    name: str
    description: str
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float
    leakage_limit_dbm: float
    reuse_policy: dict[str, str]
    carriers: list[CarrierOut]


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str


# ---- 校准版本 ---------------------------------------------------------------

class CalibrationIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # [[freq_mhz, offset_db], ...] 频率严格递增
    factors: list[list[float]] = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("校准名称不能为空")
        return v


class CalibrationOut(BaseModel):
    id: int
    name: str
    version: int
    factors: list[list[float]]
    description: str
    created_at: Optional[str] = None


# ---- 测量批次 ---------------------------------------------------------------

class MeasurementImportIn(BaseModel):
    filename: str = ""
    content: str = Field(min_length=1)


class MeasurementBatchOut(BaseModel):
    id: int
    scenario_id: int
    batch_key: str
    carrier_name: str
    sampled_at: str
    calibration_name: str
    calibration_version: int
    status: str            # confirmed / superseded
    violation: bool
    max_excess_db: float
    points_count: int
    imported_at: Optional[str] = None


class MeasurementBatchDetail(MeasurementBatchOut):
    # 列式曲线：原始记录 / 校准后 / 理论 / 偏差 / 保守包络
    curves: dict


class MeasurementImportResult(BaseModel):
    # confirmed=成为当前结论；superseded=迟到旧批次被归档；duplicate=幂等重放
    outcome: Literal["confirmed", "superseded", "duplicate"]
    batch: MeasurementBatchOut
    superseded_batch_ids: list[int] = []
    expired_plan_ids: list[int] = []
    message: str = ""


# ---- 计划记录 ---------------------------------------------------------------

class PlanRecordOut(BaseModel):
    id: int
    scenario_id: int
    created_at: Optional[str] = None
    mode: str
    post_check_basis: str
    feasible: bool
    status: str            # active / expired
    expired_reason: str = ""
    expired_at: Optional[str] = None
    counts: Optional[dict] = None


class PlanRecordDetail(PlanRecordOut):
    band_low_mhz: float
    band_high_mhz: float
    rules: dict
    assignments: list
    post_check: dict
