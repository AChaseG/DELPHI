/* Thin API client. Each browser gets a persistent anonymous user id so
   feeds/alerts are per-user. */
const USER_ID = (() => {
  let id = localStorage.getItem("gnd_user_id");
  if (!id) {
    id = "u-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem("gnd_user_id", id);
  }
  return id;
})();

async function api(path, options = {}) {
  const resp = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": USER_ID,
      ...(options.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* not json */ }
    throw new Error(detail);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

const API = {
  meta: () => api("/api/meta"),
  sources: () => api("/api/sources"),
  addSource: (body) => api("/api/sources", { method: "POST", body: JSON.stringify(body) }),
  patchSource: (id, body) => api(`/api/sources/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSource: (id) => api(`/api/sources/${id}`, { method: "DELETE" }),
  trackTopic: (query) => api("/api/sources/topic-tracker", { method: "POST", body: JSON.stringify({ query }) }),

  feeds: () => api("/api/feeds"),
  createFeed: (body) => api("/api/feeds", { method: "POST", body: JSON.stringify(body) }),
  updateFeed: (id, body) => api(`/api/feeds/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteFeed: (id) => api(`/api/feeds/${id}`, { method: "DELETE" }),
  reorderFeeds: (order) => api("/api/feeds/reorder", { method: "POST", body: JSON.stringify({ order }) }),
  feedArticles: (id) => api(`/api/feeds/${id}/articles`),

  alerts: () => api("/api/alerts"),
  createAlert: (body) => api("/api/alerts", { method: "POST", body: JSON.stringify(body) }),
  updateAlert: (id, body) => api(`/api/alerts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteAlert: (id) => api(`/api/alerts/${id}`, { method: "DELETE" }),
  alertEvents: (id) => api(`/api/alerts/${id}/events`),
  markAlertSeen: (id) => api(`/api/alerts/${id}/mark-seen`, { method: "POST" }),

  search: (criteria, sort = "newest", limit = 60) =>
    api(`/api/articles/search?sort=${sort}&limit=${limit}`, { method: "POST", body: JSON.stringify({ criteria }) }),
  validateQuery: (query) => api("/api/query/validate", { method: "POST", body: JSON.stringify({ query }) }),
  runIngest: () => api("/api/ingest/run", { method: "POST" }),
  seedDemo: () => api("/api/demo/seed", { method: "POST" }),
};
