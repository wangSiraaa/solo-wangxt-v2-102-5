import { useRef, useState } from 'react'
import { api } from '../api.js'

const STATUS_LABEL = { confirmed: '已确认', superseded: '已归档' }
const OUTCOME_LABEL = {
  confirmed: '已确认为当前结论',
  superseded: '迟到旧批次，已归档（不覆盖新结论）',
  duplicate: '相同批次重放，幂等忽略',
}

/** 测量批次：离线扫频文件导入（原子化、幂等）、批次列表与叠加选择。 */
export default function MeasurementPanel({ scenarioId, batches, selectedBatchId,
                                           onSelectBatch, onChanged, disabled }) {
  const fileRef = useRef(null)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const doImport = async (file) => {
    if (!file || !scenarioId) return
    setBusy(true); setMsg(''); setErr('')
    try {
      const content = await file.text()
      const res = await api.importMeasurement(scenarioId, file.name, content)
      const extra = res.expired_plan_ids?.length
        ? `；${res.expired_plan_ids.length} 个关联计划已过期` : ''
      setMsg(`${OUTCOME_LABEL[res.outcome] || res.outcome}${extra}`)
      onChanged(res.batch?.id)
    } catch (e) {
      setErr(String(e.message || e))
      onChanged()
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  return (
    <div>
      <div className="row">
        <input ref={fileRef} type="file" accept=".json,.csv,.txt"
               style={{ display: 'none' }}
               onChange={(e) => doImport(e.target.files?.[0])} />
        <button className="primary" disabled={disabled || busy || !scenarioId}
                onClick={() => fileRef.current?.click()}>
          {busy ? '导入中…' : '导入扫频文件'}
        </button>
        {!scenarioId && <span className="muted">先保存场景后才能导入测量</span>}
      </div>
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="err-msg">{err}</div>}
      <div className="hint">
        支持 JSON / CSV 扫频记录（含批次标识、采样时刻、频率/功率点、校准版本）；
        格式错误、非单调频率或缺失校准会整体拒绝，不写入半个批次。
      </div>
      {batches?.length > 0 && (
        <ul className="batch-list">
          {batches.map((b) => (
            <li key={b.id}
                className={`${b.status} ${selectedBatchId === b.id ? 'sel' : ''}`}
                onClick={() => onSelectBatch(selectedBatchId === b.id ? null : b.id)}>
              <div className="row">
                <b>{b.batch_key}</b>
                <span className={`tag ${b.status}`}>{STATUS_LABEL[b.status] || b.status}</span>
                {b.violation && <span className="tag violation">越限 +{b.max_excess_db} dB</span>}
              </div>
              <div className="muted">
                {b.carrier_name} · 采样 {String(b.sampled_at).replace('T', ' ').slice(0, 19)}
                · 校准 {b.calibration_name}@v{b.calibration_version} · {b.points_count} 点
              </div>
            </li>
          ))}
        </ul>
      )}
      {batches?.length > 0 && (
        <div className="hint">点击批次在发射谱上叠加 理论 / 实测（校准后）/ 保守包络。</div>
      )}
    </div>
  )
}
