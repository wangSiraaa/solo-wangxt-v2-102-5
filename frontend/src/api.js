const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  health: () => request('/health'),
  masks: () => request('/masks'),
  listScenarios: () => request('/scenarios'),
  getScenario: (id) => request(`/scenarios/${id}`),
  createScenario: (payload) =>
    request('/scenarios', { method: 'POST', body: JSON.stringify(payload) }),
  updateScenario: (id, payload) =>
    request(`/scenarios/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteScenario: (id) =>
    request(`/scenarios/${id}`, { method: 'DELETE' }),
  analyze: (payload) =>
    request('/analyze', { method: 'POST', body: JSON.stringify(payload) }),
  plan: (payload) =>
    request('/plan', { method: 'POST', body: JSON.stringify(payload) }),

  // ---- 测量批次 / 校准 / 规划记录 ----
  calibrations: () => request('/calibrations'),
  createCalibration: (payload) =>
    request('/calibrations', { method: 'POST', body: JSON.stringify(payload) }),
  measurementOverlay: (scenarioId) =>
    request(`/scenarios/${scenarioId}/measurements/overlay`),
  listBatches: (scenarioId) =>
    request(`/scenarios/${scenarioId}/measurements/batches`),
  getBatch: (id) => request(`/measurements/batches/${id}`),
  importSweepText: (scenarioId, text, format = 'csv') =>
    request(`/scenarios/${scenarioId}/measurements/import-text`, {
      method: 'POST', body: JSON.stringify({ text, format }),
    }),
  confirmBatch: (id) =>
    request(`/measurements/batches/${id}/confirm`, { method: 'POST' }),
  planRun: (payload) =>
    request('/plan-runs', { method: 'POST', body: JSON.stringify(payload) }),
  listPlanRuns: (scenarioId) =>
    request(`/scenarios/${scenarioId}/plan-runs`),
  exportBundle: (scenarioId) =>
    request(`/scenarios/${scenarioId}/measurements/export`),
  importBundle: (scenarioId, bundle) =>
    request(`/scenarios/${scenarioId}/measurements/import-bundle`, {
      method: 'POST', body: JSON.stringify({ bundle }),
    }),
}
