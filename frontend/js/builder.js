/* Feed & alert builder modal, including the draw-on-map geofence editor. */
const Builder = {
  mode: "feed",        // "feed" | "alert"
  editing: null,       // existing feed/alert object when editing
  keywords: [],
  exclude: [],
  geo: null,
  map: null,
  drawnLayer: null,
  meta: null,
  sources: [],
  onSaved: null,

  init(meta, sources) {
    this.meta = meta;
    this.sources = sources;
    const fill = (sel, items, val, label) => {
      sel.innerHTML = "";
      for (const it of items) {
        const o = document.createElement("option");
        o.value = val(it); o.textContent = label(it);
        sel.appendChild(o);
      }
    };
    fill(el("b-countries"), meta.countries, c => c.iso2, c => `${flagEmoji(c.iso2)} ${c.name}`);
    fill(el("b-categories"), meta.categories, c => c, c => c);
    fill(el("b-languages"), meta.languages, l => l, l => l);
    fill(el("b-sources"), sources, s => s.id, s => `${s.name} (${s.scope}${s.country ? ", " + s.country : ""})`);
    this._wire();
  },

  step: 0,
  LAST_STEP: 3,

  showStep(n) {
    this.step = Math.max(0, Math.min(this.LAST_STEP, n));
    for (const s of document.querySelectorAll(".wiz-step"))
      s.hidden = +s.dataset.step !== this.step;
    for (const b of el("wiz-nav").querySelectorAll("button"))
      b.classList.toggle("active", +b.dataset.step === this.step);
    el("btn-wiz-back").disabled = this.step === 0;
    el("btn-wiz-next").hidden = this.step === this.LAST_STEP;
    el("btn-save-item").hidden = this.step !== this.LAST_STEP;
    if (this.step === this.LAST_STEP) this._renderSummary();
    if (this.step === 1 && this.map && !el("builder-map").hidden)
      setTimeout(() => this.map.invalidateSize(), 60);
  },

  _renderSummary() {
    const { name, criteria: c, sort } = this.collect();
    const lines = [];
    const names = (c.countries || []).map(iso => COUNTRY_NAMES.get(iso) || iso);
    for (let i = 0; i < (c.queries || []).length; i++)
      lines.push([c.queries.length > 1 ? `Query ${i + 1}` : "Query", c.queries[i]]);
    if (c.keywords.length) lines.push(["Keywords (any)", c.keywords.join(", ")]);
    if (c.exclude_keywords.length) lines.push(["Excluding", c.exclude_keywords.join(", ")]);
    if (names.length) lines.push(["Countries", names.join(", ")]);
    if (c.geo) lines.push(["Map area", c.geo.type === "Circle"
      ? `circle, ${c.geo.radius_km.toFixed(0)} km radius` : "drawn shape"]);
    if (c.categories.length) lines.push(["Categories", c.categories.join(", ")]);
    if (c.scopes.length) lines.push(["Scope", c.scopes.join(", ")]);
    if (c.platforms.length) lines.push(["Platforms", c.platforms.join(", ")]);
    if (c.languages.length) lines.push(["Languages", c.languages.join(", ")]);
    if (c.min_importance) lines.push(["Importance", `≥ ${c.min_importance} (${impTier(c.min_importance).label}+)`]);
    if (c.hours) lines.push(["Recency", `last ${c.hours}h`]);
    if (c.source_ids.length) lines.push(["Sources", `${c.source_ids.length} selected`]);
    if ((c.queries.length || c.keywords.length) && c.auto_coverage)
      lines.push(["Coverage", "ingesting worldwide press for this topic"]);
    const box = el("wiz-summary");
    box.innerHTML = "";
    const h = document.createElement("div");
    h.className = "wiz-summary-name";
    h.textContent = `${this.mode === "alert" ? "🔔" : "📋"} ${name}`;
    box.appendChild(h);
    if (!lines.length) {
      const p = document.createElement("div");
      p.className = "wiz-summary-row";
      p.textContent = "No filters — matches all ingested news, sorted by " + sort + ".";
      box.appendChild(p);
    }
    for (const [k, v] of lines) {
      const row = document.createElement("div");
      row.className = "wiz-summary-row";
      const key = document.createElement("b"); key.textContent = k + ": ";
      row.append(key, document.createTextNode(v));
      box.appendChild(row);
    }
  },

  _wire() {
    if (this._wired) return;
    this._wired = true;
    el("btn-close-builder").onclick = () => this.close();
    el("btn-wiz-back").onclick = () => this.showStep(this.step - 1);
    el("btn-wiz-next").onclick = () => this.showStep(this.step + 1);
    el("wiz-nav").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-step]");
      if (b) this.showStep(+b.dataset.step);
    });
    el("modal-backdrop").addEventListener("mousedown", (e) => {
      if (e.target === el("modal-backdrop")) this.close();
    });

    wireChips("b-keywords", "b-keywords-box", this.keywords);
    wireChips("b-exclude", "b-exclude-box", this.exclude);

    const imp = el("b-importance");
    imp.oninput = () => {
      el("b-imp-val").textContent = imp.value;
      el("b-imp-tier").textContent = imp.value > 0 ? `(${impTier(+imp.value).label}+)` : "(any)";
    };

    el("btn-add-query").onclick = () => this._addQueryRow("");
    // live-validate any query input (rows are dynamic — delegate; each row
    // debounces independently so editing one row can't cancel another's check)
    el("b-queries").addEventListener("input", (e) => {
      const input = e.target.closest(".b-query-input");
      if (!input) return;
      const row = input.closest(".query-row");
      clearTimeout(row._qTimer);
      row._qTimer = setTimeout(async () => {
        const st = row.querySelector(".query-status");
        const q = input.value.trim();
        if (!q) { st.textContent = ""; st.className = "query-status"; return; }
        try {
          const r = await API.validateQuery(q);
          st.textContent = r.valid ? "✓ valid query" : "✗ " + r.error;
          st.className = "query-status " + (r.valid ? "ok" : "err");
        } catch (e) { /* offline */ }
      }, 350);
    });

    el("btn-toggle-map").onclick = () => this.toggleMap();
    el("btn-clear-geo").onclick = () => this.setGeo(null);
    el("btn-preview").onclick = () => this.preview();
    el("btn-save-item").onclick = () => this.save();
    el("btn-delete-item").onclick = () => this.remove();
  },

  open(mode, item = null) {
    this.mode = mode;
    this.editing = item;
    const c = (item && item.criteria) || {};
    el("builder-title").textContent =
      (item ? "Edit " : "New ") + (mode === "alert" ? "alert" : "feed");
    el("b-name").value = item ? item.name : "";
    selectValues(el("b-countries"), c.countries || []);
    selectValues(el("b-categories"), c.categories || []);
    selectValues(el("b-languages"), c.languages || []);
    selectValues(el("b-sources"), (c.source_ids || []).map(String));
    for (const cb of el("b-scopes").querySelectorAll("input"))
      cb.checked = (c.scopes || []).includes(cb.value);
    for (const cb of el("b-platforms").querySelectorAll("input"))
      cb.checked = (c.platforms || []).includes(cb.value);
    this.keywords.length = 0; this.keywords.push(...(c.keywords || []));
    this.exclude.length = 0; this.exclude.push(...(c.exclude_keywords || []));
    renderChips("b-keywords-box", this.keywords);
    renderChips("b-exclude-box", this.exclude);
    const queries = [...(c.queries || [])];
    if (c.query && !queries.includes(c.query)) queries.unshift(c.query);  // legacy
    el("b-queries").innerHTML = "";
    if (!queries.length) queries.push("");
    for (const q of queries) this._addQueryRow(q);
    el("b-coverage").checked = item ? !!c.auto_coverage : true;
    el("b-importance").value = c.min_importance || 0;
    el("b-importance").dispatchEvent(new Event("input"));
    el("b-hours").value = c.hours || "";
    el("alert-only-fields").hidden = mode !== "alert";
    el("feed-only-fields").hidden = mode !== "feed";
    el("b-group").checked = item ? !!item.group_events : false;
    el("b-active").checked = item ? !!item.active : true;
    const sort = (item && item.sort) || "newest";
    for (const r of document.querySelectorAll('input[name="b-sort"]')) r.checked = r.value === sort;
    el("btn-delete-item").hidden = !item;
    el("preview-count").textContent = "";
    el("builder-map").hidden = true;
    el("btn-toggle-map").textContent = "Draw area on map";
    this.setGeo(c.geo || null, /*renderOnly*/ true);
    this.showStep(0);
    el("modal-backdrop").hidden = false;
  },

  close() { el("modal-backdrop").hidden = true; },

  _addQueryRow(value) {
    const row = document.createElement("div");
    row.className = "query-row";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "b-query-input";
    input.placeholder = '("supply chain" OR semiconductor) AND (china OR taiwan) NOT rumor';
    input.value = value || "";
    const st = document.createElement("span");
    st.className = "query-status";
    const rm = document.createElement("button");
    rm.className = "icon-btn"; rm.textContent = "✕"; rm.title = "Remove this query";
    rm.onclick = () => {
      row.remove();
      if (!el("b-queries").children.length) this._addQueryRow("");
    };
    const line = document.createElement("div");
    line.className = "query-line";
    line.append(input, rm);
    row.append(line, st);
    el("b-queries").appendChild(row);
  },

  _queryValues() {
    return [...el("b-queries").querySelectorAll(".b-query-input")]
      .map(i => i.value.trim()).filter(Boolean);
  },

  collect() {
    const scopes = [...el("b-scopes").querySelectorAll("input:checked")].map(i => i.value);
    const platforms = [...el("b-platforms").querySelectorAll("input:checked")].map(i => i.value);
    const criteria = {
      countries: selectedValues(el("b-countries")),
      scopes,
      platforms,
      categories: selectedValues(el("b-categories")),
      languages: selectedValues(el("b-languages")),
      source_ids: selectedValues(el("b-sources")).map(Number),
      keywords: [...this.keywords],
      exclude_keywords: [...this.exclude],
      query: "",
      queries: this._queryValues(),
      min_importance: +el("b-importance").value,
      hours: el("b-hours").value ? +el("b-hours").value : null,
      geo: this.geo,
      auto_coverage: el("b-coverage").checked,
    };
    const sort = document.querySelector('input[name="b-sort"]:checked').value;
    return { name: el("b-name").value.trim() || "Untitled", criteria, sort };
  },

  async preview() {
    try {
      const { criteria, sort } = this.collect();
      const arts = await API.search(criteria, sort, 200);
      el("preview-count").textContent =
        `${arts.length}${arts.length === 200 ? "+" : ""} matching article(s) currently stored`;
    } catch (e) {
      el("preview-count").textContent = "Preview failed: " + e.message;
    }
  },

  async save() {
    const body = this.collect();
    try {
      if (this.mode === "alert") {
        body.active = el("b-active").checked;
        delete body.sort;
        if (this.editing) await API.updateAlert(this.editing.id, body);
        else await API.createAlert(body);
      } else {
        body.width = this.editing ? this.editing.width || 1 : 1;
        body.group_events = el("b-group").checked;
        if (this.editing) await API.updateFeed(this.editing.id, body);
        else await API.createFeed(body);
      }
      this.close();
      if (this.onSaved) this.onSaved(this.mode);
    } catch (e) {
      alert("Could not save: " + e.message);
    }
  },

  async remove() {
    if (!this.editing) return;
    if (!confirm(`Delete this ${this.mode}?`)) return;
    if (this.mode === "alert") await API.deleteAlert(this.editing.id);
    else await API.deleteFeed(this.editing.id);
    this.close();
    if (this.onSaved) this.onSaved(this.mode);
  },

  /* ----- map / geofence ----- */

  toggleMap() {
    if (typeof L === "undefined") {
      el("geo-summary").textContent =
        "Map library could not be loaded (offline?) — geographic filters still work via the API.";
      return;
    }
    const box = el("builder-map");
    box.hidden = !box.hidden;
    el("btn-toggle-map").textContent = box.hidden ? "Draw area on map" : "Hide map";
    if (!box.hidden) {
      if (!this.map) this._initMap();
      setTimeout(() => { this.map.invalidateSize(); this._fitToGeo(); }, 60);
    }
  },

  _initMap() {
    this.map = L.map("builder-map", { worldCopyJump: true }).setView([25, 10], 2);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(this.map);
    this.drawnItems = new L.FeatureGroup().addTo(this.map);
    this.map.addControl(new L.Control.Draw({
      draw: {
        polygon: { allowIntersection: false, showArea: true },
        rectangle: {},
        circle: {},
        polyline: false, marker: false, circlemarker: false,
      },
      edit: { featureGroup: this.drawnItems, edit: false, remove: true },
    }));
    this.map.on(L.Draw.Event.CREATED, (e) => {
      this.drawnItems.clearLayers();          // one geofence per feed/alert
      this.drawnItems.addLayer(e.layer);
      if (e.layerType === "circle") {
        const c = e.layer.getLatLng();
        this.geo = { type: "Circle", center: [c.lat, c.lng], radius_km: e.layer.getRadius() / 1000 };
      } else {
        this.geo = e.layer.toGeoJSON().geometry;
      }
      this._updateGeoSummary();
    });
    this.map.on(L.Draw.Event.DELETED, () => this.setGeo(null));
    this._renderGeo();
  },

  setGeo(geo, renderOnly = false) {
    this.geo = geo;
    this._updateGeoSummary();
    if (this.map && !renderOnly) this._renderGeo();
    if (this.map && renderOnly) this._renderGeo();
  },

  _renderGeo() {
    if (!this.drawnItems) return;
    this.drawnItems.clearLayers();
    if (!this.geo) return;
    if (this.geo.type === "Circle") {
      this.drawnItems.addLayer(
        L.circle([this.geo.center[0], this.geo.center[1]], { radius: this.geo.radius_km * 1000 })
      );
    } else {
      L.geoJSON({ type: "Feature", geometry: this.geo }).eachLayer(l => this.drawnItems.addLayer(l));
    }
  },

  _fitToGeo() {
    if (this.geo && this.drawnItems && this.drawnItems.getLayers().length) {
      this.map.fitBounds(this.drawnItems.getBounds().pad(0.4));
    }
  },

  _updateGeoSummary() {
    const s = el("geo-summary");
    if (!this.geo) { s.textContent = "No area set"; el("btn-clear-geo").hidden = true; return; }
    s.textContent = this.geo.type === "Circle"
      ? `Circle, radius ${this.geo.radius_km.toFixed(1)} km`
      : `${this.geo.type} area set`;
    el("btn-clear-geo").hidden = false;
  },
};

/* ----- small shared helpers ----- */
function el(id) { return document.getElementById(id); }

function selectedValues(sel) { return [...sel.selectedOptions].map(o => o.value); }
function selectValues(sel, values) {
  const set = new Set(values.map(String));
  for (const o of sel.options) o.selected = set.has(o.value);
}

function flagEmoji(iso2) {
  if (!iso2 || iso2.length !== 2) return "🌐";
  return String.fromCodePoint(...[...iso2.toUpperCase()].map(ch => 0x1f1e6 + ch.charCodeAt(0) - 65));
}

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function impTier(score) {
  // Colors are the fixed status palette (never reused as series colors).
  if (score >= 80) return { cls: "imp-critical", icon: "▲", label: "Critical", color: "#d03b3b" };
  if (score >= 60) return { cls: "imp-high", icon: "◆", label: "High", color: "#ec835a" };
  if (score >= 40) return { cls: "imp-notable", icon: "●", label: "Notable", color: "#fab219" };
  return { cls: "imp-routine", icon: "○", label: "Routine", color: "#898781" };
}

function wireChips(inputId, boxId, list) {
  const input = el(inputId);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      const v = input.value.trim().replace(/,$/, "");
      if (v && !list.includes(v)) { list.push(v); renderChips(boxId, list); }
      input.value = "";
    } else if (e.key === "Backspace" && !input.value && list.length) {
      list.pop(); renderChips(boxId, list);
    }
  });
}

function renderChips(boxId, list) {
  const box = el(boxId);
  for (const c of box.querySelectorAll(".chip")) c.remove();
  const input = box.querySelector("input");
  for (let i = 0; i < list.length; i++) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = list[i] + " ";
    const x = document.createElement("button");
    x.textContent = "✕";
    x.onclick = () => { list.splice(i, 1); renderChips(boxId, list); };
    chip.appendChild(x);
    box.insertBefore(chip, input);
  }
}
