"""离线扫频测量批次的领域逻辑（纯函数，不触库）。

- 解析课堂导出的 CSV（带批次标识、采样时刻、校准版本与频率/功率点）。
- 用校准版本的增益折线（dB 域分段线性）修正原始读数。
- 与“当前理论掩模”逐点对比（同中心/带宽/功率的教学平顶+折线模型）。
- 保守包络：逐频率取理论与校准后测量的最大值（dB 域 max = 线性域功率更大，
  即更保守），供规划器 post-check 替代理论 PSD。

全部为离线简化教学模型：测量文件由人工导入，不连接任何设备。
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import numpy as np

from .masks import get_mask

# 判定“测量越限”的容差 (dB)：高于理论曲线该值以上才算发现越限，
# 避免校准噪声把贴着理论值的点误报成越限。
VIOLATION_MARGIN_DB = 0.5
# 理论掩模跨度之外，按掩模最后一段衰减值外推为恒定地板参与对比；
# 高于该地板 VIOLATION_MARGIN_DB 的带外尾巴同样算越限（可能造成邻频泄漏）。
FLOOR_EXTRAPOLATE = True


class MeasurementFormatError(ValueError):
    """文件结构/内容格式错误（整批拒绝的信号）。"""


@dataclass(frozen=True)
class Calibration:
    id: int
    version: str
    points: tuple[tuple[float, float], ...]
    description: str = ""

    @property
    def f_min(self) -> float:
        return self.points[0][0]

    @property
    def f_max(self) -> float:
        return self.points[-1][0]

    def gain_db(self, f_mhz: Iterable[float]) -> np.ndarray:
        """频率点上的校准增益 (dB)，线性插值；超出校准覆盖范围抛错（不得外推）。"""
        xs = np.array([p[0] for p in self.points], dtype=float)
        ys = np.array([p[1] for p in self.points], dtype=float)
        f = np.asarray(list(f_mhz), dtype=float)
        if f.size and (float(f.min()) < xs[0] - 1e-9 or float(f.max()) > xs[-1] + 1e-9):
            raise MeasurementFormatError(
                f"测量频率 [{float(f.min()):g}, {float(f.max()):g}] MHz 超出校准版本 "
                f"{self.version!r} 的覆盖范围 [{xs[0]:g}, {xs[-1]:g}] MHz，拒绝导入")
        return np.interp(f, xs, ys)


@dataclass(frozen=True)
class ParsedSweep:
    batch_ref: str
    carrier_name: str
    sampled_at: datetime
    calibration_version: str
    points: tuple[tuple[float, float], ...]

    @property
    def content_hash(self) -> str:
        payload = repr((self.batch_ref, self.carrier_name, self.sampled_at.isoformat(),
                        self.calibration_version, self.points))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_HEADER_ALIASES = {
    "f_mhz": ("f_mhz", "frequency_mhz", "freq_mhz", "frequency", "freq", "频率mhz", "频率"),
    "power": ("power_dbm_hz", "psd_dbm_hz", "power_dbm", "psd", "功率", "功率dbm"),
}


def _norm_header(h: str) -> str:
    return h.strip().lstrip("﻿").lower().replace(" ", "").replace("_", "")


def _match_column(headers: list[str], aliases: tuple[str, ...]) -> Optional[int]:
    norm = [_norm_header(h) for h in headers]
    wanted = {_norm_header(a) for a in aliases}
    for i, h in enumerate(norm):
        if h in wanted:
            return i
    return None


def _parse_meta_value(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise MeasurementFormatError(f"元数据行必须为 'key: value'，得到 {raw!r}")
    k, v = raw.split(":", 1)
    return k.strip().lower(), v.strip()


def _parse_datetime(raw: str) -> datetime:
    v = raw.strip()
    # 支持 "Z" 结尾的 UTC 表示
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise MeasurementFormatError(
            f"采样时刻 {raw!r} 不是合法 ISO-8601 时间（如 2026-09-30T10:15:00+08:00）")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_sweep_csv(text: str) -> ParsedSweep:
    """解析课堂扫频 CSV。

    结构（以 # 开头的行或表头前的 key: value 均为元数据）::

        # batch: B2026-09-30-01
        # carrier: C1
        # sampled_at: 2026-09-30T10:15:00+08:00
        # calibration: cal-v1
        f_mhz,power_dbm_hz
        90.00,-100.4
        ...

    任何格式错误都抛 MeasurementFormatError；调用方必须保证拒绝时不写库。
    """
    if not text or not text.strip():
        raise MeasurementFormatError("文件为空")

    meta: dict[str, str] = {}
    data_rows: list[str] = []
    header_line: Optional[str] = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            k, v = _parse_meta_value(line[1:])
            meta[k] = v
            continue
        if header_line is None:
            # 表头前允许不带 # 的 key: value 元数据行
            if _norm_header(line.split(",")[0]) in {
                    _norm_header(x) for xs in _HEADER_ALIASES.values() for x in xs}:
                header_line = line
                continue
            if ":" in line and "," not in line:
                k, v = _parse_meta_value(line)
                meta[k] = v
                continue
            raise MeasurementFormatError(f"第 {lineno} 行：缺少列头 f_mhz,power_dbm_hz")
        data_rows.append(line)

    required = {"batch": "批次标识（# batch: ...）",
                "carrier": "载波名（# carrier: ...）",
                "sampled_at": "采样时刻（# sampled_at: ...）",
                "calibration": "校准版本（# calibration: ...）"}
    for key, desc in required.items():
        if not meta.get(key):
            raise MeasurementFormatError(f"缺少元数据：{desc}")

    if header_line is None:
        raise MeasurementFormatError("缺少列头 f_mhz,power_dbm_hz")
    headers = next(csv.reader(io.StringIO(header_line)))
    fi = _match_column(headers, _HEADER_ALIASES["f_mhz"])
    pi = _match_column(headers, _HEADER_ALIASES["power"])
    if fi is None or pi is None:
        raise MeasurementFormatError(f"列头必须包含频率列与功率(dBm/Hz)列，得到 {headers}")

    points: list[tuple[float, float]] = []
    for offset, row_text in enumerate(data_rows, start=1):
        cells = next(csv.reader(io.StringIO(row_text)))
        if not any(c.strip() for c in cells):
            continue
        if len(cells) <= max(fi, pi):
            raise MeasurementFormatError(f"数据行 {offset} 列数不足：{row_text!r}")
        try:
            f = float(cells[fi])
            p = float(cells[pi])
        except ValueError:
            raise MeasurementFormatError(
                f"数据行 {offset} 频率/功率不是数字：{row_text!r}")
        if not math.isfinite(f) or not math.isfinite(p):
            raise MeasurementFormatError(f"数据行 {offset} 频率/功率不能为 NaN/Inf")
        points.append((f, p))

    if len(points) < 2:
        raise MeasurementFormatError("至少需要 2 个采样点")

    # 频率必须严格单调递增（不允许乱序/重复）
    for i in range(1, len(points)):
        if points[i][0] <= points[i - 1][0]:
            raise MeasurementFormatError(
                f"频率必须严格单调递增：第 {i + 1} 点 {points[i][0]:g} MHz "
                f"未大于前一点 {points[i - 1][0]:g} MHz")

    return ParsedSweep(
        batch_ref=meta["batch"].strip(),
        carrier_name=meta["carrier"].strip(),
        sampled_at=_parse_datetime(meta["sampled_at"]),
        calibration_version=meta["calibration"].strip(),
        points=tuple(points),
    )


def parse_sweep_json(payload: dict) -> ParsedSweep:
    """解析 JSON 直传的扫频（前端解析 CSV 后也可走同一校验链路）。"""
    try:
        batch_ref = str(payload["batch_ref"]).strip()
        carrier_name = str(payload["carrier_name"]).strip()
        calibration_version = str(payload["calibration_version"]).strip()
        sampled_at = _parse_datetime(str(payload["sampled_at"]))
        raw_points = payload["points"]
    except (KeyError, TypeError) as e:
        raise MeasurementFormatError(f"缺少字段：{e}")
    if not batch_ref or not carrier_name or not calibration_version:
        raise MeasurementFormatError("批次标识/载波名/校准版本不能为空")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise MeasurementFormatError("至少需要 2 个采样点")
    points: list[tuple[float, float]] = []
    for i, pt in enumerate(raw_points):
        try:
            if isinstance(pt, dict):
                f = float(pt["f_mhz"])
                p = float(pt["power_dbm_hz"])
            else:  # 导出包使用紧凑形式 [f_mhz, power_dbm_hz]
                f, p = float(pt[0]), float(pt[1])
        except (KeyError, TypeError, ValueError, IndexError):
            raise MeasurementFormatError(f"第 {i + 1} 个点不是合法的频率/功率点")
        if not math.isfinite(f) or not math.isfinite(p):
            raise MeasurementFormatError(f"第 {i + 1} 个点不能为 NaN/Inf")
        points.append((f, p))
    for i in range(1, len(points)):
        if points[i][0] <= points[i - 1][0]:
            raise MeasurementFormatError(
                f"频率必须严格单调递增：第 {i + 1} 点未大于前一点")
    return ParsedSweep(batch_ref=batch_ref, carrier_name=carrier_name,
                       sampled_at=sampled_at,
                       calibration_version=calibration_version,
                       points=tuple(points))


def calibrate(points: Iterable[tuple[float, float]], cal: Calibration
              ) -> list[tuple[float, float]]:
    """原始读数 + 校准增益 -> 校准后曲线 (f_mhz, dBm/Hz)。"""
    pts = list(points)
    gains = cal.gain_db([p[0] for p in pts])
    return [(f, round(p + float(g), 4)) for (f, p), g in zip(pts, gains)]


def theory_psd(mask_name: str, f_mhz: Iterable[float], center_mhz: float,
               bandwidth_mhz: float, power_dbm: float) -> np.ndarray:
    """当前理论掩模在给定频率上的 PSD (dBm/Hz)。

    跨度内：带内平顶、带外折线衰减；跨度之外按最后一段衰减值外推恒定地板，
    使“测得的带外尾巴高于掩模末端”也能被量化为偏差。
    """
    mask = get_mask(mask_name)
    f = np.asarray(list(f_mhz), dtype=float)
    psd_floor = power_dbm - 10.0 * math.log10(bandwidth_mhz * 1e6)
    att = np.asarray(mask.attenuation_db(f - center_mhz), dtype=float)
    end_att = float(mask.points[-1][1])
    if FLOOR_EXTRAPOLATE:
        att = np.where(np.isfinite(att), att, end_att)
    in_band = np.abs(f - center_mhz) <= bandwidth_mhz / 2.0 + 1e-12
    psd = np.full(f.shape, psd_floor + end_att, dtype=float)
    ib_or_span = in_band | np.isfinite(
        np.asarray(mask.attenuation_db(f - center_mhz), dtype=float))
    psd[in_band] = psd_floor
    oo = (~in_band) & ib_or_span
    psd[oo] = psd_floor + att[oo]
    return psd


def evaluate_against_theory(calibrated: list[tuple[float, float]], *,
                            mask_name: str, center_mhz: float,
                            bandwidth_mhz: float, power_dbm: float
                            ) -> dict:
    """逐点比较校准后曲线与当前理论掩模，返回偏差与越限点。"""
    f = np.array([p[0] for p in calibrated], dtype=float)
    meas = np.array([p[1] for p in calibrated], dtype=float)
    theory = theory_psd(mask_name, f, center_mhz, bandwidth_mhz, power_dbm)
    deviation = meas - theory  # 正值 = 测量高于理论（更差）

    deviations = [
        {"f_mhz": round(float(x), 4),
         "measured_dbm_hz": round(float(m), 3),
         "theory_dbm_hz": round(float(t), 3),
         "excess_db": round(float(d), 3)}
        for x, m, t, d in zip(f, meas, theory, deviation)
    ]
    violations = [d for d in deviations if d["excess_db"] > VIOLATION_MARGIN_DB]
    return {
        "theory_curve": [[round(float(x), 4), round(float(t), 3)]
                         for x, t in zip(f, theory)],
        "deviations": deviations,
        "violations": violations,
        "max_excess_db": round(float(deviation.max()), 3) if deviation.size else 0.0,
        "has_violation": bool(np.any(deviation > VIOLATION_MARGIN_DB)),
    }


# --- 保守包络 ---------------------------------------------------------------

def envelope_curve(curves: list[list[tuple[float, float]]],
                   fallback: Optional[list[tuple[float, float]]] = None
                   ) -> list[tuple[float, float]]:
    """所有曲线与 fallback 频率并集网格上的保守包络（dB 域逐点取最大）。

    dB 最大等价于线性 W/Hz 域功率最大——即“最差情况”。
    fallback（通常是理论曲线）既参与逐点取最大，也用于补齐测量未覆盖的频率：
    网格取所有曲线与 fallback 频率的并集，各曲线在非自身网格点上线性插值、
    不做外推；某频率没有任何曲线（含 fallback）覆盖时不输出。
    """
    all_f = {float(f) for curve in curves for f, _ in curve}
    if fallback is not None:
        all_f.update(float(f) for f, _ in fallback)
    if not all_f:
        return []
    grid = np.asarray(sorted(all_f), dtype=float)
    acc = np.full(grid.shape, -np.inf)
    for curve in curves:
        cf = np.asarray([p[0] for p in curve], dtype=float)
        cp = np.asarray([p[1] for p in curve], dtype=float)
        vals = np.interp(grid, cf, cp, left=-np.inf, right=-np.inf)
        acc = np.maximum(acc, vals)
    if fallback is not None:
        fb_f = np.asarray([p[0] for p in fallback], dtype=float)
        fb_p = np.asarray([p[1] for p in fallback], dtype=float)
        fb = np.interp(grid, fb_f, fb_p, left=-np.inf, right=-np.inf)
        acc = np.maximum(acc, fb)
    return [(round(float(x), 4), round(float(p), 3))
            for x, p in zip(grid, acc) if math.isfinite(p)]
