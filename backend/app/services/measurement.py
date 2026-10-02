"""测量批次领域逻辑：校准曲线、保守包络、与理论掩模的偏差。

- 校准版本是不可变的：修订校准 = 新建版本；历史批次的校准后曲线在导入时
  按当时的校准版本计算并持久化，之后不随校准更新而改变（历史报告可按原校准复现）。
- 保守包络 = 逐点 max(理论掩模 PSD, 校准后实测 PSD)；理论无定义（掩模跨度之外）
  的频点取实测值（实测到的能量是真实存在的）。
- 越限判定：在理论掩模有定义的频点上，校准后实测超过理论即越限。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .masks import get_mask, psd_on_grid
from .sweepfile import ParsedSweep

# 越限容差 (dB)：校准后实测高出理论掩模超过该值即判定越限
EXCEED_TOLERANCE_DB = 0.0
# 存储精度 (dB / MHz)
CURVE_DECIMALS = 3


@dataclass(frozen=True)
class CalibrationCurve:
    """一个校准版本：频率相关的修正量 (dB)，分段线性、端点外持平。"""

    name: str
    version: int
    freqs: tuple[float, ...]
    offsets_db: tuple[float, ...]

    @classmethod
    def from_factors(cls, name: str, version: int,
                     factors: list[list[float]]) -> "CalibrationCurve":
        if not factors:
            raise ValueError("校准修正点不能为空")
        pts = []
        for i, row in enumerate(factors):
            try:
                f, off = float(row[0]), float(row[1])
            except (TypeError, IndexError, ValueError):
                raise ValueError(f"校准修正第 {i + 1} 点格式错误: {row!r}") from None
            if not (np.isfinite(f) and np.isfinite(off)) or f <= 0:
                raise ValueError(f"校准修正第 {i + 1} 点数值非法: {row!r}")
            pts.append((f, off))
        freqs = [p[0] for p in pts]
        if any(b <= a for a, b in zip(freqs, freqs[1:])):
            raise ValueError("校准修正点的频率必须严格递增")
        return cls(name=name, version=version,
                   freqs=tuple(freqs), offsets_db=tuple(p[1] for p in pts))

    def offset_db(self, f_mhz: np.ndarray) -> np.ndarray:
        """给定频率数组，返回每点的校准修正量 (dB)；范围外取端点值。"""
        return np.interp(np.asarray(f_mhz, dtype=float),
                         np.asarray(self.freqs), np.asarray(self.offsets_db))


@dataclass(frozen=True)
class EnvelopeCurve:
    """某载波经确认的实测曲线（校准后），用于规划 post-check 的保守包络。"""

    carrier_name: str
    f_mhz: tuple[float, ...]
    calibrated_dbm_hz: tuple[float, ...]

    def measured_on(self, f_mhz: np.ndarray) -> np.ndarray:
        """在任意频率网格上插值实测 PSD (dBm/Hz)；测量范围外为 -inf（无贡献）。"""
        return np.interp(np.asarray(f_mhz, dtype=float),
                         np.asarray(self.f_mhz), np.asarray(self.calibrated_dbm_hz),
                         left=-np.inf, right=-np.inf)


def compute_batch_curves(parsed: ParsedSweep, carrier,
                         cal: CalibrationCurve) -> tuple[dict, bool, float]:
    """计算一个批次的持久化曲线。

    carrier 为分析域 Carrier（场景当前配置的理论掩模来源）。
    返回 (curves, violation, max_excess_db)：
    curves 为列式 dict，含原始记录、校准后曲线、理论值、偏差与保守包络。
    """
    f = np.array([p[0] for p in parsed.points], dtype=float)
    raw = np.array([p[1] for p in parsed.points], dtype=float)
    calibrated = raw + cal.offset_db(f)

    mask = get_mask(carrier.mask_name)
    theory = psd_on_grid(mask, f, carrier.center_mhz,
                         carrier.bandwidth_mhz, carrier.power_dbm)

    theory_out: list[float | None] = []
    deviation_out: list[float | None] = []
    envelope_out: list[float] = []
    max_dev = float("-inf")
    for t, c in zip(theory, calibrated):
        if np.isfinite(t):
            dev = float(c - t)
            theory_out.append(round(float(t), CURVE_DECIMALS))
            deviation_out.append(round(dev, CURVE_DECIMALS))
            envelope_out.append(round(max(float(t), float(c)), CURVE_DECIMALS))
            max_dev = max(max_dev, dev)
        else:
            # 理论掩模跨度之外：模型认为无信号，保守包络取实测
            theory_out.append(None)
            deviation_out.append(None)
            envelope_out.append(round(float(c), CURVE_DECIMALS))

    violation = max_dev > EXCEED_TOLERANCE_DB
    max_excess = round(max(0.0, max_dev), CURVE_DECIMALS) if violation else 0.0
    curves = {
        "freq_mhz": [round(float(v), CURVE_DECIMALS) for v in f],
        "raw_dbm_hz": [round(float(v), CURVE_DECIMALS) for v in raw],
        "calibrated_dbm_hz": [round(float(v), CURVE_DECIMALS) for v in calibrated],
        "theory_dbm_hz": theory_out,
        "deviation_db": deviation_out,
        "envelope_dbm_hz": envelope_out,
    }
    return curves, violation, max_excess
