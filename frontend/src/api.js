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
  exportScenario: (id) => request(`/scenarios/${id}/export`),
  importScenario: (doc) =>
    request('/scenarios/import', { method: 'POST', body: JSON.stringify(doc) }),
  analyze: (payload) =>
    request('/analyze', { method: 'POST', body: JSON.stringify(payload) }),
  plan: (payload) =>
    request('/plan', { method: 'POST', body: JSON.stringify(payload) }),
  // 校准版本
  listCalibrations: () => request('/calibrations'),
  createCalibration: (payload) =>
    request('/calibrations', { method: 'POST', body: JSON.stringify(payload) }),
  // 测量批次
  listMeasurements: (sid) => request(`/scenarios/${sid}/measurements`),
  getMeasurement: (sid, bid) => request(`/scenarios/${sid}/measurements/${bid}`),
  importMeasurement: (sid, filename, content) =>
    request(`/scenarios/${sid}/measurements/import`,
            { method: 'POST', body: JSON.stringify({ filename, content }) }),
  // 计划记录
  listPlans: (sid) => request(`/scenarios/${sid}/plans`),
  getPlan: (sid, pid) => request(`/scenarios/${sid}/plans/${pid}`),
}
