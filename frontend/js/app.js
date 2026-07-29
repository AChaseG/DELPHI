/* Dashboard: feed board, alerts panel, sources panel, live SSE updates. */
let META = null;
let SOURCES = [];
let FEEDS = [];
let ALERTS = [];
let PANTHEONS = [];          // organizations this account belongs to
let PANTHEON_INVITES = [];   // pending invitations for this account
let COUNTRY_NAMES = new Map();
let VIEW = localStorage.getItem("gnd_view") || "home";  // "home" | "mine" | "pantheon:<id>"

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
  // Adopt the account's saved preferences FIRST: everything below (theme,
  // language picker, staleness, settings panel) initializes from Settings,
  // and this device's localStorage may be empty (new browser / new origin).
  try { Settings.adopt((await API.getSettings()).settings); } catch (e) { /* offline */ }
  [META, SOURCES] = await Promise.all([API.meta(), API.sources()]);
  COUNTRY_NAMES = new Map(META.countries.map(c => [c.iso2, c.name]));
  Builder.init(META, SOURCES);
  Builder.onSaved = async (mode, converted = false) => {
    // Whatever was cached for this feed was matched by its previous
    // criteria, so it has to be thrown away rather than aged out.
    if (Builder.editing) invalidateFeedCache(Builder.editing);
    // A conversion changes both lists: one side gains, the other loses.
    if (mode === "alert" || converted) await refreshAlerts();
    if (mode === "feed") {
      FEEDS = await API.feeds();
      if (VIEW.startsWith("pantheon:")) await renderBoard();  // edited a shared feed in place
      else setView("mine");  // show the user what they just created/edited
    } else if (converted) {
      await refreshFeeds();  // feed became an alert: drop its column
    }
  };
  wireTopbar();
  initBoardScrollbar();
  renderStats();
  await refreshPantheons();  // before the board: VIEW may be a Pantheon's
  await Promise.all([refreshFeeds(), refreshAlerts()]);
  connectStream();
  runOnboarding();
  setInterval(checkForUpdates, UPDATE_POLL_MS);
  // First run on an empty database: pull the catalog once in the background.
  if (META.stats.total_articles === 0) {
    toast("Fetching news", "First run — polling the news wires and the first of the "
          + "local city feeds. This can take a minute…");
    API.runIngest().then(async (r) => {
      const found = r.discovered ? ` · discovered ${r.discovered} new source${plural(r.discovered)}` : "";
      toast("Ingest complete", `${r.new_articles} article${plural(r.new_articles)} from `
        + `${r.sources_ok}/${r.sources_total} sources${found}`);
      renderStats(await API.meta());
      refreshFeeds();
    }).catch(() => {});
  }
}

function setView(view) {
  VIEW = view;
  localStorage.setItem("gnd_view", view);
  updateViewButtons();
  renderBoard();
}

function updateViewButtons() {
  el("btn-view-home").classList.toggle("active", VIEW === "home");
  el("btn-view-mine").classList.toggle("active", VIEW === "mine");
  for (const b of document.querySelectorAll(".view-switch .view-pantheon"))
    b.classList.toggle("active", b.dataset.view === VIEW);
}

/* One board button per Pantheon, next to Home and My feeds. */
function renderViewSwitch() {
  for (const b of document.querySelectorAll(".view-switch .view-pantheon")) b.remove();
  const sw = document.querySelector(".view-switch");
  for (const p of PANTHEONS) {
    const b = document.createElement("button");
    b.className = "view-pantheon";
    b.dataset.view = "pantheon:" + p.id;
    b.textContent = "🏛 " + (p.name.length > 16 ? p.name.slice(0, 15) + "…" : p.name);
    b.title = `${p.name} — shared board · ${p.member_count} member${plural(p.member_count)}`;
    b.onclick = () => setView("pantheon:" + p.id);
    sw.appendChild(b);
  }
  updateViewButtons();
}

/* Bumped by every render. A render that finds the counter moved on has been
   superseded — it stops fetching rather than spending the (deliberately small)
   concurrency budget filling columns that are no longer on screen. */
let BOARD_GENERATION = 0;

/* Which feeds are on each Pantheon's board, as last seen.

   Home and My feeds are built from state the client already holds, so their
   columns appear the instant you click. A Pantheon's board has to ask the
   server what is on it — and while that request was in flight the board stood
   empty, which is the wipe you see when switching to a shared board on a busy
   server. Remembering the list lets the board come back immediately and
   reconcile behind that. */
const PANTHEON_FEEDS = new Map();

/* ---------- warming the boards you aren't looking at ----------

   Once the board on screen has finished loading, the boards behind it are
   fetched too, so switching to one is instant instead of a wait. Three rules
   keep this from becoming the load problem it is meant to hide:

     · it never starts until the visible board is done, and it stops the moment
       you switch — the board you are actually looking at always has the
       network to itself;
     · one request at a time, where the foreground gets two, because none of
       this is urgent;
     · a column fetched within PREFETCH_FRESH_MS is left alone, so switching
       back and forth doesn't re-fetch the same feeds over and over.

   Nothing here touches the DOM. It only fills the caches that renderBoard and
   feedColumn already paint from. */
const PREFETCH_DELAY_MS = 1200;        // let the visible board settle first
const PREFETCH_FRESH_MS = 2 * 60 * 1000;
const FEED_CACHE_AT = new Map();       // cache key -> when it was last fetched

let prefetchTimer = null;

function schedulePrefetch(generation) {
  clearTimeout(prefetchTimer);
  prefetchTimer = setTimeout(() => { prefetchOtherBoards(generation); }, PREFETCH_DELAY_MS);
}

/* Forget what a feed last returned. Ageing an entry out is right when the news
   may have moved on; dropping it is right when the question itself changed —
   the feed was edited, or the server will now answer differently. */
function invalidateFeedCache(feed) {
  const key = feedCacheKey(feed);
  FEED_CACHE.delete(key);
  FEED_CACHE_AT.delete(key);
}

/* Only the timestamps: the articles stay on screen while every column
   re-queries, so a forced refresh still never blanks the board. */
function invalidateAllFeedCaches() {
  FEED_CACHE_AT.clear();
}

/* The feeds making up the board currently on screen. */
function visibleFeeds() {
  if (VIEW === "home") return DELPHI_FEEDS;
  if (VIEW.startsWith("pantheon:")) return PANTHEON_FEEDS.get(+VIEW.split(":")[1]) || [];
  return FEEDS;
}

/* Every feed that is on a board other than the one being viewed. Pantheon
   boards need their feed list first; that list is cached too, which is what
   makes a first switch to a shared board paint immediately. */
async function feedsOnOtherBoards(stillWanted) {
  const others = [];
  if (VIEW !== "home") others.push(...DELPHI_FEEDS);
  if (VIEW !== "mine") others.push(...FEEDS);
  for (const p of PANTHEONS) {
    if (VIEW === "pantheon:" + p.id) continue;
    let feeds = PANTHEON_FEEDS.get(p.id);
    if (!feeds) {
      if (!stillWanted()) return others;
      try {
        feeds = await API.pantheonFeeds(p.id);
        PANTHEON_FEEDS.set(p.id, feeds);
      } catch (e) { continue; }   // speculative: a failure costs nothing
    }
    others.push(...feeds);
  }
  return others;
}

async function prefetchOtherBoards(generation) {
  const stillWanted = () => generation === BOARD_GENERATION && !document.hidden;
  if (!stillWanted()) return;
  const others = await feedsOnOtherBoards(stillWanted);
  for (const feed of others) {
    if (!stillWanted()) return;
    const key = feedCacheKey(feed);
    if (Date.now() - (FEED_CACHE_AT.get(key) || 0) < PREFETCH_FRESH_MS) continue;
    try { await fetchFeedItems(feed); } catch (e) { /* speculative */ }
  }
}

// A tab that was hidden when its turn came round gets one when it returns.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) schedulePrefetch(BOARD_GENERATION);
});

/* Swap the board's contents in one operation. Nothing is removed until the
   replacement exists, so there is never a frame with an empty board. The
   search column is not part of a view and survives the swap. */
function paintBoard(columns) {
  const board = el("board");
  const searchCol = el("search-col");
  board.replaceChildren(...(searchCol ? [searchCol, ...columns] : columns));
  syncBoardScrollbar();
}

/* The placeholder column for a Pantheon nobody has shared anything with yet.
   Built with the DOM rather than innerHTML because a Pantheon's name is
   written by another user and read by every member. */
function pantheonEmptyNote(p) {
  const note = document.createElement("section");
  note.className = "feed-col";
  const head = document.createElement("div"); head.className = "feed-head";
  const row = document.createElement("div"); row.className = "feed-head-row";
  const title = document.createElement("h3"); title.textContent = "🏛 " + p.name;
  row.appendChild(title); head.appendChild(row);
  const body = document.createElement("div"); body.className = "feed-body";
  const empty = document.createElement("div"); empty.className = "feed-empty";
  empty.textContent = "No shared feeds yet. Open 📋 My feeds and press 🏛 on any " +
    "feed to share it with this Pantheon — everyone here will see it. Alerts are " +
    "shared the same way from the 🔔 panel.";
  body.appendChild(empty);
  note.append(head, body);
  return note;
}

const pantheonColumns = (p, feeds) =>
  (feeds.length ? feeds.map(f => feedColumn(f)) : [pantheonEmptyNote(p)]);

/* Same feeds, in the same order? Then the painted board is already correct and
   repainting would only throw away columns the user is reading. */
const sameFeeds = (a, b) =>
  a.length === b.length && a.every((f, i) => f.id === b[i].id && f.name === b[i].name);

async function renderBoard() {
  const generation = ++BOARD_GENERATION;
  const current = () => generation === BOARD_GENERATION;

  if (VIEW.startsWith("pantheon:")) {
    const pid = +VIEW.split(":")[1];
    const p = PANTHEONS.find(x => x.id === pid);
    if (!p) { setView("home"); return; }
    el("empty-state").hidden = true;

    // Paint something for this board straight away: its last known columns if
    // we have them, otherwise a placeholder. Leaving the previous view's
    // columns up would make the click look like it hadn't registered.
    const known = PANTHEON_FEEDS.get(pid);
    paintBoard(known ? pantheonColumns(p, known)
                     : [feedColumnNote(`🏛 ${p.name}`, "Loading this board…")]);

    let feeds;
    try {
      feeds = await API.pantheonFeeds(pid);
    } catch (e) {
      // Bouncing to Home on a failed request threw the user off a board that
      // was working a second ago. Keep what's painted; say so if there is
      // nothing to keep.
      if (!current()) return;
      if (!known) {
        paintBoard([feedColumnNote(`🏛 ${p.name}`,
          `Couldn't load this board (${e.message}). It will come back when the server ` +
          "answers — press ⟳, or switch away and back, to retry.")]);
      } else {
        for (const body of document.querySelectorAll("#board .feed-col .feed-body"))
          staleNotice(body, `Couldn't refresh this board (${e.message}) — showing the last update.`);
      }
      return;
    }
    if (!current()) return;                 // a later switch owns the board now
    PANTHEON_FEEDS.set(pid, feeds);
    if (!known || !sameFeeds(known, feeds)) paintBoard(pantheonColumns(p, feeds));
    await mapLimited(feeds, BOARD_LOAD_CONCURRENCY, loadFeedArticles, current);
  } else if (VIEW === "home") {
    el("empty-state").hidden = true;
    paintBoard(DELPHI_FEEDS.map(hf => feedColumn(hf, /*readonly*/ true)));
    await mapLimited(DELPHI_FEEDS, BOARD_LOAD_CONCURRENCY, loadFeedArticles, current);
  } else {
    el("empty-state").hidden = FEEDS.length > 0;
    paintBoard(FEEDS.map(feed => feedColumn(feed)));
    await mapLimited(FEEDS, BOARD_LOAD_CONCURRENCY, loadFeedArticles, current);
  }

  syncBoardScrollbar();   // widths can change as columns fill
  // The visible board is done; warm the others so switching to one is instant.
  if (current()) schedulePrefetch(generation);
}

/* A column that carries a message instead of articles. */
function feedColumnNote(title, message) {
  const col = document.createElement("section");
  col.className = "feed-col";
  const head = document.createElement("div"); head.className = "feed-head";
  const row = document.createElement("div"); row.className = "feed-head-row";
  const h = document.createElement("h3"); h.textContent = title;
  row.appendChild(h); head.appendChild(row);
  const body = document.createElement("div"); body.className = "feed-body";
  body.appendChild(feedEmpty(message));
  col.append(head, body);
  return col;
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
  updateViewButtons();
  wirePantheons();
  wireAdmin();
  wireActionRail();
  wireLocations();
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
    toast("Refreshing", "Polling news wires now… (local city feeds refresh in the background)");
    // Pressing Refresh means "show me current news", so the board re-queries
    // whatever the poll does — including when a cycle is already running and
    // this request is declined. Only the freshness timestamps are dropped; the
    // articles stay on screen while the columns re-query behind them.
    invalidateAllFeedCaches();
    try {
      const r = await API.runIngest();
      const found = r.discovered ? ` · discovered ${r.discovered} new source${plural(r.discovered)}` : "";
      toast("Ingest complete", `${r.new_articles} new article${plural(r.new_articles)} `
        + `(${r.sources_ok}/${r.sources_total} polled ok)${found}`);
      renderStats(await API.meta());
    } catch (e) {
      toast(e.message.includes("already running") ? "Refresh" : "Refresh failed", e.message);
    }
    await refreshFeeds();
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
  feedback(el("btn-track-topic"), "Searching…");
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
  feedback(el("btn-track-social"), "Searching…");

  // Typing filters the list in place; the catalog runs to hundreds of sources,
  // so scrolling to find one was impractical.
  let srcFilterTimer;
  el("src-search").addEventListener("input", () => {
    clearTimeout(srcFilterTimer);
    srcFilterTimer = setTimeout(renderSourcesPanel, 120);
  });
  wireAddSource();
}

/* ---------- add-source dialog ---------- */
const AddSource = {
  open() {
    for (const id of ["src-name", "src-url", "src-categories"]) el(id).value = "";
    el("src-language").value = "en";
    el("src-platform").value = "news";
    el("src-scope").value = "national";
    el("src-country").value = "";
    el("src-paywall").checked = false;
    this.clearError();
    el("src-backdrop").hidden = false;
    el("src-name").focus();
  },
  close() { el("src-backdrop").hidden = true; },
  showError(msg) {
    const box = el("src-error");
    box.textContent = msg; box.hidden = false;
  },
  clearError() { el("src-error").hidden = true; },

  async save() {
    const name = el("src-name").value.trim(), url = el("src-url").value.trim();
    // Report the problem next to the form rather than in a toast that appears
    // behind the dialog the user is still looking at.
    if (!name) return this.showError("Give the source a name.");
    if (!url) return this.showError("A source needs the URL of its RSS or Atom feed.");
    if (!/^https?:\/\//i.test(url))
      return this.showError("The feed URL has to start with http:// or https://");
    this.clearError();
    try {
      await API.addSource({
        name, rss_url: url,
        platform: el("src-platform").value,
        scope: el("src-scope").value,
        country: el("src-country").value,
        language: el("src-language").value.trim() || "en",
        categories: el("src-categories").value.split(",").map(s => s.trim()).filter(Boolean),
        paywall: el("src-paywall").checked,
      });
    } catch (e) { return this.showError(e.message); }
    this.close();
    await reloadSources();
    toast("Source added", `${name} — refresh ⟳ to pull it for the first time.`);
  },
};

function wireAddSource() {
  el("btn-open-add-source").onclick = () => AddSource.open();
  el("btn-close-src").onclick = () => AddSource.close();
  el("btn-src-cancel").onclick = () => AddSource.close();
  el("src-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("src-backdrop")) AddSource.close();
  });
  el("btn-add-source").onclick = () => AddSource.save();
  feedback(el("btn-add-source"), "Adding…");
  // Enter anywhere in the form submits, as in any other dialog.
  el("src-backdrop").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") el("btn-add-source").click();
    if (e.key === "Escape") AddSource.close();
  });
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

/* ---------- onboarding: first-visit guide + what's-new-while-away popup ---- */

let FAQ_AFTER_UPDATES = false;  // chain: close What's-new → open the guide

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
    openHelp("howto");
  }
}

/* ---------- help ----------
   Two tabs: How to (procedure) and FAQ (explanation). Anyone who opens help
   without asking for a particular tab wants the instructions, so that's what
   they get; the tab is not remembered, because the next visit is a new
   question and rarely the same kind. */
function showHelpTab(tab) {
  for (const s of document.querySelectorAll(".help-tab")) s.hidden = s.dataset.tab !== tab;
  for (const b of el("help-tabs").querySelectorAll("button"))
    b.classList.toggle("active", b.dataset.tab === tab);
}

function openHelp(tab = "howto") {
  showHelpTab(tab);
  el("faq-backdrop").hidden = false;
  // A reopened dialog should start at the top, not where it was left scrolled.
  const body = document.querySelector(`.help-tab[data-tab="${tab}"]`);
  if (body) body.scrollTop = 0;
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
      ? `Shipped during the ${days} day${plural(days)} you were away.`
      : "Shipped since your last visit.");
    FAQ_AFTER_UPDATES = !!h.faq_due;   // FAQ follows once this is dismissed
  } else if (h.faq_due) {
    openHelp("howto");   // first visit, or a week or more away
  }
}

function wireSettings() {
  el("btn-settings").onclick = () => { el("settings-panel").hidden = false; };
  el("btn-close-settings").onclick = () => { el("settings-panel").hidden = true; };
  el("btn-open-faq").onclick = () => openHelp("howto");
  el("btn-close-faq").onclick = () => { el("faq-backdrop").hidden = true; };
  el("faq-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("faq-backdrop")) el("faq-backdrop").hidden = true;
  });
  el("help-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) showHelpTab(b.dataset.tab);
  });
  el("btn-whats-new").onclick = async () => {
    try { showUpdates(await API.changelog(), "The full release history, newest first."); }
    catch (e) { toast("Unavailable", e.message); }
  };
  feedback(el("btn-whats-new"), "Loading…");
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
    invalidateAllFeedCaches();   // the server applies this, not the client
    await renderBoard();
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
  // Set only the label span — replacing the button's text would drop the icon
  // the rail shows when collapsed.
  const who = Session.username() || "account";
  el("btn-profile").querySelector(".rail-label").textContent = who;
  el("btn-profile").title = `Signed in as ${who} — your feeds and alerts are private to your account`;
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
  // Neutral in-progress note — say() would paint it as an error (red), which
  // reads as "your sign-up failed" while it is actually still working.
  const note = (msg) => { st.textContent = msg; st.className = "query-status"; };
  const show = (pane) => {
    for (const id of ["gate-signin", "gate-register", "gate-forgot", "gate-reset"])
      el(id).hidden = id !== pane;
  };
  // Wire defensively: this gate is the only way into the app, so one missing
  // element must not abort the rest of the wiring and leave, say, the Create
  // account button inert with no clue why. A miss is logged loudly instead.
  const on = (id, handler) => {
    const node = el(id);
    if (!node) { console.error(`[gate] #${id} is missing from the page`); return; }
    node.onclick = handler;
  };
  const focus = (id) => { const n = el(id); if (n) n.focus(); };

  on("gate-to-register", (e) => { e.preventDefault(); say(""); show("gate-register"); focus("reg-username"); });
  on("gate-to-signin", (e) => { e.preventDefault(); say(""); show("gate-signin"); focus("auth-username"); });
  on("gate-to-forgot", (e) => { e.preventDefault(); say(""); show("gate-forgot"); focus("forgot-email"); });
  on("gate-forgot-back", (e) => { e.preventDefault(); say(""); show("gate-signin"); });

  const finish = async (r) => {
    Session.set(r.token, r.username, r.user_key);
    location.replace(location.pathname);  // drop any action params, reload
  };
  // Run an auth request with visible progress: without this a slow reply (the
  // server may be waiting on an SMTP relay) looks like a button that does
  // nothing, and double-clicks fire duplicate registrations.
  const busy = async (btn, working, fn) => {
    const b = el(btn), label = b.textContent;
    b.disabled = true;
    b.textContent = working;
    note(working);
    try { await fn(); }
    catch (e) {
      // Last-resort surface: an unexpected fault here (network dropped, a bug
      // in the handler) must never look like a button that did nothing.
      console.error("[gate]", e);
      say(e && e.message ? e.message : "Something went wrong — please try again.");
    } finally { b.disabled = false; b.textContent = label; }
  };
  el("btn-login").onclick = () => busy("btn-login", "Signing in…", async () => {
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
  });
  el("btn-register").onclick = () => busy("btn-register", "Creating your account…", async () => {
    try {
      const r = await API.register(
        el("reg-username").value.trim(), el("reg-email").value.trim(), el("reg-password").value);
      if (r.token) return finish(r);            // self-host mode: no mail configured
      show("gate-signin");                      // verification required
      say(`Almost there — we emailed a verification link to ${r.email}. Click it, then sign in.`, true);
    } catch (e) { say(e.message); }
  });
  el("btn-forgot").onclick = () => busy("btn-forgot", "Sending reset link…", async () => {
    const r = await API.forgotPassword(el("forgot-email").value.trim()).catch(() => ({}));
    show("gate-signin");
    say(r.mail_enabled === false
      ? "Email isn't configured on this server — ask the administrator to reset your password."
      : "If that address has an account, a reset link is on its way.", true);
  });

  // Action links from emails. Current links put the token in the path
  // (/reset/<token>) because that is the only part mail systems don't mangle:
  // query strings get dropped by registrar forwarding, and fragments get
  // percent-encoded to %23 by link-rewriting scanners. Older ?param= and
  // #param= links are still accepted so anything already sent keeps working.
  const query = new URLSearchParams(location.search);
  const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
  const path = location.pathname.match(/^\/(reset|verify)\/(.+)$/);
  const actionToken = (name) =>
    (path && path[1] === name ? decodeURIComponent(path[2]) : null)
    || fragment.get(name) || query.get(name);
  if (actionToken("verify")) {
    API.verifyEmail(actionToken("verify"))
      .then(r => say(`Email verified — welcome, ${r.username}! Sign in below.`, true))
      .catch(e => say(e.message));
    history.replaceState(null, "", location.pathname);
  } else if (actionToken("reset")) {
    show("gate-reset");
    const token = actionToken("reset");
    el("btn-reset").onclick = () => busy("btn-reset", "Updating password…", async () => {
      try {
        const r = await API.resetPassword(token, el("reset-password").value);
        show("gate-signin");
        say(`Password updated, ${r.username} — sign in with it below.`, true);
        history.replaceState(null, "", location.pathname);
      } catch (e) { say(e.message); }
    });
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
  el("btn-admin").hidden = !META.is_admin;  // operator-only console
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
  col.className = "feed-col";
  col.id = "feed-" + (feed.id ?? feed.home);
  applyColWidth(col, feed);

  const head = document.createElement("div");
  head.className = "feed-head";
  const row = document.createElement("div");
  row.className = "feed-head-row";
  const h = document.createElement("h3");
  h.textContent = feed.name;
  h.title = feed.name;
  const tools = document.createElement("div");
  tools.className = "feed-tools";
  // Every column can be refreshed on its own — the ⟳ in the rail re-polls every
  // source, which is slow and heavy when you only want this one column current.
  tools.append(toolBtn("⟳", "Refresh this feed", async () => {
    await loadFeedArticles(feed, /*force*/ true);
  }));
  const pinBtn = () => toolBtn("📌", "Add a copy to My feeds (editable)", async () => {
    await API.createFeed({ name: feed.name.replace(/^[^\w]*\s*/, ""), criteria: feed.criteria,
                           sort: feed.sort, group_events: !!feed.group_events });
    FEEDS = await API.feeds();
    toast("Added to My feeds", `“${feed.name}” is now yours to edit.`);
  });
  if (feed.pantheon_id) {           // a Pantheon's shared board
    tools.append(pinBtn());
    if (feed.can_edit) {
      tools.append(
        toolBtn("✎", "Edit shared feed", () => Builder.open("feed", feed)),
        toolBtn("🗑", "Remove from this Pantheon", async () => {
          if (!confirm(`Remove “${feed.name}” from this Pantheon's board?`)) return;
          await API.deleteFeed(feed.id);
          // Drop the remembered list, or the next render paints the column
          // back from cache before the server's answer removes it again.
          PANTHEON_FEEDS.delete(feed.pantheon_id);
          await renderBoard();
        }),
      );
    }
  } else if (readonly) {
    tools.append(pinBtn());
  } else {
    tools.append(
      toolBtn("◀", "Move left", () => moveFeed(feed.id, -1)),
      toolBtn("▶", "Move right", () => moveFeed(feed.id, +1)),
      toolBtn("⇔", "Switch between the standard and wide width — or drag either edge",
              () => toggleWidth(feed, col)),
      toolBtn("✎", "Edit feed", () => Builder.open("feed", feed)),
    );
    if (PANTHEONS.length) {
      tools.append(toolBtn("🏛", "Share with a Pantheon", (e) =>
        pantheonPickMenu(e.currentTarget, async (p) => {
          try {
            await API.shareFeed(feed.id, p.id);
            PANTHEON_FEEDS.delete(p.id);   // that board gained a feed
            toast("Feed shared", `“${feed.name}” is now on ${p.name}'s board.`);
          } catch (err) { toast("Could not share", err.message); }
        })));
    }
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
  if (feed.shared_by) {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = `👤 ${feed.shared_by}`;
    t.title = `Shared by ${feed.shared_by}`;
    badges.appendChild(t);
  }
  if (badges.childElementCount) head.appendChild(badges);

  const body = document.createElement("div");
  body.className = "feed-body";
  // Paint the last known contents here, while the column is being built, rather
  // than waiting for loadFeedArticles. That runs behind the concurrency
  // limiter, so every column past the first few used to sit on "Loading…" for
  // as long as the columns ahead of it took — switching panels looked like it
  // had wiped them. From cache the whole board comes back at once and only
  // refreshes in the background.
  const cached = FEED_CACHE.get(feedCacheKey(feed));
  if (cached) renderFeedItems(body, feed, cached);
  else body.innerHTML = '<div class="feed-empty">Loading…</div>';
  col.append(head, body, resizeGrip(col, feed, "left"), resizeGrip(col, feed, "right"));
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
  if (nq) out.push(tag(nq > 1 ? `Boolean ×${nq}` : "Boolean",
                       [...(c.queries || []), c.query].filter(Boolean).join("  |  ")));
  if ((c.source_ids || []).length) {
    const names = c.source_ids.map(id => (SOURCES.find(s => s.id === id) || {}).name)
      .filter(Boolean).sort((a, b) => a.localeCompare(b));
    const label = names.length ? names[0] + (names.length > 1 ? ` +${names.length - 1}` : "")
                               : `${c.source_ids.length} sources`;
    out.push(tag("📡 " + label,
                 "Only pulls from: " + (names.join(", ") || c.source_ids.join(", "))));
  }
  if (c.min_importance) out.push(tag("imp≥" + c.min_importance));
  // Both keys: feeds saved before multiple areas carry the single `geo`.
  const areas = (c.geos || []).length + (c.geo ? 1 : 0);
  if (areas) out.push(tag(areas > 1 ? `📍 ${areas} map areas` : "📍 map area"));
  if (c.hours) out.push(tag("last " + c.hours + "h"));
  if (c.date_from || c.date_to)
    out.push(tag(`📅 ${c.date_from || "…"}→${c.date_to || "…"}`));
  if (c.hide_stale) out.push(tag("🕰 auto-hide", "Events with no recent updates are hidden (threshold in Settings)"));
  if (sort === "importance") out.push(tag("by importance"));
  return out;
}

function toolBtn(txt, title, fn) {
  const b = document.createElement("button");
  b.className = "icon-btn"; b.textContent = txt; b.title = title;
  b.setAttribute("aria-label", title);  // icon-only button needs a text label
  b.onclick = fn;
  // Shared factory for every column/source/alert icon button (📌 ✎ 🗑 🏛 🔧),
  // so wrapping here gives the whole set busy state and error reporting. No
  // label: these are glyphs, and replacing the text would lose the icon.
  return feedback(b);
}

/* A ".feed-empty" note whose text is set safely (never parsed as HTML) —
   use for anything containing a server message or user-supplied string. */
function feedEmpty(text) {
  const d = document.createElement("div");
  d.className = "feed-empty";
  d.textContent = text;
  return d;
}

/* Only http(s) URLs are safe as an href/src. Feed-derived links pass through
   here so a hostile "javascript:"/"data:" URL can never become clickable —
   defense in depth alongside the backend ingest filter (also covers rows
   stored before that filter existed). Returns "" for anything unsafe. */
function safeUrl(url) {
  return /^https?:\/\//i.test(url || "") ? url : "";
}

/* How many feed columns may be fetching at once.

   Every column is a full search. Firing all of them together (7 on Home, more
   on a busy board) buries a small server: the requests queue, each one gets
   slower, and the unlucky ones hit the client timeout and report themselves as
   failures — even though nothing is actually broken. A small window keeps the
   server responsive, fills columns progressively instead of all-at-once, and
   costs little on a fast one.

   Columns outside the window are not blank while they wait: feedColumn paints
   them from FEED_CACHE as it builds them, so the size of this window changes
   only how fast the board becomes *fresh*, never whether it has news on it. */
const BOARD_LOAD_CONCURRENCY = 2;

/* Run `fn` over `items`, at most `limit` at a time. Rejections are contained so
   one failing column can't abandon the rest of the board. `stillWanted` is an
   optional predicate checked between items: when it goes false the remaining
   work is dropped, so a superseded board stops competing for the window. */
async function mapLimited(items, limit, fn, stillWanted = () => true) {
  const queue = items.slice();
  const worker = async () => {
    while (queue.length && stillWanted()) {
      const item = queue.shift();
      try { await fn(item); } catch (e) { console.error("[board]", e); }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}

/* Last successfully loaded items per column.

   The board is rebuilt from scratch whenever the view changes, so without this
   every switch between Home, My feeds, and a Pantheon threw away news that had
   already been fetched and left empty columns until the network answered again.
   Keeping the last good result lets a column paint instantly from memory and
   refresh behind that — and lets a failed refresh keep showing what it had
   rather than replacing real news with an error. Cleared on reload; the server
   remains the source of truth. */
const FEED_CACHE = new Map();

const feedCacheKey = (feed) => (feed.home ? "home:" + feed.home : "feed:" + feed.id);

/* ---------- column widths ----------
   Narrow enough that a headline still fits on two lines, wide enough to read a
   summary without it becoming a wall. Widths live in Settings (see api.js). */
const COL_MIN = 240, COL_MAX = 1000;
const COL_DEFAULT = 360, COL_WIDE = 734;   // the two presets ⇔ toggles between
/* Keeping every column a user ever resized would eventually outgrow the 4 KB
   the server allows for settings, so the map is bounded. Object key order is
   insertion order, so the oldest entries are the ones dropped. */
const COL_WIDTHS_MAX = 150;

const colWidths = () => Settings.get("col_widths") || {};

function colWidth(feed) {
  const w = colWidths()[feedCacheKey(feed)];
  return Number.isFinite(w) ? Math.min(COL_MAX, Math.max(COL_MIN, w)) : 0;
}

/* px = 0 clears the override and returns the column to its default width. */
function setColWidth(feed, px) {
  const all = { ...colWidths() };
  const key = feedCacheKey(feed);
  delete all[key];                                   // re-insert so it counts as recent
  if (px) all[key] = Math.round(Math.min(COL_MAX, Math.max(COL_MIN, px)));
  const keys = Object.keys(all);
  for (const stale of keys.slice(0, Math.max(0, keys.length - COL_WIDTHS_MAX))) delete all[stale];
  Settings.set("col_widths", all);
}

function applyColWidth(col, feed) {
  const px = colWidth(feed) || (feed.width > 1 ? COL_WIDE : 0);
  // Both properties: the board sets flex-basis as well as width, and a bare
  // inline width would lose to it.
  col.style.width = px ? px + "px" : "";
  col.style.flexBasis = px ? px + "px" : "";
}

/* Draggable edges. Both edges resize this column — the left one grows it as you
   drag left, which is what the edge under the cursor appears to do. Nothing is
   saved until the drag ends, so a resize is one settings write, not sixty. */
function resizeGrip(col, feed, side) {
  const grip = document.createElement("div");
  grip.className = "col-grip col-grip-" + side;
  grip.tabIndex = 0;
  grip.setAttribute("role", "separator");
  grip.setAttribute("aria-orientation", "vertical");
  grip.setAttribute("aria-label", `Resize “${feed.name}” (arrow keys, or double-click to reset)`);
  grip.title = "Drag to resize · double-click to reset";

  let startX = 0, startW = 0, dragging = false;
  const width = () => colWidth(feed) || col.offsetWidth;
  const preview = (px) => {
    col.style.width = col.style.flexBasis =
      Math.round(Math.min(COL_MAX, Math.max(COL_MIN, px))) + "px";
    syncBoardScrollbar();
  };

  grip.addEventListener("pointerdown", (e) => {
    dragging = true; startX = e.clientX; startW = col.offsetWidth;
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    document.body.classList.add("col-resizing");
    e.preventDefault();   // or the pointer drag turns into a text selection
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    preview(startW + (side === "right" ? dx : -dx));
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove("dragging");
    document.body.classList.remove("col-resizing");
    setColWidth(feed, col.offsetWidth);
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
  grip.addEventListener("dblclick", () => {
    setColWidth(feed, 0);
    applyColWidth(col, feed);
    syncBoardScrollbar();
  });
  grip.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 60 : 12;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const dir = e.key === "ArrowRight" ? 1 : -1;
      const px = width() + dir * step * (side === "right" ? 1 : -1);
      preview(px);
      setColWidth(feed, col.offsetWidth);
      e.preventDefault();
    } else if (e.key === "Home") {
      setColWidth(feed, 0); applyColWidth(col, feed); syncBoardScrollbar();
      e.preventDefault();
    }
  });
  return grip;
}

function renderFeedItems(body, feed, items) {
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
}

/* A quiet strip at the top of a column saying its contents are the last known
   good ones. Not an error state: the news below is still real, just not fresh. */
function staleNotice(body, message) {
  const old = body.querySelector(".feed-stale");
  if (old) old.remove();
  const note = document.createElement("div");
  note.className = "feed-stale";
  note.textContent = message;
  body.prepend(note);
}

/* Fetch one feed's contents and cache them. No DOM: this is also how boards
   that aren't on screen get warmed, and those have no column to write to. */
async function fetchFeedItems(feed) {
  let items;
  if (feed.home) {  // Delphi-generated Home column: ad-hoc criteria search
    items = feed.group_events
      ? await API.searchGrouped(feed.criteria, feed.sort)
      : await API.search(feed.criteria, feed.sort, 40);
  } else {
    items = feed.group_events ? await API.feedEvents(feed.id) : await API.feedArticles(feed.id);
  }
  const key = feedCacheKey(feed);
  FEED_CACHE.set(key, items);
  FEED_CACHE_AT.set(key, Date.now());
  return items;
}

/* A column refreshed this recently is left as it is when a board is re-rendered.
   Switching away and back used to re-query every column each time, which is the
   most avoidable load the client generates. New articles don't wait for this:
   the event stream refreshes the visible board as they arrive. */
const FOREGROUND_FRESH_MS = 30 * 1000;

async function loadFeedArticles(feed, force = false) {
  const body = document.querySelector(`#feed-${feed.id ?? feed.home} .feed-body`);
  if (!body) return;
  const key = feedCacheKey(feed);
  const cached = FEED_CACHE.get(key);
  // Paint what we already have before waiting on the network, so switching
  // views shows news immediately instead of an empty column.
  if (cached && !body.querySelector(".article, .event-group"))
    renderFeedItems(body, feed, cached);
  if (!force && cached && Date.now() - (FEED_CACHE_AT.get(key) || 0) < FOREGROUND_FRESH_MS)
    return;
  try {
    renderFeedItems(body, feed, await fetchFeedItems(feed));
  } catch (e) {
    // Never trade real articles for an error message. Only a column with
    // nothing to show falls back to reporting the failure.
    if (cached && cached.length) {
      if (!body.querySelector(".article, .event-group"))
        renderFeedItems(body, feed, cached);
      staleNotice(body, `Couldn't refresh (${e.message}) — showing the last update.`);
    } else {
      body.replaceChildren(feedEmpty("Failed to load: " + e.message));
    }
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
  meta.textContent = `🧵 ${g.total_count} report${plural(g.total_count)} · ${srcs} — open event ⤢`;
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
  tag(`🧵 ${d.article_count} report${plural(d.article_count)} · ${d.sources.length} source${plural(d.sources.length)}`);
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
  el("ev-count").textContent = `— ${d.articles.length} update${plural(d.articles.length)}, newest first`;
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
    if (article && safeUrl(article.url)) {
      chip.href = safeUrl(article.url); chip.target = "_blank"; chip.rel = "noopener";
      chip.title = article.title;
    }
    src.appendChild(chip);
    if (article && safeUrl(article.archive_url)) {
      const arch = document.createElement("a");
      arch.className = "chip chip-link archive-chip";
      arch.textContent = "🔓 archive.ph";
      arch.href = safeUrl(article.archive_url); arch.target = "_blank"; arch.rel = "noopener";
      arch.title = "Read the full article via archive.ph (paywalled source)";
      src.appendChild(arch);
    }
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
    why.textContent = `${r.why} · ${r.article_count} report${plural(r.article_count)} · ${timeAgo(r.updated_at)}`;
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
  if (mode === "link") {
    const href = safeUrl(a.url);
    if (href) { row.href = href; row.target = "_blank"; row.rel = "noopener"; }
  }
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
  // Favourite-location hits are marked wherever the article shows up.
  for (const n of a.near || []) {
    const pin = document.createElement("span");
    pin.className = "near-pin";
    pin.textContent = `📍 ${n.name}`;
    pin.title = `Inside your favourite location “${n.name}”`;
    meta.appendChild(pin);
  }
  if (a.translated_from) bits.push("🌐 translated from " + a.translated_from.toUpperCase());
  if ((a.categories || []).length) bits.push(a.categories.slice(0, 3).join(" · "));
  if (mode === "focus") bits.push("⤢ event");
  const span = document.createElement("span");
  span.textContent = bits.join("  ·  ");
  meta.appendChild(span);
  if (a.archive_url) {
    // A <span> (not a nested <a>, which is invalid inside a link-mode row):
    // open archive.ph and suppress the row's own navigation.
    const arch = document.createElement("span");
    arch.className = "archive-link";
    arch.textContent = "🔓 archive.ph";
    arch.title = "Read the full article via archive.ph (paywalled source)";
    arch.setAttribute("role", "link");
    arch.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      window.open(a.archive_url, "_blank", "noopener");
    };
    meta.appendChild(arch);
  }

  const text = document.createElement("div");
  text.className = "article-text";
  text.append(title, meta);
  if (a.summary) {
    const s = document.createElement("div");
    s.className = "summary"; s.textContent = a.summary;
    text.appendChild(s);
  }
  row.appendChild(text);
  if (safeUrl(a.image_url)) {
    const img = document.createElement("img");
    img.className = "thumb";
    img.loading = "lazy";
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    img.onerror = () => img.remove();  // dead image -> clean text-only row
    img.src = safeUrl(a.image_url);
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

/* The two presets the ⇔ button alternates between. It writes the same
   per-account width the drag handles do, so the button and the edges can't
   disagree about how wide the column is. */
function toggleWidth(feed, col) {
  const now = colWidth(feed) || (feed.width > 1 ? COL_WIDE : COL_DEFAULT);
  setColWidth(feed, now >= COL_WIDE ? COL_DEFAULT : COL_WIDE);
  applyColWidth(col, feed);
  syncBoardScrollbar();
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

/* ---------- pantheons (organizations) ---------- */
async function refreshPantheons() {
  try {
    const r = await API.pantheons();
    PANTHEONS = r.mine;
    PANTHEON_INVITES = r.invites;
  } catch (e) { PANTHEONS = []; PANTHEON_INVITES = []; }
  renderViewSwitch();
  const badge = el("pantheon-invite-count");
  badge.hidden = PANTHEON_INVITES.length === 0;
  badge.textContent = PANTHEON_INVITES.length;
  if (!el("pantheons-panel").hidden) renderPantheonsPanel();
}

function wirePantheons() {
  el("btn-pantheons").onclick = async () => {
    el("pantheons-panel").hidden = false;
    await refreshPantheons();
    renderPantheonsPanel();
  };
  el("btn-close-pantheons").onclick = () => { el("pantheons-panel").hidden = true; };
  wirePantheonModal();
  el("btn-create-pantheon").onclick = () => PantheonModal.open();
}

/* ---------- board scrollbar ----------
   A real, always-visible horizontal scrollbar for the feed board. Native ones
   are overlays on most platforms: they take no layout space and fade out when
   idle, so nothing on screen tells you more feeds exist to the right. This one
   lives in the layout under the board, so it is visible at any window height,
   and hides itself when everything already fits. */
function initBoardScrollbar() {
  const board = el("board");
  const bar = el("board-scroll");
  if (!board || !bar) return;
  const thumb = bar.querySelector(".board-scroll-thumb");

  const sync = () => {
    const hidden = board.scrollWidth <= board.clientWidth + 2;
    bar.hidden = hidden;
    if (hidden) return;
    const track = bar.clientWidth;
    const width = Math.max(48, track * (board.clientWidth / board.scrollWidth));
    const maxScroll = board.scrollWidth - board.clientWidth;
    const x = maxScroll ? (board.scrollLeft / maxScroll) * (track - width) : 0;
    thumb.style.width = `${width}px`;
    thumb.style.transform = `translateX(${x}px)`;
    bar.setAttribute("aria-valuenow", String(Math.round(
      maxScroll ? (board.scrollLeft / maxScroll) * 100 : 0)));
  };
  syncBoardScrollbar = sync;   // let the board re-render refresh it

  // Position the board from a pointer x within the track.
  const scrollTo = (clientX) => {
    const rect = bar.getBoundingClientRect();
    const width = thumb.offsetWidth;
    const travel = rect.width - width;
    const pos = Math.min(Math.max(clientX - rect.left - width / 2, 0), travel);
    board.scrollLeft = travel ? (pos / travel) * (board.scrollWidth - board.clientWidth) : 0;
  };

  let dragging = false;
  thumb.addEventListener("pointerdown", (e) => {
    dragging = true;
    thumb.classList.add("dragging");
    thumb.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  thumb.addEventListener("pointermove", (e) => { if (dragging) scrollTo(e.clientX); });
  const endDrag = () => { dragging = false; thumb.classList.remove("dragging"); };
  thumb.addEventListener("pointerup", endDrag);
  thumb.addEventListener("pointercancel", endDrag);
  // Clicking the track jumps there.
  bar.addEventListener("pointerdown", (e) => { if (e.target === bar) scrollTo(e.clientX); });

  board.addEventListener("scroll", sync, { passive: true });
  addEventListener("resize", sync);
  // Three ways the scrollable width changes, all of which have to move the bar:
  // the board's own box (window resize, panels), the set of columns on it
  // (switching views, adding or deleting a feed), and a column's width (drag to
  // resize). Observing only the board left the bar showing its previous state
  // for as long as a re-render took — which is how it came to be on screen with
  // too few columns to scroll.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(sync);
    ro.observe(board);
    const observeColumns = () => {
      for (const c of board.querySelectorAll(".feed-col")) ro.observe(c);
    };
    new MutationObserver(() => { observeColumns(); sync(); })
      .observe(board, { childList: true });
    observeColumns();
  }
  sync();
}
// Replaced by the real implementation once the board exists.
let syncBoardScrollbar = () => {};

/* ---------- action rail ---------- */
function wireActionRail() {
  const rail = el("action-rail");
  const toggle = el("btn-rail-toggle");
  if (!rail || !toggle) return;
  const apply = (open) => {
    rail.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.title = open ? "Collapse actions (\\)" : "Expand actions (\\)";
  };
  apply(Settings.get("rail_open") === true);
  toggle.onclick = () => {
    const open = !rail.classList.contains("open");
    apply(open);
    Settings.set("rail_open", open);   // follows the account, like other prefs
  };
  // Backslash toggles it — no modifier, and never while the user is typing.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "\\" || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    e.preventDefault();
    toggle.click();
  });
}

/* ---------- admin / operator console ---------- */
let ADMIN_ME = null;   // caller's own account id, so the UI never offers self-lockout

function wireAdmin() {
  el("btn-admin").onclick = async () => {
    el("admin-panel").hidden = false;
    await renderAdminUsers();
  };
  el("btn-close-admin").onclick = () => { el("admin-panel").hidden = true; };
  let t;
  el("admin-search").addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => renderAdminUsers(el("admin-search").value.trim()), 250);
  });
}

async function renderAdminUsers(q = "") {
  const box = el("admin-users");
  box.textContent = "Loading…";
  let data;
  try { data = await API.adminUsers(q); }
  catch (e) { box.textContent = ""; toast("Admin", e.message); return; }
  ADMIN_ME = data.me;
  el("admin-summary").textContent =
    `${data.users.length} account${plural(data.users.length)} · ${data.admin_count} operator${plural(data.admin_count)}`;
  box.innerHTML = "";
  if (!data.users.length) { box.textContent = "No matching accounts."; return; }
  for (const u of data.users) box.appendChild(adminUserRow(u));
}

function adminUserRow(u) {
  const row = document.createElement("div");
  row.className = "admin-row" + (u.disabled ? " disabled" : "");

  const head = document.createElement("div");
  head.className = "admin-row-head";
  const name = document.createElement("span");
  name.className = "admin-name";
  name.textContent = u.username;
  head.appendChild(name);
  const badge = (text, cls) => {
    const b = document.createElement("span");
    b.className = "admin-badge " + cls;
    b.textContent = text;
    head.appendChild(b);
  };
  if (u.id === ADMIN_ME) badge("you", "you");
  if (u.is_admin) badge(u.config_admin ? "operator · built-in" : "operator", "op");
  if (u.disabled) badge("suspended", "warn");
  if (!u.email_verified) badge("unverified", "warn");
  row.appendChild(head);

  const meta = document.createElement("div");
  meta.className = "admin-meta";
  const seen = u.last_seen_at ? "last seen " + timeAgo(u.last_seen_at) : "never signed in";
  meta.textContent = `${u.email || "no email"} · ${u.feeds} feed${plural(u.feeds)}, `
    + `${u.alerts} alert${plural(u.alerts)}, ${u.pantheons} pantheon${plural(u.pantheons)} · ${seen}`;
  row.appendChild(meta);

  const acts = document.createElement("div");
  acts.className = "admin-acts";
  const btn = (label, title, fn, cls = "") => {
    const b = document.createElement("button");
    b.className = "btn small" + (cls ? " " + cls : "");
    b.textContent = label; if (title) b.title = title;
    b.onclick = fn;
    feedback(b);   // every operator action disables while it runs
    acts.appendChild(b);
    return b;
  };
  const refresh = () => renderAdminUsers(el("admin-search").value.trim());
  const guard = async (fn, okMsg) => {
    try { await fn(); if (okMsg) toast("Done", okMsg); await refresh(); }
    catch (e) { toast("Admin", e.message); }
  };

  if (!u.email_verified)
    btn("Verify", "Mark this email verified", () =>
      guard(() => API.adminVerify(u.id), `${u.username} verified.`));

  if (u.id !== ADMIN_ME && !u.config_admin) {
    if (u.is_admin)
      btn("Demote", "Revoke operator access", () =>
        guard(() => API.adminSetAdmin(u.id, false), `${u.username} is no longer an operator.`));
    else
      btn("Make operator", "Grant operator access", () =>
        guard(() => API.adminSetAdmin(u.id, true), `${u.username} is now an operator.`));
  }

  if (u.id !== ADMIN_ME && !u.config_admin) {
    if (u.disabled)
      btn("Reinstate", "Allow this account to sign in again", () =>
        guard(() => API.adminSetDisabled(u.id, false), `${u.username} reinstated.`));
    else
      btn("Suspend", "Block this account from signing in", () =>
        guard(() => API.adminSetDisabled(u.id, true), `${u.username} suspended.`), "warn");
  }

  btn("Reset password", "Set a new password for this account", () => {
    const pw = prompt(`New password for ${u.username} (min 8 characters):`);
    if (pw == null) return;
    if (pw.length < 8) { toast("Too short", "Password must be at least 8 characters."); return; }
    guard(() => API.adminResetPassword(u.id, pw), `Password reset for ${u.username}.`);
  });

  if (u.id !== ADMIN_ME && !u.config_admin)
    btn("Delete", "Permanently delete this account and all its data", () => {
      if (!confirm(`Delete ${u.username} and all their feeds, alerts, and pantheons? This cannot be undone.`)) return;
      guard(() => API.adminDeleteUser(u.id), `${u.username} deleted.`);
    }, "danger");

  row.appendChild(acts);
  return row;
}

/* ---------- favourite locations ----------
   A place plus a radius. Anything reported inside it is flagged wherever it
   appears, and every location shares one feed. Place lookup is served by
   the built-in gazetteer, so no third-party geocoder ever sees what the user
   is watching; anywhere the gazetteer doesn't know can be dropped as a pin. */
let LOCATIONS = [];

const LocationsPanel = {
  map: null,
  marker: null,
  circle: null,
  point: null,        // {lat, lon} currently being placed
  editing: null,      // location being edited, if any

  async open() {
    el("locations-panel").hidden = false;
    this._initMap();
    await this.refresh();
    // Leaflet measures the container; it was display:none until a moment ago.
    setTimeout(() => this.map && this.map.invalidateSize(), 60);
  },

  close() { el("locations-panel").hidden = true; },

  _initMap() {
    if (this.map) return;
    this.map = L.map("loc-map", { worldCopyJump: true }).setView([25, 10], 2);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(this.map);
    this.saved = L.featureGroup().addTo(this.map);
    this.map.on("click", (e) => this.setPoint(e.latlng.lat, e.latlng.lng));
  },

  setPoint(lat, lon, name) {
    this.point = { lat, lon };
    if (name && !el("loc-name").value.trim()) el("loc-name").value = name;
    el("loc-coords").textContent = `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
    el("btn-loc-save").disabled = false;
    this._drawPending();
    this.map.setView([lat, lon], Math.max(this.map.getZoom(), 6));
  },

  radiusKm() { return Number(el("loc-radius").value) || 25; },

  _drawPending() {
    if (!this.map) return;
    if (this.marker) this.marker.remove();
    if (this.circle) this.circle.remove();
    if (!this.point) return;
    const { lat, lon } = this.point;
    this.marker = L.marker([lat, lon]).addTo(this.map);
    this.circle = L.circle([lat, lon], {
      radius: this.radiusKm() * 1000, color: "#d4af37", weight: 2, fillOpacity: 0.08,
    }).addTo(this.map);
  },

  async refresh() {
    try { LOCATIONS = await API.locations(); }
    catch (e) { this.showError(e.message); return; }
    this.renderList();
    this.renderSaved();
  },

  renderSaved() {
    if (!this.saved) return;
    this.saved.clearLayers();
    for (const loc of LOCATIONS) {
      L.circle([loc.lat, loc.lon], {
        radius: loc.radius_km * 1000, color: "#4caf50", weight: 1.5,
        fillOpacity: 0.06, interactive: false,
      }).addTo(this.saved);
    }
  },

  renderList() {
    const box = el("loc-list");
    box.innerHTML = "";
    if (!LOCATIONS.length) {
      box.appendChild(feedEmpty("None yet — search for a place or click the map."));
      return;
    }
    for (const loc of LOCATIONS) {
      const row = document.createElement("div");
      row.className = "pn-row";
      const name = document.createElement("span");
      name.className = "pn-name";
      name.textContent = `📍 ${loc.name}`;
      const meta = document.createElement("span");
      meta.className = "s-meta";
      meta.textContent = `${loc.radius_km} km`
        + (loc.pantheon_id ? ` · shared by ${loc.shared_by || "a member"}` : "");
      row.append(name, meta);

      const show = document.createElement("button");
      show.className = "btn small";
      show.textContent = "Show";
      show.title = "Centre the map here";
      show.onclick = () => this.map.setView([loc.lat, loc.lon], 8);
      row.appendChild(show);

      // Shared copies belong to the Pantheon; only the owner edits them.
      if (loc.mine && !loc.pantheon_id) {
        const edit = document.createElement("button");
        edit.className = "btn small";
        edit.textContent = "Edit";
        edit.onclick = () => this.startEdit(loc);
        row.appendChild(edit);

        if (PANTHEONS.length) {
          const share = toolBtn("🏛", `Share ${loc.name} with a Pantheon`, (e) =>
            pantheonPickMenu(e.currentTarget, async (p) => {
              try {
                await API.shareLocation(loc.id, p.id);
                toast("Location shared", `“${loc.name}” now flags news for ${p.name}.`);
                await this.refresh();
              } catch (err) { this.showError(err.message); }
            }));
          row.appendChild(share);
        }
        const del = toolBtn("🗑", `Delete ${loc.name}`, async () => {
          if (!confirm(`Delete “${loc.name}”? Your 📍 Favourite Locations feed stops `
                       + "covering it.")) return;
          await API.deleteLocation(loc.id);
          await this.refresh();
          await refreshFeeds();
        });
        row.appendChild(del);
      }
      box.appendChild(row);
    }
  },

  startEdit(loc) {
    this.editing = loc;
    el("loc-name").value = loc.name;
    el("loc-radius").value = loc.radius_km;
    el("loc-radius-val").textContent = `${loc.radius_km} km`;
    el("btn-loc-save").textContent = "Save changes";
    el("btn-loc-cancel").hidden = false;
    this.setPoint(loc.lat, loc.lon);
  },

  cancelEdit() {
    this.editing = null;
    this.point = null;
    el("loc-name").value = "";
    el("loc-coords").textContent = "";
    el("btn-loc-save").textContent = "Save location";
    el("btn-loc-save").disabled = true;
    el("btn-loc-cancel").hidden = true;
    this.clearError();
    this._drawPending();
  },

  async save() {
    this.clearError();
    const name = el("loc-name").value.trim();
    if (!name) { this.showError("Give the location a name."); el("loc-name").focus(); return; }
    if (!this.point) { this.showError("Pick a point — search for a place or click the map."); return; }
    const body = { name, lat: this.point.lat, lon: this.point.lon, radius_km: this.radiusKm() };
    try {
      if (this.editing) {
        await API.updateLocation(this.editing.id, body);
        toast("Location updated", `“${name}” now covers ${body.radius_km} km.`);
      } else {
        await API.createLocation(body);
        toast("Location saved", `News within ${body.radius_km} km of “${name}” is now `
              + "flagged, and appears in your 📍 Favourite Locations feed.");
      }
      this.cancelEdit();
      await this.refresh();
      await refreshFeeds();
    } catch (e) { this.showError(e.message); }
  },

  showError(msg) { const b = el("loc-error"); b.textContent = msg; b.hidden = false; },
  clearError() { const b = el("loc-error"); b.textContent = ""; b.hidden = true; },
};

function wireLocations() {
  el("btn-locations").onclick = () => LocationsPanel.open();
  el("btn-close-locations").onclick = () => LocationsPanel.close();
  el("btn-loc-save").onclick = () => LocationsPanel.save();
  feedback(el("btn-loc-save"), "Saving…");
  el("btn-loc-cancel").onclick = () => LocationsPanel.cancelEdit();

  el("loc-radius").addEventListener("input", () => {
    el("loc-radius-val").textContent = `${LocationsPanel.radiusKm()} km`;
    LocationsPanel._drawPending();
  });

  let searchTimer;
  el("loc-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = el("loc-search").value.trim();
      const box = el("loc-results");
      if (q.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
      let hits = [];
      try { hits = await API.placeSearch(q); } catch (e) { return; }
      box.innerHTML = "";
      box.hidden = hits.length === 0;
      for (const h of hits) {
        const b = document.createElement("button");
        b.className = "loc-result";
        b.textContent = `${h.kind === "country" ? "🌐" : "🏙"} ${h.name}`
          + (h.kind === "city" && h.country ? ` · ${COUNTRY_NAMES.get(h.country) || h.country}` : "");
        b.onclick = () => {
          LocationsPanel.setPoint(h.lat, h.lon, h.name);
          box.hidden = true;
          el("loc-search").value = "";
        };
        box.appendChild(b);
      }
    }, 220);
  });
}

/* ---------- Pantheon create / manage modal ----------
   Same shape as the feed & alert builder: tabs, one working area, actions along
   the bottom. Replaces the inline form and the accordion that used to live in
   the side panel, where members, invites, and access settings were stacked into
   a narrow column. */
const PantheonModal = {
  id: null,          // null = creating
  detail: null,      // server detail while managing
  tab: "details",

  open(pantheon = null) {
    this.id = pantheon ? pantheon.id : null;
    this.detail = null;
    this.clearError();
    el("pn-modal-title").textContent = pantheon ? `🏛 ${pantheon.name}` : "New Pantheon";
    el("pn-f-name").value = pantheon ? pantheon.name : "";
    el("pn-f-desc").value = pantheon ? pantheon.description || "" : "";
    el("pn-f-visibility").value = pantheon ? pantheon.visibility : "private";
    el("btn-pn-save").textContent = pantheon ? "Save changes" : "Create";
    // Members and Access only mean something once the Pantheon exists.
    for (const b of el("pn-tabs").querySelectorAll("button"))
      b.hidden = !pantheon && b.dataset.tab !== "details";
    el("btn-pn-board").hidden = !pantheon;
    el("btn-pn-delete").hidden = true;
    el("btn-pn-leave").hidden = true;
    this.showTab("details");
    el("pn-backdrop").hidden = false;
    el("pn-f-name").focus();
    if (pantheon) this.loadDetail();
  },

  close() { el("pn-backdrop").hidden = true; },

  showTab(tab) {
    this.tab = tab;
    for (const s of document.querySelectorAll(".pn-tab"))
      s.hidden = s.dataset.tab !== tab;
    for (const b of el("pn-tabs").querySelectorAll("button"))
      b.classList.toggle("active", b.dataset.tab === tab);
  },

  async loadDetail() {
    try {
      const d = await API.pantheonDetail(this.id);
      this.detail = d;
      const admin = d.role === "owner" || d.role === "admin";
      el("btn-pn-delete").hidden = d.role !== "owner";
      el("btn-pn-leave").hidden = d.role === "owner";
      // Owners and admins edit the identity fields; everyone else reads them.
      for (const id of ["pn-f-name", "pn-f-desc", "pn-f-visibility"])
        el(id).disabled = !admin;
      el("btn-pn-save").hidden = !admin;
      this.renderMembers(d);
      this.renderAccess(d);
    } catch (e) {
      this.showError(e.message);
    }
  },

  renderMembers(d) {
    const box = el("pn-members");
    box.innerHTML = "";
    const admin = d.role === "owner" || d.role === "admin";
    const mayInvite = admin || (d.settings && d.settings.who_can_invite === "members");
    el("pn-invite-row").hidden = !mayInvite;

    for (const m of d.members) {
      const row = document.createElement("div");
      row.className = "pn-row";
      const name = document.createElement("span");
      name.className = "pn-name";
      name.textContent =
        `${m.role === "owner" ? "👑" : m.role === "admin" ? "🛡" : "👤"} ${m.username}`;
      const meta = document.createElement("span");
      meta.className = "s-meta";
      meta.textContent = m.role;
      row.append(name, meta);

      if (d.role === "owner" && m.role !== "owner") {
        const promote = document.createElement("button");
        promote.className = "btn small";
        promote.textContent = m.role === "admin" ? "Demote" : "Make admin";
        promote.onclick = async () => {
          await API.setMemberRole(d.id, m.user_id, m.role === "admin" ? "member" : "admin");
          await this.loadDetail();
        };
        feedback(promote);
        row.appendChild(promote);
      }
      if (admin && m.role !== "owner" && (d.role === "owner" || m.role === "member")) {
        const kick = document.createElement("button");
        kick.className = "icon-btn";
        kick.textContent = "✕";
        kick.title = `Remove ${m.username}`;
        kick.setAttribute("aria-label", `Remove ${m.username}`);
        kick.onclick = async () => {
          if (!confirm(`Remove ${m.username} from ${d.name}?`)) return;
          await API.removeMember(d.id, m.user_id);
          await this.loadDetail();
          refreshPantheons();
        };
        feedback(kick);
        row.appendChild(kick);
      }
      box.appendChild(row);
    }
    const pending = el("pn-pending");
    pending.textContent = (d.pending_invites && d.pending_invites.length)
      ? "Invited, not yet accepted: " + d.pending_invites.join(", ") : "";
  },

  renderAccess(d) {
    const admin = d.role === "owner" || d.role === "admin";
    const s = d.settings || {};
    el("pn-f-invitepol").value = s.who_can_invite || "members";
    el("pn-f-sharepol").value = s.who_can_share || "members";
    el("pn-f-invitepol").disabled = !admin;
    el("pn-f-sharepol").disabled = !admin;
    el("pn-access-note").textContent = admin
      ? "Applies to everyone in this Pantheon."
      : "Only the owner and admins can change these.";
  },

  showError(msg) {
    const box = el("pn-error");
    box.textContent = msg;
    box.hidden = false;
  },
  clearError() {
    const box = el("pn-error");
    if (box) { box.textContent = ""; box.hidden = true; }
  },

  async save() {
    this.clearError();
    const name = el("pn-f-name").value.trim();
    if (!name) {
      this.showTab("details");
      this.showError("Give the Pantheon a name.");
      el("pn-f-name").focus();
      return;
    }
    const body = {
      name,
      description: el("pn-f-desc").value.trim(),
      visibility: el("pn-f-visibility").value,
    };
    try {
      if (this.id) {
        await API.updatePantheon(this.id, {
          ...body,
          settings: {
            who_can_invite: el("pn-f-invitepol").value,
            who_can_share: el("pn-f-sharepol").value,
          },
        });
        toast("Saved", `${name} updated.`);
      } else {
        const created = await API.createPantheon(body);
        toast("Pantheon founded", `“${name}” is ready — invite members from the Members tab.`);
        this.open(created);   // stay open so the user can invite straight away
        await refreshPantheons();
        renderPantheonsPanel();
        renderBoard();
        return;
      }
      await refreshPantheons();
      renderPantheonsPanel();
      renderBoard();
      this.close();
    } catch (e) {
      this.showError(e.message);
    }
  },
};

function wirePantheonModal() {
  el("btn-close-pn").onclick = () => PantheonModal.close();
  el("pn-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("pn-backdrop")) PantheonModal.close();
  });
  for (const b of el("pn-tabs").querySelectorAll("button"))
    b.onclick = () => PantheonModal.showTab(b.dataset.tab);

  el("btn-pn-save").onclick = () => PantheonModal.save();
  feedback(el("btn-pn-save"), "Saving…");

  el("btn-pn-board").onclick = () => {
    const id = PantheonModal.id;
    PantheonModal.close();
    el("pantheons-panel").hidden = true;
    if (id) setView("pantheon:" + id);
  };

  el("btn-pn-invite").onclick = async () => {
    const who = el("pn-f-invite").value.trim();
    if (!who) return;
    try {
      const r = await API.invitePantheon(PantheonModal.id, who);
      el("pn-f-invite").value = "";
      toast("Invitation sent", `${r.username} can accept it from their 🏛 panel.`);
      await PantheonModal.loadDetail();
    } catch (e) { PantheonModal.showError(e.message); }
  };
  feedback(el("btn-pn-invite"), "Inviting…");

  el("btn-pn-delete").onclick = async () => {
    const d = PantheonModal.detail;
    if (!d || !confirm(`Delete ${d.name} for everyone, including its shared feeds and alerts?`)) return;
    try {
      await API.deletePantheon(d.id);
      PantheonModal.close();
      await refreshPantheons();
      renderPantheonsPanel();
      if (VIEW === "pantheon:" + d.id) setView("home");
    } catch (e) { PantheonModal.showError(e.message); }
  };
  feedback(el("btn-pn-delete"), "Deleting…");

  el("btn-pn-leave").onclick = async () => {
    const d = PantheonModal.detail;
    if (!d || !confirm(`Leave ${d.name}?`)) return;
    try {
      await API.leavePantheon(d.id);
      PantheonModal.close();
      await refreshPantheons();
      renderPantheonsPanel();
      if (VIEW === "pantheon:" + d.id) setView("home");
    } catch (e) { PantheonModal.showError(e.message); }
  };
  feedback(el("btn-pn-leave"), "Leaving…");
}

/* Small anchored chooser: pick one of my Pantheons. */
function pantheonPickMenu(anchor, onPick) {
  const old = document.querySelector(".pop-menu");
  if (old) old.remove();
  const menu = document.createElement("div");
  menu.className = "pop-menu";
  for (const p of PANTHEONS) {
    const b = document.createElement("button");
    b.textContent = `🏛 ${p.name}`;
    b.onclick = () => { menu.remove(); onPick(p); };
    menu.appendChild(b);
  }
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + "px";
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - menu.offsetWidth - 8)) + "px";
  setTimeout(() => document.addEventListener("mousedown", function close(e) {
    if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("mousedown", close); }
  }), 0);
}

async function renderPantheonsPanel() {
  // pending invitations
  const invSection = el("pn-invites-section");
  const invBox = el("pn-invites");
  invBox.innerHTML = "";
  invSection.hidden = PANTHEON_INVITES.length === 0;
  for (const inv of PANTHEON_INVITES) {
    const row = document.createElement("div");
    row.className = "pn-row";
    const label = document.createElement("span");
    label.className = "pn-name";
    label.textContent = `🏛 ${inv.name}`;
    label.title = `Invited by ${inv.invited_by} · ${inv.member_count} member${plural(inv.member_count)}`;
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `from ${inv.invited_by}`;
    const yes = document.createElement("button");
    yes.className = "btn small"; yes.textContent = "Accept";
    yes.onclick = async () => {
      try {
        await API.acceptInvite(inv.id);
        await refreshPantheons();
        renderPantheonsPanel();
        renderBoard();
        toast("Welcome", `You joined ${inv.name} — its board is now in the view switcher.`);
      } catch (e) { toast("Could not join", e.message); }
    };
    feedback(yes, "Joining…");
    const no = document.createElement("button");
    no.className = "icon-btn"; no.textContent = "✕"; no.title = "Decline";
    no.onclick = async () => {
      await API.declineInvite(inv.id).catch(() => {});
      await refreshPantheons();
      renderPantheonsPanel();
    };
    feedback(no);
    row.append(label, meta, yes, no);
    invBox.appendChild(row);
  }

  // my pantheons
  const mine = el("pn-mine");
  mine.innerHTML = "";
  if (!PANTHEONS.length) {
    mine.innerHTML = '<div class="feed-empty">None yet — create one above, accept an invitation, or join a public Pantheon below.</div>';
  }
  for (const p of PANTHEONS) mine.appendChild(pantheonCard(p));

  // public directory
  const pub = el("pn-public");
  pub.innerHTML = '<div class="feed-empty">Loading…</div>';
  try {
    const list = (await API.publicPantheons()).filter(p => !p.joined);
    pub.innerHTML = "";
    if (!list.length)
      pub.innerHTML = '<div class="feed-empty">No public Pantheons to join right now.</div>';
    for (const p of list) {
      const row = document.createElement("div");
      row.className = "pn-row";
      const label = document.createElement("span");
      label.className = "pn-name";
      label.textContent = `🌐 ${p.name}`;
      label.title = p.description || p.name;
      const meta = document.createElement("span");
      meta.className = "s-meta";
      meta.textContent = `${p.member_count} member${plural(p.member_count)}`;
      const join = document.createElement("button");
      join.className = "btn small"; join.textContent = "Join";
      join.onclick = async () => {
        try {
          await API.joinPantheon(p.id);
          await refreshPantheons();
          renderPantheonsPanel();
          renderBoard();
          toast("Joined", `Welcome to ${p.name}.`);
        } catch (e) { toast("Could not join", e.message); }
      };
      feedback(join, "Joining…");
      row.append(label, meta, join);
      pub.appendChild(row);
    }
  } catch (e) { pub.innerHTML = '<div class="feed-empty">Could not load the directory.</div>'; }
}

function pantheonCard(p) {
  const card = document.createElement("div");
  card.className = "pn-card";
  const head = document.createElement("div");
  head.className = "pn-row";
  const label = document.createElement("span");
  label.className = "pn-name";
  label.textContent = `${p.visibility === "public" ? "🌐" : "🔒"} ${p.name}`;
  const meta = document.createElement("span");
  meta.className = "s-meta";
  meta.textContent = `${p.role} · ${p.member_count} member${plural(p.member_count)} · ${p.feed_count}📋 ${p.alert_count}🔔`;
  const openBtn = document.createElement("button");
  openBtn.className = "btn small"; openBtn.textContent = "Board";
  openBtn.title = "Open this Pantheon's shared board";
  openBtn.onclick = () => { el("pantheons-panel").hidden = true; setView("pantheon:" + p.id); };
  const detailBtn = document.createElement("button");
  detailBtn.className = "btn small"; detailBtn.textContent = "Manage";
  detailBtn.title = "Members, invitations, and access settings";
  detailBtn.onclick = () => PantheonModal.open(p);
  head.append(label, meta, openBtn, detailBtn);
  card.appendChild(head);
  return card;
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
      "(keywords, Boolean search, countries, importance, or a drawn map area).</div>";
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
    const delivery = (alert.notify_email ? " ✉️" : "") + (alert.webhook_url ? " 🔗" : "");
    h.textContent = `${alert.name}${delivery}${alert.unseen ? ` (${alert.unseen} new)` : ""}`;
    if (delivery) h.title = "Delivers out-of-app:" +
      (alert.notify_email ? " email" : "") + (alert.webhook_url ? " webhook" : "");
    const tools = [];
    if (alert.pantheon_id) {
      const t = document.createElement("span");
      t.className = "tag";
      t.textContent = `🏛 ${alert.pantheon_name}`;
      t.title = `Shared with ${alert.pantheon_name} by ${alert.shared_by}`;
      tools.push(t);
    }
    const canEdit = alert.pantheon_id ? alert.can_edit : true;
    if (canEdit) {
      tools.push(
        toolBtn(alert.active ? "⏸" : "▶", alert.active ? "Pause" : "Resume", async () => {
          await API.updateAlert(alert.id, { name: alert.name, criteria: alert.criteria, active: !alert.active });
          await refreshAlerts();
        }),
        toolBtn("✎", "Edit alert", () => Builder.open("alert", alert)),
      );
    }
    if (!alert.pantheon_id && PANTHEONS.length) {
      tools.push(toolBtn("🏛", "Share with a Pantheon", (e) =>
        pantheonPickMenu(e.currentTarget, async (p) => {
          try {
            await API.shareAlert(alert.id, p.id);
            toast("Alert shared", `“${alert.name}” now fires for everyone in ${p.name}.`);
            await refreshAlerts();
          } catch (err) { toast("Could not share", err.message); }
        })));
    }
    tools.push(toolBtn("👁", "Mark all seen", async () => { await API.markAlertSeen(alert.id); await refreshAlerts(); }));
    head.append(h, ...tools);
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
      const href = safeUrl(ev.article.url);
      if (href) { link.href = href; link.target = "_blank"; link.rel = "noopener"; }
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
/* Match a source against the filter box: name, feed URL, country code and
   name, language, scope, platform, and how it was added — so "JP", "Japan",
   "reddit", "auto-discovered" and "paywall" all find what you'd expect. */
function sourceMatches(s, needle) {
  if (!needle) return true;
  const hay = [
    s.name, s.rss_url, s.homepage, s.country, COUNTRY_NAMES.get(s.country) || "",
    s.region, s.language, s.scope, s.platform, s.added_by,
    ...(s.categories || []),
    s.paywall ? "paywall paywalled" : "",
    s.repaired_from ? "repaired" : "",
    (s.last_status || "").startsWith("ok") ? "ok healthy" : (s.last_status ? "error broken" : ""),
  ].join(" ").toLowerCase();
  // Every word must appear somewhere, so terms narrow rather than widen.
  return needle.toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w));
}

async function renderSourcesPanel() {
  const box = el("sources-list");
  box.innerHTML = "";
  const needle = (el("src-search")?.value || "").trim();
  const shown = SOURCES.filter(s => sourceMatches(s, needle));
  const count = el("src-count");
  if (count) {
    count.textContent = needle
      ? `${shown.length} of ${SOURCES.length}`
      : `${SOURCES.length} source${plural(SOURCES.length)}`;
  }
  if (!shown.length) {
    box.appendChild(feedEmpty(needle
      ? `No source matches “${needle}”.`
      : "No sources yet."));
    return;
  }
  for (const s of shown) {
    const row = document.createElement("div");
    row.className = "source-row";
    const ok = (s.last_status || "").startsWith("ok");
    const status = document.createElement("span");
    status.className = ok ? "s-status-ok" : (s.last_status ? "s-status-bad" : "");
    status.title = s.last_status || "not yet polled";
    status.textContent = ok ? "●" : (s.last_status ? "●" : "○");
    const name = document.createElement("span");
    name.className = "s-name";
    name.textContent = `${s.paywall ? "🔒 " : ""}${PLATFORM_ICONS[s.platform] || "📰"} ${flagEmoji(s.country)} ${s.name}`;
    name.title = s.rss_url + (s.repaired_from ? `\nAuto-repaired — original URL: ${s.repaired_from}` : "")
      + (s.paywall ? "\nPaywalled — headlines only; readers get an archive.ph link" : "");
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `${s.scope} · ${s.language}${s.added_by !== "catalog" ? " · " + s.added_by : ""}`
      + (s.repaired_from ? " · 🔧 repaired" : "") + (s.paywall ? " · 🔒 paywalled" : "");
    const tools = [];
    if (!ok && s.last_status) {
      tools.push(toolBtn("🔧", "Attempt automatic repair (re-check the URL, rediscover the feed)", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true; btn.textContent = "⏳";
        try {
          const r = await API.repairSource(s.id);
          if (!r.repaired) toast("Could not repair", r.detail || r.status);
          else if (r.changed) toast("Source repaired",
                       `Feed switched to ${r.rss_url} — ${r.new_articles} `
                       + `article${plural(r.new_articles)} pulled.`);
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
  const paywall = document.createElement("input");
  paywall.type = "checkbox"; paywall.checked = !!s.paywall;
  const paywallField = document.createElement("label");
  paywallField.className = "check-inline";
  // Just the label — what it does is explained in the FAQ, not squeezed in here.
  paywallField.title = "FAQ → Where the news comes from → Paywalled outlets";
  paywallField.append(paywall, document.createTextNode(" 🔒 Paywalled"));
  form.append(
    field("Name", name), field("Feed URL", url),
    field("Platform", platform), field("Scope", scope),
    field("Country", country), field("Language (code)", language),
    field("Categories (comma-separated)", cats),
    paywallField,
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
        paywall: paywall.checked,
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
// The rolling poller broadcasts every ~15s; without throttles every open tab
// would refetch /api/meta and reload every visible column on each tick.
let lastStatsRefresh = 0;   // stat tiles: at most once a minute
let lastBoardRefresh = 0;   // board columns: at most every 30s (auto-refresh)
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
      if (Date.now() - lastStatsRefresh > 60000) {
        lastStatsRefresh = Date.now();
        API.meta().then(renderStats).catch(() => {});
      }
    } else if (msg.type === "articles") {
      clearTimeout(refreshTimer);
      // Coalesce bursts: reload soon after quiet, but never more than once
      // per 30s while ticks keep producing articles.
      const wait = Math.max(1500, 30000 - (Date.now() - lastBoardRefresh));
      refreshTimer = setTimeout(async () => {
        lastBoardRefresh = lastStatsRefresh = Date.now();
        renderStats(await API.meta());
        // Whatever is actually on screen. This used to refresh the account's
        // own feeds regardless, so a Pantheon board never picked up new
        // articles between renders.
        await mapLimited(visibleFeeds(), BOARD_LOAD_CONCURRENCY,
                         (f) => loadFeedArticles(f, /*force*/ true));
        schedulePrefetch(BOARD_GENERATION);   // keep the other boards current too
      }, wait);
    } else if (msg.type === "alert" && (msg.user_id === Session.userKey()
               || (msg.pantheon_id && PANTHEONS.some(p => p.id === msg.pantheon_id)))) {
      const t = impTier(msg.importance);
      toast(`🔔 ${msg.alert_name}`, `${t.icon} ${t.label} — ${msg.title}`, true);
      alertSound();
      desktopNotify(`D.E.L.P.H.I. alert: ${msg.alert_name}`, `${t.label} — ${msg.title}`);
      refreshAlerts();
    }
  };
  es.onerror = () => { es.close(); setTimeout(connectStream, 5000); };
}

/* ---------- button feedback ----------
   Wrap a button that already has an async onclick so the click is always
   visibly acknowledged: the control disables while the work is in flight (which
   also stops double-submits), and a failure becomes a toast instead of nothing
   at all. Call it right after assigning the handler:

       btn.onclick = async () => { ... };
       feedback(btn);                       // spinner state, keeps its label
       feedback(btn, "Saving…");            // text buttons can say what's happening

   Without a label the button keeps its text and gets the .working class, so
   icon-only buttons (✕, 🔧, 🗑) don't lose their glyph. */
function feedback(btn, label = "") {
  const handler = btn.onclick;
  if (!handler) return btn;
  btn.onclick = async (ev) => {
    if (btn.disabled) return;               // already running
    const text = btn.textContent, wasDisabled = btn.disabled;
    btn.disabled = true;
    if (label) btn.textContent = label;
    btn.classList.add("working");
    try {
      return await handler.call(btn, ev);
    } catch (e) {
      console.error("[action]", e);
      toast("That didn't work", (e && e.message) || String(e));
    } finally {
      // The handler often re-renders the list this button lives in, which
      // discards the node — only restore it if it's still on the page.
      if (btn.isConnected) {
        btn.disabled = wasDisabled;
        // Restore the old label only if the handler didn't set its own. A
        // handler may legitimately relabel the button it was invoked from
        // (Create -> Save changes); putting the previous text back would undo
        // that, and the button would then lie about what it does.
        if (label && btn.textContent === label) btn.textContent = text;
        btn.classList.remove("working");
      }
    }
  };
  return btn;
}

// Nothing should fail silently: an async handler that rejects without its own
// catch would otherwise leave the user staring at an unchanged screen.
window.addEventListener("unhandledrejection", (ev) => {
  const reason = ev.reason;
  console.error("[unhandled]", reason);
  toast("Something went wrong", (reason && reason.message) || String(reason));
});

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
