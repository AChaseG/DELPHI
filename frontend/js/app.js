/* Dashboard: feed board, alerts panel, sources panel, live SSE updates. */
let META = null;
let SOURCES = [];
let FEEDS = [];
let ALERTS = [];
let COUNTRY_NAMES = new Map();

async function boot() {
  [META, SOURCES] = await Promise.all([API.meta(), API.sources()]);
  COUNTRY_NAMES = new Map(META.countries.map(c => [c.iso2, c.name]));
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

  wireAuth();
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
    } catch (e) {
      toast(e.message.includes("already running") ? "Refresh" : "Refresh failed", e.message);
    }
    el("btn-refresh").disabled = false;
  };
  el("btn-alerts-panel").onclick = () => { el("alerts-panel").hidden = false; renderAlertsPanel(); };
  el("btn-close-alerts").onclick = () => { el("alerts-panel").hidden = true; };
  el("btn-toggle-alerts-map").onclick = () => {
    localStorage.setItem("gnd_alerts_map", alertsMapWanted() ? "0" : "1");
    renderAlertsPanel();
  };
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

function wireAuth() {
  const refreshProfileBtn = () => {
    el("btn-profile").textContent = Session.username() ? `👤 ${Session.username()}` : "👤 Sign in";
  };
  refreshProfileBtn();

  el("btn-profile").onclick = () => {
    if (Session.username()) {
      if (confirm(`Signed in as ${Session.username()}. Sign out?\n(This browser then shows its anonymous profile again.)`)) {
        Session.clear();
        location.reload();
      }
    } else {
      el("auth-status").textContent = "";
      el("auth-backdrop").hidden = false;
      el("auth-username").focus();
    }
  };
  el("btn-close-auth").onclick = () => { el("auth-backdrop").hidden = true; };

  const finish = async (r) => {
    const hadAnonFeeds = FEEDS.length > 0 || ALERTS.length > 0;
    Session.set(r.token, r.username, r.user_key);
    if (hadAnonFeeds &&
        confirm("Bring this browser's existing feeds and alerts into your account?")) {
      await API.claim();
    }
    location.reload();
  };
  const authCall = async (fn) => {
    const st = el("auth-status");
    st.className = "query-status err";
    try {
      const r = await fn(el("auth-username").value.trim(), el("auth-password").value);
      await finish(r);
    } catch (e) { st.textContent = e.message; }
  };
  el("btn-login").onclick = () => authCall(API.login);
  el("btn-register").onclick = () => authCall(API.register);
  el("auth-password").addEventListener("keydown", (e) => {
    if (e.key === "Enter") el("btn-login").click();
  });
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
  const row = document.createElement("div");
  row.className = "feed-head-row";
  const h = document.createElement("h3");
  h.textContent = feed.name;
  h.title = feed.name;
  const tools = document.createElement("div");
  tools.className = "feed-tools";
  tools.append(
    toolBtn("◀", "Move left", () => moveFeed(feed.id, -1)),
    toolBtn("▶", "Move right", () => moveFeed(feed.id, +1)),
    toolBtn("⇔", "Toggle width", () => toggleWidth(feed)),
    toolBtn("✎", "Edit feed", () => Builder.open("feed", feed)),
  );
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
  if (c.query) out.push(tag("boolean", c.query));
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
        "Open the feed (✎) and use <b>Preview matches</b> while removing one filter " +
        "at a time to see which criterion is limiting it. If the feed has a query or " +
        "keywords, make sure <b>🔍 Automatically ingest worldwide coverage</b> is " +
        "checked and re-save — Delphi only searches articles its sources publish, " +
        "and that option adds a source pulling in press coverage of your query.</div>";
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
    box.innerHTML = '<div class="feed-empty">No alerts yet. Create one with “+ Alert” — ' +
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
      const style = { color: "#3987e5", weight: 1.5, dashArray: "5 5", fillOpacity: 0.06 };
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
    const ok = s.last_status === "ok";
    const status = document.createElement("span");
    status.className = ok ? "s-status-ok" : (s.last_status ? "s-status-bad" : "");
    status.title = s.last_status || "not yet polled";
    status.textContent = ok ? "●" : (s.last_status ? "●" : "○");
    const name = document.createElement("span");
    name.className = "s-name";
    name.textContent = `${PLATFORM_ICONS[s.platform] || "📰"} ${flagEmoji(s.country)} ${s.name}`;
    name.title = s.rss_url;
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `${s.scope} · ${s.language}${s.added_by !== "catalog" ? " · " + s.added_by : ""}`;
    row.append(
      status, name, meta,
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
    } else if (msg.type === "alert" && msg.user_id === Session.userKey()) {
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
