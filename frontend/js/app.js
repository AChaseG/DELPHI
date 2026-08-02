/* Dashboard: feed board, alerts panel, sources panel, live SSE updates. */
let META = null;
// The full catalog — a thousand outlets and half a megabyte — is fetched only
// when the Sources panel or the wizard's picker needs it. Startup takes the
// slim list below instead, which is all a feed badge requires.
let SOURCES = [];
let SOURCE_NAMES = new Map();   // id -> name, always loaded
let FEEDS = [];
let ALERTS = [];
let PANTHEONS = [];          // organizations this account belongs to
let PANTHEON_INVITES = [];   // pending invitations for this account
let COUNTRY_NAMES = new Map();
// iso2 -> {lat, lon}. The centre of a country, used when an article names no
// place of its own and the browser has to decide whether it is near one of
// the reader's favourite locations.
let COUNTRY_POINTS = new Map();
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

/* ---------- change password ---------- */
function wireChangePassword() {
  const backdrop = el("pw-backdrop");
  const err = el("pw-error");
  const fields = ["pw-current", "pw-new", "pw-confirm"];
  const close = () => {
    backdrop.hidden = true;
    // Never leave a password sitting in a field behind a closed dialog.
    fields.forEach(id => { el(id).value = ""; });
    err.hidden = true;
  };
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };

  el("btn-change-password").onclick = () => {
    close();
    backdrop.hidden = false;
    el("pw-current").focus();
  };
  el("btn-close-pw").onclick = close;
  el("btn-pw-cancel").onclick = close;
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });

  el("btn-pw-save").onclick = async () => {
    const current = el("pw-current").value;
    const next = el("pw-new").value;
    // Checked here as well as on the server, because these two are the ones a
    // person can see are wrong before a round trip.
    if (!current) return fail("Enter your current password.");
    if (next.length < 8) return fail("The new password must be at least 8 characters.");
    if (next !== el("pw-confirm").value) return fail("The two new passwords don't match.");

    try {
      // API.changePassword adopts the new token itself, so nothing between the
      // reply and the session update can fire a request with the dead one.
      await API.changePassword(current, next);
    } catch (e) {
      // A wrong current password answers 403, not 401, on purpose: 401 is what
      // the API layer treats as "this session is over" and reloads on, so
      // mistyping it would sign the reader out instead of telling them.
      return fail(e.message);
    }
    close();
    toast("Password changed",
          "Your other devices have been signed out. This one is still signed in.");
  };
  feedback(el("btn-pw-save"), "Changing…");
}

/* Handlers that used to be onerror=/onclick= attributes in the markup.
   A Content-Security-Policy of script-src 'self' refuses inline handlers,
   because the browser cannot tell one the author wrote from one injected into
   the page — that refusal is most of what a CSP is for.

   Images need care in the move: they start loading before this script does, so
   one can have failed already, and an error listener attached afterwards never
   fires. `complete` with no `naturalWidth` is what a finished-and-failed image
   looks like, so check for that as well as listening. */
function wireStaticHandlers() {
  const onBroken = (id, replace) => {
    const img = el(id);
    if (!img) return;
    img.addEventListener("error", () => replace(img));
    if (img.complete && img.naturalWidth === 0) replace(img);
  };
  // The logo carries the name, so its fallback is the name as text.
  onBroken("brand-logo", (img) => img.replaceWith(Object.assign(
    document.createElement("span"), { textContent: "D.E.L.P.H.I." })));
  onBroken("gate-logo", (img) => {
    img.remove();
    const title = document.querySelector(".gate-title");
    if (title) title.hidden = false;
  });
  const reload = el("btn-stream-reload");
  if (reload) reload.onclick = () => location.reload();
}

async function boot() {
  wireStaticHandlers();
  if (!Session.token()) {   // account required: show the sign-in gate only
    wireGate();
    el("gate").hidden = false;
    document.querySelector(".topbar").hidden = true;
    return;
  }
  // Everything boot needs is an independent read, so ask for all of it at once.
  // These used to be awaited one after another — settings, then meta, then
  // locations, then Pantheons, then feeds — six round trips deep before the
  // board could start, and five of them carrying almost no data. On a link with
  // 150ms of latency that put the first column at 1,083ms; asked for in one
  // round, and with the catalog no longer among them, it lands at 274ms.
  const local = hydrateFeedCache();          // disk, not network: no reason to wait
  const wanted = {
    settings: API.getSettings().then(r => Settings.adopt(r.settings)).catch(() => {}),
    meta: API.meta(),
    locations: API.locations().catch(() => []),
    pantheons: API.pantheons().catch(() => ({ mine: [], invites: [] })),
    feeds: API.feeds().catch(() => null),
    alerts: API.alerts().catch(() => null),
    names: API.sourceNames().catch(() => []),
  };

  // Paint from the disk copy before any of that lands. A reader who was here
  // before gets their board immediately and the network only reconciles it.
  const cached = await local;
  if (cached && cached.feeds) {
    FEEDS = cached.feeds;
    PANTHEONS = cached.pantheons || [];
    LOCATIONS = cached.locations || LOCATIONS;
  }

  META = await wanted.meta;
  COUNTRY_NAMES = new Map(META.countries.map(c => [c.iso2, c.name]));
  COUNTRY_POINTS = new Map(META.countries.map(c => [c.iso2, { lat: c.lat, lon: c.lon }]));
  wanted.names.then((rows) => {
    SOURCE_NAMES = new Map(rows.map(r => [r.id, r.name]));
  });
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
  wirePressFeedback();
  wireRowPressRecovery();
  renderStats();

  // A board from the disk copy, on screen before the server has answered.
  if (FEEDS.length || PANTHEONS.length) {
    renderViewSwitch();
    renderBoard();
  }

  // Now reconcile with what the server says, and only redraw if it differs.
  const [locations, pantheons, feeds] = await Promise.all([
    wanted.locations, wanted.pantheons, wanted.feeds, wanted.settings,
  ]);
  LOCATIONS = locations;
  rememberList(LIST_LOCATIONS, LOCATIONS);
  const listChanged = feeds && !sameFeeds(FEEDS, feeds);
  const groupsChanged = JSON.stringify(PANTHEONS.map(p => p.id))
    !== JSON.stringify(pantheons.mine.map(p => p.id));
  PANTHEONS = pantheons.mine;
  PANTHEON_INVITES = pantheons.invites;
  if (feeds) FEEDS = feeds;
  rememberList(LIST_FEEDS, FEEDS);
  rememberList(LIST_PANTHEONS, PANTHEONS);
  renderPantheonBadge();
  if (groupsChanged) renderViewSwitch();
  // Past this point the app is usable whatever happened: a board that fails to
  // load is a column with an error in it, not a reason to replace the whole
  // dashboard — rail, Settings and sign-out included.
  if (listChanged || !(FEEDS.length || PANTHEONS.length)) {
    await renderBoard().catch((e) => console.error("[boot] board", e));
  } else {
    refreshVisibleColumns();   // same columns: just bring their contents current
  }
  // Boot already asked for the alerts; hand them over rather than asking twice.
  await refreshAlerts(await wanted.alerts).catch((e) => console.error("[boot] alerts", e));
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
  Store.remove(key).catch(() => {});   // and on disk, or a reload restores it
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
/* Columns a repaint wanted to put up while a column was being dragged. */
let BOARD_PAINT_HELD = null;

function paintBoard(columns) {
  // A background refresh lands every few seconds, and repainting replaces
  // every column. Do that mid-drag and the column under the pointer is torn
  // out of the board it belongs to; the drag then re-inserts a node the board
  // has already replaced, and the reader is left looking at the same column
  // twice. Hold the paint until the pointer comes up — the drag ends with a
  // render of its own, so nothing is lost by waiting.
  if (document.body.classList.contains("board-reordering")) {
    BOARD_PAINT_HELD = columns;
    return;
  }
  const board = el("board");
  const searchCol = el("search-col");
  board.replaceChildren(...(searchCol ? [searchCol, ...columns] : columns));
  syncBoardScrollbar();
}

/* Put up a paint that was held during a drag. Only needed where the drag ends
   without rendering for itself. */
function releaseBoardPaint() {
  const held = BOARD_PAINT_HELD;
  BOARD_PAINT_HELD = null;
  if (held) paintBoard(held);
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
  el("btn-create").onclick = () => openBuilder("feed");
  el("btn-empty-new-feed").onclick = () => openBuilder("feed");
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
  el("btn-close-story").onclick = closeStory;
  el("btn-story-export").onclick = () => storyExportMenu(el("btn-story-export"));
  el("story-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("story-backdrop")) closeStory();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el("story-backdrop").hidden) closeStory();
  });
  el("btn-alerts-panel").onclick = () => { el("alerts-panel").hidden = false; renderAlertsPanel(); };
  el("btn-close-alerts").onclick = () => { el("alerts-panel").hidden = true; };
  el("btn-toggle-alerts-map").onclick = () => {
    localStorage.setItem("gnd_alerts_map", alertsMapWanted() ? "0" : "1");
    renderAlertsPanel();
  };
  el("btn-sources").onclick = openSourcesPanel;
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
      await reloadSources();
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
   Three tabs: How to (procedure), FAQ (explanation), Troubleshooting (symptoms).
   Anyone who opens help without asking for a particular tab wants the
   instructions, so that's what they get; the tab is not remembered, because the
   next visit is a new question and rarely the same kind. */
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
  el("btn-open-trouble").onclick = () => openHelp("trouble");
  wireChangePassword();
  el("btn-signout-all").onclick = async () => {
    if (!confirm("Sign out of every device?\n\nEvery signed-in browser and phone, "
                 + "including this one, will be asked to sign in again. Your feeds, "
                 + "alerts, and settings are untouched.")) return;
    await API.signOutEverywhere();
    // The token this tab holds was just invalidated server-side, so clear the
    // cached news with it: signing out everywhere is what someone does when
    // they think this device is not solely theirs either.
    try { await Store.clear(); } catch (e) { /* nothing to clear */ }
    Session.clear();
    try {
      sessionStorage.setItem("gnd_signed_out_reason",
        "Signed out everywhere. Every other device has been signed out too — "
        + "sign in again to carry on.");
    } catch (e) { /* private mode: the gate just won't have the note */ }
    location.reload();
  };
  feedback(el("btn-signout-all"), "Signing out…");
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
    catch (e) { toast("Couldn't load the release history", e.message); }
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
    // Applied in the browser now, so this is a repaint of what is already
    // here — no column has to be fetched again.
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
  el("btn-profile").onclick = async () => {
    if (!confirm(`Signed in as ${Session.username()}. Sign out?`)) return;
    // The cached news is a reading history sitting on a disk that may not be
    // this person's alone. Signing out erases it.
    try { await Store.clear(); } catch (e) { /* nothing to clear */ }
    Session.clear();
    location.reload();
  };
  feedback(el("btn-profile"));
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

  // Why the board vanished. A session that expires, is suspended, or belongs to
  // a deleted account all end the same way — a reload back to this card — and
  // without the reason the reader is left to guess which, or to assume Delphi
  // lost their work. api() records it on the way out; it is shown once.
  try {
    const reason = sessionStorage.getItem("gnd_signed_out_reason");
    if (reason) {
      sessionStorage.removeItem("gnd_signed_out_reason");
      say(reason);
    }
  } catch (e) { /* private mode: no note, the gate still works */ }

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
    let r;
    try {
      r = await API.forgotPassword(el("forgot-email").value.trim());
    } catch (e) {
      // Swallowing this reported "a reset link is on its way" for a request
      // that never arrived — the one message here that must never be wrong,
      // because the reader's next move is to go and wait for an email.
      say(`No reset was requested. ${e.message}`);
      return;
    }
    show("gate-signin");
    say(r.mail_enabled === false
      ? "Email isn't configured on this server, so no link can be sent — ask "
        + "the administrator to reset your password from the operator console."
      : "If that address has an account, a reset link is on its way. It is "
        + "valid for one hour. Nothing arrives if the address isn't registered.",
      true);
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

/* The full catalog is close to half a megabyte, and nothing on the board needs
   it — a feed badge only wants a source's name, which the slim list carries.
   So it is fetched the first time something actually lists outlets: the
   wizard's source picker or the Sources panel. Once fetched it is kept. */
let sourcesWanted = null;
function ensureSources() {
  if (!sourcesWanted) {
    sourcesWanted = API.sources()
      .then((rows) => { SOURCES = rows; Builder.init(META, SOURCES); })
      .catch((e) => { sourcesWanted = null; throw e; });   // let a retry try again
  }
  return sourcesWanted;
}

// Opening the wizard has to wait for the catalog, or its picker would show an
// empty list. Everything else in the modal is ready immediately.
async function openBuilder(mode, item = null) {
  try {
    await ensureSources();
  } catch (e) {
    // Everything except "only these sources" still works without the catalog.
    toast("Source list unavailable", `${e.message} — the rest of the ${mode} builder still works.`);
  }
  Builder.open(mode, item);
}

async function openSourcesPanel() {
  el("sources-panel").hidden = false;
  const box = el("sources-list");
  if (!SOURCES.length) {
    box.innerHTML = "";
    box.appendChild(feedEmpty("Loading the source catalog…"));
  }
  try {
    await ensureSources();
  } catch (e) {
    box.innerHTML = "";
    box.appendChild(feedEmpty(`Could not load the source catalog: ${e.message}`));
    return;
  }
  renderSourcesPanel();
}

// Used after something changes the catalog — a new source, a topic tracker, a
// deletion — so the panel shows the change rather than the copy it started with.
async function reloadSources() {
  sourcesWanted = null;
  await ensureSources();
  renderSourcesPanel();
}

const PLATFORM_ICONS = { news: "📰", reddit: "👽", mastodon: "🐘", bluesky: "🦋", youtube: "▶️" };

/* ---------- stats ---------- */
function renderStats(meta) {
  if (meta) META = meta;
  el("admin-setting").hidden = !META.is_admin;  // operator-only console, in Settings
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
  rememberList(LIST_FEEDS, FEEDS);
  await renderBoard();
}

/* Bring the columns already on screen up to date without rebuilding them. Used
   when the server confirms the board is the one already painted from disk: the
   layout is right, only the contents may have moved on. */
function refreshVisibleColumns() {
  return mapLimited(visibleFeeds(), BOARD_LOAD_CONCURRENCY, loadFeedArticles)
    .then(() => schedulePrefetch(BOARD_GENERATION))
    .catch((e) => console.error("[board]", e));
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
  // Every column can leave as a file — including Home's and a Pantheon's,
  // which nobody owns and which are the ones most worth taking away.
  tools.append(toolBtn("⤓", "Export this column — Excel, Word, CSV, Markdown or JSON",
                       (e) => exportMenu(e.currentTarget, feed)));
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
        toolBtn("✎", "Edit shared feed", () => openBuilder("feed", feed)),
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
    // Moving a column is dragging it now, not stepping it one place at a time
    // with ◀ ▶. Six columns and a move of four places was four clicks and four
    // round trips; the arrows also said nothing about where the column would
    // land. The header is the handle — see dragToReorder.
    dragToReorder(head, col, feed);
    tools.append(
      toolBtn("⇔", "Switch between the standard and wide width — or drag either edge",
              () => toggleWidth(feed, col)),
      toolBtn("✎", "Edit feed", () => openBuilder("feed", feed)),
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
  col.append(head, body, resizeGrip(col, feed));
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
    const names = c.source_ids.map(id => SOURCE_NAMES.get(id))
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

/* ---------- work done here rather than on the server ----------

   Which favourite locations an article is near, and whether its event is too
   stale to show, are both pure functions of data this browser already holds —
   the article's tagged places, the reader's own saved locations, and a
   threshold from their own settings. Computing them here removes two queries
   and a per-article geometry pass from every feed request, on a server that is
   shared by everyone, and does it on a machine that is sitting idle in front of
   one person. It also means grouped feeds get the 📍 badges, which they never
   did while this ran server-side. */

const EARTH_KM = 6371;

function haversineKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const p1 = lat1 * rad, p2 = lat2 * rad;
  const dp = (lat2 - lat1) * rad, dl = (lon2 - lon1) * rad;
  const a = Math.sin(dp / 2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_KM * Math.asin(Math.sqrt(a));
}

/* Favourite locations are always a point and a radius, so this is the circle
   case only — the wizard's drawn polygons stay server-side, where the SQL that
   selects the candidates lives. */
const withinLocation = (lat, lon, loc) =>
  haversineKm(lat, lon, loc.lat, loc.lon) <= loc.radius_km;

/* Mirrors places_match_geo: a tagged place inside the radius, or — for an
   article that names no place at all — its country's centre. */
function nearLocations(article) {
  if (!LOCATIONS.length) return [];
  const places = article.places || [];
  const hits = [];
  for (const loc of LOCATIONS) {
    let hit = places.some((p) => withinLocation(p.lat || 0, p.lon || 0, loc));
    if (!hit && !places.length && article.country) {
      const c = COUNTRY_POINTS.get(article.country);
      hit = !!c && withinLocation(c.lat, c.lon, loc);
    }
    if (hit) hits.push({ id: loc.id, name: loc.name, color: loc.color });
  }
  return hits;
}

/* Feeds that ticked 🕰 hide events with nothing new inside the reader's own
   threshold. Display-only, as it was on the server: clustering keeps attaching
   reports to a hidden event, which reappears the moment one arrives. */
/* The saved locations the badges are computed against. Loaded at boot and after
   any change, so a column painted from cache badges correctly without asking
   the server what is near what. */
/* Drop the cached page of the one feed that covers every favourite location:
   its areas move whenever a location is saved, moved, or deleted. */
function forgetLocationsFeed() {
  for (const loc of LOCATIONS) if (loc.feed_id) invalidateFeedCache({ id: loc.feed_id });
}

async function refreshLocations() {
  try {
    LOCATIONS = await API.locations();
    rememberList(LIST_LOCATIONS, LOCATIONS);
  } catch (e) { LOCATIONS = []; }
}

/* ---------- the cache on disk ----------
   Read the persisted columns into the in-memory maps at boot. Every read path
   stays synchronous — feedColumn paints during layout and cannot await — so the
   disk copy is only ever loaded here and written behind.

   The lists of feeds and Pantheons are cached alongside the columns. Without
   them a returning reader still waited on /api/feeds before a single column
   could be built, however much news was already on the disk — the board knew
   what every column contained but not which columns there were. */
const LIST_FEEDS = "list:feeds", LIST_PANTHEONS = "list:pantheons",
      LIST_LOCATIONS = "list:locations";

async function hydrateFeedCache() {
  let rows = [];
  try { rows = await Store.load(Session.userKey()); } catch (e) { return null; }
  const lists = {};
  for (const row of rows) {
    if (row.key === LIST_FEEDS) { lists.feeds = row.items; continue; }
    if (row.key === LIST_PANTHEONS) { lists.pantheons = row.items; continue; }
    if (row.key === LIST_LOCATIONS) { lists.locations = row.items; continue; }
    FEED_CACHE.set(row.key, row.items);
    // Treat a restored column as exactly as old as it was when it was written,
    // so the freshness rules decide what to re-query — a reload must not be an
    // excuse to re-query everything, nor to show yesterday's news as current.
    FEED_CACHE_AT.set(row.key, row.at || 0);
  }
  return lists;
}

const rememberList = (key, items) =>
  Store.put(Session.userKey(), key, items, Date.now()).catch(() => {});

function dropStale(feed, items) {
  if (!feed.criteria || !feed.criteria.hide_stale) return items;
  const hours = +Settings.get("stale_hours") || 0;
  if (!hours) return items;
  const cutoff = Date.now() - hours * 3600 * 1000;
  const fresh = (stamp) => !stamp || Date.parse(stamp) >= cutoff;
  return items.filter((it) => (it.articles      // an event group
    ? (it.event_id == null || fresh(it.updated_at))
    : (it.event_id == null || fresh(it.event_updated_at))));
}

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
  // A column with no width of its own takes a share of whatever a wide monitor
  // leaves over; one the reader dragged to a width keeps exactly that width.
  // Without this, sizing a column would last until the next wide-screen
  // reflow stretched it back.
  col.style.flexGrow = px ? "0" : "";
  col.style.maxWidth = px ? "none" : "";
}

/* The draggable right edge. Both edges used to resize the column, which meant
   the boundary between two columns carried two handles doing opposite things —
   grab the wrong one and the neighbour moved instead. A column is now dragged
   by its own right edge only, the way a table column is. Nothing is saved until
   the drag ends, so a resize is one settings write, not sixty. */
/* Drag a column by its header to move it along the board.

   This replaced ◀ ▶, which stepped one place per click: moving a column four
   places was four clicks and four saved orders, and neither arrow told you
   where the column would end up. Dragging shows it.

   Pointer events rather than HTML5 drag-and-drop, for two reasons that matter
   here: HTML5 drag has no useful touch story, and it insists on its own drag
   image, which for a column the height of the viewport is a full-size ghost
   that hides the board you are aiming at.

   Deliberate details:

   *A threshold before it counts as a drag.* The header holds buttons and the
   title; a click that wanders two pixels must stay a click. Nothing moves
   until the pointer has travelled far enough to have meant it.

   *Buttons are excluded.* Starting a drag on ⟳ or ✎ would make those controls
   feel unreliable, so a press that lands on one is left alone.

   *The order is saved once, at the end.* Reordering while dragging would send
   a request per column crossed and fight the render.

   *Position is committed to the DOM as you go*, so the gap sits where the
   column will land — the answer to what the arrows could never show.

   *Holding at either edge scrolls the board.* Past four or five columns the
   board is wider than the window, and without this the only reachable drop
   targets are the ones that happen to be on screen.

   *← and → still move a column one place.* The arrows were operable from the
   keyboard and a drag is not, so replacing them with a gesture alone would
   have taken the board away from anyone who does not use a pointer. The
   header is focusable and answers the arrow keys, which is what the buttons
   did — the drag is the addition, not the replacement. */
function dragToReorder(head, col, feed) {
  head.classList.add("feed-head-draggable");
  head.title = "Drag to move this column — or focus it and press ← or →";
  head.tabIndex = 0;
  head.setAttribute("role", "group");
  head.setAttribute("aria-label",
                    `“${feed.name}” column — drag to move it, or press ← and →`);

  // The board holds Home's columns and Pantheon boards too; only the reader's
  // own feeds have an order to save, so the ids are read back off the DOM and
  // filtered to those.
  const saveOrder = async (board) => {
    const order = [...board.children]
      .map((c) => Number(String(c.id).replace("feed-", "")))
      .filter((id) => Number.isFinite(id) && FEEDS.some((f) => f.id === id));
    if (!order.length) return false;
    try {
      await API.reorderFeeds(order);
      return true;
    } catch (e) {
      toast("Couldn't save the new order", e.message);
      BOARD_PAINT_HELD = null;        // the render below supersedes it
      await renderBoard();            // put it back where the server has it
      return false;
    }
  };

  head.addEventListener("keydown", async (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    if (e.target !== head) return;    // a control inside the header has focus
    const board = col.parentElement;
    if (!board || board.children.length < 2) return;
    const sibling = e.key === "ArrowLeft"
      ? col.previousElementSibling
      : col.nextElementSibling && col.nextElementSibling.nextElementSibling;
    if (e.key === "ArrowLeft" ? !sibling : !col.nextElementSibling) return;
    e.preventDefault();
    board.insertBefore(col, sibling);
    // No re-render afterwards: the board already shows the new order, and
    // rebuilding it would throw away the focus that is driving this.
    head.focus();
    await saveOrder(board);
  });

  head.addEventListener("pointerdown", (down) => {
    if (down.button !== 0) return;
    // Leave the controls and the resize grip alone.
    if (down.target.closest("button, a, input, select, .col-grip")) return;
    const board = col.parentElement;
    if (!board || board.children.length < 2) return;

    const startX = down.clientX;
    let dragging = false;
    let pointerX = down.clientX;
    const started = () => {
      dragging = true;
      col.classList.add("col-dragging");
      document.body.classList.add("board-reordering");
    };

    // The board scrolls sideways once the columns outrun the window, so the
    // place you want to drop a column is often not on screen when you pick it
    // up. Holding near either edge scrolls the board under the pointer, which
    // is the only way to reach it without putting the column down first.
    let scrolling = 0;
    const edgeScroll = () => {
      scrolling = 0;
      if (!dragging) return;
      const r = board.getBoundingClientRect();
      const EDGE = 60, SPEED = 18;
      let dx = 0;
      if (pointerX < r.left + EDGE) dx = -SPEED;
      else if (pointerX > r.right - EDGE) dx = SPEED;
      if (dx) {
        const was = board.scrollLeft;
        board.scrollLeft += dx;
        // Re-place the column for the columns that just slid past, or it stays
        // put while the board moves and lands nowhere near the pointer.
        if (board.scrollLeft !== was) place();
        scrolling = requestAnimationFrame(edgeScroll);
      }
    };

    // Which column is under the pointer, and which side of it.
    const place = () => {
      if (col.parentElement !== board) return;   // repainted out from under us
      for (const other of board.children) {
        if (other === col) continue;
        const r = other.getBoundingClientRect();
        if (pointerX < r.left || pointerX > r.right) continue;
        const before = pointerX < r.left + r.width / 2;
        board.insertBefore(col, before ? other : other.nextSibling);
        break;
      }
    };

    const move = (e) => {
      pointerX = e.clientX;
      if (!dragging) {
        if (Math.abs(e.clientX - startX) < 6) return;   // still a click
        started();
      }
      place();
      if (!scrolling) edgeScroll();
    };

    const up = async () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      if (scrolling) cancelAnimationFrame(scrolling);
      scrolling = 0;
      if (!dragging) return;
      col.classList.remove("col-dragging");
      document.body.classList.remove("board-reordering");
      if (await saveOrder(board)) {
        BOARD_PAINT_HELD = null;    // refreshFeeds renders in its place
        await refreshFeeds();
      } else {
        releaseBoardPaint();        // nothing saved: put up the paint we held
      }
    };

    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    // Without this a cancelled gesture would leave the board held: paints are
    // suppressed while a drag is in flight, and the drag would never end.
    document.addEventListener("pointercancel", up);
  });
}


function resizeGrip(col, feed) {
  const grip = document.createElement("div");
  grip.className = "col-grip";
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
    // The moment a width is being chosen by hand, the column stops sharing in
    // the board's leftover space — otherwise the grip would appear stuck as
    // soon as the drag passed the grow cap, and let go somewhere else entirely.
    col.style.flexGrow = "0";
    col.style.maxWidth = "none";
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
    preview(startW + dx);
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
      const px = width() + dir * step;
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

function renderFeedItems(body, feed, allItems) {
  body.innerHTML = "";
  // Remember which column a story was opened from, so the view can say which
  // of that column's words are in it. Capture phase, so this runs before the
  // row's own handler opens the story rather than after it.
  if (!body._whyWired) {
    body._whyWired = true;
    body.addEventListener("click", () => { STORY_FROM_FEED = body._feed || null; },
                          true);
  }
  body._feed = feed && feed.id ? feed : null;
  // Staleness is applied here, not on the server: the threshold is this
  // reader's setting, so changing it re-filters what is already on the page
  // instead of re-querying every column.
  const items = dropStale(feed, allItems);
  if (!items.length && allItems.length) {
    // Everything it matched was filtered out here, which is a different problem
    // from matching nothing — say which.
    body.replaceChildren(feedEmpty(
      `All ${allItems.length} matching event${plural(allItems.length)} have had no `
      + "update recently, and this feed hides those (🕰). Raise or clear "
      + "“Hide events with no updates for” in ⚙ Settings to see them."));
    return;
  }
  if (!items.length) {
    body.innerHTML = '<div class="feed-empty">No matching articles yet. ' +
      "Open the feed (✎) and use <b>Preview matches</b> while removing one filter " +
      "at a time to see which criterion is limiting it. If the feed has a query or " +
      "keywords, make sure <b>🔍 Automatically ingest worldwide coverage</b> is " +
      "checked and re-save — D.E.L.P.H.I. only searches articles its sources publish, " +
      "and that option adds a source pulling in press coverage of your query.</div>";
    return;
  }
  paintRows(body, feed, items);
}

/* How many rows go in before the browser is allowed to breathe, and how many
   follow per chunk. A column shows roughly this many at once, so the first
   batch is everything the reader can actually see. */
const FIRST_ROWS = 12, ROW_CHUNK = 12;

/* Fill a column without blocking the page.

   Thirteen columns of forty rows is 520 rows, and building them in one go was
   measured at ~380ms of frozen UI on a mid-range laptop — clicks ignored,
   scrolling stuck. The work is unavoidable, but doing it in one task is not:
   the rows the reader can see go in immediately and the rest follow in chunks,
   so no single task is long enough to be felt.

   The obvious alternative, content-visibility:auto, is 3.8x better still and
   was rejected: Chrome drops skipped rows from the accessibility tree, so a
   screen reader would have found a fraction of each column. Chunking costs
   more and keeps every row real.

   Each column carries a token; starting a new render invalidates the chunks
   still queued for the old one, so a fast switch can't interleave two feeds
   into one column. */
let PAINT_TOKEN = 0;

function paintRows(body, feed, items) {
  const token = ++PAINT_TOKEN;
  body.dataset.paint = String(token);
  const build = (item) => (feed.group_events ? eventGroup(item) : articleRow(item, "focus"));

  // A fragment for each batch: one mutation instead of one per row.
  const flush = (from, to) => {
    const frag = document.createDocumentFragment();
    for (let i = from; i < to; i++) frag.appendChild(build(items[i]));
    body.appendChild(frag);
  };

  flush(0, Math.min(FIRST_ROWS, items.length));
  if (items.length <= FIRST_ROWS) return;

  let next = FIRST_ROWS;
  const step = () => {
    // Superseded, or the column was replaced entirely: stop.
    if (body.dataset.paint !== String(token) || !body.isConnected) return;
    flush(next, Math.min(next + ROW_CHUNK, items.length));
    next += ROW_CHUNK;
    if (next < items.length) schedule(step);
  };
  schedule(step);
}

/* Run after the browser has dealt with anything more urgent. requestIdleCallback
   is exactly this; Safari doesn't have it, so a timeout stands in. */
const schedule = (fn) => (window.requestIdleCallback
  ? requestIdleCallback(fn, { timeout: 250 })
  : setTimeout(fn, 16));

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
  const at = Date.now();
  FEED_CACHE.set(key, items);
  FEED_CACHE_AT.set(key, at);
  // Behind the render, and never awaited: a full disk or a private window must
  // cost nothing but the loss of the cache.
  Store.put(Session.userKey(), key, items, at).catch(() => {});
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
  meta.textContent = `🧵 ${g.total_count} report${plural(g.total_count)} · ${srcs} — open ⤢`;
  wrap.appendChild(meta);
  // The card's own lead article opens the view at once; the payload it fetches
  // carries the whole event anyway, so this is the by-event path without the wait.
  wrap.onclick = () => openStory(g.articles[0]);
  wrap.onpointerenter = () => prefetchStory(g.articles[0] && g.articles[0].id);
  return wrap;
}

/* The map inside the story view. */
let STORY_MAP = null;
let STORY_MAP_SEQ = 0;   // which story the map is being drawn for

/* A row can be taken out from under a press. Columns refresh on a timer and the
   alerts panel rebuilds every time an alert fires; if that lands between the
   press and the release, the row the reader aimed at is no longer in the
   document — and Chrome then fires no click at all, so nothing acts on it. The
   press is remembered and honoured on release instead.

   Only when the row really was removed: a press deliberately released somewhere
   else leaves the row in place, its own click still fires, and this stays out of
   the way. A drag or a scroll moves too far to count. */
let PRESSED_ROW = null;
const PRESS_SLOP_PX = 12;

/* Every control acknowledges the press itself, whatever it goes on to do.
   A handler that starts work synchronously can stop the browser painting
   :active at all, and on a touch screen that state is gone before the finger
   lifts — so the class is added on press and removed on a timer, which always
   survives a frame. Delegated, so anything rendered later is covered too. */
const PRESS_FLASH_MS = 200;

function wirePressFeedback() {
  const PRESSABLE = "button, .chip, .loc-result, .article-focus, .event-clickable,"
    + " .src-pick-row, .ev-related-row, .ar-cov-row";
  document.addEventListener("pointerdown", (e) => {
    const target = e.target.closest && e.target.closest(PRESSABLE);
    if (!target || target.disabled) return;
    target.classList.add("pressed");
    setTimeout(() => target.classList.remove("pressed"), PRESS_FLASH_MS);
  }, true);
}

function wireRowPressRecovery() {
  document.addEventListener("pointerdown", (e) => {
    const row = e.target.closest && e.target.closest(".article-focus");
    PRESSED_ROW = row && row.dataset.articleId
      ? { id: +row.dataset.articleId, node: row, x: e.clientX, y: e.clientY }
      : null;
  }, true);
  document.addEventListener("pointercancel", () => { PRESSED_ROW = null; }, true);
  document.addEventListener("pointerup", (e) => {
    const pressed = PRESSED_ROW;
    PRESSED_ROW = null;
    if (!pressed || pressed.node.isConnected) return;   // its own click will run
    if (Math.abs(e.clientX - pressed.x) > PRESS_SLOP_PX
        || Math.abs(e.clientY - pressed.y) > PRESS_SLOP_PX) return;   // a drag
    openStory(pressed.id);
  }, true);
}

/* ---------- story focus ----------
   What a headline opens, and the only place a story is read from. A single
   report and a story forty outlets are carrying are the same thing at
   different distances, so this is one view rather than two: the report the
   reader picked, and — when others have it — the whole event around it.

   A column card is a teaser: a truncated summary, a few tags. Here the
   publisher's summary is whole, the body has an extract, and the outlet's own
   page is a marked button, never an accident. */
let STORY_SEQ = 0;
// What the Focus view is currently showing, so Export knows its subject.
let STORY_ON_SCREEN = null;    // which story the view is being drawn for
// Which of the reader's own columns the open story was pressed in. Only a feed
// has a set of words to answer "why is this here" with; a story opened from
// Home, from search or from an alert has nothing to explain.
let STORY_FROM_FEED = null;

/* Say which of a column's words are in this story, and where they are.

   The question this answers came from a real report: a WNBA story in a feed
   whose query was six phrases about data centres, none of them anywhere the
   reader could see. They were in the page's own furniture — a promo rail — and
   stored as part of the article, which nothing on screen showed. A reader
   should not have to guess between "the search is broken" and "the words are
   somewhere I am not looking"; those are fixed in different places. */
async function showWhyItMatched(articleId) {
  const box = el("st-why");
  if (!box) return;
  box.hidden = true;
  box.textContent = "";
  const feed = STORY_FROM_FEED;
  if (!feed || !articleId) return;
  let why;
  try {
    why = await API.whyItMatched(feed.id, articleId);
  } catch (e) {
    return;                       // an explanation is never worth an error
  }
  // The view may have moved on while this was in the air.
  if (!STORY_ON_SCREEN || STORY_ON_SCREEN.id !== articleId) return;
  if (!why.hits || !why.hits.length) return;

  const WHERE = { headline: "the headline", summary: "the summary",
                  body: "the article body" };
  const first = why.hits[0];
  const more = why.hits.length - 1;
  box.textContent =
    `In “${feed.name}”: matched “${first.term}” in ${WHERE[first.where] || first.where}`
    + (more > 0 ? ` (and ${more} other term${plural(more)})` : "")
    + (why.body_only
       ? " — nothing in the headline or summary put it here, so this may be the "
         + "page's own menu or a promo rather than the story."
       : ".");
  box.title = first.snippet || "";
  box.className = "ar-why" + (why.body_only ? " ar-why-odd" : "");
  box.hidden = false;
}

/* A story already fetched, kept briefly so reopening one costs nothing. The
   server answers in about 18ms on a large database, but the round trip does
   not, and this view is opened constantly while triaging a board. */
const STORY_CACHE = new Map();      // article id -> {data, at}
const STORY_WANTED = new Map();     // article id -> in-flight promise
const STORY_FRESH_MS = 120000;
const STORY_CACHE_MAX = 60;

function cachedStory(id) {
  const hit = STORY_CACHE.get(id);
  if (!hit) return null;
  if (Date.now() - hit.at > STORY_FRESH_MS) { STORY_CACHE.delete(id); return null; }
  return hit.data;
}

function fetchStory(id) {
  const already = STORY_WANTED.get(id);
  if (already) return already;                 // one request per story, not one per hover
  const wanted = API.story(id).then((data) => {
    STORY_CACHE.set(id, { data, at: Date.now() });
    if (STORY_CACHE.size > STORY_CACHE_MAX) {  // bounded: drop the oldest entry
      STORY_CACHE.delete(STORY_CACHE.keys().next().value);
    }
    return data;
  }).finally(() => STORY_WANTED.delete(id));
  STORY_WANTED.set(id, wanted);
  return wanted;
}

/* Warm a story before it is asked for. A reader's pointer lands on a headline
   well before the click does, and moving the keyboard onto a row is the same
   signal, so by the time either turns into an open the answer is usually here.
   Costs one small request for a row the reader is already looking at. */
let storyPrefetchTimer = null;
function prefetchStory(id) {
  if (!id || cachedStory(id) || STORY_WANTED.has(id)) return;
  clearTimeout(storyPrefetchTimer);
  storyPrefetchTimer = setTimeout(() => fetchStory(id).catch(() => {}), 70);
}

/* Open at a report. `article` may be the row's own article object — the view is
   then painted from it immediately and filled in when the rest arrives — or
   just an id, which costs a round trip before anything appears. */
async function openStory(article) {
  const known = article && typeof article === "object" ? article : null;
  const id = known ? known.id : article;
  const wanted = ++STORY_SEQ;

  const cached = cachedStory(id);
  if (cached) { paintStory(cached); return; }     // nothing to wait for

  if (known) {
    // Everything the row already holds: headline, summary, outlet, time, image,
    // badges. The story around it follows; the reader is reading by then.
    paintStory({ article: known, event: null, articles: [], sources: [], related: [] },
               { partial: true });
  }
  let d;
  try {
    d = await fetchStory(id);
  } catch (e) {
    if (wanted !== STORY_SEQ) return;
    if (known) return;                            // the report is on screen already
    toast("Could not open the story", e.message);
    return;
  }
  if (wanted !== STORY_SEQ) return;
  paintStory(d);
}

// From a related story, where the reader has an event rather than a report.
// The view opens at the latest report of it.
async function openStoryByEvent(eventId) {
  const wanted = ++STORY_SEQ;
  // Only the event is known here; paintStory marks the article too once the
  // payload names it.
  markStoryRead({ event_id: eventId });
  let d;
  try {
    d = await API.storyByEvent(eventId);
  } catch (e) {
    toast("Could not open the story", e.message);
    return;
  }
  if (wanted !== STORY_SEQ) return;
  STORY_CACHE.set(d.article.id, { data: d, at: Date.now() });
  paintStory(d);
}

/* Remember that a story has been read — on screen, in the caches, and on the
   server.

   Two things used to go wrong here. Only the node that was clicked was dimmed,
   so the same story sitting in three other columns stayed bright; and nothing
   told the cached copies, so any repaint that came from cache — a reload, a
   switch between boards, the prefetch filling a column — brought the row back
   undimmed. The dimming was real, it just kept being forgotten.

   An article that belongs to no event is remembered by its own id. That is not
   a hypothetical: a clustering pass that fails leaves a batch of articles with
   no event, and those could never be marked read at all. */
function markStoryRead(article) {
  if (!article) return;
  const eventId = article.event_id || null;
  const articleId = article.id;

  const selector = eventId
    ? `[data-event-id="${eventId}"]`
    : `[data-article-id="${articleId}"]`;
  for (const node of document.querySelectorAll(selector))
    node.classList.add("event-viewed");

  rememberRead(eventId, articleId);

  const request = eventId ? API.markEventViewed(eventId)
                          : API.markArticleViewed(articleId);
  // Losing this write costs the dimming after the next refresh, nothing more,
  // so it stays quiet rather than interrupting the story that just opened.
  request.catch(() => {});
}

/* Set `viewed` on every cached copy, and write the changed columns back to
   disk so a reload agrees with what the reader just did. */
function rememberRead(eventId, articleId) {
  const hit = (a) => (eventId ? a.event_id === eventId : a.id === articleId);
  for (const [key, items] of FEED_CACHE) {
    let touched = false;
    for (const item of items) {
      // A column holds either articles or event groups; groups carry theirs.
      if (item.articles) {
        if (eventId ? item.event_id === eventId : item.articles.some(hit)) {
          item.viewed = true;
          touched = true;
        }
        for (const a of item.articles) if (hit(a) && !a.viewed) { a.viewed = true; touched = true; }
      } else if (hit(item) && !item.viewed) {
        item.viewed = true;
        touched = true;
      }
    }
    if (touched)
      Store.put(Session.userKey(), key, items, FEED_CACHE_AT.get(key) || Date.now())
        .catch(() => {});
  }
}

/* Draw the view. `partial` means only the report is known so far: the sections
   that describe the wider story stay hidden rather than flashing up empty. */
function paintStory(d, { partial = false } = {}) {
  const a = d.article;
  const ev = d.event;
  markStoryRead(a);
  // Remembered so the Export button knows what is on screen. It stays usable
  // while the story is still filling in: an unclustered report exports as
  // itself, so there is nothing to wait for.
  STORY_ON_SCREEN = { id: a.id, title: a.title,
                      reports: (d.articles || []).length || 1 };

  el("st-title").textContent = a.title;

  const badges = el("st-badges");
  badges.innerHTML = "";
  const t = impTier(a.importance);
  const imp = document.createElement("span");
  imp.className = "imp " + t.cls;
  imp.textContent = `${t.icon} ${t.label} ${a.importance}`;
  badges.appendChild(imp);
  const tag = (txt, title = "") => {
    const s = document.createElement("span");
    s.className = "tag"; s.textContent = txt;
    if (title) s.title = title;
    badges.appendChild(s);
  };
  if (ev) {
    tag(`🧵 ${ev.article_count} report${plural(ev.article_count)} · `
      + `${ev.source_count} outlet${plural(ev.source_count)}`);
  }
  if (a.country) tag(`${flagEmoji(a.country)} ${COUNTRY_NAMES.get(a.country) || a.country}`);
  for (const c of (a.categories || []).slice(0, 4)) tag(c);
  for (const n of nearLocations(a)) tag(`📍 ${n.name}`, `Inside your favourite location “${n.name}”`);
  if (a.paywall) tag("🔒 paywalled", "The outlet limits how much of this you can read");
  if (a.translated_from) tag(`🌐 translated from ${a.translated_from.toUpperCase()}`);
  if (ev) {
    tag("first seen " + timeAgo(ev.first_seen));
    if (ev.updated_at !== ev.first_seen) tag("updated " + timeAgo(ev.updated_at));
  }

  // Who published it, and when — the two things a card can only abbreviate.
  const outlet = a.source ? a.source.name : "Unknown source";
  const when = a.published_at
    ? `${new Date(a.published_at).toLocaleString([], {
        weekday: "short", year: "numeric", month: "long", day: "numeric",
        hour: "2-digit", minute: "2-digit" })} · ${timeAgo(a.published_at)}`
    : "publication date not given";
  el("st-byline").textContent = `${outlet} — ${when}`;

  const img = el("st-image");
  if (safeUrl(a.image_url)) {
    img.src = safeUrl(a.image_url);
    img.hidden = false;
    img.onerror = () => { img.hidden = true; };
  } else {
    img.hidden = true;
    img.removeAttribute("src");
  }

  el("st-summary").textContent = a.summary || "";
  el("st-summary").hidden = !a.summary;

  showWhyItMatched(a.id);

  // The body extract, when Delphi fetched one. Some outlets publish only a
  // headline and a link, and paywalled ones are stored headline-only by design.
  const excerpt = (a.excerpt || "").trim();
  el("st-excerpt-section").hidden = !excerpt;   // absent until the full story lands
  el("st-excerpt").textContent = excerpt;
  el("st-excerpt-note").hidden = !a.excerpt_truncated;

  // The map covers the whole story when several outlets have it, and just this
  // report when nobody else does.
  renderStoryMap(d.articles.length ? d.articles : [a]);

  // Every report on the story, this one included and marked as the one being
  // read. A story nobody else has yet simply has no timeline.
  const timeline = d.articles || [];
  el("st-timeline-section").hidden = partial || timeline.length < 2;
  el("st-count").textContent = timeline.length
    ? `— ${timeline.length} report${plural(timeline.length)}, newest first` : "";
  const tl = el("st-timeline");
  tl.innerHTML = "";
  for (const other of timeline) {
    const row = articleRow(other, other.id === a.id ? "current" : "focus");
    tl.appendChild(row);
  }

  // Each outlet chip goes to that outlet's report of the story.
  const sourcesBox = el("st-sources");
  sourcesBox.innerHTML = "";
  el("st-sources-section").hidden = !(d.sources || []).length;
  const articleBySource = new Map();
  for (const art of timeline)
    if (art.source && !articleBySource.has(art.source.id)) articleBySource.set(art.source.id, art);
  for (const s of d.sources || []) {
    const chip = document.createElement("a");
    chip.className = "chip chip-link";
    chip.textContent = `${PLATFORM_ICONS[s.platform] || "📰"} ${flagEmoji(s.country)} ${s.name} ↗`;
    const art = articleBySource.get(s.id);
    if (art && safeUrl(art.url)) {
      chip.href = safeUrl(art.url); chip.target = "_blank"; chip.rel = "noopener";
      chip.title = art.title;
    }
    sourcesBox.appendChild(chip);
    if (art && safeUrl(art.archive_url)) {
      const arch = document.createElement("a");
      arch.className = "chip chip-link archive-chip";
      arch.textContent = "🔓 archive.ph";
      arch.href = safeUrl(art.archive_url); arch.target = "_blank"; arch.rel = "noopener";
      arch.title = "Read the full article via archive.ph (paywalled source)";
      sourcesBox.appendChild(arch);
    }
  }

  const rel = el("st-related");
  rel.innerHTML = "";
  el("st-related-section").hidden = !(d.related || []).length;
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
    row.onclick = () => openStoryByEvent(r.id);
    rel.appendChild(row);
  }

  // Everything else worth knowing, in one list rather than scattered tags.
  const facts = el("st-facts");
  facts.innerHTML = "";
  const fact = (label, value) => {
    if (!value) return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    facts.append(dt, dd);
  };
  fact("Published", a.published_at
    ? new Date(a.published_at).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "");
  fact("Collected", a.fetched_at ? timeAgo(a.fetched_at) : "");
  if (a.source) {
    fact("Outlet", a.source.name);
    fact("Reach", a.source.scope);
    fact("Platform", a.source.platform);
  }
  fact("Language", (a.language || "").toUpperCase());
  fact("Places named", (a.places || []).map(p => p.name).join(", "));
  fact("Importance", `${a.importance} of 100 — ${t.label}`);
  if (ev) {
    fact("Story", `${ev.article_count} report${plural(ev.article_count)} from `
      + `${ev.source_count} outlet${plural(ev.source_count)}, first seen ${timeAgo(ev.first_seen)}`);
  }

  const open = el("st-open");
  const href = safeUrl(a.url);
  open.href = href || "#";
  open.hidden = !href;
  open.title = href ? `Read this at ${outlet}` : "";
  const arch = el("st-archive");
  arch.hidden = !safeUrl(a.archive_url);
  if (!arch.hidden) {
    arch.href = safeUrl(a.archive_url);
    arch.title = "Read the full text via archive.ph (the outlet paywalls it)";
  }

  const opening = el("story-backdrop").hidden;
  el("story-backdrop").hidden = false;
  // Only when the view first appears, or moves to another report: filling in
  // the rest of a story must not throw the reader back to the top of it.
  if (opening || !partial) {
    if (opening || STORY_PAINTED !== a.id) el("st-body").scrollTop = 0;
  }
  STORY_PAINTED = a.id;
  if (opening) el("btn-close-story").focus();
}

let STORY_PAINTED = null;   // which report the view currently shows

function closeStory() {
  STORY_SEQ++;                      // an answer still in flight is now stale
  STORY_PAINTED = null;
  STORY_ON_SCREEN = null;           // nothing to export once it is closed
  STORY_FROM_FEED = null;           // and nothing left to explain
  el("story-backdrop").hidden = true;
}

async function renderStoryMap(articles) {
  const box = el("story-map");
  const points = [];
  const seen = new Set();
  for (const a of articles)
    for (const p of a.places || [])
      if (!seen.has(p.name)) { seen.add(p.name); points.push({ p, imp: a.importance }); }
  if (!points.length) {
    box.hidden = true;
    if (STORY_MAP) { STORY_MAP.remove(); STORY_MAP = null; }
    return;
  }
  // The map library may still be downloading; if the reader has moved to
  // another story by the time it arrives, those are the points that belong here.
  const wanted = ++STORY_MAP_SEQ;
  try { await ensureLeaflet(); } catch (e) { box.hidden = true; return; }
  if (wanted !== STORY_MAP_SEQ) return;
  box.hidden = false;
  if (STORY_MAP) { STORY_MAP.remove(); STORY_MAP = null; }
  STORY_MAP = L.map("story-map", { worldCopyJump: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(STORY_MAP);
  const grp = L.featureGroup().addTo(STORY_MAP);
  for (const { p, imp } of points) {
    const pt = impTier(imp);
    grp.addLayer(L.circleMarker([p.lat, p.lon], {
      radius: 7, color: pt.color, fillColor: pt.color, fillOpacity: 0.8, weight: 1.5,
    }).bindPopup(p.name));
  }
  setTimeout(() => {
    STORY_MAP.invalidateSize();
    STORY_MAP.fitBounds(grp.getBounds().pad(0.5), { maxZoom: 6 });
  }, 80);
}

function articleRow(a, mode = "focus") {
  // "focus":   selecting the row opens the story
  // "plain":   inert row inside an already-clickable card
  // "current": the report being read, inside the story view's own timeline
  //
  // Nothing here navigates. A headline used to be an <a> that took the reader
  // straight to the outlet, which meant one stray click left Delphi for a page
  // they had not decided to visit yet. Every row now opens the story in the
  // dashboard, and leaving is a deliberate second click from there.
  const t = impTier(a.importance);
  const row = document.createElement("div");
  row.className = "article";
  if (mode === "current") {
    row.classList.add("article-current");
    row.setAttribute("aria-current", "true");
  }
  if (mode === "focus") {
    row.classList.add("article-focus");
    if (a.event_id) row.dataset.eventId = a.event_id;
    if (a.viewed) row.classList.add("event-viewed");
    row.setAttribute("role", "button");
    row.tabIndex = 0;    // it is a button now, so it has to be reachable as one
    row.title = "Open the story: summary, when and where it was published, who else has it";
    row.dataset.articleId = a.id;      // so a press can be honoured after a rebuild
    // The row's own article opens the view immediately; the rest of the story
    // fills in behind it. Pointing at a row is enough to start fetching.
    row.onclick = () => openStory(a);
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openStory(a); }
    };
    row.onpointerenter = () => prefetchStory(a.id);
    row.onfocus = () => prefetchStory(a.id);
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
  // Favourite-location hits are marked wherever the article shows up. Worked
  // out here rather than sent by the server: the browser has the places and the
  // locations, so it needs nobody's help — and a cached article picks up a
  // location added since it was fetched, without refetching anything.
  for (const n of nearLocations(a)) {
    const pin = document.createElement("span");
    pin.className = "near-pin";
    pin.textContent = `📍 ${n.name}`;
    pin.title = `Inside your favourite location “${n.name}”`;
    meta.appendChild(pin);
  }
  if (a.translated_from) bits.push("🌐 translated from " + a.translated_from.toUpperCase());
  if ((a.categories || []).length) bits.push(a.categories.slice(0, 3).join(" · "));
  if (mode === "focus") bits.push("⤢ open");
  if (mode === "current") bits.push("— you are reading this");
  const span = document.createElement("span");
  span.textContent = bits.join("  ·  ");
  meta.appendChild(span);
  // Paywalled outlets are marked, but the way out to archive.ph lives in the
  // focused view rather than the row: nothing in a column navigates.
  if (a.paywall) {
    const lock = document.createElement("span");
    lock.className = "paywall-mark";
    lock.textContent = "🔒";
    lock.title = "The outlet paywalls this — open the story for a way to read it";
    meta.appendChild(lock);
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
    rememberList(LIST_PANTHEONS, PANTHEONS);
  } catch (e) { PANTHEONS = []; PANTHEON_INVITES = []; }
  renderViewSwitch();
  renderPantheonBadge();
  if (!el("pantheons-panel").hidden) renderPantheonsPanel();
}

function renderPantheonBadge() {
  const badge = el("pantheon-invite-count");
  badge.hidden = PANTHEON_INVITES.length === 0;
  badge.textContent = PANTHEON_INVITES.length;
}

function wirePantheons() {
  el("btn-pantheons").onclick = async () => {
    el("pantheons-panel").hidden = false;
    await refreshPantheons();
    renderPantheonsPanel();
  };
  feedback(el("btn-pantheons"));
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
    el("settings-panel").hidden = true;   // it opened from there; don't stack panels
    el("admin-panel").hidden = false;
    await renderAdminUsers();
    renderServiceHealth();
  };
  feedback(el("btn-admin"));
  el("btn-close-admin").onclick = () => { el("admin-panel").hidden = true; };
  el("btn-reclaim-space").onclick = async () => {
    if (!confirm("Rewrite the database to reclaim disk space?\n\nDelphi will be "
                 + "unresponsive while this runs — minutes on a large archive. "
                 + "It only needs doing once.")) return;
    const r = await API.reclaimSpace();
    toast(r.converted ? "Disk space reclaimed" : "Nothing to do",
          r.converted
            ? `Freed ${(r.freed_bytes / 1e6).toFixed(0)} MB. From now on, clearing `
              + "out old news returns its space to the disk as it happens."
            : r.reason, true);
    renderServiceHealth();
  };
  feedback(el("btn-reclaim-space"), "Rewriting…");
  let t;
  el("admin-search").addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => renderAdminUsers(el("admin-search").value.trim()), 250);
  });
}

/* The state of the services that fail quietly.

   Mail is sent in a background task and geocoding is a best-effort call to an
   outside host, so both can be broken for weeks with the only evidence in a log
   nobody reads — while readers wait for reset links that never arrive. An
   operator opening the console sees it here instead. */
async function renderServiceHealth() {
  const box = el("admin-health");
  if (!box) return;
  box.textContent = "Checking services…";
  let s;
  try { s = await api("/api/ingest/status"); }
  catch (e) { box.textContent = "Couldn't read service status: " + e.message; return; }

  const lines = [];
  const mail = s.mail || {};
  if (!mail.configured) {
    lines.push("✉️ Mail is not configured (NEWS_SMTP_HOST is unset). Verification "
               + "and password-reset links can't be sent, and accounts are "
               + "auto-verified instead.");
  } else if (mail.last_error) {
    lines.push(`✉️ Mail is failing via ${mail.host} — ${mail.last_error} `
               + `(${mail.failures} failure${plural(mail.failures)}, last at `
               + `${timeAgo(mail.last_error_at)}).`);
  } else {
    lines.push(`✉️ Mail via ${mail.host} — ${mail.sent} sent, no failures.`);
  }

  // Disk first, because it is the one that stops the whole system rather than
  // degrading part of it — and the one whose only previous symptom was the app
  // refusing to start, with nothing left running to explain why.
  const st = s.storage || {};
  if (st.ok) {
    const mb = (n) => `${(n / 1e6).toFixed(0)} MB`;
    const head = `💾 Disk: ${mb(st.db_bytes)} of news in ${mb(st.total_bytes)} `
      + `(${st.free_pct}% free).`;
    if (st.low) {
      lines.push(`${head} ⚠ Ingestion is PAUSED — too little room left to keep `
                 + `the database openable. Old articles are being cleared; if this `
                 + `doesn't recover, the volume needs to be bigger.`);
    } else if (st.over_ceiling_bytes > 0) {
      lines.push(`${head} Over its ${mb(st.ceiling_bytes)} ceiling by `
                 + `${mb(st.over_ceiling_bytes)}, so the oldest articles are being `
                 + `dropped to fit.`);
    } else if (st.free_pct < 25) {
      lines.push(`${head} Getting full — retention will start dropping the oldest `
                 + `articles before it becomes a problem.`);
    } else {
      lines.push(head);
    }
    if (!st.reclaimable) {
      lines.push("💾 Deleting old articles frees space inside the database file "
                 + "but doesn't hand it back to the disk, because this database "
                 + "predates Delphi setting that up at creation. Converting it is "
                 + "a one-off — press ♻ below. It rewrites the whole file and the "
                 + "site is unresponsive while that runs (minutes, on a large "
                 + "archive), so pick a quiet moment. It also needs free space "
                 + `roughly equal to the database (${mb(st.db_bytes)}).`);
    }
  } else if (st.detail) {
    lines.push(`💾 Disk: ${st.detail}`);
  }

  const geo = s.geocoder || {};
  if (geo.provider === "off") {
    lines.push("📍 Address lookup is off; only Delphi's own list of cities and "
               + "countries answers place searches.");
  } else if (geo.last_error) {
    lines.push(`📍 Address lookup via ${geo.provider} is failing — ${geo.last_error} `
               + `(${geo.failures} failure${plural(geo.failures)} of ${geo.lookups}).`);
  } else {
    lines.push(`📍 Address lookup via ${geo.provider} — ${geo.lookups} lookup`
               + `${plural(geo.lookups)}, no failures.`);
  }

  if (s.last_error)
    lines.push(`📡 Last ingest error: ${s.last_error}`);
  if (s.sources_total) {
    const unchanged = s.last_unchanged
      ? `, ${s.last_unchanged} unchanged since last time` : "";
    lines.push(`📡 Last poll: ${s.sources_ok}/${s.sources_total} sources answered`
               + `${unchanged}, ${s.last_new_articles} new `
               + `article${plural(s.last_new_articles)}.`);
  }

  // What the catalog is actually made of. A thousand outlets reads like
  // breadth until you ask how many of them have ever carried anything.
  const src = s.sources || {};
  if (src.total) {
    let line = `📚 Catalog: ${src.total} source${plural(src.total)}, `
      + `${src.enabled} enabled. ${src.producing} have carried at least one `
      + `story; ${src.silent} have been polled and never carried anything.`;
    if (src.never_polled)
      line += ` ${src.never_polled} are new and haven't been asked yet.`;
    if (src.retired)
      line += ` ${src.retired} retired after ${src.remove_after} failed polls in `
        + `a row — disabled, but their stories are kept.`;
    if (src.removed_since_start)
      line += ` ${src.removed_since_start} that had never carried anything were `
        + `removed outright since this server started.`;
    line += " Search the Sources panel for “silent” to see the quiet ones.";
    lines.push(line);
  }

  // How far Delphi's reach falls short, in the two ways it can.
  const d = s.discovery || {};
  if (d.domains_probed) {
    let line = `🔎 Reach: ${d.sources_found} outlet${plural(d.sources_found)} found `
      + `their own feed. Of ${d.domains_probed} publisher`
      + `${plural(d.domains_probed)} met, ${d.without_a_feed} publish no feed at `
      + `all — Delphi only ever sees those second-hand.`;
    if (d.feedless_examples && d.feedless_examples.length)
      line += ` Most recent: ${d.feedless_examples.slice(0, 6).join(", ")}.`;
    lines.push(line);
  }

  const c = s.content || {};
  if (c.waiting_for_a_body !== undefined) {
    const rounds = c.per_cycle ? Math.ceil(c.waiting_for_a_body / c.per_cycle) : 0;
    lines.push(`📄 Article text: ${c.waiting_for_a_body} article`
      + `${plural(c.waiting_for_a_body)} from the last ${c.window_hours}h are `
      + `waiting for their full text (${c.never_tried} never tried, `
      + `${c.tried_and_failed} tried and failed). At ${c.per_cycle} a cycle that `
      + `is ${rounds} quiet cycle${plural(rounds)} to clear. Until then those `
      + `match on their headline alone.`);
  }

  // A backlog worth more than a few cycles is the signal that fetching article
  // text, not finding articles, is what is holding coverage back.
  const backlogged = (c.per_cycle && c.waiting_for_a_body > c.per_cycle * 3);
  box.replaceChildren(...lines.map((text) => {
    const p = document.createElement("p");
    const bad = /failing|not configured|Last ingest error/.test(text)
      || (backlogged && text.startsWith("📄"));
    p.className = "admin-health-line" + (bad ? " warn" : "");
    p.textContent = text;
    return p;
  }));
}

async function renderAdminUsers(q = "") {
  const box = el("admin-users");
  box.textContent = "Loading…";
  let data;
  try { data = await API.adminUsers(q); }
  catch (e) {
    box.textContent = "";
    toast("Couldn't load the account list", e.message);
    return;
  }
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
    catch (e) { toast("That operator action didn't go through", e.message); }
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
  _mapWanted: null,   // in-flight load of the map library
  marker: null,
  circle: null,
  point: null,        // {lat, lon} currently being placed
  editing: null,      // location being edited, if any

  async open() {
    el("locations-panel").hidden = false;
    const mapReady = this._initMap();          // fetches the map library if needed
    await this.refresh();                      // the list does not wait for the map
    await mapReady;
    this.renderSaved();                        // the map may have arrived last
    // Leaflet measures the container; it was display:none until a moment ago.
    setTimeout(() => this.map && this.map.invalidateSize(), 60);
  },

  close() { el("locations-panel").hidden = true; },

  async _initMap() {
    if (this.map) return;
    if (this._mapWanted) return this._mapWanted;   // a second open, same download
    this._mapWanted = ensureLeaflet();
    try {
      await this._mapWanted;
    } catch (e) {
      this._mapWanted = null;
      el("loc-map").textContent =
        "The map could not be loaded — you can still add a place by searching for it.";
      return;
    }
    if (this.map) return;
    this.map = L.map("loc-map", { worldCopyJump: true }).setView([25, 10], 2);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(this.map);
    this.saved = L.featureGroup().addTo(this.map);
    this.map.on("click", (e) => this.setPoint(e.latlng.lat, e.latlng.lng));
  },

  setPoint(lat, lon, name, country) {
    // Two different things, kept apart on purpose. `place` is what the place
    // is called — the string worth looking for in a headline and worth asking
    // a news search for. The name field is the reader's label, theirs to
    // change to "Dad's house", which is neither of those.
    this.point = { lat, lon, place: name || "", country: country || "" };
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
          forgetLocationsFeed();
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
    this.setPoint(loc.lat, loc.lon, loc.place_name, loc.country);
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
    const body = {
      name, lat: this.point.lat, lon: this.point.lon, radius_km: this.radiusKm(),
      // Sent alongside the label so the server can gather news about the place
      // and recognise it in headlines. Empty for a point clicked on the map,
      // which has no name to carry.
      place_name: this.point.place || "",
      country: this.point.country || "",
    };
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
      // The locations feed's areas just changed on the server, so what is
      // cached for it answers a different question now.
      forgetLocationsFeed();
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

  wirePlaceSearch();
}

/* ---------- place search ----------
   Suggestions as the reader types. The bundled gazetteer answers instantly and
   privately; anything it doesn't know — a street address, a town, a district —
   comes from an address lookup the server makes, listed below the local matches
   and credited. Arrow keys move through the list, Enter takes one, Esc dismisses
   it, because a suggestion list nobody can drive from the keyboard is a list
   half the readers cannot use. */
let PLACE_SEQ = 0;      // which keystroke the shown suggestions belong to

function wirePlaceSearch() {
  const input = el("loc-search");
  const box = el("loc-results");
  let timer;
  let active = -1;      // which suggestion the keyboard is on

  const rows = () => [...box.querySelectorAll(".loc-result")];
  const dismiss = () => { box.hidden = true; box.innerHTML = ""; active = -1; };
  const highlight = (i) => {
    const list = rows();
    if (!list.length) return;
    active = (i + list.length) % list.length;
    list.forEach((r, n) => r.classList.toggle("active", n === active));
    list[active].scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", list[active].id);
  };

  const suggest = async (q) => {
    const wanted = ++PLACE_SEQ;
    let payload;
    try {
      payload = await API.placeSearch(q);
    } catch (e) {
      // Silence here meant a reader typing an address watched the list simply
      // not appear, with nothing to distinguish "no such place" from "the
      // lookup is broken". Say which, and leave the map as the way through.
      if (wanted !== PLACE_SEQ) return;         // a later keystroke owns the list
      const failed = document.createElement("div");
      failed.className = "loc-none";
      // e.message already opens with "Couldn't look that place up — …".
      failed.textContent = `${e.message} You can still click the map to drop a `
        + "pin anywhere, and Delphi's own list of cities and countries is "
        + "unaffected by this.";
      box.replaceChildren(failed);
      box.hidden = false;
      return;
    }
    if (wanted !== PLACE_SEQ) return;           // a later keystroke owns the list
    // The endpoint used to answer with a bare array; accept both shapes so a
    // browser holding an older copy of this file still gets its suggestions.
    const hits = Array.isArray(payload) ? payload : (payload.results || []);
    const credit = Array.isArray(payload) ? "" : (payload.attribution || "");
    box.innerHTML = "";
    active = -1;
    input.removeAttribute("aria-activedescendant");
    if (!hits.length) {
      const none = document.createElement("div");
      none.className = "loc-none";
      none.textContent = `Nothing found for “${q}”. Click the map to drop a pin anywhere.`;
      box.appendChild(none);
      box.hidden = false;
      return;
    }
    hits.forEach((h, i) => {
      const b = document.createElement("button");
      b.className = "loc-result";
      b.id = `loc-result-${i}`;
      b.type = "button";
      b.setAttribute("role", "option");
      const icon = h.kind === "country" ? "🌐" : h.source === "osm" ? "📍" : "🏙";
      const name = document.createElement("span");
      name.className = "loc-result-name";
      name.textContent = `${icon} ${h.name}`;
      b.appendChild(name);
      // Where it is: the country for a gazetteer hit, the full postal address
      // for a looked-up one — two "Springfield"s are only told apart by this.
      const where = h.address
        || (h.kind !== "country" && h.country
            ? (COUNTRY_NAMES.get(h.country) || h.country.toUpperCase()) : "");
      if (where) {
        const sub = document.createElement("span");
        sub.className = "loc-result-where";
        sub.textContent = where;
        b.appendChild(sub);
      }
      b.onclick = () => {
        LocationsPanel.setPoint(h.lat, h.lon, h.name, h.country);
        dismiss();
        input.value = "";
      };
      box.appendChild(b);
    });
    if (credit) {
      const note = document.createElement("div");
      note.className = "loc-credit";
      note.textContent = `📍 addresses from OpenStreetMap · ${credit}`;
      box.appendChild(note);
    }
    box.hidden = false;
  };

  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", "loc-results");
  box.setAttribute("role", "listbox");

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { dismiss(); return; }
    // Long enough to be worth an address lookup gets a beat longer, so typing
    // a street name is a few requests rather than one per letter.
    timer = setTimeout(() => suggest(q), q.length >= 3 ? 300 : 200);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); highlight(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight(active - 1); }
    else if (e.key === "Enter") {
      const list = rows();
      if (!box.hidden && list.length) {
        e.preventDefault();
        list[active >= 0 ? active : 0].click();
      }
    } else if (e.key === "Escape" && !box.hidden) {
      e.stopPropagation();       // dismiss the list, don't close the panel
      dismiss();
    }
  });

  // Clicking away puts the list down; the delay lets a click on a row land.
  input.addEventListener("blur", () => setTimeout(() => {
    if (!box.contains(document.activeElement)) dismiss();
  }, 150));

  new MutationObserver(() => input.setAttribute("aria-expanded", String(!box.hidden)))
    .observe(box, { attributes: true, attributeFilter: ["hidden"] });
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
/* ---------- exporting a column ----------

   A column is research someone did; the work usually continues somewhere else —
   a spreadsheet to sort and filter, a document to circulate, a table for a
   script. The formats are the server's; this only asks for one and saves what
   comes back. The download reaches much further than the forty rows on screen,
   because a column is a view and an export is a record. */
const EXPORT_FORMATS = [
  ["xlsx", "📊 Excel workbook", "Sorted and filtered in Excel, LibreOffice or Google Sheets"],
  ["docx", "📄 Word document", "A readable brief with a heading per story — for sending on"],
  ["csv", "📋 CSV table", "Opens anywhere; the plainest thing a spreadsheet reads"],
  ["md", "📝 Markdown", "For a wiki, a notebook, or a report you're writing"],
  ["json", "🧾 JSON", "Every field, for a script or another program"],
];

const EXPORT_LIMIT = 500;

async function exportColumn(feed, fmt) {
  const query = `format=${fmt}&limit=${EXPORT_LIMIT}${langQS()}`;
  const path = feed.home
    ? `/api/articles/export?sort=${feed.sort || "newest"}&${query}`
    : `/api/feeds/${feed.id}/export?${query}`;
  const options = feed.home
    ? { method: "POST", body: JSON.stringify({ criteria: feed.criteria, name: feed.name }) }
    : {};
  return downloadExport(path, options, fmt);
}

/* Fetch a file the server built and hand it to the browser.

   Not api(): that parses JSON, and these are files. The error handling is the
   same though — a failed export must say why, like everything else. */
async function downloadExport(path, options, fmt) {
  const headers = { "Content-Type": "application/json" };
  if (Session.token()) headers["Authorization"] = "Bearer " + Session.token();
  let resp;
  try {
    resp = await fetch(path, { ...options, headers });
  } catch (e) {
    throw new Error("Couldn't reach the server to build the export.");
  }
  if (!resp.ok) {
    let detail = "";
    try { detail = (await resp.json()).detail || ""; } catch (e) { /* not json */ }
    throw new Error(detail || `The server couldn't build that export (HTTP ${resp.status}).`);
  }
  const blob = await resp.blob();
  const named = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") || "");
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = named ? named[1] : `delphi-export.${fmt}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoking immediately can cancel the download in Safari; a tick is enough.
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return { rows: Number(resp.headers.get("X-Export-Rows") || 0), name: a.download };
}

/* Save a Focus: the report on screen and every outlet carrying it.

   Shares download(), and therefore its error handling, with the column export
   — a failed export has to say why here for the same reason it does there. */
async function exportStory(fmt) {
  if (!STORY_ON_SCREEN) return;
  return downloadExport(
    `/api/story/${STORY_ON_SCREEN.id}/export?format=${fmt}${langQS()}`, {}, fmt);
}

function storyExportMenu(anchor) {
  if (!STORY_ON_SCREEN) return;
  popMenu(anchor, `Export “${STORY_ON_SCREEN.title.slice(0, 60)}”`,
    async (fmt) => {
      const done = await exportStory(fmt);
      toast("Export ready",
            `${done.rows} report${plural(done.rows)} saved as ${done.name}. `
            + "Check your browser's downloads.");
    },
    (e) => toast("Couldn't export this story", e.message));
}

/* Put a pop-up menu against the control that opened it, and close it on the
   next press elsewhere.

   Below the control when there is room, above it when there is not. The column
   exports hang off a button at the top of the board, so dropping downwards
   always worked; the Focus export hangs off the foot of a modal, where a
   five-row menu ran off the bottom of the window and could not be clicked at
   all. */
function placeMenu(menu, anchor) {
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  const below = r.bottom + 4;
  menu.style.top = (below + menu.offsetHeight <= innerHeight - 8
                    ? below
                    : Math.max(8, r.top - menu.offsetHeight - 4)) + "px";
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - menu.offsetWidth - 8)) + "px";
  setTimeout(() => document.addEventListener("mousedown", function close(e) {
    if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("mousedown", close); }
  }), 0);
}

/* The format picker, shared by every export. Each row says what the format is
   good for, because "xlsx / docx / csv / md / json" is a list of file
   extensions rather than a choice anyone can make. */
function popMenu(anchor, headText, onPick, onError) {
  const old = document.querySelector(".pop-menu");
  if (old) old.remove();
  const menu = document.createElement("div");
  menu.className = "pop-menu export-menu";
  const head = document.createElement("div");
  head.className = "pop-menu-head";
  head.textContent = headText;
  menu.appendChild(head);
  for (const [fmt, label, why] of EXPORT_FORMATS) {
    const b = document.createElement("button");
    const name = document.createElement("span");
    name.textContent = label;
    const note = document.createElement("em");
    note.textContent = why;
    b.append(name, note);
    b.onclick = async () => {
      menu.remove();
      try { await onPick(fmt); } catch (e) { onError(e); }
    };
    menu.appendChild(b);
  }
  placeMenu(menu, anchor);
}

function exportMenu(anchor, feed) {
  popMenu(anchor, `Export “${feed.name}”`,
    async (fmt) => {
      const done = await exportColumn(feed, fmt);
      toast("Export ready", `${done.rows} article${plural(done.rows)} saved as `
        + `${done.name}. Check your browser's downloads.`);
    },
    (e) => toast(`Couldn't export “${feed.name}”`, e.message));
}

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
  placeMenu(menu, anchor);
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
// `preloaded` is for boot, which has already fetched the list.
async function refreshAlerts(preloaded = null) {
  ALERTS = preloaded || await API.alerts();
  const unseen = ALERTS.reduce((n, a) => n + a.unseen, 0);
  el("stat-alerts").textContent = unseen;
  const bc = el("bell-count");
  bc.hidden = unseen === 0;
  bc.textContent = unseen;
  if (!el("alerts-panel").hidden) renderAlertsPanel();
}

/* Which render owns the panel. Every alert that fires re-renders it, so on a
   busy account this runs constantly. */
let alertsPanelSeq = 0;
let alertsPanelShowing = null;   // signature of what the panel currently shows

async function renderAlertsPanel() {
  const box = el("alerts-list");
  const wanted = ++alertsPanelSeq;
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
  // A later render started while this one was fetching; it owns the panel now.
  if (wanted !== alertsPanelSeq) return;
  renderAlertsMap(eventsByAlert);
  // An alert firing re-renders this panel, and most of those changes nothing
  // here — a hit on another alert, a repeat of one already listed. Rebuilding
  // anyway takes every row out from under whatever the reader was reaching for.
  const signature = JSON.stringify(eventsByAlert.map(({ alert, events }) =>
    [alert.id, alert.active, alert.unseen, events.map(e => [e.event_id, e.seen])]));
  if (signature === alertsPanelShowing && el("alerts-list").children.length) return;
  alertsPanelShowing = signature;
  // Built off-screen and swapped in one go. Emptying the panel first and
  // filling it after the requests came back left it blank for as long as they
  // took — and a hit clicked in that window landed on nothing at all, which is
  // exactly when a reader reaches for it.
  const built = document.createDocumentFragment();
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
        toolBtn("✎", "Edit alert", () => openBuilder("alert", alert)),
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
    built.appendChild(block);
  }
  box.replaceChildren(built);
}

/* Map of where recent alert hits are geolocated, plus alert geofences. */
let alertsMap = null;
let alertsMapLayer = null;

function alertsMapWanted() { return localStorage.getItem("gnd_alerts_map") !== "0"; }

let alertsMapSeq = 0;

async function renderAlertsMap(eventsByAlert) {
  const box = el("alerts-map");
  if (!alertsMapWanted()) { box.hidden = true; return; }
  const wanted = ++alertsMapSeq;
  try { await ensureLeaflet(); } catch (e) { box.hidden = true; return; }
  if (wanted !== alertsMapSeq) return;   // a later render already owns the map
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
    (s.last_status || "").startsWith("retired:") ? "retired dead" : "",
    // "silent" finds the feeds still being polled that have never carried
    // anything — the ones worth pruning by hand, and impossible to pick out of
    // a thousand rows without a word for them. A retired feed is not one of
    // them: it has already stopped, and answers to "retired".
    s.last_fetched_at && !s.has_produced && s.enabled
      && !(s.last_status || "").startsWith("retired:") ? "silent nothing empty" : "",
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
    // A source that answers is not the same as a source that carries news, and
    // only the first had a marker. On a catalog that grows by itself, the ones
    // worth finding are the ones that have been asked and never had anything.
    const retired = (s.last_status || "").startsWith("retired:");
    const silent = s.last_fetched_at && !s.has_produced && s.enabled && !retired;
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `${s.scope} · ${s.language}${s.added_by !== "catalog" ? " · " + s.added_by : ""}`
      + (s.repaired_from ? " · 🔧 repaired" : "") + (s.paywall ? " · 🔒 paywalled" : "")
      + (retired ? " · 🚫 retired" : silent ? " · 🕸 nothing yet" : "");
    if (silent && !retired) {
      meta.title = "Delphi has polled this feed and never got an article from "
        + "it. That can be a quiet outlet, or a feed that answers with nothing.";
    }
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
    );
    // Deleting removes the outlet for everyone on this server and takes its
    // articles with it, so it is an operator's to do. Everyone else has ⏸,
    // which has the same effect on their own board and can be undone.
    if (META.is_admin) {
      row.append(toolBtn("🗑", "Delete source for everyone on this server", async () => {
        if (!confirm(`Delete source “${s.name}” and its articles, for every `
                     + "reader on this server? This cannot be undone — ⏸ "
                     + "disables it instead.")) return;
        await API.deleteSource(s.id);
        await reloadSources();
      }));
    }
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
  feedback(save, "Saving…");
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

async function connectStream() {
  // EventSource cannot send headers, so whatever authenticates the stream has
  // to sit in the URL — where every proxy in between writes it to a log. So
  // what goes there is a ticket that lasts a minute and opens only the stream,
  // fetched with the session token over a normal (header-authenticated) call.
  // Reconnects come back through here and ask for a new one, which is why the
  // retry path closes the EventSource rather than letting it retry itself with
  // a ticket that has since expired.
  let ticket;
  try {
    ticket = (await API.streamTicket()).ticket;
  } catch (e) {
    // No ticket, no stream. Treat it exactly like a dropped connection: the
    // most likely cause is the same one (server down, or the session ended),
    // and the retry ladder below already says so at the right moment.
    return retryStream();
  }
  const es = new EventSource("/api/stream?ticket=" + encodeURIComponent(ticket));
  es.onopen = () => {
    reportStreamBack();
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
  es.onerror = () => {
    es.close();
    retryStream();
  };
}

/* A dropped stream is normal — a deploy severs it, and a phone waking from
   sleep severs it. What is not normal is it never coming back, because while
   it is down alerts do not arrive at all and nothing on screen looks any
   different. So: retry quietly a few times, then say so plainly, and back off
   rather than hammering a server that may be the problem. */
function retryStream() {
  STREAM_FAILURES += 1;
  if (STREAM_FAILURES === STREAM_QUIET_RETRIES) reportStreamDown();
  const wait = Math.min(60000, 5000 * Math.min(STREAM_FAILURES, 6));
  setTimeout(connectStream, wait);
}

/* How many reconnects to make silently before telling the reader. Three at
   five seconds apart covers a deploy, a sleeping laptop, and a flaky tunnel;
   past that, something is actually wrong. */
const STREAM_QUIET_RETRIES = 3;
let STREAM_FAILURES = 0;

function reportStreamDown() {
  const banner = el("stream-down");
  if (banner) banner.hidden = false;
  toast("Live updates have stopped",
        "Alerts won't reach this tab until the connection is back. Delphi is "
        + "still collecting news — reload once the banner clears, and check "
        + "🛠 Troubleshooting if it doesn't.", true);
}

function reportStreamBack() {
  const banner = el("stream-down");
  if (banner) banner.hidden = true;
  if (STREAM_FAILURES >= STREAM_QUIET_RETRIES)
    toast("Live updates are back", "Alerts are reaching this tab again.");
  STREAM_FAILURES = 0;
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
/* What to call a button in a message about it.

   Its own label if it has words, otherwise the tooltip or aria-label an
   icon-only button carries — ✕, 🔧 and 🗑 all read the same in a toast
   otherwise. Icons and the busy label are stripped so the name is what the
   reader saw before they clicked. */
function buttonName(btn, originalText = "") {
  const words = (s) => (s || "").replace(/[\u{1F300}-\u{1FAFF}\u{2190}-\u{27BF}]/gu, "")
    .replace(/\s+/g, " ").trim();
  return words(originalText) || words(btn.getAttribute("aria-label"))
    || words((btn.title || "").split(/[—·(]/)[0]) || "That action";
}

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
      // Name the control that failed. This wrapper is the last line of defence
      // for every button in the app, so its message used to be the same
      // "That didn't work" no matter which one broke — true of everything, and
      // therefore no help in reporting anything.
      toast(`“${buttonName(btn, text)}” didn't go through`,
            (e && e.message)
            || "The browser reported no reason. Reload and try again; if it "
               + "keeps happening, note what you clicked and check ⚙ Settings "
               + "→ 🛠 Troubleshooting.");
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

/* Boot failed. Say which kind of failure it was.

   This used to read "Could not reach the backend" whatever went wrong, because
   any exception anywhere in boot lands here — a bug in the dashboard's own
   startup reported itself as a network problem, and sent the search for the
   cause in the wrong direction. The two cases need different first moves, so
   they get different words. */
function showBootFailure(e) {
  console.error("[boot]", e);
  const msg = (e && e.message) || String(e);
  // The client raises these two itself when fetch fails or times out; anything
  // else reaching here came out of the dashboard's own code.
  const unreachable = /reach the server|didn't respond within/i.test(msg);

  const panel = document.createElement("div");
  panel.className = "empty-state boot-error";
  const h = document.createElement("h2");
  h.textContent = unreachable ? "Can't reach the server" : "D.E.L.P.H.I. failed to start";
  const p = document.createElement("p");
  p.textContent = msg;
  const hint = document.createElement("p");
  hint.className = "set-note";
  hint.textContent = unreachable
    ? "The server isn't answering. It may be restarting or under load — this "
      + "usually clears on its own within a minute."
    : "This is a fault in the dashboard itself rather than the connection. The "
      + "browser console has the full details, including which line failed.";
  const retry = document.createElement("button");
  retry.className = "btn btn-primary";
  retry.textContent = "Try again";
  retry.onclick = () => location.reload();
  panel.append(h, p, hint, retry);

  // Replace any earlier failure rather than stacking panels on a retry loop.
  for (const old of document.querySelectorAll(".boot-error")) old.remove();
  document.body.appendChild(panel);
}

boot().catch(showBootFailure);
