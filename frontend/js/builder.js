/* Feed & alert builder modal, including the draw-on-map geofence editor. */
const Builder = {
  mode: "feed",        // "feed" | "alert" — what Save will produce
  originalMode: "feed",// what `editing` currently is; differing = conversion
  editing: null,       // existing feed/alert object when editing
  keywords: [],
  exclude: [],
  geos: [],        // any number of areas, matched as OR
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
    this._renderSources();
    this._wire();
  },

  /* ---------- source picker ----------
     The catalog runs to several hundred outlets, so this is a searchable list
     of checkboxes rather than a multi-select: with a plain <select multiple>
     you had to scroll to find an outlet and ctrl-click to keep the ones you
     already had. Chosen sources are listed above the results so a selection
     stays visible after the search that found it is cleared. */
  chosenSources: new Set(),

  _renderSources() {
    const box = el("b-sources");
    if (!box) return;
    const needle = (el("b-src-search")?.value || "").trim();
    const shown = this.sources.filter(s => sourceMatches(s, needle));
    box.innerHTML = "";
    if (!shown.length) {
      const p = document.createElement("div");
      p.className = "s-meta";
      p.textContent = needle ? `No source matches “${needle}”.` : "No sources yet.";
      box.appendChild(p);
    }
    // Cap the rendered rows: an unfiltered catalog is hundreds of checkboxes,
    // and building them all on every keystroke is what makes a picker feel slow.
    const LIMIT = 200;
    for (const s of shown.slice(0, LIMIT)) box.appendChild(this._sourceRow(s));
    if (shown.length > LIMIT) {
      const more = document.createElement("div");
      more.className = "s-meta";
      more.textContent = `…and ${shown.length - LIMIT} more — narrow the search to see them.`;
      box.appendChild(more);
    }
    this._renderChosenSources();
  },

  _sourceRow(s) {
    const row = document.createElement("label");
    row.className = "src-pick-row";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = String(s.id);
    cb.checked = this.chosenSources.has(s.id);
    cb.onchange = () => {
      if (cb.checked) this.chosenSources.add(s.id);
      else this.chosenSources.delete(s.id);
      this._renderChosenSources();
    };
    const name = document.createElement("span");
    name.className = "src-pick-name";
    name.textContent = `${s.paywall ? "🔒 " : ""}${flagEmoji(s.country)} ${s.name}`;
    const meta = document.createElement("span");
    meta.className = "s-meta";
    meta.textContent = `${s.platform || "news"} · ${s.scope} · ${s.language}`;
    row.append(cb, name, meta);
    return row;
  },

  /* The running selection, plus the summary line on the <summary> element so
     you can tell a restricted feed from an unrestricted one while it's shut. */
  _renderChosenSources() {
    const chosen = el("b-src-chosen");
    const label = el("b-src-summary");
    const byId = new Map(this.sources.map(s => [s.id, s]));
    const ids = [...this.chosenSources];
    if (label) {
      label.textContent = ids.length
        ? `(${ids.length} source${plural(ids.length)})`
        : "(optional — all sources)";
    }
    if (!chosen) return;
    chosen.innerHTML = "";
    chosen.hidden = !ids.length;
    for (const id of ids) {
      const s = byId.get(id);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = (s ? s.name : `source ${id}`) + " ✕";
      chip.title = "Remove from the selection";
      chip.onclick = () => {
        this.chosenSources.delete(id);
        this._renderSources();
      };
      chosen.appendChild(chip);
    }
    // Boxes for rows already on screen have to follow the chips.
    for (const cb of el("b-sources").querySelectorAll("input[type=checkbox]"))
      cb.checked = this.chosenSources.has(+cb.value);
    if (this.step === this.LAST_STEP) this._renderSummary();
  },

  step: 0,
  LAST_STEP: 3,

  showStep(n) {
    this.clearError();   // a previous rejection doesn't apply once you move on
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
      lines.push([c.queries.length > 1 ? `Search ${i + 1}` : "Search", c.queries[i]]);
    if (c.keywords.length) lines.push(["Keywords (any)", c.keywords.join(", ")]);
    if (c.exclude_keywords.length) lines.push(["Excluding", c.exclude_keywords.join(", ")]);
    if (names.length) lines.push(["Countries", names.join(", ")]);
    // Both keys: collect() writes `geos`, but an item saved before multiple
    // areas existed still carries a single `geo`. Reading only `geo` left every
    // drawn area out of the summary the user checks before saving.
    const areas = [...(c.geos || []), ...(c.geo ? [c.geo] : [])];
    if (areas.length) {
      const describe = (g) => (g.type === "Circle"
        ? `circle, ${g.radius_km.toFixed(0)} km radius` : g.type.toLowerCase());
      lines.push([areas.length > 1 ? `Map areas (any of ${areas.length})` : "Map area",
                  areas.map(describe).join(" · ")]);
    }
    if (c.categories.length) lines.push(["Categories", c.categories.join(", ")]);
    if (c.scopes.length) lines.push(["Scope", c.scopes.join(", ")]);
    if (c.platforms.length) lines.push(["Platforms", c.platforms.join(", ")]);
    if (c.languages.length) lines.push(["Languages", c.languages.join(", ")]);
    if (c.min_importance) lines.push(["Importance", `≥ ${c.min_importance} (${impTier(c.min_importance).label}+)`]);
    if (c.hours) lines.push(["Recency", `last ${c.hours}h`]);
    if (c.date_from || c.date_to)
      lines.push(["Date range", `${c.date_from || "…"} → ${c.date_to || "…"}`]);
    if (c.hide_stale) lines.push(["Staleness", "hide events with no recent updates (threshold in Settings)"]);
    if (c.source_ids.length) {
      const byId = new Map(this.sources.map(s => [s.id, s.name]));
      const named = c.source_ids.map(id => byId.get(id) || `source ${id}`);
      lines.push(["Only these sources", named.length > 6
        ? `${named.slice(0, 6).join(", ")} +${named.length - 6} more`
        : named.join(", ")]);
    }
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
    el("builder-mode").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-mode]");
      if (b) this.setMode(b.dataset.mode);
    });
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
          // A query can be valid and still not mean what it looks like it
          // means — NOT after an OR list binds to the last alternative only,
          // which is how sports coverage ends up in a data-centre feed. That
          // is a note, not a refusal: the query saves either way.
          const note = (r.advisories || [])[0];
          if (!r.valid) {
            st.textContent = "✗ " + r.error;
            st.className = "query-status err";
          } else if (note) {
            st.textContent = "⚠ " + note;
            st.className = "query-status warn";
          } else {
            st.textContent = "✓ valid query";
            st.className = "query-status ok";
          }
        } catch (e) { /* offline */ }
      }, 350);
    });

    el("btn-clear-dates").onclick = () => {
      el("b-date-from").value = ""; el("b-date-to").value = "";
    };

    let srcTimer;
    el("b-src-search").addEventListener("input", () => {
      clearTimeout(srcTimer);
      srcTimer = setTimeout(() => this._renderSources(), 120);
    });
    // "Select shown" acts on the current search, which is the only way to pick
    // out a group — every Japanese outlet, say — without clicking each one.
    el("btn-b-src-all").onclick = () => {
      const needle = el("b-src-search").value.trim();
      for (const s of this.sources) if (sourceMatches(s, needle)) this.chosenSources.add(s.id);
      this._renderSources();
    };
    el("btn-b-src-none").onclick = () => {
      this.chosenSources.clear();
      this._renderSources();
    };
    // Controls living on the Review step refresh the summary in place.
    for (const sel of ["#b-stale", "#b-group", "#b-active", 'input[name="b-sort"]']) {
      for (const node of document.querySelectorAll(sel)) {
        node.addEventListener("change", () => {
          if (this.step === this.LAST_STEP) this._renderSummary();
        });
      }
    }
    el("btn-toggle-map").onclick = () => this.toggleMap();
    el("btn-clear-geo").onclick = () => this.setGeo([]);
    el("btn-preview").onclick = () => this.preview();
    el("btn-save-item").onclick = () => this.save();
    el("btn-delete-item").onclick = () => this.remove();
  },

  /* Flip what Save produces. Editing an item and picking the other mode turns
     the save into a conversion (feed → alert or alert → feed). */
  setMode(mode) {
    this.mode = mode;
    for (const b of el("builder-mode").querySelectorAll("button"))
      b.classList.toggle("active", b.dataset.mode === mode);
    el("alert-only-fields").hidden = mode !== "alert";
    el("feed-only-fields").hidden = mode !== "feed";
    const noun = mode === "alert" ? "alert" : "feed";
    el("builder-title").textContent = !this.editing ? `New ${noun}`
      : mode === this.originalMode ? `Edit ${noun}`
      : `Convert to ${noun}`;
    if (this.step === this.LAST_STEP) this._renderSummary();
  },

  open(mode, item = null) {
    this.editing = item;
    this.originalMode = mode;
    const c = (item && item.criteria) || {};
    el("b-name").value = item ? item.name : "";
    selectValues(el("b-countries"), c.countries || []);
    selectValues(el("b-categories"), c.categories || []);
    selectValues(el("b-languages"), c.languages || []);
    this.chosenSources = new Set(c.source_ids || []);
    el("b-src-search").value = "";
    // Open the picker when the item being edited actually restricts sources,
    // so a restriction can't sit hidden behind a closed disclosure.
    el("b-sources-details").open = this.chosenSources.size > 0;
    this._renderSources();
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
    el("b-date-from").value = c.date_from || "";
    el("b-date-to").value = c.date_to || "";
    el("b-stale").checked = !!c.hide_stale;
    el("b-group").checked = !!(item && item.group_events);
    // Feeds have no `active`; default true so a feed→alert conversion fires.
    el("b-active").checked = item && item.active !== undefined ? !!item.active : true;
    el("b-notify-email").checked = !!(item && item.notify_email);
    el("b-webhook").value = (item && item.webhook_url) || "";
    // Pantheon-shared copies can be edited but not converted feed↔alert.
    el("builder-mode").hidden = !!(item && item.pantheon_id);
    this.setMode(mode);
    const sort = (item && item.sort) || "newest";
    for (const r of document.querySelectorAll('input[name="b-sort"]')) r.checked = r.value === sort;
    el("btn-delete-item").hidden = !item;
    el("preview-count").textContent = "";
    el("builder-map").hidden = true;
    el("btn-toggle-map").textContent = "Draw area on map";
    this.setGeo(c.geos && c.geos.length ? c.geos : (c.geo || null), /*renderOnly*/ true);
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
    rm.className = "icon-btn"; rm.textContent = "✕"; rm.title = "Remove this search";
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
      source_ids: [...this.chosenSources],
      keywords: [...this.keywords],
      exclude_keywords: [...this.exclude],
      query: "",
      queries: this._queryValues(),
      min_importance: +el("b-importance").value,
      hours: el("b-hours").value ? +el("b-hours").value : null,
      date_from: el("b-date-from").value || "",
      date_to: el("b-date-to").value || "",
      hide_stale: el("b-stale").checked,
      geos: this.geos,
      geo: null,   // superseded by geos; cleared so it can't double-count
      auto_coverage: el("b-coverage").checked,
    };
    const sort = document.querySelector('input[name="b-sort"]:checked').value;
    // No "Untitled" fallback: a silently auto-named alert is one the user
    // can't recognize later, and it also made the server's name rule
    // unreachable. save() asks for a name instead.
    return { name: el("b-name").value.trim(), criteria, sort };
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
    if (!body.name) {
      // Caught here rather than on the server so the answer is instant and
      // points at the field that needs filling in.
      this.showStep(0);   // clears the error, so set it after moving
      this.showError(`Give this ${this.mode} a name first.`);
      el("b-name").focus();
      return;
    }
    // Converting = editing an item while the toggle points at the other kind.
    // Criteria are shared between feeds and alerts, so the new item is created
    // first (with everything carried over) and the old one deleted only after
    // that succeeds — a failure never loses the original.
    const converting = this.editing && this.mode !== this.originalMode;
    try {
      if (this.mode === "alert") {
        body.active = el("b-active").checked;
        body.notify_email = el("b-notify-email").checked;
        body.webhook_url = el("b-webhook").value.trim();
        delete body.sort;
        if (converting) {
          await API.createAlert(body);
          await API.deleteFeed(this.editing.id);
        } else if (this.editing) await API.updateAlert(this.editing.id, body);
        else await API.createAlert(body);
      } else {
        body.width = this.editing && !converting ? this.editing.width || 1 : 1;
        body.group_events = el("b-group").checked;
        if (converting) {
          await API.createFeed(body);
          await API.deleteAlert(this.editing.id);
        } else if (this.editing) await API.updateFeed(this.editing.id, body);
        else await API.createFeed(body);
      }
      this.close();
      if (this.onSaved) this.onSaved(this.mode, converting);
    } catch (e) {
      // Keep the wizard open with everything the user typed intact, and say
      // what was wrong next to the Save button. A native alert() can be
      // suppressed by the browser ("don't show more dialogs"), which leaves a
      // failed save looking like a button that did nothing.
      this.showError(e.message || "Could not save.");
    }
  },

  showError(msg) {
    const box = el("builder-error");
    if (!box) return;
    box.textContent = msg;
    box.hidden = false;
    box.scrollIntoView({ block: "nearest" });
  },

  clearError() {
    const box = el("builder-error");
    if (box) { box.textContent = ""; box.hidden = true; }
  },

  async remove() {
    if (!this.editing) return;
    // Delete targets what the item IS, regardless of where the toggle sits.
    if (!confirm(`Delete this ${this.originalMode}?`)) return;
    if (this.originalMode === "alert") await API.deleteAlert(this.editing.id);
    else await API.deleteFeed(this.editing.id);
    this.close();
    if (this.onSaved) this.onSaved(this.originalMode);
  },

  /* ----- map / geofence ----- */

  async toggleMap() {
    const box = el("builder-map");
    box.hidden = !box.hidden;
    el("btn-toggle-map").textContent = box.hidden ? "Draw area on map" : "Hide map";
    if (box.hidden) return;
    try {
      await ensureLeaflet();
    } catch (e) {
      box.hidden = true;
      el("btn-toggle-map").textContent = "Draw area on map";
      el("geo-summary").textContent =
        "The map could not be loaded (offline?) — geographic filters still work without it.";
      return;
    }
    if (!this.map) this._initMap();
    setTimeout(() => { this.map.invalidateSize(); this._fitToGeo(); }, 60);
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
    // Areas accumulate: draw as many as the feed needs, matched as OR.
    this.map.on(L.Draw.Event.CREATED, (e) => {
      this.drawnItems.addLayer(e.layer);
      if (e.layerType === "circle") {
        const c = e.layer.getLatLng();
        this.geos.push({ type: "Circle", center: [c.lat, c.lng],
                         radius_km: e.layer.getRadius() / 1000 });
      } else {
        this.geos.push(e.layer.toGeoJSON().geometry);
      }
      this._updateGeoSummary();
    });
    // Leaflet.draw hands back only the layers removed, so rebuild the list
    // from what is still on the map.
    this.map.on(L.Draw.Event.DELETED, () => {
      this.geos = this._geosFromLayers();
      this._updateGeoSummary();
    });
    this._renderGeo();
  },

  /* Read the areas back off the map after a deletion. */
  _geosFromLayers() {
    const out = [];
    if (!this.drawnItems) return out;
    this.drawnItems.eachLayer((l) => {
      if (l instanceof L.Circle) {
        const c = l.getLatLng();
        out.push({ type: "Circle", center: [c.lat, c.lng], radius_km: l.getRadius() / 1000 });
      } else if (l.toGeoJSON) {
        out.push(l.toGeoJSON().geometry);
      }
    });
    return out;
  },

  setGeo(geos, renderOnly = false) {
    // Accepts a list, a single area (older saved feeds), or null.
    this.geos = Array.isArray(geos) ? geos.filter(Boolean) : (geos ? [geos] : []);
    this._updateGeoSummary();
    if (this.map) this._renderGeo();
  },

  _renderGeo() {
    if (!this.drawnItems) return;
    this.drawnItems.clearLayers();
    for (const geo of this.geos) {
      if (geo.type === "Circle") {
        this.drawnItems.addLayer(
          L.circle([geo.center[0], geo.center[1]], { radius: geo.radius_km * 1000 })
        );
      } else {
        L.geoJSON({ type: "Feature", geometry: geo })
          .eachLayer(l => this.drawnItems.addLayer(l));
      }
    }
  },

  _fitToGeo() {
    if (this.geos.length && this.drawnItems && this.drawnItems.getLayers().length) {
      this.map.fitBounds(this.drawnItems.getBounds().pad(0.4));
    }
  },

  _updateGeoSummary() {
    const s = el("geo-summary");
    if (!this.geos.length) { s.textContent = "No area set"; el("btn-clear-geo").hidden = true; return; }
    const describe = (g) => (g.type === "Circle"
      ? `circle ${g.radius_km.toFixed(0)} km` : g.type.toLowerCase());
    s.textContent = this.geos.length === 1
      ? `1 area — ${describe(this.geos[0])}`
      : `${this.geos.length} areas — ${this.geos.map(describe).join(", ")}`;
    el("btn-clear-geo").hidden = false;
  },
};


/* ----- small shared helpers ----- */
function el(id) { return document.getElementById(id); }

/* The "s" on a count. Written out everywhere it's needed rather than falling
   back to "article(s)", which reads like a form. */
function plural(n, suffix = "s") { return n === 1 ? "" : suffix; }

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
  const d = new Date(iso);
  const fmt = (typeof Settings !== "undefined" && Settings.get("timefmt")) || "relative";
  const pad = (n) => String(n).padStart(2, "0");
  if (fmt === "local") {
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  if (fmt === "utc") {
    const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${MON[d.getUTCMonth()]} ${d.getUTCDate()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`;
  }
  if (fmt === "dtg") {  // military date-time group: 112036Z JUL 26
    const MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    return `${pad(d.getUTCDate())}${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}Z ${MON[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
  }
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
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
