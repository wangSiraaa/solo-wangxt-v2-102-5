"""测量保守包络在分析/规划口径下的纯函数测试。"""
import math

from app.services.analysis import (AnalysisRules, Carrier, analyze,
                                   leakage_power_dbm)
from app.services.masks import spectrum_curve
from app.services.planner import BandLimits, plan
from app.services import measurements as M

RULES = AnalysisRules(guard_required_mhz=1.0, leakage_limit_dbm=-45.0,
                      reuse_policy={})


def c(name, center, power=20.0, pol="H", mask="strict", bw=4.0):
    return Carrier(id=None, name=name, center_mhz=center, bandwidth_mhz=bw,
                   power_dbm=power, polarization=pol, mask_name=mask)


def _theoretical_offset_envelope(tx: Carrier):
    """用理论谱本身（相对中心偏移）作为包络：应与无 override 的结果一致。"""
    mask = M.get_mask(tx.mask_name)
    f, psd = spectrum_curve(mask, tx.center_mhz, tx.bandwidth_mhz, tx.power_dbm)
    return [(round(float(x - tx.center_mhz), 4), round(float(p), 4))
            for x, p in zip(f, psd) if math.isfinite(p)]


def test_relative_override_matches_theory_when_curve_is_theory():
    tx, victim = c("a", 100, 30, mask="loose"), c("b", 106, 20, mask="strict")
    base = leakage_power_dbm(tx, victim)
    env = _theoretical_offset_envelope(tx)
    # override 用相对偏移，且曲线即理论谱 -> 泄漏相同（包络曲线 4 位小数量化误差）
    assert math.isclose(leakage_power_dbm(tx, victim, psd_override=env),
                        base, abs_tol=2e-3)

    # 规划把载波移走后，包络随载波平移：虚拟受害载波位置同样平移 -> 泄漏不变
    tx2 = Carrier(id=None, name="a", center_mhz=150.0, bandwidth_mhz=4.0,
                  power_dbm=30.0, polarization="H", mask_name="loose")
    victim2 = Carrier(id=None, name="b", center_mhz=156.0, bandwidth_mhz=4.0,
                      power_dbm=20.0, polarization="H", mask_name="strict")
    moved = leakage_power_dbm(tx2, victim2, psd_override=env)
    assert math.isclose(moved, base, rel_tol=1e-9, abs_tol=2e-3)


def test_conservative_envelope_never_below_theory():
    tx = c("a", 100, 30, mask="loose")
    env_pts = _theoretical_offset_envelope(tx)
    # 人为抬高 10 dB 的包络：泄漏严格更大
    raised = [(o, p + 10.0) for o, p in env_pts]
    victim = c("b", 106, 20, mask="strict")
    assert (leakage_power_dbm(tx, victim, psd_override=raised)
            > leakage_power_dbm(tx, victim) + 9.0)


def test_envelope_curve_pointwise_max():
    a = [(0.0, -80.0), (1.0, -70.0), (2.0, -60.0)]
    b = [(0.0, -75.0), (1.0, -72.0), (2.0, -50.0)]
    env = M.envelope_curve([a, b])
    assert [p for _, p in env] == [-75.0, -70.0, -50.0]
    # fallback 补齐频率：测量未覆盖处取理论
    env2 = M.envelope_curve([[(0.0, -60.0)]],
                            fallback=[(0.0, -90.0), (1.0, -91.0)])
    assert dict(env2) == {0.0: -60.0, 1.0: -91.0}


def test_plan_with_identical_envelope_matches_mask_aware():
    carriers = [c("C1", 100, 30, mask="loose"), c("C2", 104.5, 20, mask="loose"),
                c("C4", 130, 30, mask="strict")]
    base = plan(carriers, RULES, BandLimits(90, 300), "mask_aware")
    envs = {x.name: _theoretical_offset_envelope(x) for x in carriers}
    same = plan(carriers, RULES, BandLimits(90, 300),
                "measured_envelope", psd_overrides=envs)
    assert base["feasible"] and same["feasible"]
    # 同一发射形状：目标值与占用跨度一致（求解器可能选等价的另一最优排列）
    assert abs(base["objective_khz"] - same["objective_khz"]) <= 1.0
    assert math.isclose(base["occupied_span_mhz"], same["occupied_span_mhz"],
                        abs_tol=1e-6)
    post = analyze(
        [c(x["name"], x["center_mhz"], x["power_dbm"], x["polarization"],
           x["mask_name"], x["bandwidth_mhz"]) for x in same["assignments"]],
        RULES, psd_overrides=envs)
    assert post["counts"]["error"] == 0
