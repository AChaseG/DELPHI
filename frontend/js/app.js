/* Dashboard: feed board, alerts panel, sources panel, live SSE updates. */
let META = null;
let SOURCES = [];
let FEEDS = [];
let ALERTS = [];

async function boot() {
  [META, SOURCES] = await Promise.all([API.meta(), API.sources()]);
  Builder.init(META, SOURCES);
  Builder.onSaved = async (mode) => {
    if (mode === "alert") await refreshAlerts();
    else await refreshFeeds();
  };
  wireTopbar();
  renderStats();
  await Promise.all([refreshFeeds(), refreshAlerts()]);
  connectStream();
  // First run on an empty database: pull the catalog once in the background.
  if (META.stats.total_articles === 0) {
    toast("Fetching news", "First run — polling all sources. This can take a minute…");
    API.runIngest().then(async (r) => {
      toast("Ingest complete", `${r.new_articles} articles from ${r.sources_ok}/${r.sources_total} sources`);
      renderStats(await API.meta());
      refreshFeeds();
    }).catch(() => {});
  }
}

function wireTopbar() {
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

  el("btn-new-feed").onclick = () => Builder.open("feed");
  el("btn-empty-new-feed").onclick = () => Builder.open("feed");
  el("btn-new-alert").onclick = () => Builder.open("alert");
  el("btn-starter-pack").onclick = starterPack;
  el("btn-refresh").onclick = async () => {
    el("btn-refresh").disabled = true;
    toast("Refreshing", "Polling all sources…");
    try {
      const r = await API.runIngest();
      toast("Ingest complete", `${r.new_articles} new articles (${r.sources_ok}/${r.sources_total} sources ok)`);
      renderStats(await API.meta());
      await refreshFeeds();
    } catch (e) { toast("Refresh failed", e.message); }
    el("btn-refresh").disabled = false;
  };
  el("btn-alerts-panel").onclick = () => { el("alerts-panel").hidden = false; renderAlertsPanel(); };
  el("btn-close-alerts").onclick = () => { el("alerts-panel").hidden = true; };
  el("btn-sources").onclick = () => { el("sources-panel").hidden = false; renderSourcesPanel(); };
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
  el("btn-add-source").onclick = async () => {
    const name = el("src-name").value.trim(), url = el("src-url").value.trim();
    if (!name || !url) return;
    try {
      await API.addSource({ name, rss_url: url });
      el("src-name").value = ""; el("src-url").value = "";
      SOURCES = await API.sources();
      Builder.init(META, SOURCES);
      renderSourcesPanel();
      toast("Source added", name);
    } catch (e) { toast("Could not add source", e.message); }
  };
}

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
  const board = el("board");
  const searchCol = el("search-col");
  board.innerHTML = "";
  if (searchCol) board.appendChild(searchCol);
  el("empty-state").hidden = FEEDS.length > 0;
  for (const feed of FEEDS) board.appendChild(feedColumn(feed));
  await Promise.all(FEEDS.map(loadFeedArticles));
}

function feedColumn(feed) {
  const col = document.createElement("section");
  col.className = "feed-col" + (feed.width > 1 ? " wide" : "");
  col.id = "feed-" + feed.id;

  const head = document.createElement("div");
  head.className = "feed-head";
  const h = document.createElement("h3");
  h.textContent = feed.name;
  h.title = feed.name;
  const badges = document.createElement("div");
  badges.className = "feed-badges";
  badges.append(...criteriaBadges(feed.criteria, feed.sort));
  if (feed.group_events) {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = "🧵 events";
    badges.appendChild(t);
  }
  const tools = document.createElement("div");
  tools.className = "feed-tools";
  tools.append(
    toolBtn("◀", "Move left", () => moveFeed(feed.id, -1)),
    toolBtn("▶", "Move right", () => moveFeed(feed.id, +1)),
    toolBtn("⇔", "Toggle width", () => toggleWidth(feed)),
    toolBtn("✎", "Edit feed", () => Builder.open("feed", feed)),
  );
  head.append(h, badges, tools);

  const body = document.createElement("div");
  body.className = "feed-body";
  body.innerHTML = '<div class="feed-empty">Loading…</div>';
  col.append(head, body);
  return col;
}

function criteriaBadges(c, sort) {
  const out = [];
  const tag = (t) => { const s = document.createElement("span"); s.className = "tag"; s.textContent = t; return s; };
  if ((c.countries || []).length) out.push(tag(c.countries.map(flagEmoji).join(" ")));
  for (const cat of (c.categories || []).slice(0, 3)) out.push(tag(cat));
  if ((c.scopes || []).length && c.scopes.length < 3) out.push(tag(c.scopes.join("/")));
  if ((c.keywords || []).length) out.push(tag("kw:" + c.keywords.length));
  if (c.query) out.push(tag("boolean"));
  if (c.min_importance) out.push(tag("imp≥" + c.min_importance));
  if (c.geo) out.push(tag("📍 map area"));
  if (c.hours) out.push(tag("last " + c.hours + "h"));
  if (sort === "importance") out.push(tag("by importance"));
  return out;
}

function toolBtn(txt, title, fn) {
  const b = document.createElement("button");
  b.className = "icon-btn"; b.textContent = txt; b.title = title; b.onclick = fn;
  return b;
}

async function loadFeedArticles(feed) {
  const body = document.querySelector(`#feed-${feed.id} .feed-body`);
  if (!body) return;
  try {
    const items = feed.group_events ? await API.feedEvents(feed.id) : await API.feedArticles(feed.id);
    body.innerHTML = "";
    if (!items.length) {
      body.innerHTML = '<div class="feed-empty">No matching articles yet. ' +
        "Try widening the criteria, or hit ⟳ to poll sources.</div>";
      return;
    }
    if (feed.group_events) for (const g of items) body.appendChild(eventGroup(g));
    else for (const a of items) body.appendChild(articleRow(a));
  } catch (e) {
    body.innerHTML = `<div class="feed-empty">Failed to load: ${e.message}</div>`;
  }
}

function eventGroup(g) {
  const wrap = document.createElement("div");
  wrap.className = "event-group";
  wrap.appendChild(articleRow(g.articles[0]));
  if (g.articles.length > 1) {
    const det = document.createElement("details");
    det.className = "event-timeline";
    const sum = document.createElement("summary");
    const srcs = g.source_count > 1 ? `${g.source_count} sources` : "1 source";
    sum.textContent = `🧵 ${g.total_count} reports · ${srcs} — show timeline`;
    det.appendChild(sum);
    for (const a of g.articles.slice(1)) det.appendChild(articleRow(a));
    wrap.appendChild(det);
  }
  return wrap;
}

function articleRow(a) {
  const t = impTier(a.importance);
  const row = document.createElement("a");
  row.className = "article";
  row.href = a.url; row.target = "_blank"; row.rel = "noopener";

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
  const span = document.createElement("span");
  span.textContent = bits.join("  ·  ");
  meta.appendChild(span);

  row.append(title, meta);
  if (a.summary) {
    const s = document.createElement("div");
    s.className = "summary"; s.textContent = a.summary;
    row.appendChild(s);
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
  for (const a of arts) body.appendChild(articleRow(a));
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
  if (META.stats.total_articles === 0) {
    await API.seedDemo();   // instant content even before the first live poll finishes
    renderStats(await API.meta());
  }
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
    box.innerHTML = '<div class="feed-empty">No alerts yet. Create one with “+ Alert” — ' +
      "you'll get a live notification whenever a new article matches its criteria " +
      "(keywords, boolean query, countries, importance, or a drawn map area).</div>";
    return;
  }
  for (const alert of ALERTS) {
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
    block.append(head, events);
    box.appendChild(block);
    API.alertEvents(alert.id).then(evs => {
      if (!evs.length) { events.innerHTML = '<div class="feed-empty">No hits yet.</div>'; return; }
      for (const ev of evs.slice(0, 10)) events.appendChild(articleRow(ev.article));
    });
  }
}

/* ---------- sources panel ---------- */
async function renderSourcesPanel() {
  const box = el("sources-list");
  box.innerHTML = "";
  for (const s of SOURCES) {
    const row = document.createElement("div");
    row.className = "source-row";
    const ok = s.last_status === "ok";
    const status = document.createElement("span");
    status.className = ok ? "s-status-ok" : (s.last_status ? "s-status-bad" : "");
    status.title = s.last_status || "not yet polled";
    status.textContent = ok ? "●" : (s.last_status ? "●" : "○");
    const name = document.createElement("span");
    name.className = "s-name";
    name.textContent = `${flagEmoji(s.country)} ${s.name}`;
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `${s.scope} · ${s.language}${s.added_by !== "catalog" ? " · " + s.added_by : ""}`;
    row.append(
      status, name, meta,
      toolBtn(s.enabled ? "⏸" : "▶", s.enabled ? "Disable" : "Enable", async () => {
        await API.patchSource(s.id, { enabled: !s.enabled });
        SOURCES = await API.sources(); renderSourcesPanel();
      }),
      toolBtn("🗑", "Delete source", async () => {
        if (!confirm(`Delete source “${s.name}” and its articles?`)) return;
        await API.deleteSource(s.id);
        SOURCES = await API.sources(); Builder.init(META, SOURCES); renderSourcesPanel();
      }),
    );
    box.appendChild(row);
  }
}

/* ---------- live stream ---------- */
let refreshTimer = null;
function connectStream() {
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "cycle") {
      API.meta().then(renderStats).catch(() => {});
    } else if (msg.type === "articles") {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(async () => {
        renderStats(await API.meta());
        await Promise.all(FEEDS.map(loadFeedArticles));
      }, 1500);
    } else if (msg.type === "alert" && msg.user_id === USER_ID) {
      const t = impTier(msg.importance);
      toast(`🔔 ${msg.alert_name}`, `${t.icon} ${t.label} — ${msg.title}`, true);
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
