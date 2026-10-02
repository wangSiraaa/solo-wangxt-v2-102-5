import { useMemo } from 'react'
import Plot from './Plot.jsx'

const POL_COLOR = { H: '#4da3ff', V: '#f5a623', LHCP: '#b08cff', RHCP: '#3ecf8e' }

/** 发射谱：各载波掩模曲线（细）+ 线性域功率叠加的聚合谱（粗白）；
 *  可叠加测量批次的 理论 / 实测（校准后）/ 保守包络 三条曲线。 */
export default function SpectrumChart({ spectrum, bands, overlay, showCarriers = true }) {
  const { f_mhz: f, curves = [], aggregate_dbm_hz: agg = [] } = spectrum || {}

  const data = useMemo(() => {
    const traces = []
    if (showCarriers) {
      for (const c of curves) {
        traces.push({
          x: f, y: c.psd_dbm_hz,
          mode: 'lines', type: 'scattergl',
          name: `${c.name} (${c.polarization}, ${c.mask_name})`,
          line: { color: POL_COLOR[c.polarization] || '#8595a3', width: 1 },
          opacity: 0.55,
          connectgaps: false,
          hovertemplate: `${c.name} %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>`,
        })
      }
    }
    // 载波频带背景（形状）在 layout 里；聚合谱置顶
    traces.push({
      x: f, y: agg,
      mode: 'lines', type: 'scattergl',
      name: '聚合谱（线性域叠加）',
      line: { color: '#ffffff', width: 2.2 },
      connectgaps: false,
      hovertemplate: '聚合 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
    })
    // 测量批次叠加：理论@测点、实测（校准后）、保守包络
    if (overlay?.freq_mhz?.length) {
      const label = overlay.label || '测量'
      traces.push({
        x: overlay.freq_mhz, y: overlay.theory_dbm_hz,
        mode: 'lines', type: 'scattergl',
        name: `理论@测点（${label}）`,
        line: { color: '#8595a3', width: 1.2, dash: 'dot' },
        connectgaps: false,
        hovertemplate: '理论 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
      })
      traces.push({
        x: overlay.freq_mhz, y: overlay.calibrated_dbm_hz,
        mode: 'lines+markers', type: 'scattergl',
        name: `实测·校准后（${label}）`,
        line: { color: '#3ecfcf', width: 1.6 },
        marker: { size: 4, color: '#3ecfcf' },
        connectgaps: false,
        hovertemplate: '实测 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
      })
      traces.push({
        x: overlay.freq_mhz, y: overlay.envelope_dbm_hz,
        mode: 'lines', type: 'scattergl',
        name: `保守包络 max(理论,实测)（${label}）`,
        line: { color: '#ff7ad9', width: 2.4, dash: 'dash' },
        connectgaps: false,
        hovertemplate: '包络 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
      })
    }
    return traces
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spectrum, showCarriers, overlay])

  const shapes = useMemo(() => (bands || []).map((b) => ({
    type: 'rect', x0: b.low_mhz, x1: b.high_mhz, y0: 0, y1: 1, yref: 'paper',
    fillcolor: POL_COLOR[b.polarization] || '#888',
    opacity: 0.06, line: { width: 0 },
  })), [bands])

  const layout = {
    height: 340,
    margin: { l: 58, r: 16, t: 28, b: 44 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b8c4cf', size: 11 },
    title: { text: overlay
      ? '发射谱 · 理论 / 实测（校准后）/ 保守包络叠加'
      : '发射谱与掩模尾部（聚合在 W/Hz 线性域叠加后换算 dB）', font: { size: 12 } },
    xaxis: { title: '频率 (MHz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    yaxis: { title: 'PSD (dBm/Hz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    legend: { orientation: 'h', y: -0.22, font: { size: 10 } },
    shapes,
    hovermode: 'closest',
  }

  return <Plot data={data} layout={layout}
               revision={JSON.stringify({ n: curves.length, o: overlay?.freq_mhz?.length || 0 })} />
}
