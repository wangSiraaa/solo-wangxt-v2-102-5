"""离线频谱扫频记录文件解析（纯函数，不碰数据库）。

支持两种格式（按内容自动识别，首个非空白字符为 ``{`` 视为 JSON，否则按 CSV）：

JSON::

    {
      "batch_key": "SWEEP-2026-09-30-001",
      "carrier_name": "C1",
      "sampled_at": "2026-09-30T14:22:00Z",
      "calibration": {"name": "CAL-LAB-A", "version": 1},
      "points": [{"freq_mhz": 98.0, "power_dbm_hz": -56.2}, ...]
    }

CSV（``#`` 行为元数据头，随后是列头与数据行）::

    # batch_key: SWEEP-2026-09-30-001
    # carrier: C1
    # sampled_at: 2026-09-30T14:22:00Z
    # calibration: CAL-LAB-A@1
    freq_mhz,power_dbm_hz
    98.0,-56.2
    98.5,-55.8

解析阶段做全部格式校验：任何坏行、非单调频率、缺失字段都抛 ValueError，
调用方据此整体拒绝，不写入半个批次。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MIN_POINTS = 2


@dataclass(frozen=True)
class ParsedSweep:
    """一次扫频记录的解析结果（已校验，尚未与数据库交互）。"""

    batch_key: str
    carrier_name: str
    sampled_at: datetime  # 统一为 UTC aware
    calibration_name: str
    calibration_version: int
    # (freq_mhz, power_dbm_hz)，频率严格递增
    points: tuple[tuple[float, float], ...]

    @property
    def sampled_at_iso(self) -> str:
        """规范化的 UTC ISO 字符串（固定微秒宽度，字典序即时间序）。"""
        return self.sampled_at.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_instant(raw: Any, where: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{where}：采样时刻 sampled_at 缺失或不是字符串")
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ValueError(f"{where}：采样时刻 {text!r} 不是合法 ISO-8601 时间") from None
    if dt.tzinfo is None:  # 无时区按 UTC 处理
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_calibration_ref(meta: dict[str, Any], where: str) -> tuple[str, int]:
    """从元数据里取校准引用；支持 {"calibration": {"name","version"}}、
    {"calibration": "NAME@VERSION"} 或 calibration_name/calibration_version 两个键。"""
    cal = meta.get("calibration")
    name = ver = None
    if isinstance(cal, dict):
        name, ver = cal.get("name"), cal.get("version")
    elif isinstance(cal, str) and "@" in cal:
        name, _, v = cal.partition("@")
        name = name.strip()
        try:
            ver = int(v.strip())
        except ValueError:
            raise ValueError(f"{where}：校准引用 {cal!r} 的版本号不是整数") from None
    if name is None and meta.get("calibration_name") is not None:
        name = meta.get("calibration_name")
        ver = meta.get("calibration_version")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}：缺失校准名称（calibration.name）")
    try:
        ver = int(ver)
    except (TypeError, ValueError):
        raise ValueError(f"{where}：缺失或非法的校准版本号（calibration.version）") from None
    if ver < 1:
        raise ValueError(f"{where}：校准版本号必须 >= 1")
    return name.strip(), ver


def _check_points(pairs: list[tuple[float, float]], where: str) -> tuple[tuple[float, float], ...]:
    """公共数值校验：有限、频率为正、严格单调递增。"""
    if len(pairs) < MIN_POINTS:
        raise ValueError(f"{where}：扫频点不足（至少 {MIN_POINTS} 个，实际 {len(pairs)} 个）")
    prev = None
    for f, p in pairs:
        if f != f or p != p or f in (float("inf"), float("-inf")) \
                or p in (float("inf"), float("-inf")):
            raise ValueError(f"{where}：存在非法数值（freq={f!r}, power={p!r}）")
        if f <= 0:
            raise ValueError(f"{where}：频率必须为正（得到 {f} MHz）")
        if prev is not None and f <= prev:
            raise ValueError(
                f"{where}：频率非单调递增（{prev} MHz 之后出现 {f} MHz）")
        prev = f
    return tuple(pairs)


def _parse_json(doc: Any) -> ParsedSweep:
    if not isinstance(doc, dict):
        raise ValueError("JSON 扫频文件：顶层必须是对象")
    where = "JSON 扫频文件"
    batch_key = str(doc.get("batch_key") or "").strip()
    if not batch_key:
        raise ValueError(f"{where}：缺失批次标识 batch_key")
    carrier = str(doc.get("carrier_name") or doc.get("carrier") or "").strip()
    if not carrier:
        raise ValueError(f"{where}：缺失载波名 carrier_name")
    sampled_at = _parse_instant(doc.get("sampled_at"), where)
    cal_name, cal_ver = _parse_calibration_ref(doc, where)

    raw_points = doc.get("points")
    if not isinstance(raw_points, list):
        raise ValueError(f"{where}：points 必须是数组")
    pairs: list[tuple[float, float]] = []
    for i, pt in enumerate(raw_points):
        try:
            if isinstance(pt, dict):
                f, p = float(pt["freq_mhz"]), float(pt["power_dbm_hz"])
            else:
                f, p = float(pt[0]), float(pt[1])
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError(f"{where}：第 {i + 1} 个扫频点格式错误: {pt!r}") from None
        pairs.append((f, p))
    return ParsedSweep(batch_key, carrier, sampled_at, cal_name, cal_ver,
                       _check_points(pairs, where))


def _parse_csv(text: str) -> ParsedSweep:
    where = "CSV 扫频文件"
    meta: dict[str, str] = {}
    header: list[str] | None = None
    pairs: list[tuple[float, float]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            for sep in (":", "="):
                if sep in body:
                    k, _, v = body.partition(sep)
                    meta[k.strip().lower()] = v.strip()
                    break
            continue
        if header is None:
            header = [c.strip().lower() for c in line.split(",")]
            if header != ["freq_mhz", "power_dbm_hz"]:
                raise ValueError(
                    f"{where}：第 {lineno} 行列头必须是 'freq_mhz,power_dbm_hz'，"
                    f"得到 {line!r}")
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) != 2:
            raise ValueError(f"{where}：第 {lineno} 行应有 2 列，得到 {len(cols)} 列: {line!r}")
        try:
            f, p = float(cols[0]), float(cols[1])
        except ValueError:
            raise ValueError(f"{where}：第 {lineno} 行存在非数值内容: {line!r}") from None
        pairs.append((f, p))

    if header is None:
        raise ValueError(f"{where}：缺少列头行 'freq_mhz,power_dbm_hz'")

    batch_key = (meta.get("batch_key") or "").strip()
    if not batch_key:
        raise ValueError(f"{where}：头部缺失批次标识（# batch_key: ...）")
    carrier = (meta.get("carrier") or meta.get("carrier_name") or "").strip()
    if not carrier:
        raise ValueError(f"{where}：头部缺失载波名（# carrier: ...）")
    sampled_at = _parse_instant(meta.get("sampled_at"), where)
    cal_meta: dict[str, Any] = dict(meta)
    if "calibration" in meta:
        cal_meta["calibration"] = meta["calibration"]
    cal_name, cal_ver = _parse_calibration_ref(cal_meta, where)
    return ParsedSweep(batch_key, carrier, sampled_at, cal_name, cal_ver,
                       _check_points(pairs, where))


def parse_sweep_file(content: str) -> ParsedSweep:
    """解析并完整校验一份扫频记录；任何格式问题都抛 ValueError。"""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("扫频文件为空")
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 扫频文件：解析失败（{e}）") from None
        return _parse_json(doc)
    return _parse_csv(content)
