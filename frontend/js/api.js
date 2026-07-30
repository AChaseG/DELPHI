/* Thin API client. An account is required to use the system, so every request
   is authenticated with the signed-in account's bearer token. */

/* Display & notification preferences. localStorage is the fast local copy;
   the account's server-side copy is authoritative so settings follow the
   user across browsers, devices, and origin changes (each Codespace URL is
   a fresh origin with empty localStorage — settings must not live only
   there). Boot adopts the account copy; every change writes through. */
const Settings = {
  defaults: {
    theme: "dark",        // dark | light | system
    timefmt: "relative",  // relative | local | utc | dtg
    toast_pos: "br",      // br | bl | tr | tl
    volume: 40,           // 0-100 alert sound volume (0 = silent)
    desktop_notif: false, // browser notifications when tab is hidden
    compact: false,       // hide summaries/thumbnails
    stale_hours: 48,      // hide-stale threshold for feeds that opt in (0 = never)
    // Per-column pixel widths, keyed the same way as the feed cache
    // ("feed:12" / "home:world"). Kept here rather than on the feed row so a
    // Pantheon member resizing a shared column doesn't relayout the board for
    // everyone else, and so Home's built-in columns can be resized at all.
    col_widths: {},
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
    this._push();
  },
  /* Merge the account's saved copy over this device's (server wins; local
     fills gaps). The reading language rides along in the same payload. */
  adopt(remote) {
    if (!remote || typeof remote !== "object") return;
    const { lang, ...prefs } = remote;
    localStorage.setItem("gnd_settings", JSON.stringify({ ...this._load(), ...prefs }));
    if (typeof lang === "string") localStorage.setItem("gnd_lang", lang);
  },
  _pushTimer: null,
  _push() {  // debounced write-through; losing one save to a crash is fine
    clearTimeout(this._pushTimer);
    this._pushTimer = setTimeout(() => {
      if (!Session.token()) return;
      api("/api/session/settings", {
        method: "PUT",
        body: JSON.stringify({ settings: { ...this._load(), lang: getLang() } }),
      }).catch(() => { /* offline — the local copy still applies */ });
    }, 800);
  },
};

/* Account session: the signed-in user's token scopes feeds/alerts to their
   account and authenticates every request. */
const Session = {
  token: () => localStorage.getItem("gnd_token") || "",
  username: () => localStorage.getItem("gnd_username") || "",
  userKey: () => localStorage.getItem("gnd_user_key") || "",
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

/* No request may hang forever. A server that stops answering (wedged worker,
   dropped connection, a proxy holding the response) would otherwise leave the
   UI stuck on "Creating your account…" with nothing to act on, which is
   indistinguishable from a broken button. Abort instead and surface it.
   Generous by design: slow is not the same as dead. */
const API_TIMEOUT_MS = 30000;

/* Turn any error payload into a sentence a person can act on.

   Endpoint errors are plain strings, but request-validation failures come back
   as FastAPI's list of {loc, msg} objects. Passing that straight to Error()
   stringifies it as "[object Object]", which is how a rejected save ends up
   telling the user nothing at all. */
function errorText(detail, status = 0) {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d) => {
      // loc is like ["body", "name"] — the last entry names the offending field.
      const field = Array.isArray(d.loc)
        ? d.loc.filter((p) => p !== "body" && typeof p === "string").pop()
        : null;
      const msg = (d.msg || "is invalid").replace(/^Value error,\s*/, "");
      return field ? `${field}: ${msg}` : msg;
    }).join("; ");
  }
  if (detail && typeof detail === "object" && detail.msg) return String(detail.msg);
  return status ? `Request failed (HTTP ${status})` : "Request failed";
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (Session.token()) headers["Authorization"] = "Bearer " + Session.token();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), options.timeout || API_TIMEOUT_MS);
  let resp;
  try {
    resp = await fetch(path, { ...options, headers, signal: ctl.signal });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error(
        `The server didn't respond within ${Math.round((options.timeout || API_TIMEOUT_MS) / 1000)}s. `
        + "It may be restarting or overloaded — try again in a moment.");
    }
    throw new Error("Couldn't reach the server — check your connection.");
  } finally {
    clearTimeout(timer);
  }
  if (resp.status === 401 && Session.token()) {
    Session.clear();  // expired session: return to the sign-in gate
    location.reload();
    return new Promise(() => {});
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail ?? detail; } catch (e) { /* not json */ }
    throw new Error(errorText(detail, resp.status));
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
function setLang(lang) {
  localStorage.setItem("gnd_lang", lang);
  Settings._push();  // the reading language syncs with the account too
}
const langQS = () => (getLang() ? `&lang=${encodeURIComponent(getLang())}` : "");

const API = {
  meta: () => api("/api/meta"),
  sources: () => api("/api/sources"),
  // Just id and name: what startup needs to label a restricted feed.
  sourceNames: () => api("/api/sources?slim=1"),
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
  feedArticles: (id) => api(`/api/feeds/${id}/articles?limit=40${langQS()}`),
  feedEvents: (id) => api(`/api/feeds/${id}/events?limit=30${langQS()}`),
  eventDetail: (id) => api(`/api/events/${id}?x=1${langQS()}`),
  // One story in full, for the focused view a headline opens.
  article: (id) => api(`/api/articles/${id}?x=1${langQS()}`),
  markEventViewed: (id) => api(`/api/events/${id}/viewed`, { method: "POST" }),
  rebuildEvents: () => api("/api/events/rebuild", { method: "POST" }),

  alerts: () => api("/api/alerts"),
  createAlert: (body) => api("/api/alerts", { method: "POST", body: JSON.stringify(body) }),
  updateAlert: (id, body) => api(`/api/alerts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteAlert: (id) => api(`/api/alerts/${id}`, { method: "DELETE" }),
  alertEvents: (id) => api(`/api/alerts/${id}/events?limit=50${langQS()}`),
  markAlertSeen: (id) => api(`/api/alerts/${id}/mark-seen`, { method: "POST" }),

  search: (criteria, sort = "newest", limit = 60) =>
    api(`/api/articles/search?sort=${sort}&limit=${limit}${langQS()}`, { method: "POST", body: JSON.stringify({ criteria }) }),
  searchGrouped: (criteria, sort = "newest", limit = 30) =>
    api(`/api/articles/search-grouped?sort=${sort}&limit=${limit}${langQS()}`, { method: "POST", body: JSON.stringify({ criteria }) }),
  validateQuery: (query) => api("/api/query/validate", { method: "POST", body: JSON.stringify({ query }) }),
  runIngest: () => api("/api/ingest/run", { method: "POST" }),

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

  adminUsers: (q = "") => api("/api/admin/users" + (q ? "?q=" + encodeURIComponent(q) : "")),
  adminSetDisabled: (id, disabled) => api(`/api/admin/users/${id}/disable`, { method: "POST", body: JSON.stringify({ disabled }) }),
  adminVerify: (id) => api(`/api/admin/users/${id}/verify`, { method: "POST" }),
  adminSetAdmin: (id, is_admin) => api(`/api/admin/users/${id}/admin`, { method: "POST", body: JSON.stringify({ is_admin }) }),
  adminResetPassword: (id, password) => api(`/api/admin/users/${id}/reset-password`, { method: "POST", body: JSON.stringify({ password }) }),
  adminDeleteUser: (id) => api(`/api/admin/users/${id}`, { method: "DELETE" }),

  locations: () => api("/api/locations"),
  createLocation: (body) => api("/api/locations", { method: "POST", body: JSON.stringify(body) }),
  updateLocation: (id, body) => api(`/api/locations/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteLocation: (id, keepFeed = false) =>
    api(`/api/locations/${id}?keep_feed=${keepFeed ? "true" : "false"}`, { method: "DELETE" }),
  shareLocation: (id, pantheonId) =>
    api(`/api/locations/${id}/share`, { method: "POST", body: JSON.stringify({ pantheon_id: pantheonId }) }),
  placeSearch: (q) => api(`/api/geo/search?q=${encodeURIComponent(q)}`),

  hello: () => api("/api/session/hello", { method: "POST" }),
  checkUpdates: () => api("/api/session/check-updates", { method: "POST" }),
  getSettings: () => api("/api/session/settings"),
  changelog: () => api("/api/changelog"),

  register: (username, email, password) =>
    api("/api/auth/register", { method: "POST", body: JSON.stringify({ username, email, password }) }),
  login: (username, password) =>
    api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  verifyEmail: (token) => api("/api/auth/verify?token=" + encodeURIComponent(token)),
  resendVerification: (username) =>
    api("/api/auth/resend-verification", { method: "POST", body: JSON.stringify({ username }) }),
  forgotPassword: (email) =>
    api("/api/auth/forgot", { method: "POST", body: JSON.stringify({ email }) }),
  resetPassword: (token, password) =>
    api("/api/auth/reset", { method: "POST", body: JSON.stringify({ token, password }) }),
};

/* ---------- map library, loaded when a map is actually opened ----------
   Leaflet and Leaflet.draw are a quarter of a megabyte of script and CSS that
   most sessions never use: the board has no map on it. They used to be in the
   page head, so every reader paid for them before the first column appeared.
   Now every caller awaits this first, and the files are fetched once. */
let leafletWanted = null;
function ensureLeaflet() {
  if (typeof L !== "undefined") return Promise.resolve();
  if (leafletWanted) return leafletWanted;
  const sheet = (href) => new Promise((ok, fail) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.onload = ok;
    link.onerror = () => fail(new Error(`could not load ${href}`));
    document.head.appendChild(link);
  });
  const script = (src) => new Promise((ok, fail) => {
    const tag = document.createElement("script");
    tag.src = src;
    tag.onload = ok;
    tag.onerror = () => fail(new Error(`could not load ${src}`));
    document.body.appendChild(tag);
  });
  // The stylesheets are independent, but leaflet.draw extends L, so it can only
  // run after Leaflet itself.
  leafletWanted = Promise.all([
    sheet("/vendor/leaflet/leaflet.css"),
    sheet("/vendor/leaflet-draw/leaflet.draw.css"),
    script("/vendor/leaflet/leaflet.js").then(() => script("/vendor/leaflet-draw/leaflet.draw.js")),
  ]).catch((e) => { leafletWanted = null; throw e; });   // a later open can retry
  return leafletWanted;
}
