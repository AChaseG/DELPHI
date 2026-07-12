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

/* Display & notification preferences, persisted per browser. */
const Settings = {
  defaults: {
    theme: "dark",        // dark | light | system
    timefmt: "relative",  // relative | local | utc | dtg
    toast_pos: "br",      // br | bl | tr | tl
    volume: 40,           // 0-100 alert sound volume (0 = silent)
    desktop_notif: false, // browser notifications when tab is hidden
    compact: false,       // hide summaries/thumbnails
    stale_hours: 48,      // hide-stale threshold for feeds that opt in (0 = never)
  },
  _load() {
    try { return { ...this.defaults, ...(JSON.parse(localStorage.getItem("gnd_settings")) || {}) }; }
    catch { return { ...this.defaults }; }
  },
  get(key) { return this._load()[key]; },
  set(key, value) {
    const s = this._load();
    s[key] = value;
    localStorage.setItem("gnd_settings", JSON.stringify(s));
  },
};

/* Account session (optional): a signed-in user's token scopes feeds/alerts
   to their account instead of the anonymous browser profile. */
const Session = {
  token: () => localStorage.getItem("gnd_token") || "",
  username: () => localStorage.getItem("gnd_username") || "",
  userKey: () => localStorage.getItem("gnd_user_key") || USER_ID,
  set(token, username, userKey) {
    localStorage.setItem("gnd_token", token);
    localStorage.setItem("gnd_username", username);
    localStorage.setItem("gnd_user_key", userKey);
  },
  clear() {
    localStorage.removeItem("gnd_token");
    localStorage.removeItem("gnd_username");
    localStorage.removeItem("gnd_user_key");
  },
};

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-User-Id": USER_ID,
    ...(options.headers || {}),
  };
  if (Session.token()) headers["Authorization"] = "Bearer " + Session.token();
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401 && Session.token()) {
    Session.clear();  // expired session: fall back to the anonymous profile
    location.reload();
    return new Promise(() => {});
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* not json */ }
    throw new Error(detail);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

/* User's preferred reading language ("" = originals). Defaults to the
   browser's language so translation is automatic out of the box. */
function getLang() {
  const stored = localStorage.getItem("gnd_lang");
  if (stored !== null) return stored;
  return (navigator.language || "en").slice(0, 2).toLowerCase();
}
function setLang(lang) { localStorage.setItem("gnd_lang", lang); }
const langQS = () => (getLang() ? `&lang=${encodeURIComponent(getLang())}` : "");
const staleQS = () => `&stale=${Settings.get("stale_hours") || 0}`;

const API = {
  meta: () => api("/api/meta"),
  sources: () => api("/api/sources"),
  addSource: (body) => api("/api/sources", { method: "POST", body: JSON.stringify(body) }),
  patchSource: (id, body) => api(`/api/sources/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSource: (id) => api(`/api/sources/${id}`, { method: "DELETE" }),
  repairSource: (id) => api(`/api/sources/${id}/repair`, { method: "POST" }),
  trackTopic: (query) => api("/api/sources/topic-tracker", { method: "POST", body: JSON.stringify({ query }) }),
  trackSocial: (query) => api("/api/sources/social-tracker", { method: "POST", body: JSON.stringify({ query }) }),

  feeds: () => api("/api/feeds"),
  createFeed: (body) => api("/api/feeds", { method: "POST", body: JSON.stringify(body) }),
  updateFeed: (id, body) => api(`/api/feeds/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteFeed: (id) => api(`/api/feeds/${id}`, { method: "DELETE" }),
  reorderFeeds: (order) => api("/api/feeds/reorder", { method: "POST", body: JSON.stringify({ order }) }),
  feedArticles: (id) => api(`/api/feeds/${id}/articles?limit=40${langQS()}${staleQS()}`),
  feedEvents: (id) => api(`/api/feeds/${id}/events?limit=30${langQS()}${staleQS()}`),
  eventDetail: (id) => api(`/api/events/${id}?x=1${langQS()}`),
  markEventViewed: (id) => api(`/api/events/${id}/viewed`, { method: "POST" }),
  rebuildEvents: () => api("/api/events/rebuild", { method: "POST" }),

  alerts: () => api("/api/alerts"),
  createAlert: (body) => api("/api/alerts", { method: "POST", body: JSON.stringify(body) }),
  updateAlert: (id, body) => api(`/api/alerts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteAlert: (id) => api(`/api/alerts/${id}`, { method: "DELETE" }),
  alertEvents: (id) => api(`/api/alerts/${id}/events?limit=50${langQS()}`),
  markAlertSeen: (id) => api(`/api/alerts/${id}/mark-seen`, { method: "POST" }),

  search: (criteria, sort = "newest", limit = 60) =>
    api(`/api/articles/search?sort=${sort}&limit=${limit}${langQS()}${staleQS()}`, { method: "POST", body: JSON.stringify({ criteria }) }),
  searchGrouped: (criteria, sort = "newest", limit = 30) =>
    api(`/api/articles/search-grouped?sort=${sort}&limit=${limit}${langQS()}${staleQS()}`, { method: "POST", body: JSON.stringify({ criteria }) }),
  validateQuery: (query) => api("/api/query/validate", { method: "POST", body: JSON.stringify({ query }) }),
  runIngest: () => api("/api/ingest/run", { method: "POST" }),
  purgeDemo: () => api("/api/demo/purge", { method: "POST" }),

  pantheons: () => api("/api/pantheons"),
  createPantheon: (body) => api("/api/pantheons", { method: "POST", body: JSON.stringify(body) }),
  publicPantheons: () => api("/api/pantheons/public"),
  pantheonDetail: (id) => api(`/api/pantheons/${id}`),
  updatePantheon: (id, body) => api(`/api/pantheons/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deletePantheon: (id) => api(`/api/pantheons/${id}`, { method: "DELETE" }),
  invitePantheon: (id, user) => api(`/api/pantheons/${id}/invite`, { method: "POST", body: JSON.stringify({ user }) }),
  acceptInvite: (id) => api(`/api/pantheons/invites/${id}/accept`, { method: "POST" }),
  declineInvite: (id) => api(`/api/pantheons/invites/${id}/decline`, { method: "POST" }),
  joinPantheon: (id) => api(`/api/pantheons/${id}/join`, { method: "POST" }),
  leavePantheon: (id) => api(`/api/pantheons/${id}/leave`, { method: "POST" }),
  removeMember: (id, uid) => api(`/api/pantheons/${id}/members/${uid}`, { method: "DELETE" }),
  setMemberRole: (id, uid, role) => api(`/api/pantheons/${id}/members/${uid}/role`, { method: "POST", body: JSON.stringify({ role }) }),
  pantheonFeeds: (id) => api(`/api/pantheons/${id}/feeds`),
  shareFeed: (feedId, pantheonId) => api(`/api/feeds/${feedId}/share`, { method: "POST", body: JSON.stringify({ pantheon_id: pantheonId }) }),
  shareAlert: (alertId, pantheonId) => api(`/api/alerts/${alertId}/share`, { method: "POST", body: JSON.stringify({ pantheon_id: pantheonId }) }),

  hello: () => api("/api/session/hello", { method: "POST" }),
  checkUpdates: () => api("/api/session/check-updates", { method: "POST" }),
  changelog: () => api("/api/changelog"),

  register: (username, email, password) =>
    api("/api/auth/register", { method: "POST", body: JSON.stringify({ username, email, password }) }),
  login: (username, password) =>
    api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  claim: () => api("/api/auth/claim", { method: "POST" }),
  verifyEmail: (token) => api("/api/auth/verify?token=" + encodeURIComponent(token)),
  resendVerification: (username) =>
    api("/api/auth/resend-verification", { method: "POST", body: JSON.stringify({ username }) }),
  forgotPassword: (email) =>
    api("/api/auth/forgot", { method: "POST", body: JSON.stringify({ email }) }),
  resetPassword: (token, password) =>
    api("/api/auth/reset", { method: "POST", body: JSON.stringify({ token, password }) }),
};
