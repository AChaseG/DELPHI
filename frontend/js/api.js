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
    // Display only. Every distance is stored, sent and compared in kilometres
    // — the API, the database and the geo maths never see a mile. Converting
    // at the edge is the difference between a preference and a whole class of
    // unit bug, which is a lesson aerospace paid more for than we would.
    units: "km",          // km | mi
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
  return statusText(status);
}

/* What a bare status code means here, in the terms of this application.

   A status alone tells a reader nothing: "HTTP 409" is not a sentence, and
   every one of these means a different next move — wait, sign in again, change
   what you typed, or report it. Only reached when the server sent no message of
   its own, which for anything but a crash or a proxy is unusual. */
const STATUS_MEANING = {
  400: "The server rejected the request as malformed. This is a bug — please report it.",
  403: "You don't have permission to do that. If it's a Pantheon item, the "
       + "owner or an admin has to make the change.",
  404: "That no longer exists — it was probably deleted in another tab or by "
       + "someone else on a shared board. Reload to see the current state.",
  409: "Something else changed that first. Reload and try again.",
  413: "That was too large for the server to accept.",
  422: "The server couldn't make sense of one of the values.",
  429: "You're going faster than the server allows. Wait a moment, then retry.",
  500: "The server hit an internal error. Nothing was saved.",
  502: "The server is unreachable behind its proxy — it may be mid-restart.",
  503: "The server is temporarily unable to answer — usually a restart or a "
       + "burst of load. Try again shortly.",
  504: "The server took too long to answer.",
};

function statusText(status) {
  if (!status) return "The request failed before it reached the server.";
  return STATUS_MEANING[status] || `The server answered with an unexpected status (HTTP ${status}).`;
}

/* Which operation failed, in the reader's words rather than the route's.

   "Request failed" names nothing, and a reader who has to report a problem
   can only say "it broke". Paths are matched most-specific-first. */
const OPERATION_NAMES = [
  [/^\/api\/auth\/register/, "create your account"],
  [/^\/api\/auth\/login/, "sign you in"],
  [/^\/api\/auth\/forgot/, "start a password reset"],
  [/^\/api\/auth\/reset/, "set your new password"],
  [/^\/api\/auth\/verify|resend-verification/, "verify your email address"],
  [/^\/api\/auth\/change-password/, "change your password"],
  [/^\/api\/auth\/sign-out-everywhere/, "sign you out of every device"],
  [/^\/api\/stream\/ticket/, "connect to live updates"],
  [/^\/api\/feeds\/\d+\/articles/, "load a feed's articles"],
  [/^\/api\/feeds\/\d+\/events/, "load a feed's events"],
  [/^\/api\/feeds\/reorder/, "save the column order"],
  [/^\/api\/feeds\/\d+\/share/, "share that feed"],
  [/^\/api\/feeds/, "load or save your feeds"],
  [/^\/api\/alerts\/\d+\/events/, "load an alert's history"],
  [/^\/api\/alerts\/\d+\/mark-seen/, "mark those alert hits as seen"],
  [/^\/api\/alerts/, "load or save your alerts"],
  [/^\/api\/articles\/search-grouped/, "group a column's articles into events"],
  [/^\/api\/articles\/search/, "run that search"],
  [/^\/api\/story\/\d+\/export/, "export this story"],
  [/^\/api\/story\//, "open the story"],
  [/^\/api\/sources\/\d+\/repair/, "repair that source"],
  [/^\/api\/sources\/(topic|social)-tracker/, "add that tracker"],
  [/^\/api\/sources/, "load or save sources"],
  [/^\/api\/pantheons/, "work with your Pantheons"],
  [/^\/api\/locations/, "work with your favourite locations"],
  [/^\/api\/geo\/search/, "look that place up"],
  [/^\/api\/admin\//, "run that operator action"],
  [/^\/api\/ingest\/run/, "start a poll of every source"],
  [/^\/api\/events\/rebuild/, "rebuild the event clusters"],
  [/^\/api\/session\/settings/, "save your settings"],
  [/^\/api\/meta/, "load the dashboard's headline numbers"],
];

function operationName(path) {
  const clean = String(path).split("?")[0];
  for (const [pattern, name] of OPERATION_NAMES)
    if (pattern.test(clean)) return name;
  return "";
}

/* Why a signed-in session stopped being one. The gate reads this back after the
   reload, so "you were signed out" comes with the reason rather than leaving
   the reader to guess between an expiry, a suspension, and a bug. */
async function signOutReason(resp) {
  let detail = "";
  try { detail = (await resp.clone().json()).detail || ""; } catch (e) { /* no body */ }
  if (detail) return detail;
  return "Your session expired — sessions last 30 days. Sign in to carry on; "
       + "nothing of yours was lost.";
}

/* The account is in use on as many devices as it is allowed to be, and this is
   one too many.

   It takes over the page rather than raising a toast because it is not a
   failed action — nothing on the board can load, so leaving the dashboard
   visible behind a notice would be a lie about what the reader is looking at.
   Shown once however many requests were refused; the flag is what stops a
   board of eight columns stacking eight identical copies of it.

   The way out is an emailed link, because the two obvious alternatives are
   both worse: signing out one of the other devices from here would let anyone
   holding the password evict the person actually using the account, and
   waiting for the activity window to lapse strands someone who has genuinely
   lost the other device. */
let deviceLimitShown = false;

function showDeviceLimit(message) {
  if (deviceLimitShown) return;
  deviceLimitShown = true;

  const wrap = document.createElement("div");
  wrap.className = "modal-backdrop";
  wrap.innerHTML = `
    <div class="modal narrow">
      <div class="modal-head"><h2>This account is in use elsewhere</h2></div>
      <div class="modal-body">
        <p></p>
        <p>If you no longer have the other devices — or you'd rather start
           fresh — we can email you a link that signs the account out of all of
           them. Every device will need to sign in again, including this one.</p>
        <label class="field"><span>Your email address</span>
          <input id="devlimit-email" type="email" autocomplete="email"
                 placeholder="you@example.com"></label>
        <p id="devlimit-status" class="s-meta"></p>
      </div>
      <div class="modal-foot">
        <button id="devlimit-send" class="btn btn-primary">Email me a sign-out link</button>
        <span class="spacer"></span>
        <button id="devlimit-retry" class="btn">Try again</button>
      </div>
    </div>`;
  // The server's sentence, set as text rather than interpolated into the
  // markup above — safe today, and it stays safe the day it carries a value
  // somebody else supplied.
  wrap.querySelector(".modal-body p").textContent = message;
  document.body.appendChild(wrap);

  const status = wrap.querySelector("#devlimit-status");
  wrap.querySelector("#devlimit-retry").onclick = () => location.reload();
  wrap.querySelector("#devlimit-send").onclick = async () => {
    const email = (wrap.querySelector("#devlimit-email").value || "").trim();
    if (!email) { status.textContent = "Enter the address the account uses."; return; }
    status.textContent = "Sending…";
    try {
      const r = await fetch("/api/auth/devices/release-link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const body = await r.json().catch(() => ({}));
      // Deliberately the same words whether or not that address has an
      // account: this endpoint is reachable without signing in, and a
      // different answer would turn it into a way to test addresses.
      status.textContent = body.mail_enabled === false
        ? "This server has no email configured, so it cannot send the link. "
          + "An operator can clear the devices from the admin console."
        : "If that address has an account here, the link is on its way. "
          + "It is valid for one hour.";
    } catch (e) {
      status.textContent = "Could not reach the server. Check your connection "
                         + "and try again.";
    }
  };
}

/* The paywall, raised by the first 402 that comes back.

   Every panel on the board makes its own request, so without this a reader
   whose access ran out would get twenty copies of the same message as twenty
   separate errors, and no way to act on any of them. Same shape as the
   device-limit screen above and for the same reason: one thing on screen, with
   the two ways forward on it.

   The screen is the whole answer, so it does not offer to dismiss itself.
   Signing out is there because being stuck on a paywall with no way back to
   the sign-in card is how somebody ends up locked out of an account they have
   another way to pay for. */
let paywallShown = false;

function showPaywall(status) {
  if (paywallShown) return;
  paywallShown = true;
  const s = status || {};
  const price = s.price_label || "";
  const trial = s.trial_days
    ? `New accounts get ${s.trial_days} day${s.trial_days === 1 ? "" : "s"} free.` : "";

  const wrap = document.createElement("div");
  wrap.className = "modal-backdrop";
  wrap.innerHTML = `
    <div class="modal narrow">
      <div class="modal-head"><h2>🏛 Your access has ended</h2></div>
      <div class="modal-body">
        <p id="pay-blurb"></p>
        <p id="pay-price" class="pay-price"></p>
        <p id="pay-trial" class="s-meta"></p>
        <p class="s-meta">Payment is handled by Stripe — Delphi never sees your
           card. You can cancel at any time from the same place.</p>
        <details>
          <summary>I have an invitation code</summary>
          <label class="field"><span>Code</span>
            <input id="pay-code" type="text" placeholder="ABCD-EFGH-JKLM"
                   autocomplete="off" spellcheck="false"></label>
          <button id="pay-redeem" class="btn">Use this code</button>
        </details>
        <p id="pay-status" class="s-meta"></p>
      </div>
      <div class="modal-foot">
        <button id="pay-subscribe" class="btn btn-primary">Subscribe</button>
        <span class="spacer"></span>
        <button id="pay-signout" class="btn">Sign out</button>
      </div>
    </div>`;
  // Every one of these is set as text, not markup: the blurb is the operator's
  // to write and the status line carries the server's words.
  wrap.querySelector("#pay-blurb").textContent = s.blurb
    || "Delphi is a paid service on this instance.";
  wrap.querySelector("#pay-price").textContent = price ? `${price}` : "";
  wrap.querySelector("#pay-trial").textContent = trial;
  document.body.appendChild(wrap);

  const line = wrap.querySelector("#pay-status");
  wrap.querySelector("#pay-signout").onclick = () => { Session.clear(); location.reload(); };
  wrap.querySelector("#pay-subscribe").onclick = async () => {
    line.textContent = "Opening Stripe…";
    try {
      const r = await api("/api/billing/checkout", { method: "POST" });
      if (r && r.url) location.assign(r.url);
      else line.textContent = "Stripe did not return a checkout page. Try again shortly.";
    } catch (e) {
      line.textContent = e.message;
    }
  };
  wrap.querySelector("#pay-redeem").onclick = async () => {
    const code = (wrap.querySelector("#pay-code").value || "").trim();
    if (!code) { line.textContent = "Enter the code you were given."; return; }
    line.textContent = "Checking…";
    try {
      await api("/api/billing/redeem", { method: "POST", body: JSON.stringify({ code }) });
      line.textContent = "That worked — reloading.";
      location.reload();
    } catch (e) {
      line.textContent = e.message;
    }
  };
}

/* Set while this browser is swapping its own token for a new one.

   Changing a password mints a replacement, and the old token stops being
   accepted the instant the server commits — which is before the reply carrying
   the new one has arrived. Anything already in flight in that window (the
   stream ticket, the update poll, a board refresh) comes back 401 meaning "your
   token was rotated", not "your session ended". Treating those alike reloads
   the page and dumps the reader at the sign-in screen for doing the right
   thing; measured in a browser, it happened in roughly one attempt in six. */
let rotation = null;

/* This browser's identity as a *device*, so the server can tell how many
   places an account is being used in at once.

   Kept in local storage rather than derived from the browser, because the
   question is "is this the laptop or the phone" and the honest way to know
   that is to have written it down here the first time. Every tab in this
   browser reads the same value, which is the point: four tabs on one laptop
   are one device, and a limit that counted tabs would be unusable.

   It identifies, it never authenticates. The session token proves who the
   account is; this only says which of that account's devices is asking, so a
   copied value gains nothing that the token did not already grant. Clearing
   site data or opening a private window makes a new one — the honest limit of
   recognising a browser without fingerprinting it. */
function deviceKey() {
  let key = localStorage.getItem("gnd_device");
  if (!key || !/^[A-Za-z0-9_-]{8,64}$/.test(key)) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    key = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    localStorage.setItem("gnd_device", key);
  }
  return key;
}

async function api(path, options = {}) {
  const sentWith = Session.token();
  const headers = {
    "Content-Type": "application/json",
    "X-Delphi-Device": deviceKey(),
    ...(options.headers || {}),
  };
  if (Session.token()) headers["Authorization"] = "Bearer " + Session.token();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), options.timeout || API_TIMEOUT_MS);
  const doing = operationName(path);
  const trying = doing ? `Couldn't ${doing}` : "The request failed";
  let resp;
  try {
    resp = await fetch(path, { ...options, headers, signal: ctl.signal });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error(
        `${trying}: the server didn't respond within `
        + `${Math.round((options.timeout || API_TIMEOUT_MS) / 1000)}s. `
        + "It may be restarting or overloaded — try again in a moment.");
    }
    // fetch() rejects identically for "no network", "DNS failed", "server
    // refused" and "blocked by an extension", so say which of those the
    // browser can still rule out rather than asserting one of them.
    throw new Error(
      `${trying}: the browser couldn't reach the server. `
      + (navigator.onLine
         ? "This device is online, so the server itself may be down or blocked "
           + "by a proxy or browser extension."
         : "This device reports it is offline — check your connection."));
  } finally {
    clearTimeout(timer);
  }
  if (resp.status === 401 && Session.token()) {
    // Before concluding the session is over, rule out the one case that looks
    // identical and isn't: this browser replaced its own token while the
    // request was in the air. Wait for any rotation to settle, and if the
    // token really did change underneath us, the request was simply sent with
    // the previous one — send it again with the current one. Only retried
    // once, and only when the token demonstrably changed, so a genuinely dead
    // session still lands on the sign-out below.
    if (!options.afterRotation) {
      if (rotation) await rotation.catch(() => {});
      if (Session.token() && Session.token() !== sentWith)
        return api(path, { ...options, afterRotation: true });
    }
    // Expired or revoked mid-session. Say so before the reload, or the board
    // simply vanishes back to the sign-in card with no explanation.
    Session.clear();
    try { sessionStorage.setItem("gnd_signed_out_reason", await signOutReason(resp)); }
    catch (e) { /* private mode: the gate just won't have the note */ }
    location.reload();
    return new Promise(() => {});
  }
  if (!resp.ok) {
    let detail = null;
    let reference = resp.headers.get("X-Delphi-Error") || "";
    let code = "";
    let billingStatus = null;
    try {
      const body = await resp.json();
      detail = body.detail ?? null;
      reference = body.reference || reference;
      code = body.code || "";
      billingStatus = body.billing || null;
    } catch (e) { /* not json — a proxy page, or an empty body */ }
    // The device limit is refused per request, so without this every panel on
    // the board would raise its own copy of the same message and the reader
    // would get a pile of toasts instead of one thing to do about it.
    if (code === "device_limit") {
      showDeviceLimit(errorText(detail, resp.status));
      return new Promise(() => {});
    }
    // Access ran out. Same treatment: one screen, and the requests that were
    // in flight behind it never resolve rather than each raising its own copy.
    if (resp.status === 402 || code === "payment_required") {
      showPaywall(billingStatus);
      return new Promise(() => {});
    }
    let message = `${trying} — ${errorText(detail, resp.status)}`;
    // The reference ties this exact failure to one line in the server log.
    if (reference && !message.includes(reference))
      message += ` (reference ${reference})`;
    const err = new Error(message);
    err.status = resp.status;
    err.reference = reference;
    throw err;
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
  // The focused view: one report, plus the story around it when other outlets
  // are carrying the same thing. Openable from either end.
  story: (id) => api(`/api/story/${id}?x=1${langQS()}`),
  storyByEvent: (id) => api(`/api/story/by-event/${id}?x=1${langQS()}`),
  // Which of a column's words are in one of its articles, and where.
  whyItMatched: (feedId, articleId) => api(`/api/feeds/${feedId}/why/${articleId}`),

  // Access and payment.
  billingStatus: () => api("/api/billing/status"),
  checkout: () => api("/api/billing/checkout", { method: "POST" }),
  billingPortal: () => api("/api/billing/portal", { method: "POST" }),
  redeemInvite: (code) => api("/api/billing/redeem",
                              { method: "POST", body: JSON.stringify({ code }) }),
  adminBilling: () => api("/api/admin/billing"),
  saveAdminBilling: (settings) => api("/api/admin/billing",
                                      { method: "PUT", body: JSON.stringify({ settings }) }),
  adminInvites: () => api("/api/admin/invites"),
  createInvite: (body) => api("/api/admin/invites",
                              { method: "POST", body: JSON.stringify(body) }),
  revokeInvite: (id, undo) => api(`/api/admin/invites/${id}/revoke`,
                                  { method: "POST", body: JSON.stringify({ undo: !!undo }) }),
  compUser: (id, comped, note) => api(`/api/admin/users/${id}/comp`,
                                      { method: "POST", body: JSON.stringify({ comped, note }) }),
  markEventViewed: (id) => api(`/api/events/${id}/viewed`, { method: "POST" }),
  // For an article with no event — nothing else can remember it.
  markArticleViewed: (id) => api(`/api/articles/${id}/viewed`, { method: "POST" }),
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

  // Typhon: whatever is burning inside the rectangle the map is showing.
  // `geometry` is opt-in: a fire perimeter is far larger than the row that
  // carries it and is sub-pixel below about zoom 6, so the map asks for shapes
  // only when it is close enough for one to mean anything.
  hazards: (bbox, limit = 300, geometry = false, kinds = null) =>
    api(`/api/hazards?bbox=${encodeURIComponent(bbox)}&limit=${limit}`
        + (geometry ? "&geometry=1" : "")
        // Only the kinds currently switched on. Somebody watching earthquakes
        // alone should not be shipped every air-quality station in view.
        + (kinds && kinds.length
           ? `&kind=${encodeURIComponent(kinds.join(","))}` : "")),

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

  signupInfo: () => api("/api/auth/signup-info"),
  register: (username, email, password, invite) =>
    api("/api/auth/register",
        { method: "POST", body: JSON.stringify({ username, email, password, invite }) }),
  login: (username, password) =>
    api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  verifyEmail: (token) => api("/api/auth/verify?token=" + encodeURIComponent(token)),
  resendVerification: (username) =>
    api("/api/auth/resend-verification", { method: "POST", body: JSON.stringify({ username }) }),
  forgotPassword: (email) =>
    api("/api/auth/forgot", { method: "POST", body: JSON.stringify({ email }) }),
  resetPassword: (token, password) =>
    api("/api/auth/reset", { method: "POST", body: JSON.stringify({ token, password }) }),
  releaseDevices: (token) =>
    api("/api/auth/devices/release", { method: "POST", body: JSON.stringify({ token }) }),
  adminDevices: (uid) => api(`/api/admin/users/${uid}/devices`),
  adminSetDeviceLimit: (uid, limit) =>
    api(`/api/admin/users/${uid}/device-limit`,
        { method: "POST", body: JSON.stringify({ limit }) }),
  adminReleaseDevices: (uid) =>
    api(`/api/admin/users/${uid}/devices/release`, { method: "POST" }),
  /* Adopts the new token itself rather than leaving that to the caller: the
     old one is dead from the moment the server commits, so the gap between
     the reply arriving and the session being updated is a window in which
     every other request fails. Owning both here makes that gap as small as it
     can be, and `rotation` covers what is left of it. */
  changePassword: async (current, next) => {
    let settle;
    rotation = new Promise((resolve) => { settle = resolve; });
    try {
      const r = await api("/api/auth/change-password", {
        method: "POST", body: JSON.stringify({ current, new: next }),
        // This request must not wait on the rotation it is itself performing.
        afterRotation: true,
      });
      Session.set(r.token, r.username, r.user_key);
      return r;
    } finally {
      rotation = null;
      settle();
    }
  },
  signOutEverywhere: () => api("/api/auth/sign-out-everywhere", { method: "POST" }),
  // Long and blocking by nature — give it far more than the usual patience.
  reclaimSpace: () => api("/api/maintenance/reclaim-space",
                          { method: "POST", timeout: 15 * 60 * 1000 }),
  // Short-lived credential for the event stream; see connectStream in app.js.
  streamTicket: () => api("/api/stream/ticket", { method: "POST" }),
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
