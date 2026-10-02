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


# ---- 校准版本 --------------------------------------------------------------

class CalibrationIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    # [[f_mhz, gain_db], ...]，频率严格单调升，至少 2 点
    points: list[list[float]] = Field(min_length=2)
    description: str = ""

    @field_validator("version")
    @classmethod
    def defuzz_version(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("校准版本号不能为空")
        return v

    @field_validator("points")
    @classmethod
    def check_points(cls, v: list[list[float]]) -> list[list[float]]:
        prev = None
        for p in v:
            if len(p) != 2:
                raise ValueError("每个校准点必须是 [f_mhz, gain_db]")
            f, g = float(p[0]), float(p[1])
            if not (f == f and g == g):  # NaN 检查
                raise ValueError("校准点不能为 NaN")
            if prev is not None and f <= prev:
                raise ValueError("校准点频率必须严格单调递增")
            prev = f
        return v


class CalibrationOut(BaseModel):
    id: int
    version: str
    points: list[list[float]]
    description: str
    is_active: bool
    created_at: Optional[str] = None


# ---- 测量批次 --------------------------------------------------------------

class MeasurementPointIn(BaseModel):
    f_mhz: float
    power_dbm_hz: float


class MeasurementBatchIn(BaseModel):
    """离线扫频文件（JSON 直传；CSV 由 /import-text 入口先解析成同一结构）。"""
    batch_ref: str = Field(min_length=1, max_length=128)
    carrier_name: str = Field(min_length=1, max_length=64)
    sampled_at: str = Field(description="ISO-8601，可带时区")
    calibration_version: str = Field(min_length=1, max_length=64)
    points: list[MeasurementPointIn] = Field(min_length=2)


class MeasurementBatchSummary(BaseModel):
    id: int
    batch_ref: str
    scenario_id: int
    carrier_name: str
    calibration_version: str
    sampled_at: str
    status: str
    arrived_late: bool
    has_violation: bool
    max_excess_db: float
    point_count: int
    supersedes_batch_id: Optional[int] = None
    created_at: Optional[str] = None
    confirmed_at: Optional[str] = None


class MeasurementBatchOut(MeasurementBatchSummary):
    mask_name: str
    carrier_snapshot: dict
    calibration_points_snapshot: list
    raw_points: list[list[float]]
    calibrated_curve: list
    theory_curve: list
    deviations: list
    violations: list


# ---- 规划记录 --------------------------------------------------------------

class PlanRequestV2(PlanRequest):
    scenario_id: Optional[int] = None
    # 以该场景下经确认的测量保守包络反算间隔并做 post-check
    use_measured_envelope: bool = False


class PlanRecordOut(BaseModel):
    id: int
    scenario_id: Optional[int]
    mode: str
    used_measured_envelope: bool
    envelope_batch_ids: list[int]
    calibration_version: str
    stale: bool
    stale_reason: Optional[str] = None
    stale_detail: str = ""
    created_at: Optional[str] = None
    feasible: bool
    result: dict
