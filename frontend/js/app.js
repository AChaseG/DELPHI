/* Dashboard: feed board, alerts panel, sources panel, live SSE updates. */
let META = null;
let SOURCES = [];
let FEEDS = [];
let ALERTS = [];
let COUNTRY_NAMES = new Map();
let VIEW = localStorage.getItem("gnd_view") || "home";  // "home" | "mine"

/* Delphi-curated Home columns, generated live from the shared corpus. */
const DELPHI_FEEDS = [
  { home: "top", name: "🌍 Top events worldwide", criteria: { min_importance: 55 }, sort: "importance", group_events: true },
  { home: "breaking", name: "⚡ Breaking now", criteria: { hours: 6, min_importance: 40 }, sort: "importance" },
  { home: "conflict", name: "Conflict & disasters", criteria: { categories: ["conflict", "disaster"] }, sort: "newest", group_events: true },
  { home: "politics", name: "Politics & diplomacy", criteria: { categories: ["politics"] }, sort: "newest" },
  { home: "business", name: "Business & economy", criteria: { categories: ["business", "economy"] }, sort: "newest" },
  { home: "scitech", name: "Science & technology", criteria: { categories: ["science", "technology"] }, sort: "newest" },
  { home: "social", name: "💬 Social pulse", criteria: { platforms: ["reddit", "mastodon", "bluesky", "youtube"] }, sort: "newest" },
];

async function boot() {
  if (!Session.token()) {   // account required: show the sign-in gate only
    wireGate();
    el("gate").hidden = false;
    document.querySelector(".topbar").hidden = true;
    return;
  }
  [META, SOURCES] = await Promise.all([API.meta(), API.sources()]);
  COUNTRY_NAMES = new Map(META.countries.map(c => [c.iso2, c.name]));
  Builder.init(META, SOURCES);
  Builder.onSaved = async (mode, converted = false) => {
    // A conversion changes both lists: one side gains, the other loses.
    if (mode === "alert" || converted) await refreshAlerts();
    if (mode === "feed") {
      FEEDS = await API.feeds();
      setView("mine");  // show the user what they just created/edited
    } else if (converted) {
      await refreshFeeds();  // feed became an alert: drop its column
    }
  };
  wireTopbar();
  renderStats();
  await Promise.all([refreshFeeds(), refreshAlerts()]);
  connectStream();
  runOnboarding();
  setInterval(checkForUpdates, UPDATE_POLL_MS);
  // First run on an empty database: pull the catalog once in the background.
  if (META.stats.total_articles === 0) {
    toast("Fetching news", "First run — polling all sources. This can take a minute…");
    API.runIngest().then(async (r) => {
      const found = r.discovered ? ` · discovered ${r.discovered} new source${r.discovered === 1 ? "" : "s"}` : "";
      toast("Ingest complete", `${r.new_articles} articles from ${r.sources_ok}/${r.sources_total} sources${found}`);
      renderStats(await API.meta());
      refreshFeeds();
    }).catch(() => {});
  }
}

function setView(view) {
  VIEW = view;
  localStorage.setItem("gnd_view", view);
  el("btn-view-home").classList.toggle("active", view === "home");
  el("btn-view-mine").classList.toggle("active", view === "mine");
  renderBoard();
}

async function renderBoard() {
  const board = el("board");
  const searchCol = el("search-col");
  board.innerHTML = "";
  if (searchCol) board.appendChild(searchCol);
  if (VIEW === "home") {
    el("empty-state").hidden = true;
    for (const hf of DELPHI_FEEDS) board.appendChild(feedColumn(hf, /*readonly*/ true));
    await Promise.all(DELPHI_FEEDS.map(f => loadFeedArticles(f)));
  } else {
    el("empty-state").hidden = FEEDS.length > 0;
    for (const feed of FEEDS) board.appendChild(feedColumn(feed));
    await Promise.all(FEEDS.map(f => loadFeedArticles(f)));
  }
}

function wireTopbar() {
  // The board is a lateral rail: a vertical mouse wheel over it scrolls
  // sideways — except over a feed's own article list, which scrolls
  // vertically as usual.
  el("board").addEventListener("wheel", (e) => {
    if (!e.deltaY || e.deltaX || e.shiftKey) return;
    if (e.target.closest(".feed-body")) return;
    el("board").scrollLeft += e.deltaY;
    e.preventDefault();
  }, { passive: false });

  el("btn-view-home").onclick = () => setView("home");
  el("btn-view-mine").onclick = () => setView("mine");
  el("btn-view-home").classList.toggle("active", VIEW === "home");
  el("btn-view-mine").classList.toggle("active", VIEW === "mine");
  // Reading-language picker: articles in other languages are translated
  // automatically into this language (defaults to the browser language).
  const sel = el("lang-select");
  const langs = META.ui_languages || { en: "English" };
  sel.appendChild(new Option("Original languages", ""));
  for (const [code, name] of Object.entries(langs)) sel.appendChild(new Option(name, code));
  const current = getLang();
  sel.value = Object.prototype.hasOwnProperty.call(langs, current) ? current : "";
  if (sel.value !== current) setLang(sel.value);
  if (!META.translation.enabled) sel.title = "Translation is disabled on this server (NEWS_TRANSLATE_PROVIDER=off)";
  sel.onchange = async () => {
    setLang(sel.value);
    await Promise.all([refreshFeeds(), refreshAlerts()]);
  };

  wireAuth();
  wireSettings();
  applySettings();
  el("btn-create").onclick = () => Builder.open("feed");
  el("btn-empty-new-feed").onclick = () => Builder.open("feed");
  el("btn-starter-pack").onclick = starterPack;
  el("btn-refresh").onclick = async () => {
    el("btn-refresh").disabled = true;
    toast("Refreshing", "Polling all sources…");
    try {
      const r = await API.runIngest();
      const found = r.discovered ? ` · discovered ${r.discovered} new source${r.discovered === 1 ? "" : "s"}` : "";
      toast("Ingest complete", `${r.new_articles} new articles (${r.sources_ok}/${r.sources_total} sources ok)${found}`);
      renderStats(await API.meta());
      await refreshFeeds();
    } catch (e) {
      toast(e.message.includes("already running") ? "Refresh" : "Refresh failed", e.message);
    }
    el("btn-refresh").disabled = false;
  };
  el("btn-close-event").onclick = () => { el("event-backdrop").hidden = true; };
  el("event-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("event-backdrop")) el("event-backdrop").hidden = true;
  });
  el("btn-alerts-panel").onclick = () => { el("alerts-panel").hidden = false; renderAlertsPanel(); };
  el("btn-close-alerts").onclick = () => { el("alerts-panel").hidden = true; };
  el("btn-toggle-alerts-map").onclick = () => {
    localStorage.setItem("gnd_alerts_map", alertsMapWanted() ? "0" : "1");
    renderAlertsPanel();
  };
  el("btn-sources").onclick = () => { el("sources-panel").hidden = false; reloadSources(); };
  el("btn-close-sources").onclick = () => { el("sources-panel").hidden = true; };

  el("global-search").addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const q = e.target.value.trim();
    if (!q) { const c = el("search-col"); if (c) c.remove(); return; }
    const looksBoolean = /\b(AND|OR|NOT)\b|["()]/.test(q);
    const criteria = looksBoolean ? { query: q } : { keywords: [q] };
    const arts = await API.search(criteria, "newest", 60);
    renderSearchColumn(q, arts);
  });

  el("btn-track-topic").onclick = async () => {
    const q = el("topic-query").value.trim();
    if (!q) return;
    try {
      await API.trackTopic(q);
      el("topic-query").value = "";
      SOURCES = await API.sources();
      Builder.init(META, SOURCES);
      renderSourcesPanel();
      toast("Topic tracker added", `Now ingesting worldwide coverage of “${q}”. Refresh to pull articles.`);
    } catch (e) { toast("Could not add topic", e.message); }
  };
  // country dropdown for the add-source form
  const cs = el("src-country");
  cs.appendChild(new Option("🌐 Global", ""));
  for (const c of META.countries) cs.appendChild(new Option(`${flagEmoji(c.iso2)} ${c.name}`, c.iso2));

  el("btn-track-social").onclick = async () => {
    const q = el("topic-query").value.trim();
    if (!q) return;
    try {
      const r = await API.trackSocial(q);
      el("topic-query").value = "";
      await reloadSources();
      toast("Social trackers added", r.created.join(" · ") + " — refresh ⟳ to pull posts.");
    } catch (e) { toast("Could not add social trackers", e.message); }
  };

  el("btn-purge-demo").onclick = async () => {
    if (!confirm("Delete all demo/sample articles and local test sources? Real news is untouched.")) return;
    try {
      const r = await API.purgeDemo();
      toast("Demo data removed",
        `${r.articles} sample articles, ${r.sources} test sources, ${r.events} empty events deleted`);
      SOURCES = await API.sources();
      Builder.init(META, SOURCES);
      renderSourcesPanel();
      renderStats(await API.meta());
      await Promise.all([refreshFeeds(), refreshAlerts()]);
    } catch (e) { toast("Purge failed", e.message); }
  };
  el("btn-add-source").onclick = async () => {
    const name = el("src-name").value.trim(), url = el("src-url").value.trim();
    if (!name || !url) { toast("Missing fields", "A source needs a name and a feed URL."); return; }
    try {
      await API.addSource({
        name, rss_url: url,
        platform: el("src-platform").value,
        scope: el("src-scope").value,
        country: el("src-country").value,
      });
      el("src-name").value = ""; el("src-url").value = "";
      await reloadSources();
      toast("Source added", `${name} — refresh ⟳ to pull it for the first time.`);
    } catch (e) { toast("Could not add source", e.message); }
  };
}

/* ---------- settings ---------- */
function applySettings() {
  const theme = Settings.get("theme");
  const resolved = theme === "system"
    ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
    : theme;
  document.documentElement.dataset.theme = resolved;
  document.body.classList.toggle("compact", !!Settings.get("compact"));
  el("toasts").className = "toasts pos-" + (Settings.get("toast_pos") || "br");
}

let _audioCtx = null;
function alertSound() {
  const vol = +Settings.get("volume");
  if (!vol) return;
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const now = _audioCtx.currentTime;
    for (const [freq, at] of [[880, 0], [1175, 0.12]]) {  // two-tone chirp
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.25 * (vol / 100), now + at);
      gain.gain.exponentialRampToValueAtTime(0.001, now + at + 0.15);
      osc.connect(gain).connect(_audioCtx.destination);
      osc.start(now + at); osc.stop(now + at + 0.16);
    }
  } catch (e) { /* audio not available */ }
}

function desktopNotify(title, body) {
  if (!Settings.get("desktop_notif") || !("Notification" in window)) return;
  if (Notification.permission === "granted" && document.hidden) {
    try { new Notification(title, { body, icon: undefined }); } catch (e) { /* ignore */ }
  }
}

/* ---------- onboarding: first-visit FAQ + what's-new-while-away popup ----- */

let FAQ_AFTER_UPDATES = false;  // chain: close What's-new → open the FAQ

function showUpdates(entries, note = "") {
  const box = el("updates-body");
  box.innerHTML = "";
  for (const entry of entries) {   // newest first; one block per date
    const block = document.createElement("div");
    block.className = "update-entry";
    const date = document.createElement("div");
    date.className = "update-date";
    date.textContent = new Date(entry.date + "T00:00:00").toLocaleDateString([],
      { weekday: "long", year: "numeric", month: "long", day: "numeric" });
    const title = document.createElement("h3");
    title.className = "update-title";
    title.textContent = entry.title;
    const ul = document.createElement("ul");
    for (const item of entry.items) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    }
    block.append(date, title, ul);
    box.appendChild(block);
  }
  el("updates-note").textContent = note;
  el("updates-backdrop").hidden = false;
}

function closeUpdates() {
  el("updates-backdrop").hidden = true;
  if (FAQ_AFTER_UPDATES) {
    FAQ_AFTER_UPDATES = false;
    el("faq-backdrop").hidden = false;
  }
}

/* Live what's-new: open sessions learn about updates without a re-login.
   Checked on a timer and immediately after the event stream reconnects —
   a redeploy severs the stream, so reconnection is the "update just
   shipped" signal. */
const UPDATE_POLL_MS = 5 * 60 * 1000;

async function checkForUpdates() {
  let r;
  try { r = await API.checkUpdates(); } catch (e) { return; }
  if (r.updates && r.updates.length)
    showUpdates(r.updates, "Just shipped — new since you loaded this session.");
}

async function runOnboarding() {
  // One call per app load: the server compares "now" with when this account
  // last opened Delphi, and hands back what to show.
  let h;
  try { h = await API.hello(); } catch (e) { return; }
  if (h.updates && h.updates.length) {
    const days = Math.floor(h.away_days || 0);
    showUpdates(h.updates, days >= 1
      ? `Shipped during the ${days} day${days === 1 ? "" : "s"} you were away.`
      : "Shipped since your last visit.");
    FAQ_AFTER_UPDATES = !!h.faq_due;   // FAQ follows once this is dismissed
  } else if (h.faq_due) {
    el("faq-backdrop").hidden = false; // first visit, or a week+ away
  }
}

function wireSettings() {
  el("btn-settings").onclick = () => { el("settings-panel").hidden = false; };
  el("btn-close-settings").onclick = () => { el("settings-panel").hidden = true; };
  el("btn-open-faq").onclick = () => { el("faq-backdrop").hidden = false; };
  el("btn-close-faq").onclick = () => { el("faq-backdrop").hidden = true; };
  el("faq-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("faq-backdrop")) el("faq-backdrop").hidden = true;
  });
  el("btn-whats-new").onclick = async () => {
    try { showUpdates(await API.changelog(), "The full release history, newest first."); }
    catch (e) { toast("Unavailable", e.message); }
  };
  el("btn-close-updates").onclick = closeUpdates;
  el("btn-updates-ok").onclick = closeUpdates;
  el("updates-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("updates-backdrop")) closeUpdates();
  });

  const theme = el("set-theme"), compact = el("set-compact"),
        timefmt = el("set-timefmt"), pos = el("set-toast-pos"),
        vol = el("set-volume"), desktop = el("set-desktop"),
        stale = el("set-stale");
  stale.value = String(Settings.get("stale_hours"));
  stale.onchange = async () => {
    Settings.set("stale_hours", +stale.value);
    await renderBoard();  // re-filter visible feeds immediately
  };
  theme.value = Settings.get("theme");
  compact.checked = !!Settings.get("compact");
  timefmt.value = Settings.get("timefmt");
  pos.value = Settings.get("toast_pos");
  vol.value = Settings.get("volume");
  el("set-vol-val").textContent = vol.value + (vol.value === "0" ? " (off)" : "");
  desktop.checked = !!Settings.get("desktop_notif");

  theme.onchange = () => { Settings.set("theme", theme.value); applySettings(); };
  compact.onchange = () => { Settings.set("compact", compact.checked); applySettings(); };
  timefmt.onchange = async () => {
    Settings.set("timefmt", timefmt.value);
    await renderBoard();          // re-render timestamps everywhere
    if (!el("alerts-panel").hidden) renderAlertsPanel();
  };
  pos.onchange = () => { Settings.set("toast_pos", pos.value); applySettings(); };
  vol.oninput = () => {
    Settings.set("volume", +vol.value);
    el("set-vol-val").textContent = vol.value + (vol.value === "0" ? " (off)" : "");
  };
  el("btn-test-notif").onclick = () => {
    alertSound();
    toast("🔔 Test alert", "This is what an alert hit looks and sounds like.", true);
    desktopNotify("D.E.L.P.H.I. test alert", "Desktop notifications are working.");
  };
  desktop.onchange = async () => {
    if (desktop.checked && "Notification" in window && Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        desktop.checked = false;
        toast("Desktop notifications blocked", "Allow notifications for this site in your browser settings.");
      }
    }
    Settings.set("desktop_notif", desktop.checked);
  };
}

function wireAuth() {
  el("btn-profile").textContent = `👤 ${Session.username() || "account"}`;
  el("btn-profile").onclick = () => {
    if (confirm(`Signed in as ${Session.username()}. Sign out?`)) {
      Session.clear();
      location.reload();
    }
  };
}

function wireGate() {
  const st = el("auth-status");
  const say = (msg, ok = false) => {
    st.textContent = msg;
    st.className = "query-status " + (ok ? "ok" : "err");
  };
  const show = (pane) => {
    for (const id of ["gate-signin", "gate-register", "gate-forgot", "gate-reset"])
      el(id).hidden = id !== pane;
  };
  el("gate-to-register").onclick = (e) => { e.preventDefault(); say(""); show("gate-register"); el("reg-username").focus(); };
  el("gate-to-signin").onclick = (e) => { e.preventDefault(); say(""); show("gate-signin"); el("auth-username").focus(); };
  el("gate-to-forgot").onclick = (e) => { e.preventDefault(); say(""); show("gate-forgot"); el("forgot-email").focus(); };
  el("gate-forgot-back").onclick = (e) => { e.preventDefault(); say(""); show("gate-signin"); };

  const finish = async (r) => {
    Session.set(r.token, r.username, r.user_key);
    try { await API.claim(); } catch (e) { /* nothing to migrate */ }
    location.replace(location.pathname);  // drop any action params, reload
  };
  el("btn-login").onclick = async () => {
    const ident = el("auth-username").value.trim();
    try { await finish(await API.login(ident, el("auth-password").value)); }
    catch (e) {
      if (e.message.startsWith("unverified")) {
        say("Your email isn't verified yet — check your inbox. ");
        const a = document.createElement("a");
        a.href = "#"; a.textContent = "Resend the link";
        a.onclick = async (ev) => {
          ev.preventDefault();
          await API.resendVerification(ident).catch(() => {});
          say("Verification email sent again — check your inbox.", true);
        };
        st.appendChild(a);
      } else say(e.message);
    }
  };
  el("btn-register").onclick = async () => {
    try {
      const r = await API.register(
        el("reg-username").value.trim(), el("reg-email").value.trim(), el("reg-password").value);
      if (r.token) return finish(r);            // self-host mode: no mail configured
      show("gate-signin");                      // verification required
      say(`Almost there — we emailed a verification link to ${r.email}. Click it, then sign in.`, true);
    } catch (e) { say(e.message); }
  };
  el("btn-forgot").onclick = async () => {
    const r = await API.forgotPassword(el("forgot-email").value.trim()).catch(() => ({}));
    show("gate-signin");
    say(r.mail_enabled === false
      ? "Email isn't configured on this server — ask the administrator to reset your password."
      : "If that address has an account, a reset link is on its way.", true);
  };

  // Action links from emails land here as URL parameters.
  const params = new URLSearchParams(location.search);
  if (params.get("verify")) {
    API.verifyEmail(params.get("verify"))
      .then(r => say(`Email verified — welcome, ${r.username}! Sign in below.`, true))
      .catch(e => say(e.message));
    history.replaceState(null, "", location.pathname);
  } else if (params.get("reset")) {
    show("gate-reset");
    const token = params.get("reset");
    el("btn-reset").onclick = async () => {
      try {
        const r = await API.resetPassword(token, el("reset-password").value);
        show("gate-signin");
        say(`Password updated, ${r.username} — sign in with it below.`, true);
        history.replaceState(null, "", location.pathname);
      } catch (e) { say(e.message); }
    };
  }

  el("auth-password").addEventListener("keydown", (e) => { if (e.key === "Enter") el("btn-login").click(); });
  el("reg-password").addEventListener("keydown", (e) => { if (e.key === "Enter") el("btn-register").click(); });
  el("auth-username").focus();
}

async function reloadSources() {
  SOURCES = await API.sources();
  Builder.init(META, SOURCES);
  renderSourcesPanel();
}

const PLATFORM_ICONS = { news: "📰", reddit: "👽", mastodon: "🐘", bluesky: "🦋", youtube: "▶️" };

/* ---------- stats ---------- */
function renderStats(meta) {
  if (meta) META = meta;
  const s = META.stats;
  el("stat-articles").textContent = s.articles_24h.toLocaleString();
  el("stat-countries").textContent = s.countries_24h;
  const tile = el("stat-sources");
  if (!(META.ingest && META.ingest.last_run)) {
    // No ingest cycle has completed yet — "0 healthy" would be misleading.
    tile.textContent = `…/${s.sources_total}`;
    tile.title = "First poll of all sources is in progress — refresh in a minute";
  } else {
    tile.textContent = `${s.sources_ok}/${s.sources_total}`;
    tile.title = s.sources_ok === 0
      ? "No source could be fetched — check the Sources panel for per-source errors"
      : "";
  }
}

/* ---------- feed board ---------- */
async function refreshFeeds() {
  FEEDS = await API.feeds();
  await renderBoard();
}

function feedColumn(feed, readonly = false) {
  const col = document.createElement("section");
  col.className = "feed-col" + (feed.width > 1 ? " wide" : "");
  col.id = "feed-" + (feed.id ?? feed.home);

  const head = document.createElement("div");
  head.className = "feed-head";
  const row = document.createElement("div");
  row.className = "feed-head-row";
  const h = document.createElement("h3");
  h.textContent = feed.name;
  h.title = feed.name;
  const tools = document.createElement("div");
  tools.className = "feed-tools";
  if (readonly) {
    tools.append(toolBtn("📌", "Add a copy to My feeds (editable)", async () => {
      await API.createFeed({ name: feed.name.replace(/^[^\w]*\s*/, ""), criteria: feed.criteria,
                             sort: feed.sort, group_events: !!feed.group_events });
      FEEDS = await API.feeds();
      toast("Added to My feeds", `“${feed.name}” is now yours to edit.`);
    }));
  } else {
    tools.append(
      toolBtn("◀", "Move left", () => moveFeed(feed.id, -1)),
      toolBtn("▶", "Move right", () => moveFeed(feed.id, +1)),
      toolBtn("⇔", "Toggle width", () => toggleWidth(feed)),
      toolBtn("✎", "Edit feed", () => Builder.open("feed", feed)),
    );
  }
  row.append(h, tools);
  head.appendChild(row);
  const badges = document.createElement("div");
  badges.className = "feed-badges";
  badges.append(...criteriaBadges(feed.criteria, feed.sort));
  if (feed.group_events) {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = "🧵 events";
    badges.appendChild(t);
  }
  if (badges.childElementCount) head.appendChild(badges);

  const body = document.createElement("div");
  body.className = "feed-body";
  body.innerHTML = '<div class="feed-empty">Loading…</div>';
  col.append(head, body);
  return col;
}

function criteriaBadges(c, sort) {
  const out = [];
  const tag = (t, tip) => {
    const s = document.createElement("span");
    s.className = "tag"; s.textContent = t;
    if (tip) s.title = tip;
    return s;
  };
  // Compact multi-value criteria: first value alphabetically + "+N", full
  // list in the tooltip — a long selection must never crowd out the title.
  if ((c.countries || []).length) {
    const names = c.countries
      .map(iso => ({ iso, name: COUNTRY_NAMES.get(iso) || iso }))
      .sort((a, b) => a.name.localeCompare(b.name));
    const more = names.length > 1 ? ` +${names.length - 1}` : "";
    out.push(tag(`${flagEmoji(names[0].iso)} ${names[0].name}${more}`,
                 names.map(n => n.name).join(", ")));
  }
  if ((c.categories || []).length) {
    const cats = [...c.categories].sort();
    const more = cats.length > 1 ? ` +${cats.length - 1}` : "";
    out.push(tag(cats[0] + more, cats.join(", ")));
  }
  if ((c.scopes || []).length && c.scopes.length < 3) out.push(tag(c.scopes.join("/")));
  if ((c.platforms || []).length && c.platforms.length < 5) {
    const ps = [...c.platforms].sort();
    const more = ps.length > 2 ? ` +${ps.length - 2}` : "";
    out.push(tag("📡 " + ps.slice(0, 2).join("/") + more, ps.join(", ")));
  }
  if ((c.keywords || []).length) {
    const kws = [...c.keywords].sort((a, b) => a.localeCompare(b));
    const more = kws.length > 1 ? ` +${kws.length - 1}` : "";
    out.push(tag(`“${kws[0]}”${more}`, kws.join(", ")));
  }
  const nq = (c.queries || []).filter(q => q && q.trim()).length + (c.query ? 1 : 0);
  if (nq) out.push(tag(nq > 1 ? `boolean ×${nq}` : "boolean",
                       [...(c.queries || []), c.query].filter(Boolean).join("  |  ")));
  if (c.min_importance) out.push(tag("imp≥" + c.min_importance));
  if (c.geo) out.push(tag("📍 map area"));
  if (c.hours) out.push(tag("last " + c.hours + "h"));
  if (c.date_from || c.date_to)
    out.push(tag(`📅 ${c.date_from || "…"}→${c.date_to || "…"}`));
  if (c.hide_stale) out.push(tag("🕰 auto-hide", "Events with no recent updates are hidden (threshold in Settings)"));
  if (sort === "importance") out.push(tag("by importance"));
  return out;
}

function toolBtn(txt, title, fn) {
  const b = document.createElement("button");
  b.className = "icon-btn"; b.textContent = txt; b.title = title; b.onclick = fn;
  return b;
}

async function loadFeedArticles(feed) {
  const body = document.querySelector(`#feed-${feed.id ?? feed.home} .feed-body`);
  if (!body) return;
  try {
    let items;
    if (feed.home) {  // Delphi-generated Home column: ad-hoc criteria search
      items = feed.group_events
        ? await API.searchGrouped(feed.criteria, feed.sort)
        : await API.search(feed.criteria, feed.sort, 40);
    } else {
      items = feed.group_events ? await API.feedEvents(feed.id) : await API.feedArticles(feed.id);
    }
    body.innerHTML = "";
    if (!items.length) {
      body.innerHTML = '<div class="feed-empty">No matching articles yet. ' +
        "Open the feed (✎) and use <b>Preview matches</b> while removing one filter " +
        "at a time to see which criterion is limiting it. If the feed has a query or " +
        "keywords, make sure <b>🔍 Automatically ingest worldwide coverage</b> is " +
        "checked and re-save — D.E.L.P.H.I. only searches articles its sources publish, " +
        "and that option adds a source pulling in press coverage of your query.</div>";
      return;
    }
    if (feed.group_events) for (const g of items) body.appendChild(eventGroup(g));
    else for (const a of items) body.appendChild(articleRow(a, "focus"));
  } catch (e) {
    body.innerHTML = `<div class="feed-empty">Failed to load: ${e.message}</div>`;
  }
}

function eventGroup(g) {
  const wrap = document.createElement("div");
  wrap.className = "event-group";
  if (!g.event_id) {           // unclustered stray: behave like a plain article
    wrap.appendChild(articleRow(g.articles[0]));
    return wrap;
  }
  // Selecting the event opens Event Focus; article links live inside it.
  wrap.classList.add("event-clickable");
  wrap.dataset.eventId = g.event_id;
  if (g.viewed) wrap.classList.add("event-viewed");
  wrap.setAttribute("role", "button");
  wrap.title = "Open event: summary, timeline, sources, map, related events";
  wrap.appendChild(articleRow(g.articles[0], "plain"));
  const meta = document.createElement("div");
  meta.className = "event-open-hint";
  const srcs = g.source_count > 1 ? `${g.source_count} sources` : "1 source";
  meta.textContent = `🧵 ${g.total_count} report${g.total_count > 1 ? "s" : ""} · ${srcs} — open event ⤢`;
  wrap.appendChild(meta);
  wrap.onclick = () => openEventFocus(g.event_id);
  return wrap;
}

/* ---------- event focus ---------- */
let EVENT_MAP = null;

async function openEventFocus(eventId) {
  // Remember the view (per account) and dim every card of this event now.
  API.markEventViewed(eventId).catch(() => {});
  for (const card of document.querySelectorAll(`[data-event-id="${eventId}"]`))
    card.classList.add("event-viewed");

  let d;
  try { d = await API.eventDetail(eventId); }
  catch (e) { toast("Could not load event", e.message); return; }

  el("ev-title").textContent = (d.articles[0] && d.articles[0].title) || d.title;

  const badges = el("ev-badges");
  badges.innerHTML = "";
  const t = impTier(d.importance);
  const imp = document.createElement("span");
  imp.className = "imp " + t.cls;
  imp.textContent = `${t.icon} ${t.label} ${d.importance}`;
  badges.appendChild(imp);
  const tag = (txt) => {
    const s = document.createElement("span");
    s.className = "tag"; s.textContent = txt;
    badges.appendChild(s);
  };
  tag(`🧵 ${d.article_count} report${d.article_count > 1 ? "s" : ""} · ${d.sources.length} source${d.sources.length > 1 ? "s" : ""}`);
  for (const iso of (d.countries || []).slice(0, 5)) tag(`${flagEmoji(iso)} ${COUNTRY_NAMES.get(iso) || iso}`);
  for (const c of (d.categories || []).slice(0, 4)) tag(c);
  tag("first seen " + timeAgo(d.first_seen));
  if (d.updated_at !== d.first_seen) tag("updated " + timeAgo(d.updated_at));

  // The founding report's summary reads best as the event synopsis.
  const oldestFirst = [...d.articles].reverse();
  const synopsis = (oldestFirst.find(a => a.summary) || {}).summary || "";
  el("ev-summary").textContent = synopsis;
  el("ev-summary").hidden = !synopsis;

  renderEventMap(d);

  const tl = el("ev-timeline");
  tl.innerHTML = "";
  el("ev-count").textContent = `— ${d.articles.length} update${d.articles.length > 1 ? "s" : ""}, newest first`;
  for (const a of d.articles) tl.appendChild(articleRow(a));

  // Each outlet chip links to that outlet's report of this event.
  const src = el("ev-sources");
  src.innerHTML = "";
  const articleBySource = new Map();
  for (const a of d.articles)
    if (a.source && !articleBySource.has(a.source.id)) articleBySource.set(a.source.id, a);
  for (const s of d.sources) {
    const chip = document.createElement("a");
    chip.className = "chip chip-link";
    chip.textContent = `${PLATFORM_ICONS[s.platform] || "📰"} ${flagEmoji(s.country)} ${s.name} ↗`;
    const article = articleBySource.get(s.id);
    if (article) {
      chip.href = article.url; chip.target = "_blank"; chip.rel = "noopener";
      chip.title = article.title;
    }
    src.appendChild(chip);
  }

  const rel = el("ev-related");
  rel.innerHTML = "";
  el("ev-related-section").hidden = !(d.related || []).length;
  for (const r of d.related || []) {
    const row = document.createElement("button");
    row.className = "ev-related-row";
    const rt = impTier(r.importance);
    const head = document.createElement("span");
    head.className = "imp " + rt.cls;
    head.textContent = `${rt.icon} ${r.importance}`;
    const title = document.createElement("span");
    title.className = "ev-related-title";
    title.textContent = r.title;
    const why = document.createElement("span");
    why.className = "ev-dim";
    why.textContent = `${r.why} · ${r.article_count} report${r.article_count > 1 ? "s" : ""} · ${timeAgo(r.updated_at)}`;
    row.append(head, title, why);
    row.onclick = () => openEventFocus(r.id);
    rel.appendChild(row);
  }

  el("event-backdrop").hidden = false;
  el("ev-body").scrollTop = 0;
}

function renderEventMap(d) {
  const box = el("event-map");
  const points = [];
  const seen = new Set();
  for (const a of d.articles)
    for (const p of a.places || [])
      if (!seen.has(p.name)) { seen.add(p.name); points.push({ p, imp: a.importance }); }
  if (typeof L === "undefined" || !points.length) {
    box.hidden = true;
    if (EVENT_MAP) { EVENT_MAP.remove(); EVENT_MAP = null; }
    return;
  }
  box.hidden = false;
  if (EVENT_MAP) { EVENT_MAP.remove(); EVENT_MAP = null; }
  EVENT_MAP = L.map("event-map", { worldCopyJump: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(EVENT_MAP);
  const grp = L.featureGroup().addTo(EVENT_MAP);
  for (const { p, imp } of points) {
    const pt = impTier(imp);
    grp.addLayer(L.circleMarker([p.lat, p.lon], {
      radius: 7, color: pt.color, fillColor: pt.color, fillOpacity: 0.8, weight: 1.5,
    }).bindPopup(p.name));
  }
  setTimeout(() => {
    EVENT_MAP.invalidateSize();
    EVENT_MAP.fitBounds(grp.getBounds().pad(0.5), { maxZoom: 6 });
  }, 80);
}

function articleRow(a, mode = "link") {
  // "link":  navigates to the article (alert hits, Event Focus timeline)
  // "plain": inert row inside an already-clickable event card
  // "focus": selecting the row opens Event Focus for its event
  const t = impTier(a.importance);
  if (mode === "focus" && !a.event_id) mode = "link";  // unclustered stray
  const row = document.createElement(mode === "link" ? "a" : "div");
  row.className = "article";
  if (mode === "link") { row.href = a.url; row.target = "_blank"; row.rel = "noopener"; }
  if (mode === "focus") {
    row.classList.add("article-focus");
    row.dataset.eventId = a.event_id;
    if (a.viewed) row.classList.add("event-viewed");
    row.setAttribute("role", "button");
    row.title = "Open event: summary, timeline, sources, map, related events";
    row.onclick = () => openEventFocus(a.event_id);
  }

  const title = document.createElement("div");
  title.className = "title"; title.textContent = a.title;

  const meta = document.createElement("div");
  meta.className = "meta";
  const imp = document.createElement("span");
  imp.className = "imp " + t.cls;
  imp.textContent = `${t.icon} ${t.label} ${a.importance}`;
  meta.appendChild(imp);
  const bits = [];
  if (a.source) bits.push(a.source.name);
  if (a.country) bits.push(flagEmoji(a.country) + " " + a.country);
  bits.push(timeAgo(a.published_at));
  if (a.translated_from) bits.push("🌐 translated from " + a.translated_from.toUpperCase());
  if ((a.categories || []).length) bits.push(a.categories.slice(0, 3).join(" · "));
  if (mode === "focus") bits.push("⤢ event");
  const span = document.createElement("span");
  span.textContent = bits.join("  ·  ");
  meta.appendChild(span);

  const text = document.createElement("div");
  text.className = "article-text";
  text.append(title, meta);
  if (a.summary) {
    const s = document.createElement("div");
    s.className = "summary"; s.textContent = a.summary;
    text.appendChild(s);
  }
  row.appendChild(text);
  if (a.image_url) {
    const img = document.createElement("img");
    img.className = "thumb";
    img.loading = "lazy";
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    img.onerror = () => img.remove();  // dead image -> clean text-only row
    img.src = a.image_url;
    row.appendChild(img);
    row.classList.add("has-thumb");
  }
  return row;
}

async function moveFeed(id, dir) {
  const order = FEEDS.map(f => f.id);
  const i = order.indexOf(id), j = i + dir;
  if (j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  await API.reorderFeeds(order);
  await refreshFeeds();
}

async function toggleWidth(feed) {
  feed.width = feed.width > 1 ? 1 : 2;
  await API.updateFeed(feed.id, { name: feed.name, criteria: feed.criteria, sort: feed.sort,
                                  width: feed.width, group_events: feed.group_events });
  await refreshFeeds();
}

function renderSearchColumn(q, arts) {
  let col = el("search-col");
  if (col) col.remove();
  col = document.createElement("section");
  col.className = "feed-col"; col.id = "search-col";
  const head = document.createElement("div");
  head.className = "feed-head";
  const h = document.createElement("h3");
  h.textContent = `Search: ${q}`;
  head.append(h, toolBtn("✕", "Close search", () => col.remove()));
  const body = document.createElement("div");
  body.className = "feed-body";
  if (!arts.length) body.innerHTML = '<div class="feed-empty">No matches.</div>';
  for (const a of arts) body.appendChild(articleRow(a, "focus"));
  col.append(head, body);
  el("board").prepend(col);
}

async function starterPack() {
  const starters = [
    { name: "Top events worldwide", criteria: { min_importance: 55 }, sort: "importance", group_events: true },
    { name: "Conflict & disasters", criteria: { categories: ["conflict", "disaster"] }, sort: "newest" },
    { name: "Business & economy", criteria: { categories: ["business", "economy"] }, sort: "newest" },
    { name: "Science & technology", criteria: { categories: ["science", "technology"] }, sort: "newest" },
  ];
  for (const s of starters) await API.createFeed(s);
  await refreshFeeds();
}

/* ---------- alerts ---------- */
async function refreshAlerts() {
  ALERTS = await API.alerts();
  const unseen = ALERTS.reduce((n, a) => n + a.unseen, 0);
  el("stat-alerts").textContent = unseen;
  const bc = el("bell-count");
  bc.hidden = unseen === 0;
  bc.textContent = unseen;
  if (!el("alerts-panel").hidden) renderAlertsPanel();
}

async function renderAlertsPanel() {
  const box = el("alerts-list");
  box.innerHTML = "";
  if (!ALERTS.length) {
    el("alerts-map").hidden = true;
    box.innerHTML = '<div class="feed-empty">No alerts yet. Press “+ Create” and flip the toggle to 🔔 Alert — ' +
      "you'll get a live notification whenever a new article matches its criteria " +
      "(keywords, boolean query, countries, importance, or a drawn map area).</div>";
    return;
  }
  const eventsByAlert = await Promise.all(
    ALERTS.map(async (alert) => ({ alert, events: await API.alertEvents(alert.id).catch(() => []) }))
  );
  renderAlertsMap(eventsByAlert);
  for (const { alert, events: evs } of eventsByAlert) {
    const block = document.createElement("div");
    block.className = "alert-block" + (alert.active ? "" : " alert-inactive");
    const head = document.createElement("div");
    head.className = "alert-head";
    const h = document.createElement("h4");
    h.textContent = `${alert.name}${alert.unseen ? ` (${alert.unseen} new)` : ""}`;
    head.append(
      h,
      toolBtn(alert.active ? "⏸" : "▶", alert.active ? "Pause" : "Resume", async () => {
        await API.updateAlert(alert.id, { name: alert.name, criteria: alert.criteria, active: !alert.active });
        await refreshAlerts();
      }),
      toolBtn("✎", "Edit alert", () => Builder.open("alert", alert)),
      toolBtn("👁", "Mark all seen", async () => { await API.markAlertSeen(alert.id); await refreshAlerts(); }),
    );
    const events = document.createElement("div");
    events.className = "alert-events";
    if (!evs.length) events.innerHTML = '<div class="feed-empty">No hits yet.</div>';
    for (const ev of evs.slice(0, 10)) events.appendChild(articleRow(ev.article));
    block.append(head, events);
    box.appendChild(block);
  }
}

/* Map of where recent alert hits are geolocated, plus alert geofences. */
let alertsMap = null;
let alertsMapLayer = null;

function alertsMapWanted() { return localStorage.getItem("gnd_alerts_map") !== "0"; }

function renderAlertsMap(eventsByAlert) {
  const box = el("alerts-map");
  if (typeof L === "undefined" || !alertsMapWanted()) { box.hidden = true; return; }
  box.hidden = false;
  if (!alertsMap) {
    alertsMap = L.map("alerts-map", { worldCopyJump: true }).setView([25, 10], 1);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(alertsMap);
    alertsMapLayer = L.featureGroup().addTo(alertsMap);
  }
  alertsMapLayer.clearLayers();
  for (const { alert, events } of eventsByAlert) {
    const geo = alert.criteria && alert.criteria.geo;
    if (geo) {
      const style = { color: "#d4af37", weight: 1.5, dashArray: "5 5", fillOpacity: 0.06 };
      if (geo.type === "Circle") {
        alertsMapLayer.addLayer(L.circle([geo.center[0], geo.center[1]],
          { radius: geo.radius_km * 1000, ...style }));
      } else {
        L.geoJSON({ type: "Feature", geometry: geo }, { style })
          .eachLayer(l => alertsMapLayer.addLayer(l));
      }
    }
    for (const ev of events.slice(0, 30)) {
      const place = (ev.article.places || [])[0];
      if (!place) continue;
      const t = impTier(ev.article.importance);
      const marker = L.circleMarker([place.lat, place.lon], {
        radius: ev.seen ? 5 : 7, color: t.color, fillColor: t.color,
        fillOpacity: ev.seen ? 0.45 : 0.85, weight: 1.5,
      });
      const popup = document.createElement("div");
      const b = document.createElement("b"); b.textContent = alert.name;
      const link = document.createElement("a");
      link.href = ev.article.url; link.target = "_blank"; link.rel = "noopener";
      link.textContent = ev.article.title;
      const small = document.createElement("div");
      small.textContent = `${t.icon} ${t.label} ${ev.article.importance} · ${place.name}`;
      popup.append(b, document.createElement("br"), link, small);
      marker.bindPopup(popup);
      alertsMapLayer.addLayer(marker);
    }
  }
  setTimeout(() => {
    alertsMap.invalidateSize();
    const layers = alertsMapLayer.getLayers();
    if (layers.length) alertsMap.fitBounds(alertsMapLayer.getBounds().pad(0.35), { maxZoom: 8 });
  }, 60);
}

/* ---------- sources panel ---------- */
async function renderSourcesPanel() {
  const box = el("sources-list");
  box.innerHTML = "";
  for (const s of SOURCES) {
    const row = document.createElement("div");
    row.className = "source-row";
    const ok = (s.last_status || "").startsWith("ok");
    const status = document.createElement("span");
    status.className = ok ? "s-status-ok" : (s.last_status ? "s-status-bad" : "");
    status.title = s.last_status || "not yet polled";
    status.textContent = ok ? "●" : (s.last_status ? "●" : "○");
    const name = document.createElement("span");
    name.className = "s-name";
    name.textContent = `${PLATFORM_ICONS[s.platform] || "📰"} ${flagEmoji(s.country)} ${s.name}`;
    name.title = s.rss_url + (s.repaired_from ? `\nAuto-repaired — original URL: ${s.repaired_from}` : "");
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `${s.scope} · ${s.language}${s.added_by !== "catalog" ? " · " + s.added_by : ""}`
      + (s.repaired_from ? " · 🔧 repaired" : "");
    const tools = [];
    if (!ok && s.last_status) {
      tools.push(toolBtn("🔧", "Attempt automatic repair (re-check the URL, rediscover the feed)", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true; btn.textContent = "⏳";
        try {
          const r = await API.repairSource(s.id);
          if (!r.repaired) toast("Could not repair", r.detail || r.status);
          else if (r.changed) toast("Source repaired", `Feed switched to ${r.rss_url} — ${r.new_articles} article(s) pulled.`);
          else toast("Source recovered", "The existing feed URL works again.");
        } catch (err) { toast("Repair failed", err.message); }
        await reloadSources();
      }));
    }
    row.append(
      status, name, meta, ...tools,
      toolBtn("✎", "Edit source", () => row.replaceWith(sourceEditor(s))),
      toolBtn(s.enabled ? "⏸" : "▶", s.enabled ? "Disable" : "Enable", async () => {
        await API.patchSource(s.id, { enabled: !s.enabled });
        await reloadSources();
      }),
      toolBtn("🗑", "Delete source", async () => {
        if (!confirm(`Delete source “${s.name}” and its articles?`)) return;
        await API.deleteSource(s.id);
        await reloadSources();
      }),
    );
    box.appendChild(row);
  }
}

function sourceEditor(s) {
  const form = document.createElement("div");
  form.className = "source-edit";
  const field = (labelText, node) => {
    const l = document.createElement("label");
    const sp = document.createElement("span"); sp.textContent = labelText;
    l.append(sp, node);
    return l;
  };
  const input = (val) => { const i = document.createElement("input"); i.value = val || ""; return i; };
  const sel = (options, val) => {
    const e = document.createElement("select");
    for (const [v, label] of options) e.appendChild(new Option(label, v));
    e.value = val;
    return e;
  };
  const name = input(s.name);
  const url = input(s.rss_url);
  const platform = sel(Object.entries(PLATFORM_ICONS).map(([v, ic]) => [v, `${ic} ${v}`]), s.platform || "news");
  const scope = sel([["local", "local"], ["national", "national"], ["international", "international"]], s.scope);
  const country = sel([["", "🌐 Global"], ...META.countries.map(c => [c.iso2, `${flagEmoji(c.iso2)} ${c.name}`])], s.country || "");
  const language = input(s.language);
  const cats = input((s.categories || []).join(", "));
  form.append(
    field("Name", name), field("Feed URL", url),
    field("Platform", platform), field("Scope", scope),
    field("Country", country), field("Language (code)", language),
    field("Categories (comma-separated)", cats),
  );
  const actions = document.createElement("div");
  actions.className = "source-edit-actions";
  const save = document.createElement("button");
  save.className = "btn btn-primary"; save.textContent = "Save";
  save.onclick = async () => {
    try {
      await API.patchSource(s.id, {
        name: name.value.trim() || s.name,
        rss_url: url.value.trim(),
        platform: platform.value,
        scope: scope.value,
        country: country.value,
        language: language.value.trim() || "en",
        categories: cats.value.split(",").map(c => c.trim().toLowerCase()).filter(Boolean),
      });
      await reloadSources();
      toast("Source updated", name.value.trim());
    } catch (e) { toast("Could not save source", e.message); }
  };
  const cancel = document.createElement("button");
  cancel.className = "btn"; cancel.textContent = "Cancel";
  cancel.onclick = () => renderSourcesPanel();
  actions.append(save, cancel);
  form.appendChild(actions);
  return form;
}

/* ---------- live stream ---------- */
let refreshTimer = null;
let STREAM_CONNECTED_ONCE = false;

function connectStream() {
  // EventSource cannot send headers; the token rides as a query parameter.
  const es = new EventSource("/api/stream?token=" + encodeURIComponent(Session.token()));
  es.onopen = () => {
    // A reconnect usually means the server restarted — i.e. an update may
    // have just been deployed. (Boot-time onboarding covers the first open.)
    if (STREAM_CONNECTED_ONCE) checkForUpdates();
    STREAM_CONNECTED_ONCE = true;
  };
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "cycle") {
      API.meta().then(renderStats).catch(() => {});
    } else if (msg.type === "articles") {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(async () => {
        renderStats(await API.meta());
        const visible = VIEW === "home" ? DELPHI_FEEDS : FEEDS;
        await Promise.all(visible.map(loadFeedArticles));
      }, 1500);
    } else if (msg.type === "alert" && msg.user_id === Session.userKey()) {
      const t = impTier(msg.importance);
      toast(`🔔 ${msg.alert_name}`, `${t.icon} ${t.label} — ${msg.title}`, true);
      alertSound();
      desktopNotify(`D.E.L.P.H.I. alert: ${msg.alert_name}`, `${t.label} — ${msg.title}`);
      refreshAlerts();
    }
  };
  es.onerror = () => { es.close(); setTimeout(connectStream, 5000); };
}

/* ---------- toasts ---------- */
function toast(title, body, isAlert = false) {
  const t = document.createElement("div");
  t.className = "toast" + (isAlert ? " alert-toast" : "");
  const h = document.createElement("div"); h.className = "t-title"; h.textContent = title;
  const b = document.createElement("div"); b.className = "t-body"; b.textContent = body;
  t.append(h, b);
  el("toasts").appendChild(t);
  setTimeout(() => t.remove(), 9000);
}

boot().catch(e => {
  document.body.insertAdjacentHTML("beforeend",
    `<div class="empty-state"><h2>Could not reach the backend</h2><p>${e.message}</p></div>`);
});
