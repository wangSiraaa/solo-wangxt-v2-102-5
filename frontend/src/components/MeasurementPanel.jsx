import { useRef, useState } from 'react'
import { api } from '../api.js'

const STATUS_LABEL = {
  imported: '已归档', confirmed: '已确认', superseded: '已被取代',
}

/** 测量批次面板：CSV 导入、确认、校准修订、规划历史、导出/导入。 */
export default function MeasurementPanel({
  scenarioId, batches, calibrations, planRuns, onChanged,
}) {
  const [showImport, setShowImport] = useState(false)
  const [csvText, setCsvText] = useState('')
  const [showCal, setShowCal] = useState(false)
  const [calVersion, setCalVersion] = useState('')
  const [calPoints, setCalPoints] = useState('70, 0\n230, 0')
  const [calDesc, setCalDesc] = useState('')
  const fileRef = useRef(null)
  const bundleRef = useRef(null)

  const doImport = async () => {
    if (!scenarioId) return onChanged?.('请先选择或保存场景')
    try {
      const r = await api.importSweepText(scenarioId, csvText)
      setCsvText(''); setShowImport(false)
      await onChanged(r.replayed ? '批次重放：内容相同，未产生重复点'
                                 : `批次 ${r.batch.batch_ref} 导入成功`)
    } catch (e) { onChanged(`整批拒绝：${e.message}`) }
  }

  const onFile = async (ev) => {
    const file = ev.target.files?.[0]
    ev.target.value = ''
    if (!file || !scenarioId) return
    const text = await file.text()
    try {
      const r = await api.importSweepText(scenarioId, text)
      await onChanged(r.replayed
        ? `批次 ${r.batch.batch_ref} 重放：幂等，未产生重复点`
        : `批次 ${r.batch.batch_ref} 导入成功`)
    } catch (e) { onChanged(`整批拒绝：${e.message}`) }
  }

  const doConfirm = async (id) => {
    try {
      await api.confirmBatch(id)
      await onChanged('已确认：成为该载波当前测量包络')
    } catch (e) { onChanged(`确认被拒：${e.message}`) }
  }

  const doCreateCal = async () => {
    let points
    try {
      points = calPoints.split('\n').map((line) => {
        const [f, g] = line.split(',').map((x) => parseFloat(x))
        if (!Number.isFinite(f) || !Number.isFinite(g)) throw new Error('bad')
        return [f, g]
      })
    } catch {
      return onChanged('校准点格式错误：每行 "频率MHz, 增益dB"')
    }
    try {
      const r = await api.createCalibration(
        { version: calVersion.trim(), points, description: calDesc })
      setShowCal(false); setCalVersion('')
      await onChanged(`新校准修订 ${r.version} 生效，${r.plans_marked_stale} 个关联计划已过期`)
    } catch (e) { onChanged(`校准修订失败：${e.message}`) }
  }

  const doExport = async () => {
    const bundle = await api.exportBundle(scenarioId)
    const blob = new Blob([JSON.stringify(bundle, null, 2)],
                          { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `measurements-scenario-${scenarioId}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  const onBundleFile = async (ev) => {
    const file = ev.target.files?.[0]
    ev.target.value = ''
    if (!file) return
    try {
      const bundle = JSON.parse(await file.text())
      const r = await api.importBundle(scenarioId, bundle)
      await onChanged(`测量包恢复：共 ${r.total} 批，重放 ${r.replayed} 批`)
    } catch (e) {
      onChanged(`测量包拒绝（已整体回滚）：${e.message}`)
    }
  }

  const activeCal = calibrations.find((c) => c.is_active)

  return (
    <div className="meas-panel">
      <div className="row">
        <button onClick={() => setShowImport(!showImport)} disabled={!scenarioId}>
          粘贴 CSV 导入
        </button>
        <button onClick={() => fileRef.current?.click()} disabled={!scenarioId}>
          选择 .csv 文件
        </button>
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={onFile} />
        <span className="spacer" />
        <button onClick={() => setShowCal(!showCal)}>校准修订</button>
        <button onClick={doExport} disabled={!scenarioId || !batches.length}>导出包</button>
        <button onClick={() => bundleRef.current?.click()} disabled={!scenarioId}>导入包</button>
        <input ref={bundleRef} type="file" accept=".json,application/json"
               hidden onChange={onBundleFile} />
      </div>
      <div className="hint" style={{ marginTop: 6 }}>
        当前校准版本：<b>{activeCal?.version || '—'}</b>
        （历史批次/报告冻结在导入时的校准版本，修订只新增版本）
      </div>

      {showImport && (
        <div style={{ marginTop: 8 }}>
          <textarea className="csv-box" value={csvText} onChange={(e) => setCsvText(e.target.value)}
                    placeholder={'# batch: B2026-09-30-01\n# carrier: C1\n# sampled_at: 2026-09-30T10:15:00+08:00\n# calibration: cal-v1\nf_mhz,power_dbm_hz\n90.0,-100.4\n...'} />
          <div className="row" style={{ marginTop: 6 }}>
            <button className="primary" onClick={doImport}>原子导入（坏行/非单调/缺校准整体拒绝）</button>
            <button onClick={() => setShowImport(false)}>取消</button>
          </div>
        </div>
      )}

      {showCal && (
        <div className="cal-form" style={{ marginTop: 8 }}>
          <div className="row">
            <label className="field-label">新版本号</label>
            <input className="field" value={calVersion}
                   onChange={(e) => setCalVersion(e.target.value)} placeholder="cal-v2" />
          </div>
          <textarea className="csv-box" style={{ marginTop: 6 }} value={calPoints}
                    onChange={(e) => setCalPoints(e.target.value)} />
          <div className="row" style={{ marginTop: 6 }}>
            <input className="field" style={{ flex: 1 }} value={calDesc}
                   onChange={(e) => setCalDesc(e.target.value)} placeholder="修订说明" />
            <button className="primary" onClick={doCreateCal}>登记修订并令计划过期</button>
            <button onClick={() => setShowCal(false)}>取消</button>
          </div>
        </div>
      )}

      <table className="plan-table" style={{ marginTop: 10 }}>
        <thead>
          <tr>
            <th>批次</th><th>载波</th><th>采样时刻</th><th>校准</th>
            <th>状态</th><th>越限</th><th></th>
          </tr>
        </thead>
        <tbody>
          {batches.length === 0 && (
            <tr><td colSpan="7" className="muted" style={{ textAlign: 'center' }}>
              暂无测量批次（导入后仅归档，确认后才参与包络复核）
            </td></tr>
          )}
          {batches.map((b) => (
            <tr key={b.id} className={b.status === 'superseded' ? 'row-superseded' : ''}>
              <td title={b.batch_ref}>{b.batch_ref}</td>
              <td>{b.carrier_name}</td>
              <td>{b.sampled_at?.replace('T', ' ').slice(0, 19)}</td>
              <td>{b.calibration_version}</td>
              <td>
                <span className={`batch-status ${b.status}`}>{STATUS_LABEL[b.status]}</span>
                {b.arrived_late && <span className="late-tag" title="采样早于当前已确认结论">迟到</span>}
              </td>
              <td style={{ color: b.has_violation ? 'var(--error)' : 'var(--ok)' }}>
                {b.has_violation ? `⚠ ${b.max_excess_db} dB` : '无'}
              </td>
              <td>
                {b.status === 'imported' && !b.arrived_late && (
                  <button onClick={() => doConfirm(b.id)}>确认</button>
                )}
                {b.status === 'imported' && b.arrived_late && (
                  <span className="muted" title="旧测量不得覆盖较新的已确认结论">不可确认</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2 style={{ marginTop: 14 }}>规划历史（只追加，失效不改写结果）</h2>
      <table className="plan-table">
        <thead>
          <tr><th>#</th><th>模式</th><th>校准</th><th>包络复核</th><th>时间</th><th>状态</th></tr>
        </thead>
        <tbody>
          {planRuns.length === 0 && (
            <tr><td colSpan="6" className="muted" style={{ textAlign: 'center' }}>
              暂无规划记录
            </td></tr>
          )}
          {planRuns.map((p) => (
            <tr key={p.id} className={p.stale ? 'row-stale' : ''}>
              <td>{p.id}</td>
              <td>{p.mode}</td>
              <td>{p.calibration_version}</td>
              <td>{p.used_measured_envelope ? `是 (#${p.envelope_batch_ids.join(', #')})` : '否'}</td>
              <td>{p.created_at?.replace('T', ' ').slice(0, 19)}</td>
              <td>
                {p.stale
                  ? <span className="stale-tag" title={p.stale_detail}>
                      已过期 · {p.stale_label || p.stale_reason}
                    </span>
                  : <span style={{ color: 'var(--ok)' }}>现行</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
