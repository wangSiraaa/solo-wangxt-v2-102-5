# 频谱工作台（通信工程教学 · 离线简化模型）

用于比较一组载波频率配置的教学工具：录入少量载波与保护间隔后，绘制频段占用与发射谱，
检查 **频带重叠、保护带不足、掩模尾部越界**，在**线性域 (W)** 汇总功率后以 dBm 显示；
极化复用由输入规则决定（禁止 / 允许 / 隔离度未知待评估）；并可用 **OR-Tools CP-SAT**
为载波寻找一组满足间隔的频率位置。此外支持**离线测量批次**：原子导入带批次标识、采样时刻、
频率/功率点与校准版本的扫频文件，持久化原始记录/校准曲线/与理论掩模偏差，分析页叠加
理论/实测/保守包络，并可让规划器以经确认的测量包络反算间隔并 post-check。

> ⚠️ 纯离线简化模型：**不连接无线电设备，不生成任何发射指令**。掩模与发射谱均为教学
> 用平顶 + 折线衰减的简化数值，不对应任何标准或真实设备指标。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + Vite + Plotly.js |
| 后端 | FastAPI + Pydantic + SQLAlchemy 2 |
| 科学计算 | SciPy（掩模分段线性插值、PSD 积分）、NumPy |
| 约束求解 | OR-Tools CP-SAT（1 kHz 频率网格） |
| 数据库 | PostgreSQL（中心频率、带宽、功率、示例掩模、场景） |

## 目录

```
backend/   FastAPI 应用
  app/services/units.py     dBm<->W、线性域功率汇总
  app/services/masks.py     掩模折线、发射谱曲线（SciPy interp1d）
  app/services/analysis.py  三类冲突检查（定位到载波对/方向）
  app/services/planner.py   OR-Tools CP-SAT 频率规划
  app/db.py / seed.py       SQLAlchemy 模型与教学演示场景
frontend/  React + Plotly.js
scripts/   start-dev.sh     一键启动（本地 PostgreSQL + 后端 + 前端）
```

## 快速开始

需要本机 PostgreSQL（或修改 `DATABASE_URL`）。提供一个 conda 方式安装的便捷脚本：

```bash
# 1) Python 依赖
pip install -r backend/requirements.txt

# 2) 前端依赖
cd frontend && npm install && cd ..

# 3) 初始化数据库并写入示例掩模 + 教学演示场景
python -m app.seed          # 工作目录为 backend/，或设置 PYTHONPATH

# 4) 启动
( cd backend && uvicorn app.main:app --reload --port 8000 )
( cd frontend && npm run dev )        # http://localhost:5173 ，/api 代理到 8000
```

环境变量：`DATABASE_URL`（默认 `postgresql+psycopg2://postgres@127.0.0.1:5432/spectrum`）、
`CORS_ORIGINS`。

## 计算模型要点

### 1. 冲突检查（`POST /api/analyze`，结果含 bands / spectrum / findings / power_summary）

- **频带重叠 overlap**：两频带边缘净距 `gap = max(low) − min(high) ≤ 0`，报重叠量。
- **保护带不足 guard_shortfall**：`0 < gap < guard_required_mhz`，报净距与缺口。
- **掩模尾部越界 mask_tail（有方向）**：发射机掩模谱落入受害载波频带的功率
  `P = ∫_victim 10^(PSD_dbm/10)·1mW df`（PSD 在线性 W/Hz 域积分），超过限值即报；
  因功率/掩模不同，A→B 与 B→A 分别计算、分别定位。
- **极化复用规则**（针对异极化对）：
  - `forbidden` 禁止：重叠即冲突；
  - `allowed` 允许（已知隔离度足够）：可同频，不报几何/泄漏冲突；
  - `unknown` 待评估：重叠只提示“隔离度未知，复用待评估”，不下违规定性；
  - 同极化对无极化隔离，始终按禁止处理。

### 2. 功率汇总

dB 是对数尺度，**不能直接相加**。先换算
`P_W = Σ 1mW · 10^(p_i/10)`，再 `P_dBm = 10 log10(P_W/1mW)`。
响应里同时给出错误的“dBm 直接求和”数值供课堂对比（例如多个载波 275 dBm vs 正确 36.14 dBm）。

### 3. OR-Tools 规划（`POST /api/plan`）

CP-SAT 在 1 kHz 网格上为每个载波选中心频率，约束：

- 落在给定可用频段内；
- 需要隔离的载波对（同极化 / 禁止 / 待评估）通过布尔析取排序，
  满足 `中心距 ≥ 半宽_i + 半宽_j + 要求边缘净距`；
- 允许复用的极化对不加约束（可同址）；
- `guard_only`：统一用保护间隔；`mask_aware`：对每对载波按**双向**掩模泄漏都不越限
  反算所需净距（含 0.5 dB 规划裕量，保证返回方案在分析口径下必然达标）；
- 目标：最小化相对录入位置的总偏移。无解时返回 `INFEASIBLE` 与放宽建议。
  返回中带 `post_check`（对方案重新跑一遍完整分析）。
- `POST /api/plan-runs` 在规划的同时把请求/结果**快照落库**（只追加），可带
  `scenario_id` 与 `use_measured_envelope`，让间隔反算与 post-check 都使用该场景
  经确认测量的保守包络。

### 4. 测量批次（离线扫频校验简化掩模）

课堂实验的频谱扫频记录以 CSV 离线导入（不覆盖设计掩模本身）：

```
# batch: B2026-09-30-01
# carrier: C1
# sampled_at: 2026-09-30T10:15:00+08:00
# calibration: cal-v1
f_mhz,power_dbm_hz
86.000,-104.1
...
```

- **原子导入**：先完整校验（元数据齐全、坏行拒绝、频率严格单调、校准版本存在且
  覆盖全部测点、载波属于该场景），再在单事务落库；任何错误整批回滚，不写半个批次。
- **持久化三件套**：原始采样点、校准后曲线（校准增益 dB 域线性插值）、与导入时
  当前理论掩模的逐点偏差与越限点（阈值含 0.5 dB 容差）。
- **幂等重放**：`batch_ref` 全局唯一 + 内容哈希；同批次同内容重复导入不产生重复点，
  同标识不同内容冲突拒绝（409）。
- **迟到保护**：批次导入时为 `imported`（只归档），确认后才成为该载波当前包络；
  采样时刻早于同载波已确认结论的旧批次标记 `arrived_late`，永远不能确认覆盖新结论；
  较新批次确认后，旧确认批次置 `superseded`。
- **保守包络**：逐频率 `max(理论 PSD, 校准后测量)`（dB 最大 = 线性 W/Hz 最差情况），
  以相对载波中心的偏移表示，规划移动载波时随载波平移。分析页「测量批次」标签叠加
  理论（蓝虚线）/ 实测（橙）/ 包络（红），规划面板可勾选「测量包络 post-check」。
- **校准修订**：`POST /api/calibrations` 只追加新版本，旧版本冻结；历史批次保存
  校准版本号与校准点快照，旧报告始终按原校准复现；修订后所有现行规划记录置 stale。
- **越限即过期**：导入（或确认）的测量发现高于掩模时，关联场景的现行规划记录置
  `stale`（原因 `measurement_violation` / `envelope_revised`），历史结果快照不改写。
- **导出/导入包**：`GET …/measurements/export` 与 `…/import-bundle` 以 JSON 整包
  迁移（含批次、校准、确认/取代/迟到状态）；导入整包原子，恢复后状态与导出时一致。

主要接口：`GET/POST /api/calibrations`、
`POST /api/scenarios/{id}/measurements/import-text|batches|import-bundle`、
`GET …/measurements/batches|overlay|export`、
`POST /api/measurements/batches/{id}/confirm`、
`POST /api/plan-runs`、`GET /api/scenarios/{id}/plan-runs`。
种子数据包含默认校准 `cal-v1`（70–230 MHz，0 dB，教学示例）。

## 教学演示场景（种子数据，12 个载波，带宽均为 4 MHz、功率不同）

| 载波对 | 现象 |
|---|---|
| C1(30 dBm,loose) / C2(20 dBm,loose)，净距 0.5 MHz | 保护带不足 + 双向尾部越界，泄漏 15.4 vs 5.3 dBm（功率不同导致方向不对称） |
| C4(30,strict) / C5(25,loose)，净距 0.5 MHz | 保护带不足；仅弱载波 loose 尾部越界（强→弱 −8.6 dBm 达标边界，弱→强 10.3 dBm 越界） |
| C7(30,loose) / C8(20,strict)，净距 2 MHz | 保护带虽够，强载波拖尾仍越界 2.0 dBm；反向 −47.5 dBm 达标 |
| C9 / C10，重叠 0.5 MHz | 同极化频带重叠 |
| C1(H) / C6(V) 同频 | 规则 unknown → “复用待评估” |
| C11(RHCP) / C12(V) 同频 | 规则 allowed → 无冲突 |

## 测试

```bash
# 纯函数 + 规划 + API（API 测试用内存 SQLite，无需外部服务）
PYTHONPATH=backend pytest backend/tests -q
```

34 个测试覆盖：dBm/W 换算、三类冲突对定位、泄漏方向性、三种极化规则、
两种规划模式的可行性与规划后零越界、场景 CRUD，以及测量批次的原子导入/整批拒绝/
幂等重放/迟到保护/校准版本化/计划过期/测量包络 post-check/导出导入一致性。
