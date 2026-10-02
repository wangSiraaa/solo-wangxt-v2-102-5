import { useState } from 'react'
import { api } from '../api.js'

/** 校准版本管理：注册新版本（同名自动递增版本号，旧版本不可变）。 */
export default function CalibrationPanel({ calibrations, onChanged, disabled }) {
  const [name, setName] = useState('CAL-LAB-A')
  const [factors, setFactors] = useState('140, 0.0\n170, 0.0')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const register = async () => {
    setBusy(true); setMsg(''); setErr('')
    try {
      const rows = factors.split('\n').map((l) => l.trim()).filter(Boolean)
        .map((l) => l.split(/[,\s]+/).map(Number))
      const res = await api.createCalibration({ name, factors: rows, description: '' })
      const extra = res.expired_plan_ids?.length
        ? `；${res.expired_plan_ids.length} 个关联计划已置为过期，需重新评估` : ''
      setMsg(`已注册 ${res.name} v${res.version}${extra}`)
      onChanged()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="row">
        <input className="field" style={{ width: 130 }} value={name} disabled={disabled}
               onChange={(e) => setName(e.target.value)} placeholder="校准名称" />
        <input className="field" style={{ flex: 1 }} value={factors} disabled={disabled}
               onChange={(e) => setFactors(e.target.value)}
               placeholder="修正点：频率MHz, 修正dB（逗号/空格分隔，每行一个）" />
        <button onClick={register} disabled={disabled || busy || !name.trim()}>
          {busy ? '注册中…' : '注册新版本'}
        </button>
      </div>
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="err-msg">{err}</div>}
      {calibrations?.length > 0 && (
        <div className="cal-list">
          {calibrations.map((c) => (
            <span key={`${c.name}@${c.version}`} className="tag cal">
              {c.name} v{c.version}
            </span>
          ))}
        </div>
      )}
      <div className="hint">
        校准版本不可变：修订即生成新版本；历史测量批次仍按其导入时的版本复现。
      </div>
    </div>
  )
}
