import { useMemo, useState } from 'react'
import Plot from './Plot.jsx'

/** 分析页测量叠加：理论掩模 / 校准后测量 / 保守包络。 */
export default function MeasurementOverlay({ overlay }) {
  const carriers = overlay?.carriers || []
  const measured = carriers.filter((c) => c.confirmed_batch)
  const [selected, setSelected] = useState(null)
  const focus = measured.find((c) => c.carrier_name === selected)
    || measured[0] || null

  const data = useMemo(() => {
    if (!focus) return []
    const traces = [
      {
        x: focus.f_mhz, y: focus.theory_dbm_hz, mode: 'lines', type: 'scattergl',
        name: `理论掩模 (${focus.mask_name})`,
        line: { color: '#4da3ff', width: 1.8, dash: 'dash' },
        connectgaps: false,
        hovertemplate: '理论 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
      },
    ]
    if (focus.measured_dbm_hz) {
      traces.push({
        x: focus.f_mhz, y: focus.measured_dbm_hz, mode: 'lines', type: 'scattergl',
        name: '校准后测量',
        line: { color: '#f5a623', width: 1.4 },
        connectgaps: false,
        hovertemplate: '实测 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
      })
      traces.push({
        x: focus.f_mhz, y: focus.envelope_dbm_hz, mode: 'lines', type: 'scattergl',
        name: '保守包络 max(理论, 实测)',
        line: { color: '#ff5d5d', width: 2.4 },
        connectgaps: false,
        hovertemplate: '包络 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
      })
    }
    return traces
  }, [focus])

  if (!carriers.length) return null

  const layout = {
    height: 320,
    margin: { l: 58, r: 16, t: 30, b: 44 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b8c4cf', size: 11 },
    title: {
      text: focus
        ? `测量叠加 · ${focus.carrier_name}（${focus.polarization}, ${focus.power_dbm} dBm）`
        : '测量叠加（尚无经确认的测量批次）',
      font: { size: 12 },
    },
    xaxis: { title: '频率 (MHz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    yaxis: { title: 'PSD (dBm/Hz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    legend: { orientation: 'h', y: -0.22, font: { size: 10 } },
    hovermode: 'closest',
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="muted" style={{ fontSize: 12 }}>
          当前校准：{overlay.active_calibration_version || '—'}
        </span>
        <span className="spacer" />
        {measured.length === 0 && (
          <span className="muted" style={{ fontSize: 12 }}>
            导入并确认批次后，此处叠加理论 / 实测 / 保守包络
          </span>
        )}
        {measured.map((c) => (
          <button key={c.carrier_name}
                  className={focus?.carrier_name === c.carrier_name ? 'on' : ''}
                  style={c.has_violation
                    ? { color: 'var(--error)', borderColor: 'var(--error)' } : undefined}
                  onClick={() => setSelected(c.carrier_name)}>
            {c.carrier_name}{c.has_violation ? ' ⚠' : ''}
          </button>
        ))}
      </div>
      <Plot data={data} layout={layout}
            revision={focus ? `${focus.carrier_name}:${focus.confirmed_batch?.id}` : 'empty'} />
      {focus?.has_violation && (
        <div className="err-msg" style={{ marginTop: 4 }}>
          测量发现越限：校准后曲线高出理论掩模，最大偏差 {focus.max_excess_db} dB
          （批次 {focus.confirmed_batch.batch_ref}）；关联计划已置为过期，需重新评估。
        </div>
      )}
      {focus && !focus.has_violation && (
        <div className="hint" style={{ marginTop: 4 }}>
          保守包络在 dB 域逐点取理论与实测最大值（等价于线性 W/Hz 的最差情况），
          规划器可用它反算间隔并做 post-check。
        </div>
      )}
    </div>
  )
}
