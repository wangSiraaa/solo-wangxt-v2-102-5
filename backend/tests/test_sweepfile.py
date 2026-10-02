"""扫频文件解析与测量领域逻辑的单测（纯函数，不需要数据库/HTTP）。"""
import math

import numpy as np
import pytest

from app.services.analysis import Carrier
from app.services.measurement import (CalibrationCurve, EnvelopeCurve,
                                      compute_batch_curves)
from app.services.sweepfile import parse_sweep_file

CARRIER = Carrier(id=None, name="C1", center_mhz=150.0, bandwidth_mhz=4.0,
                  power_dbm=20.0, polarization="H", mask_name="strict")
CAL = CalibrationCurve.from_factors("CAL-A", 1, [[100.0, -1.0], [200.0, -1.0]])

JSON_DOC = """{
  "batch_key": "SW-1",
  "carrier_name": "C1",
  "sampled_at": "2026-09-30T14:22:00Z",
  "calibration": {"name": "CAL-A", "version": 1},
  "points": [{"freq_mhz": 149.0, "power_dbm_hz": -50.0},
             {"freq_mhz": 150.0, "power_dbm_hz": -46.0},
             {"freq_mhz": 151.0, "power_dbm_hz": -50.5}]
}"""

CSV_DOC = """# 课堂扫频记录
# batch_key: SW-1
# carrier: C1
# sampled_at: 2026-09-30T14:22:00Z
# calibration: CAL-A@1
freq_mhz,power_dbm_hz
149.0,-50.0
150.0,-46.0
151.0,-50.5
"""


def test_parse_json_and_csv_equivalent():
    a = parse_sweep_file(JSON_DOC)
    b = parse_sweep_file(CSV_DOC)
    assert a.batch_key == b.batch_key == "SW-1"
    assert a.carrier_name == b.carrier_name == "C1"
    assert a.calibration_name == b.calibration_name == "CAL-A"
    assert a.calibration_version == b.calibration_version == 1
    assert a.points == b.points
    # 采样时刻规范化为 UTC，字典序即时间序
    assert a.sampled_at_iso == "2026-09-30T14:22:00.000000+00:00"


def test_parse_naive_time_assumed_utc():
    doc = JSON_DOC.replace("2026-09-30T14:22:00Z", "2026-09-30T14:22:00")
    assert parse_sweep_file(doc).sampled_at_iso == "2026-09-30T14:22:00.000000+00:00"


def test_timezone_offset_normalized():
    doc = JSON_DOC.replace("2026-09-30T14:22:00Z", "2026-09-30T22:22:00+08:00")
    assert parse_sweep_file(doc).sampled_at_iso == "2026-09-30T14:22:00.000000+00:00"


@pytest.mark.parametrize("content,frag", [
    # 坏行：非数值
    (CSV_DOC.replace("150.0,-46.0", "abc,def"), "第 8 行"),
    # 坏行：列数不对
    (CSV_DOC.replace("150.0,-46.0", "150.0,-46.0,xyz"), "第 8 行"),
    # 非单调频率
    (CSV_DOC.replace("151.0,-50.5", "149.5,-50.5"), "非单调"),
    # 列头错误
    (CSV_DOC.replace("freq_mhz,power_dbm_hz", "f,p"), "列头"),
    # 缺批次标识
    (CSV_DOC.replace("# batch_key: SW-1\n", ""), "batch_key"),
    # 缺采样时刻
    (CSV_DOC.replace("# sampled_at: 2026-09-30T14:22:00Z\n", ""), "sampled_at"),
    # 缺校准
    (CSV_DOC.replace("# calibration: CAL-A@1\n", ""), "校准"),
    # 空文件
    ("", "为空"),
    # JSON 缺校准
    (JSON_DOC.replace('"calibration": {"name": "CAL-A", "version": 1},\n  ', ""), "校准"),
    # JSON 点不足
    ('{"batch_key":"x","carrier_name":"C1","sampled_at":"2026-01-01T00:00:00Z",'
     '"calibration":{"name":"C","version":1},"points":[{"freq_mhz":100,"power_dbm_hz":-50}]}',
     "不足"),
    # 频率非正
    (JSON_DOC.replace('"freq_mhz": 149.0', '"freq_mhz": -3.0'), "为正"),
    # 非法 JSON
    ("{not json", "解析失败"),
])
def test_bad_files_raise(content, frag):
    with pytest.raises(ValueError) as ei:
        parse_sweep_file(content)
    assert frag in str(ei.value)


def test_calibration_curve_interp_and_clamp():
    cal = CalibrationCurve.from_factors("C", 2, [[100.0, -1.0], [110.0, -2.0]])
    off = cal.offset_db(np.array([90.0, 105.0, 120.0]))
    assert np.allclose(off, [-1.0, -1.5, -2.0])  # 端点外持平
    with pytest.raises(ValueError):
        CalibrationCurve.from_factors("C", 3, [[110.0, -1.0], [100.0, -2.0]])


def test_compute_batch_curves_deviation_and_envelope():
    parsed = parse_sweep_file(JSON_DOC)
    curves, violation, max_excess = compute_batch_curves(parsed, CARRIER, CAL)
    # 校准偏移 -1 dB：calibrated = raw - 1
    assert curves["calibrated_dbm_hz"] == [-51.0, -47.0, -51.5]
    # 带内理论 PSD = 20 - 10log10(4e6) ≈ -46.02 dBm/Hz
    assert curves["theory_dbm_hz"][1] == pytest.approx(-46.0206, abs=1e-3)
    # 偏差 = 校准后 - 理论
    assert curves["deviation_db"][1] == pytest.approx(-47.0 + 46.0206, abs=1e-3)
    # 保守包络 = max(理论, 校准后)
    for t, c, e in zip(curves["theory_dbm_hz"], curves["calibrated_dbm_hz"],
                       curves["envelope_dbm_hz"]):
        assert e == pytest.approx(max(t, c), abs=1e-9)
    assert not violation and max_excess == 0.0


def test_compute_batch_curves_violation_and_out_of_span():
    # raw=-40 -> 校准后 -41，比带内理论(-46.02)高约 5.02 dB -> 越限；
    # 158 MHz 超出 strict 跨度(7) -> 理论 None
    doc = JSON_DOC.replace("-46.0", "-40.0").replace(
        '{"freq_mhz": 151.0, "power_dbm_hz": -50.5}',
        '{"freq_mhz": 151.0, "power_dbm_hz": -50.5}, {"freq_mhz": 158.0, "power_dbm_hz": -80.0}')
    parsed = parse_sweep_file(doc)
    curves, violation, max_excess = compute_batch_curves(parsed, CARRIER, CAL)
    assert violation
    assert max_excess == pytest.approx(46.0206 - 41.0, abs=1e-3)
    assert curves["theory_dbm_hz"][3] is None
    assert curves["deviation_db"][3] is None
    assert curves["envelope_dbm_hz"][3] == pytest.approx(-81.0)  # 跨度外取实测


def test_envelope_curve_interp_range():
    env = EnvelopeCurve("C1", (100.0, 101.0, 102.0), (-50.0, -40.0, -50.0))
    got = env.measured_on(np.array([99.0, 100.5, 103.0]))
    assert got[0] == -np.inf and got[2] == -np.inf  # 范围外无贡献
    assert got[1] == pytest.approx(-45.0)
