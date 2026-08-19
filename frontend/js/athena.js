/* Athena — what a Pantheon has actually covered.
   ══════════════════════════════════════════════

   A group writing weekly intelligence reports accumulates a question its own
   reports cannot answer: *what have we been covering, and how often?* Twenty
   documents hold that answer between them and none of them state it. This
   board is where the group files what it wrote, tagged against a vocabulary of
   its own, so the pattern over months becomes something to look at.

   Its own file rather than more of app.js, which is past five and a half
   thousand lines of global scope. Athena is a self-contained sub-application —
   its own board, its own dialogs, its own document parser — and the parser
   alone is three hundred lines that nothing else in Delphi will ever call.

   **The document never leaves the browser.** A .docx is a zip of XML, and both
   halves are unpacked here, on the reader's own machine, using
   DecompressionStream. Only the topics, themes and links a person confirmed
   are sent to Delphi. These are a group's intelligence products; the choice to
   parse client-side is about that and not about saving the server the work.

   The parser is adapted from the standalone tracker this feature came from,
   deliberately closely: its heuristics for what counts as a heading, how a
   colon-separated line reads, and how source clusters group under a label were
   worked out against real documents, and rewriting them from first principles
   would have meant learning the same lessons again. */

/* ═══ .docx extraction: a zip of XML, unpacked in the browser ═══ */

async function inflateRaw(u8) {
  const ds = new DecompressionStream("deflate-raw");
  return new Uint8Array(await new Response(new Blob([u8]).stream().pipeThrough(ds)).arrayBuffer());
}

/* Enough of the ZIP format to read named entries out of a .docx: find the end
   of the central directory, walk its records, and inflate on demand. */
async function unzip(buf) {
  const u8 = new Uint8Array(buf), dv = new DataView(buf);
  let eocd = -1;
  for (let i = u8.length - 22; i >= Math.max(0, u8.length - 22 - 65535); i--)
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  if (eocd < 0) throw new Error("That is not a .docx — it has no zip directory.");
  const count = dv.getUint16(eocd + 10, true);
  let off = dv.getUint32(eocd + 16, true);
  const files = {};
  for (let n = 0; n < count; n++) {
    if (dv.getUint32(off, true) !== 0x02014b50) break;
    const method = dv.getUint16(off + 10, true), csize = dv.getUint32(off + 20, true);
    const nameLen = dv.getUint16(off + 28, true), extraLen = dv.getUint16(off + 30, true),
          cmtLen = dv.getUint16(off + 32, true);
    const lho = dv.getUint32(off + 42, true);
    files[new TextDecoder().decode(u8.subarray(off + 46, off + 46 + nameLen))] =
      { method, csize, lho };
    off += 46 + nameLen + extraLen + cmtLen;
  }
  return { read: async (name) => {
    const f = files[name];
    if (!f) return null;
    const nl = dv.getUint16(f.lho + 26, true), el = dv.getUint16(f.lho + 28, true);
    const data = u8.subarray(f.lho + 30 + nl + el, f.lho + 30 + nl + el + f.csize);
    return new TextDecoder().decode(f.method === 8 ? await inflateRaw(data) : data.slice());
  } };
}

const athDecodeEnt = (s) => s.replace(/&amp;/g, "&").replace(/&lt;/g, "<")
  .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'")
  .replace(/&#(\d+);/g, (_, d) => String.fromCharCode(+d));
/* Word writes a trailing bracket or full stop into a hyperlink target more
   often than you would think. */
const athCleanUrl = (u) => u.replace(/[\\.,;:)\]]+$/, "");

function athRunText(runXml) {
  if (/<w:instrText/.test(runXml)) return { text: "", bold: false, ital: false };
  const prM = runXml.match(/<w:rPr>[\s\S]*?<\/w:rPr>/);
  const pr = prM ? prM[0] : "";
  const bold = /<w:b\s*\/>/.test(pr) || /<w:b\s+w:val="(1|true)"/.test(pr);
  const ital = /<w:i\s*\/>/.test(pr) || /<w:i\s+w:val="(1|true)"/.test(pr);
  let text = "", tm;
  const tRe = /<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>|<w:tab\s*\/>|<w:br\s*\/>/g;
  while ((tm = tRe.exec(runXml))) text += tm[1] !== undefined ? athDecodeEnt(tm[1]) : " ";
  return { text, bold, ital };
}

function athParseRuns(xml, rels) {
  const runs = [];
  const segRe = /<w:hyperlink\b([^>]*)>([\s\S]*?)<\/w:hyperlink>|<w:r[ >][\s\S]*?<\/w:r>/g;
  let sm;
  while ((sm = segRe.exec(xml))) {
    if (sm[1] !== undefined) {
      const idM = sm[1].match(/r:id="([^"]+)"/);
      const url = idM && rels[idM[1]] ? athCleanUrl(rels[idM[1]]) : "";
      const rRe = /<w:r[ >][\s\S]*?<\/w:r>/g;
      let rm;
      while ((rm = rRe.exec(sm[2]))) {
        const t = athRunText(rm[0]);
        if (t.text) runs.push({ ...t, url });
      }
    } else {
      const t = athRunText(sm[0]);
      if (t.text) runs.push({ ...t, url: "" });
    }
  }
  return runs;
}

/* One paragraph becomes {text, links, blank, bold, italic, boldPrefix}. Bold is
   a proportion rather than a flag, because Word will happily mark the space
   after a heading as not-bold and that should not stop it being a heading. */
function athAssembleParas(xml, rels) {
  const paras = [];
  const pRe = /<w:p[ >][\s\S]*?<\/w:p>|<w:p\/>/g;
  let pm;
  while ((pm = pRe.exec(xml))) {
    const runs = athParseRuns(pm[0], rels);
    let text = "", boldChars = 0, italChars = 0, boldPrefix = "", prefixOpen = true;
    const links = [];
    for (const r of runs) {
      text += r.text;
      const solid = r.text.replace(/\s/g, "").length;
      if (r.bold) boldChars += solid;
      if (r.ital) italChars += solid;
      if (prefixOpen) {
        if (r.bold || !solid) { if (r.bold) boldPrefix += r.text; }
        else prefixOpen = false;
      }
      if (r.url) {
        const last = links[links.length - 1];
        if (last && last.url === r.url) last.text += r.text;
        else links.push({ url: r.url, text: r.text });
      }
    }
    text = text.replace(/\s+/g, " ").trim();
    for (const u of text.match(/https?:\/\/[^\s"<>\])]+/g) || []) {
      const cu = athCleanUrl(u);
      if (!links.some((l) => l.url === cu)) links.push({ url: cu, text: "" });
    }
    links.forEach((l) => {
      l.text = l.text.replace(/\s+/g, " ").trim();
      if (/^https?:\/\//.test(l.text)) l.text = "";
    });
    const letters = text.replace(/\s/g, "").length;
    // Tracked-changes artifact: the same sentence written twice, end to end.
    if (letters > 10 && text.length % 2 === 0) {
      const h = text.slice(0, text.length / 2);
      if (h === text.slice(text.length / 2)) text = h;
    }
    paras.push({
      text, links,
      blank: !text && !links.length,
      bold: letters > 0 && boldChars / letters > 0.7,
      italic: letters > 0 && italChars / letters > 0.7,
      boldPrefix: boldPrefix.replace(/\s+/g, " ").trim(),
    });
  }
  return paras;
}

async function athExtractDocx(buf) {
  const zip = await unzip(buf);
  const relsXml = (await zip.read("word/_rels/document.xml.rels")) || "";
  const rels = {};
  const rRe = /<Relationship\b[^>]*>/g;
  let m;
  while ((m = rRe.exec(relsXml))) {
    const id = m[0].match(/\bId="([^"]+)"/), tg = m[0].match(/\bTarget="([^"]+)"/);
    if (id && tg) rels[id[1]] = athDecodeEnt(tg[1]);
  }
  const docXml = await zip.read("word/document.xml");
  if (!docXml) throw new Error("No word/document.xml inside — is that really a Word file?");
  // Headers carry the date on most house templates, so they are read first and
  // prepended: detectDate looks at the opening paragraphs and would otherwise
  // never see it.
  let headParas = [];
  for (const h of ["header1", "header2", "header3"]) {
    const hx = await zip.read("word/" + h + ".xml");
    if (!hx) continue;
    const hRelsXml = (await zip.read("word/_rels/" + h + ".xml.rels")) || "";
    const hRels = {};
    const hRe = /<Relationship\b[^>]*>/g;
    let hm;
    while ((hm = hRe.exec(hRelsXml))) {
      const id = hm[0].match(/\bId="([^"]+)"/), tg = hm[0].match(/\bTarget="([^"]+)"/);
      if (id && tg) hRels[id[1]] = athDecodeEnt(tg[1]);
    }
    headParas = headParas.concat(athAssembleParas(hx, hRels).filter((p) => !p.blank));
  }
  return headParas.concat(athAssembleParas(docXml, rels));
}

/* Plain text and markdown reach the same paragraph shape, so everything
   downstream has one input to understand. */
function athTextToParas(txt) {
  return txt.split(/\r?\n/).map((line) => {
    const raw = line.trim();
    const mdBold = /^[-*•\s]*\*\*(.+?)\*\*:?\s*(.*)$/.exec(raw);
    const links = [];
    const mdLink = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
    let lm;
    while ((lm = mdLink.exec(raw))) links.push({ url: athCleanUrl(lm[2]), text: lm[1] });
    const rest = raw.replace(mdLink, "$1");
    for (const u of rest.match(/https?:\/\/[^\s"<>\])]+/g) || []) {
      const cu = athCleanUrl(u);
      if (!links.some((l) => l.url === cu)) links.push({ url: cu, text: "" });
    }
    const text = rest.replace(/\*\*/g, "").replace(/\s+/g, " ").trim();
    return {
      text, links, blank: !text && !links.length,
      bold: !!mdBold && !mdBold[2],
      italic: /^_[^_]+_$/.test(raw),
      boldPrefix: mdBold ? mdBold[1] : "",
    };
  });
}

/* ═══ making sense of the paragraphs ═══ */

const ATH_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
                    "august", "september", "october", "november", "december"];
const athMonthNum = (s) =>
  ATH_MONTHS.findIndex((m) => m.startsWith(String(s).toLowerCase().slice(0, 3))) + 1;
const athPad = (n) => String(n).padStart(2, "0");

/* Which sort of document this is, from its name. A guess the reader can
   override — it decides only whether the entries are read as topics or as
   source clusters. */
function athClassifyFile(name) {
  if (/notes/i.test(name)) return "notes";
  if (/report/i.test(name)) return "report";
  return null;
}

/* The document's own date, wherever it put it: in the opening paragraphs, in
   the header, or in the filename. A weekly report is filed by the week it
   covers, and getting that wrong puts a whole column in the wrong place. */
function athDetectDate(name, paras, defaultYear) {
  const dy = defaultYear || new Date().getFullYear();
  const text = paras.slice(0, 20).map((p) => p.text).join(" ");
  const alt = "January|February|March|April|May|June|July|August|September|October|November|December";
  let m = new RegExp("(\\d{1,2})\\s+(" + alt + ")\\s+(20\\d{2})", "i").exec(text);
  if (m) return `${m[3]}-${athPad(athMonthNum(m[2]))}-${athPad(+m[1])}`;
  m = new RegExp("(" + alt + ")\\s+(\\d{1,2}),?\\s+(20\\d{2})", "i").exec(text);
  if (m) return `${m[3]}-${athPad(athMonthNum(m[1]))}-${athPad(+m[2])}`;
  const base = name.replace(/\.[a-z0-9]+$/i, "");
  const abbr = alt + "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec";
  m = new RegExp("(\\d{1,2})[_\\s-]+(" + abbr + ")[a-z]*", "i").exec(base);
  if (m) return `${dy}-${athPad(athMonthNum(m[2]))}-${athPad(+m[1])}`;
  m = new RegExp("(" + abbr + ")[a-z]*[_\\s-]+(\\d{1,2})", "i").exec(base);
  if (m) return `${dy}-${athPad(athMonthNum(m[1]))}-${athPad(+m[2])}`;
  m = /(\d{4})-(\d{2})-(\d{2})/.exec(base);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;
  m = /(\d{1,2})-(\d{1,2})-(\d{2,4})/.exec(base);
  if (m) {
    const y = m[3].length === 2 ? 2000 + +m[3] : +m[3];
    return `${y}-${athPad(+m[1])}-${athPad(+m[2])}`;
  }
  return null;
}

/* Lines every house template puts on every document. They are not topics, and
   left in they would be the most-covered "theme" the group has. */
const ATH_BOILER = /^(weekly intel|weekly report|internal information|additional context|sources?$|summary$|overview$|bluf|prepared by|table of contents|contents$|classification)/i;
const athIsDateLine = (t) =>
  /^\d{1,2}\s+\w+\s+20\d{2}$/.test(t) || /^\w+\s+\d{1,2},?\s+20\d{2}$/.test(t);

const athStem = (w) => w.replace(/s$/, "").replace(/ship$/, "").replace(/(.{5,})ing$/, "$1");
const athTokens = (t) => new Set(String(t).toLowerCase().replace(/[^a-z0-9\s]/g, " ")
  .split(/\s+/).filter((w) => w.length > 3).map(athStem));

/* How alike two headings are, for collapsing the summary bullet at the top of a
   report with the section further down that repeats it. */
function athSimilar(a, b) {
  const A = athTokens(a), B = athTokens(b);
  if (!A.size || !B.size) return 0;
  let inter = 0;
  for (const w of A) if (B.has(w)) inter++;
  return inter / Math.min(A.size, B.size);
}

/* A report's topics. Two shapes are common and the document usually commits to
   one: bold headings with prose beneath, or "Heading: one line about it". */
function athParseReport(paras) {
  const boldItems = [], colonItems = [];
  let current = null;
  for (const p of paras) {
    if (p.blank) continue;
    const t = p.text;
    if (!t || ATH_BOILER.test(t) || athIsDateLine(t)) { current = null; continue; }
    if (p.bold && t.length >= 15 && t.length <= 180) {
      current = { title: t.replace(/[:\s]+$/, ""), body: "", links: p.links.slice(), kind: "bold" };
      boldItems.push(current);
      continue;
    }
    const ci = t.indexOf(": ");
    if (ci >= 20 && ci <= 170 && p.boldPrefix.length >= 0.5 * ci) {
      colonItems.push({ title: t.slice(0, ci).replace(/[:\s]+$/, ""),
                        body: t.slice(ci + 2).trim(), links: p.links.slice(), kind: "colon" });
      continue;
    }
    if (current && current.kind === "bold") {
      if (current.body.length < 60) current.body = (current.body + " " + t).trim();
      current.links.push(...p.links);
    }
  }
  const items = boldItems.length >= 2 ? boldItems
    : (boldItems.length === 1 && colonItems.length === 0) ? boldItems : colonItems;
  const out = [];
  for (const it of items) {
    const dup = out.find((o) => athSimilar(o.title, it.title) >= 0.6);
    if (dup) {
      if (it.body.length > dup.body.length) dup.body = it.body;
      dup.links.push(...it.links.filter((l) => !dup.links.some((x) => x.url === l.url)));
    } else out.push(it);
  }
  out.forEach((it) => {
    delete it.kind;
    it.body = it.body.replace(/\s+/g, " ").slice(0, 600);
    it.links = it.links.filter((l, i, a) => a.findIndex((x) => x.url === l.url) === i)
      .map((l) => ({ t: l.text || "", u: l.url }));
  });
  return out.filter((it) => !ATH_BOILER.test(it.title));
}

/* Source notes: links grouped under whatever labelled them. Blank lines split
   groups, and inside a group a bold line or a short line followed by links is
   read as the label for what comes after. */
function athParseNotes(paras) {
  const groups = [];
  let g = [];
  for (const p of paras) {
    if (p.blank) { if (g.length) { groups.push(g); g = []; } }
    else g.push(p);
  }
  if (g.length) groups.push(g);

  const clusters = [];
  for (const grp of groups) {
    let cl = null;
    const startNew = (label) => { cl = { title: label || "", links: [], body: "" }; clusters.push(cl); };
    for (let i = 0; i < grp.length; i++) {
      const p = grp[i], hasLinks = p.links.length > 0, prev = grp[i - 1], next = grp[i + 1];
      const preText = p.links.reduce((t, l) => (l.text ? t.replace(l.text, "") : t), p.text)
        .replace(/\s+/g, " ").trim();
      if (p.bold && !hasLinks) { startNew(p.text); continue; }
      if (!hasLinks) {
        const labelish = !cl || (next && next.links.length
          && (!prev || prev.links.length > 0) && p.text.length < 120 && !p.italic);
        if (labelish) startNew(p.text);
        else cl.body = (cl.body ? cl.body + " " : "") + p.text;
        continue;
      }
      if (!cl) startNew(preText || p.links[0].text || "");
      else if (preText && cl.links.length && !cl.title) cl.title = preText;
      if (preText && !cl.body && cl.links.length && preText !== cl.title) cl.body = preText;
      else if (preText && !cl.title) cl.title = preText;
      for (const l of p.links)
        if (!cl.links.some((s) => s.url === l.url)) cl.links.push({ url: l.url, text: l.text });
    }
  }
  clusters.forEach((c) => {
    if (!c.title)
      c.title = (c.links[0] && c.links[0].text) ? c.links[0].text : (c.body.slice(0, 80) || "Untitled");
    c.title = c.title.replace(/[:\s]+$/, "").slice(0, 160);
    c.body = c.body.replace(/\s+/g, " ").trim().slice(0, 600);
    c.links = c.links.map((s) => ({ t: s.text || "", u: s.url }));
  });
  return clusters.filter((c) => c.links.length || c.body || (c.title && c.title !== "Untitled"));
}

/* Which themes this text looks like, from the keywords the group gave them.

   A suggestion and nothing more: every one is shown ticked-or-not in the review
   dialog and a person decides. A coverage figure is only worth reading if
   somebody agreed to each tag behind it, and a keyword match is a guess about
   wording rather than a judgement about subject. */
function athSuggestThemes(text, themes) {
  const hay = " " + String(text).toLowerCase() + " ";
  const hits = [];
  for (const t of themes) {
    let score = 0;
    for (const k of (t.keywords || [])) {
      if (k && hay.includes(k.toLowerCase())) score += 2;
    }
    // The theme's own name, as a fallback for a theme nobody gave keywords to.
    const name = String(t.name || "").toLowerCase();
    if (name.length > 4 && hay.includes(name)) score += 3;
    if (score > 0) hits.push({ slug: t.slug, score });
  }
  return hits.sort((a, b) => b.score - a.score).slice(0, 4).map((h) => h.slug);
}

/* ═══ the board ═══ */

const AthenaBoard = {
  pid: null,
  data: null,          // {domains, themes, documents, can_manage}
  tab: "coverage",
  gran: "week",
  fromIdx: 0,
  toIdx: 1e9,
  _seq: 0,

  async open(pantheonId) {
    this.pid = pantheonId;
    this.tab = "coverage";
    const generation = ++this._seq;
    const body = el("athena-body");
    body.textContent = "Loading…";
    try {
      const data = await API.athena(pantheonId);
      if (generation !== this._seq) return;   // they left while it loaded
      this.data = data;
    } catch (e) {
      if (generation !== this._seq) return;
      body.textContent = "";
      body.appendChild(athNote("Athena could not be loaded: " + e.message));
      return;
    }
    // Shown once per Pantheon, and only while there is nothing here: an empty
    // grid explains nothing about itself, and a group that has already filed
    // documents plainly does not need telling what this is.
    if (!this.data.themes.length && !this.data.documents.length
        && !athSeen(`gnd_athena_intro_${pantheonId}`)) {
      openAthenaIntro(pantheonId);
    }
    this.render();
  },

  close() { this._seq++; this.data = null; },

  /* Every document that is a report, oldest first — the columns of the grid
     come from these, so notes filed against a week do not create a column of
     their own. */
  reports() {
    return (this.data.documents || []).filter((d) => d.kind === "report");
  },

  themeBySlug(slug) {
    return (this.data.themes || []).find((t) => t.slug === slug) || null;
  },

  domainColor(slug) {
    const d = (this.data.domains || []).find((x) => x.slug === slug);
    return d ? d.color : "var(--ink-muted)";
  },

  /* The grid: themes down the side, time buckets across the top, and the
     entries that fall in each. Computed here rather than on the server because
     every input is already in hand and the reader changes the granularity far
     more often than the data changes. */
  view() {
    const reports = this.reports();
    const fromIdx = Math.max(0, Math.min(this.fromIdx, reports.length - 1));
    const toIdx = Math.max(fromIdx, Math.min(this.toIdx, reports.length - 1));
    const active = reports.slice(fromIdx, toIdx + 1);
    let buckets = [];
    if (this.gran === "week") {
      buckets = active.map((r) => ({ key: r.date, label: athShortDate(r.date), dates: [r.date] }));
    } else if (this.gran === "biweek") {
      for (let i = 0; i < active.length; i += 2) {
        const pair = active.slice(i, i + 2);
        buckets.push({ key: pair[0].date,
                       label: pair.map((r) => athShortDate(r.date)).join(" – "),
                       dates: pair.map((r) => r.date) });
      }
    } else {
      const m = new Map();
      active.forEach((r) => {
        const k = r.date.slice(0, 7);
        if (!m.has(k)) m.set(k, { key: k, label: athMonthLabel(k), dates: [] });
        m.get(k).dates.push(r.date);
      });
      buckets = [...m.values()];
    }
    const bucketOf = (d) => {
      for (const b of buckets) if (d >= b.dates[0] && d <= b.dates[b.dates.length - 1]) return b.key;
      let last = buckets.length ? buckets[0].key : null;
      for (const b of buckets) if (b.dates[0] <= d) last = b.key;
      return last;
    };
    const grid = {}, totals = {};
    for (const t of this.data.themes) {
      grid[t.slug] = {};
      totals[t.slug] = 0;
      for (const b of buckets) grid[t.slug][b.key] = [];
    }
    let entryCount = 0, max = 1;
    for (const r of active) {
      const bk = bucketOf(r.date);
      for (const entry of r.entries) {
        entryCount++;
        for (const slug of entry.themes) {
          if (grid[slug] && grid[slug][bk]) {
            grid[slug][bk].push({ doc: r, entry });
            totals[slug]++;
          }
        }
      }
    }
    for (const row of Object.values(grid))
      for (const cell of Object.values(row)) if (cell.length > max) max = cell.length;
    const from = active.length ? active[0].date : "0000";
    const to = active.length ? active[active.length - 1].date : "9999";
    const notes = (this.data.documents || []).filter(
      (d) => d.kind === "notes" && (d.week || d.date) >= from && (d.week || d.date) <= to);
    return { reports, active, notes, buckets, grid, totals, max, entryCount, fromIdx, toIdx };
  },

  render() {
    if (!this.data) return;
    const v = this.view();
    el("athena-sub").textContent =
      `${this.reports().length} report${plural(this.reports().length)} · `
      + `${this.data.documents.length - this.reports().length} note file`
        + plural(this.data.documents.length - this.reports().length)
      + ` · ${this.data.themes.length} theme${plural(this.data.themes.length)}`;
    for (const b of el("athena-tabs").querySelectorAll("button"))
      b.classList.toggle("active", b.dataset.tab === this.tab);
    el("athena-controls").hidden = this.tab !== "coverage";
    if (this.tab === "coverage") this.renderRangeControls(v);
    el("athena-stats").textContent = v.entryCount
      ? `${v.entryCount} topic${plural(v.entryCount)} across ${v.buckets.length} column`
        + plural(v.buckets.length)
      : "";

    const body = el("athena-body");
    body.replaceChildren();
    if (this.tab === "coverage") body.appendChild(this.renderCoverage(v));
    else if (this.tab === "sources") body.appendChild(this.renderSources(v));
    else if (this.tab === "documents") body.appendChild(this.renderDocuments());
    else body.appendChild(this.renderThemes());
  },

  renderRangeControls(v) {
    const reports = v.reports;
    for (const [id, idx] of [["athena-from", v.fromIdx], ["athena-to", v.toIdx]]) {
      const sel = el(id);
      sel.replaceChildren();
      reports.forEach((r, i) => {
        const o = document.createElement("option");
        o.value = String(i);
        o.textContent = athShortDate(r.date) + " " + r.date.slice(0, 4);
        sel.appendChild(o);
      });
      sel.value = String(Math.min(idx, Math.max(0, reports.length - 1)));
      sel.disabled = reports.length < 2;
    }
    el("athena-gran").value = this.gran;
  },

  renderCoverage(v) {
    const wrap = document.createElement("div");
    if (!this.data.themes.length) {
      wrap.appendChild(athNote(
        "No themes yet. The grid is themes down the side and weeks across the "
        + "top, so it needs a vocabulary before it can show you anything — open "
        + "🏷 Themes to write yours."));
      return wrap;
    }
    if (!v.active.length) {
      wrap.appendChild(athNote(
        "No reports filed yet. Press ＋ File a document to add the first one; "
        + "your themes are ready for it."));
      return wrap;
    }

    const table = document.createElement("table");
    table.className = "ath-matrix";
    const head = document.createElement("thead");
    const hr = document.createElement("tr");
    const corner = document.createElement("th");
    corner.className = "ath-corner";
    corner.textContent = "Theme";
    hr.appendChild(corner);
    for (const b of v.buckets) {
      const th = document.createElement("th");
      th.className = "ath-colhead";
      th.textContent = b.label;
      hr.appendChild(th);
    }
    const totalHead = document.createElement("th");
    totalHead.className = "ath-colhead ath-total";
    totalHead.textContent = "All";
    hr.appendChild(totalHead);
    head.appendChild(hr);
    table.appendChild(head);

    const tbody = document.createElement("tbody");
    // Grouped by domain, in the order the domains were made, with themes that
    // belong to none of them last — a theme without a domain is usually one
    // somebody added in a hurry, and burying it would hide that.
    const order = [...(this.data.domains || []).map((d) => d.slug), ""];
    for (const domainSlug of order) {
      const themes = this.data.themes.filter((t) => (t.domain || "") === domainSlug);
      if (!themes.length) continue;
      const domain = (this.data.domains || []).find((d) => d.slug === domainSlug);
      const dr = document.createElement("tr");
      dr.className = "ath-domain-row";
      const dc = document.createElement("th");
      dc.colSpan = v.buckets.length + 2;
      dc.textContent = domain ? domain.name : "Ungrouped";
      dc.style.color = domain ? domain.color : "var(--ink-muted)";
      dr.appendChild(dc);
      tbody.appendChild(dr);

      for (const t of themes) {
        const tr = document.createElement("tr");
        const th = document.createElement("th");
        th.className = "ath-rowhead";
        th.style.borderLeftColor = this.domainColor(t.domain);
        th.textContent = t.name;
        if (t.blurb) th.title = t.blurb;
        tr.appendChild(th);
        for (const b of v.buckets) {
          const cell = v.grid[t.slug][b.key] || [];
          const td = document.createElement("td");
          td.className = "ath-cell";
          if (cell.length) {
            const btn = document.createElement("button");
            btn.className = "ath-hit";
            btn.textContent = String(cell.length);
            // Shaded by how busy the cell is relative to the busiest one on
            // screen, so the eye finds the concentrations rather than reading
            // every number.
            btn.style.background = athHeat(cell.length / v.max, this.domainColor(t.domain));
            btn.title = `${t.name} · ${b.label} — ${cell.length} topic${plural(cell.length)}`;
            btn.onclick = () => AthenaBoard.showCell(t, b, cell);
            td.appendChild(btn);
          }
          tr.appendChild(td);
        }
        const total = document.createElement("td");
        total.className = "ath-cell ath-total";
        total.textContent = v.totals[t.slug] ? String(v.totals[t.slug]) : "";
        tr.appendChild(total);
        tbody.appendChild(tr);
      }
    }
    table.appendChild(tbody);
    const scroll = document.createElement("div");
    scroll.className = "ath-scroll";
    scroll.appendChild(table);
    wrap.appendChild(scroll);
    wrap.appendChild(athNote(
      "Each cell is how many topics in that period were tagged with that theme. "
      + "Press one to see which."));
    return wrap;
  },

  showCell(theme, bucket, cell) {
    const box = document.createElement("div");
    const h = document.createElement("h3");
    h.textContent = `${theme.name} · ${bucket.label}`;
    box.appendChild(h);
    for (const { doc, entry } of cell) {
      box.appendChild(athEntryCard(entry, doc));
    }
    openAthenaDetail(box);
  },

  renderSources(v) {
    const wrap = document.createElement("div");
    // Every link filed in range, grouped by the site it points at. The
    // question this answers is "who have we been reading", which is a
    // different one from "what have we written about".
    const byHost = new Map();
    const consider = [...v.active, ...v.notes];
    for (const doc of consider) {
      for (const entry of doc.entries) {
        for (const link of entry.links) {
          let host;
          try { host = new URL(link.u).hostname.replace(/^www\./, ""); }
          catch (e) { continue; }
          if (!byHost.has(host)) byHost.set(host, []);
          byHost.get(host).push({ link, entry, doc });
        }
      }
    }
    if (!byHost.size) {
      wrap.appendChild(athNote(
        "No sources in range. Links found in the documents you file appear here, "
        + "grouped by where they came from."));
      return wrap;
    }
    const hosts = [...byHost.entries()].sort((a, b) => b[1].length - a[1].length);
    const total = hosts.reduce((n, [, list]) => n + list.length, 0);
    wrap.appendChild(athNote(
      `${total} link${plural(total)} from ${hosts.length} site${plural(hosts.length)}, `
      + "most-cited first."));
    for (const [host, list] of hosts) {
      const card = document.createElement("details");
      card.className = "ath-card";
      const summary = document.createElement("summary");
      const name = document.createElement("b");
      name.textContent = host;
      const count = document.createElement("span");
      count.className = "s-meta";
      count.textContent = ` ${list.length} link${plural(list.length)}`;
      summary.append(name, count);
      card.appendChild(summary);
      for (const { link, doc } of list.slice(0, 200)) {
        const row = document.createElement("div");
        row.className = "ath-source";
        const a = document.createElement("a");
        const href = safeUrl(link.u);
        if (href) { a.href = href; a.target = "_blank"; a.rel = "noopener"; }
        a.textContent = link.t || link.u;
        const meta = document.createElement("span");
        meta.className = "s-meta";
        meta.textContent = ` ${athShortDate(doc.date)}`;
        row.append(a, meta);
        card.appendChild(row);
      }
      wrap.appendChild(card);
    }
    return wrap;
  },

  renderDocuments() {
    const wrap = document.createElement("div");
    const docs = [...(this.data.documents || [])].reverse();   // newest first
    if (!docs.length) {
      wrap.appendChild(athNote(
        "Nothing filed yet. ＋ File a document takes a .docx, .md or .txt — it is "
        + "read on your own machine and only what you confirm is sent."));
      return wrap;
    }
    for (const doc of docs) {
      const card = document.createElement("details");
      card.className = "ath-card";
      const summary = document.createElement("summary");
      const kind = document.createElement("span");
      kind.className = "ath-kind";
      kind.textContent = doc.kind === "notes" ? "🔗 Notes" : "📄 Report";
      const label = document.createElement("b");
      label.textContent = doc.label || doc.filename || athShortDate(doc.date);
      const meta = document.createElement("span");
      meta.className = "s-meta";
      meta.textContent = ` ${doc.date} · ${doc.entries.length} item`
        + plural(doc.entries.length)
        + (doc.uploaded_by ? ` · filed by ${doc.uploaded_by}` : "");
      summary.append(kind, label, meta);
      card.appendChild(summary);
      for (const entry of doc.entries) card.appendChild(athEntryCard(entry, null, this));

      const foot = document.createElement("div");
      foot.className = "ath-card-foot";
      const remove = document.createElement("button");
      remove.className = "btn small";
      remove.textContent = "Remove";
      remove.title = "Take this document out of the coverage figures";
      remove.onclick = async () => {
        if (!confirm(`Remove “${doc.label || doc.filename}”?\n\n`
                     + "Its topics stop counting towards every theme it was "
                     + "tagged with. The document itself was never stored.")) return;
        try {
          await API.athenaUnfile(AthenaBoard.pid, doc.id);
          await AthenaBoard.reload();
        } catch (e) { toast("Couldn't remove that document", e.message); }
      };
      foot.appendChild(remove);
      card.appendChild(foot);
      wrap.appendChild(card);
    }
    return wrap;
  },

  renderThemes() {
    const wrap = document.createElement("div");
    const can = this.data.can_manage;
    wrap.appendChild(athNote(can
      ? "Your group's vocabulary. Everything filed is tagged against these, and "
        + "the coverage grid counts them. Group themes into domains — each domain "
        + "takes a colour on the grid — and give each one keywords so uploads can "
        + "suggest tags for you to confirm."
      : "Your group's vocabulary. An owner or admin can add and change these."));

    if (can) {
      const form = document.createElement("div");
      form.className = "ath-theme-form";
      const name = athInput("Theme name", "e.g. Supply chain & tariffs");
      const domain = athInput("Domain (optional)", "e.g. Operations");
      const keywords = athInput("Keywords, comma separated (optional)",
                                "tariff, supplier, shortage");
      const add = document.createElement("button");
      add.className = "btn btn-primary small";
      add.textContent = "Add theme";
      add.onclick = async () => {
        const value = name.querySelector("input").value.trim();
        if (!value) return;
        try {
          await API.athenaAddTheme(AthenaBoard.pid, {
            name: value,
            domain: domain.querySelector("input").value.trim(),
            keywords: keywords.querySelector("input").value
              .split(",").map((s) => s.trim()).filter(Boolean),
          });
          await AthenaBoard.reload();
        } catch (e) { toast("Couldn't add that theme", e.message); }
      };
      form.append(name, domain, keywords, add);
      wrap.appendChild(form);
    }

    if (!this.data.themes.length) {
      wrap.appendChild(athNote("No themes yet."));
      return wrap;
    }
    for (const t of this.data.themes) {
      const row = document.createElement("div");
      row.className = "ath-theme-row";
      const dot = document.createElement("span");
      dot.className = "ath-dot";
      dot.style.background = this.domainColor(t.domain);
      const name = document.createElement("b");
      name.textContent = t.name;
      const meta = document.createElement("span");
      meta.className = "s-meta";
      const domain = (this.data.domains || []).find((d) => d.slug === t.domain);
      meta.textContent = [domain ? domain.name : "ungrouped",
                          (t.keywords || []).length
                            ? `${t.keywords.length} keyword${plural(t.keywords.length)}`
                            : "no keywords"].join(" · ");
      row.append(dot, name, meta);
      if (can) {
        const remove = document.createElement("button");
        remove.className = "btn small";
        remove.textContent = "Delete";
        remove.onclick = async () => {
          if (!confirm(`Delete the theme “${t.name}”?\n\n`
                       + "It is removed from every topic already tagged with it, "
                       + "and its column of coverage figures goes with it.")) return;
          try {
            await API.athenaDeleteTheme(AthenaBoard.pid, t.slug);
            await AthenaBoard.reload();
          } catch (e) { toast("Couldn't delete that theme", e.message); }
        };
        row.appendChild(remove);
      }
      wrap.appendChild(row);
    }
    return wrap;
  },

  async reload() {
    this.data = await API.athena(this.pid);
    this.render();
  },
};

/* ═══ small shared pieces ═══ */

function athNote(text) {
  const p = document.createElement("p");
  p.className = "set-note";
  p.textContent = text;
  return p;
}

function athInput(label, placeholder) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  const span = document.createElement("span");
  span.textContent = label;
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = placeholder || "";
  wrap.append(span, input);
  return wrap;
}

function athSeen(key) {
  try {
    if (localStorage.getItem(key)) return true;
    localStorage.setItem(key, "1");
    return false;
  } catch (e) { return true; }   // private mode: better silent than every load
}

const ATH_MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const athShortDate = (d) =>
  `${+String(d).slice(8, 10)} ${ATH_MONTH_SHORT[+String(d).slice(5, 7) - 1] || "?"}`;
const athMonthLabel = (ym) =>
  `${ATH_MONTH_SHORT[+String(ym).slice(5, 7) - 1] || "?"} ${String(ym).slice(0, 4)}`;

/* A cell's shade, from how busy it is against the busiest on screen. Alpha on
   the domain's own colour rather than a separate scale, so a row stays
   recognisably its domain's colour however hot it gets. */
function athHeat(fraction, color) {
  const alpha = 0.18 + 0.62 * Math.min(1, Math.max(0, fraction));
  return `color-mix(in srgb, ${color} ${Math.round(alpha * 100)}%, transparent)`;
}

/* One filed item, as a card. Built as nodes throughout: every string here came
   out of somebody's Word document. */
function athEntryCard(entry, doc, board) {
  const card = document.createElement("div");
  card.className = "ath-entry";
  const title = document.createElement("div");
  title.className = "ath-entry-title";
  title.textContent = entry.title || "(untitled)";
  card.appendChild(title);
  if (doc) {
    const meta = document.createElement("div");
    meta.className = "s-meta";
    meta.textContent = `${doc.label || doc.filename || ""} · ${athShortDate(doc.date)}`.trim();
    card.appendChild(meta);
  }
  if (entry.body) {
    const body = document.createElement("p");
    body.className = "ath-entry-body";
    body.textContent = entry.body;
    card.appendChild(body);
  }
  if ((entry.themes || []).length) {
    const row = document.createElement("div");
    row.className = "ath-chips";
    for (const slug of entry.themes) {
      const theme = (board || AthenaBoard).themeBySlug(slug);
      const chip = document.createElement("span");
      chip.className = "ath-chip";
      chip.textContent = theme ? theme.name : slug;
      if (theme) chip.style.borderColor = (board || AthenaBoard).domainColor(theme.domain);
      row.appendChild(chip);
    }
    card.appendChild(row);
  }
  for (const link of (entry.links || [])) {
    const a = document.createElement("a");
    a.className = "ath-link";
    const href = safeUrl(link.u);
    if (href) { a.href = href; a.target = "_blank"; a.rel = "noopener"; }
    a.textContent = link.t || link.u;
    card.appendChild(a);
  }
  return card;
}

/* ═══ filing a document ═══ */

const AthenaFiler = {
  pending: null,     // {kind, date, label, filename, entries:[…]}

  /* Read the file here, on this machine. Nothing is uploaded at any point in
     this function — the .docx is unzipped, its XML read, and the buffer
     dropped. Only what comes out of the review dialog is ever sent. */
  async pick() {
    if (!AthenaBoard.data) return;
    if (!AthenaBoard.data.themes.length
        && !confirm("You have no themes yet, so nothing can be tagged and the "
                    + "coverage grid will stay empty.\n\nFile this document "
                    + "anyway?")) {
      AthenaBoard.tab = "themes";
      AthenaBoard.render();
      return;
    }
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".docx,.md,.txt,.markdown,text/plain,text/markdown,"
      + "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    input.onchange = async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      try { await AthenaFiler.read(file); }
      catch (e) { toast("Couldn't read that document", e.message); }
    };
    input.click();
  },

  async read(file) {
    const name = file.name || "document";
    let paras;
    if (/\.docx$/i.test(name)) {
      if (typeof DecompressionStream === "undefined")
        throw new Error("This browser cannot unpack a .docx. Save it as .txt or "
                        + ".md and file that instead.");
      paras = await athExtractDocx(await file.arrayBuffer());
    } else {
      paras = athTextToParas(await file.text());
    }
    const kind = athClassifyFile(name) || "report";
    const raw = kind === "notes" ? athParseNotes(paras) : athParseReport(paras);
    if (!raw.length)
      throw new Error("Nothing in that document looked like a topic. Bold "
                      + "headings, or “Heading: a line about it”, are what it "
                      + "looks for.");
    const themes = AthenaBoard.data.themes;
    this.pending = {
      kind,
      date: athDetectDate(name, paras, new Date().getFullYear())
            || new Date().toISOString().slice(0, 10),
      label: name.replace(/\.[a-z0-9]+$/i, "").replace(/[_-]+/g, " ").trim().slice(0, 200),
      filename: name.slice(0, 200),
      entries: raw.map((item) => ({
        title: item.title,
        body: item.body || "",
        links: item.links || [],
        themes: athSuggestThemes(`${item.title} ${item.body || ""}`, themes),
        keep: true,
      })),
    };
    this.renderReview();
    el("athena-review-backdrop").hidden = false;
  },

  renderReview() {
    const p = this.pending;
    const body = el("athena-review-body");
    body.replaceChildren();

    const head = document.createElement("div");
    head.className = "ath-review-head";
    const kind = document.createElement("label");
    kind.className = "field";
    const kindSpan = document.createElement("span");
    kindSpan.textContent = "This document is";
    const kindSel = document.createElement("select");
    for (const [value, text] of [["report", "📄 A report — its topics"],
                                 ["notes", "🔗 Source notes — what we read"]]) {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = text;
      kindSel.appendChild(o);
    }
    kindSel.value = p.kind;
    kindSel.onchange = () => { p.kind = kindSel.value; };
    kind.append(kindSpan, kindSel);

    const date = document.createElement("label");
    date.className = "field";
    const dateSpan = document.createElement("span");
    dateSpan.textContent = "Dated";
    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.value = p.date;
    dateInput.onchange = () => { p.date = dateInput.value; };
    date.append(dateSpan, dateInput);

    const label = athInput("Called", "");
    label.querySelector("input").value = p.label;
    label.querySelector("input").oninput = (e) => { p.label = e.target.value; };

    head.append(kind, date, label);
    body.appendChild(head);
    body.appendChild(athNote(
      "Everything below was guessed from your document — which paragraphs were "
      + "topics, and which themes they belong to. Nothing is filed until you say "
      + "so, and a coverage figure is only worth reading if somebody agreed to "
      + "the tags behind it."));

    p.entries.forEach((entry, i) => {
      const card = document.createElement("div");
      card.className = "ath-review-item" + (entry.keep ? "" : " ath-dropped");

      const top = document.createElement("label");
      top.className = "ath-review-top";
      const keep = document.createElement("input");
      keep.type = "checkbox";
      keep.checked = entry.keep;
      keep.onchange = () => {
        entry.keep = keep.checked;
        card.classList.toggle("ath-dropped", !entry.keep);
        AthenaFiler.updateCount();
      };
      const title = document.createElement("input");
      title.type = "text";
      title.className = "ath-review-title";
      title.value = entry.title;
      title.oninput = () => { entry.title = title.value; };
      top.append(keep, title);
      card.appendChild(top);

      if (entry.body) {
        const gist = document.createElement("p");
        gist.className = "ath-entry-body";
        gist.textContent = entry.body;
        card.appendChild(gist);
      }

      const chips = document.createElement("div");
      chips.className = "ath-chips";
      for (const theme of AthenaBoard.data.themes) {
        const on = entry.themes.includes(theme.slug);
        const chip = document.createElement("button");
        chip.className = "ath-chip ath-chip-btn" + (on ? " on" : "");
        chip.textContent = theme.name;
        chip.style.borderColor = AthenaBoard.domainColor(theme.domain);
        chip.onclick = () => {
          const at = entry.themes.indexOf(theme.slug);
          if (at >= 0) entry.themes.splice(at, 1);
          else entry.themes.push(theme.slug);
          chip.classList.toggle("on", at < 0);
        };
        chips.appendChild(chip);
      }
      card.appendChild(chips);

      if (entry.links.length) {
        const meta = document.createElement("div");
        meta.className = "s-meta";
        meta.textContent = `${entry.links.length} link${plural(entry.links.length)}`;
        card.appendChild(meta);
      }
      body.appendChild(card);
    });
    this.updateCount();
  },

  updateCount() {
    const kept = this.pending.entries.filter((e) => e.keep).length;
    el("athena-review-note").textContent =
      `${kept} of ${this.pending.entries.length} item`
      + plural(this.pending.entries.length) + " will be filed";
    el("btn-athena-review-file").disabled = kept === 0;
  },

  async file() {
    const p = this.pending;
    const entries = p.entries.filter((e) => e.keep).map((e) => ({
      title: e.title, body: e.body, themes: e.themes, links: e.links,
    }));
    if (!entries.length) return;
    try {
      await API.athenaFile(AthenaBoard.pid, {
        kind: p.kind, date: p.date, label: p.label, filename: p.filename,
        week: p.kind === "notes" ? p.date : "", entries,
      });
      el("athena-review-backdrop").hidden = true;
      this.pending = null;
      await AthenaBoard.reload();
      toast("Filed", `${entries.length} item${plural(entries.length)} added to Athena`);
    } catch (e) {
      toast("Couldn't file that document", e.message);
    }
  },
};

/* ═══ dialogs and wiring ═══ */

function openAthenaIntro() {
  el("athena-intro-backdrop").hidden = false;
}

function openAthenaDetail(node) {
  const box = el("athena-review-body");
  box.replaceChildren(node);
  el("athena-review-note").textContent = "";
  el("btn-athena-review-file").hidden = true;
  el("btn-athena-review-cancel").textContent = "Close";
  el("athena-review-title").textContent = "In this cell";
  el("athena-review-backdrop").hidden = false;
}

function closeAthenaReview() {
  el("athena-review-backdrop").hidden = true;
  AthenaFiler.pending = null;
  // The detail view borrows this dialog, so put its own controls back.
  el("btn-athena-review-file").hidden = false;
  el("btn-athena-review-cancel").textContent = "Cancel";
  el("athena-review-title").textContent = "Check before filing";
}

function wireAthena() {
  el("btn-athena-add").onclick = () => AthenaFiler.pick();
  el("btn-athena-help").onclick = () => openAthenaIntro();
  el("btn-close-athena-intro").onclick = () => { el("athena-intro-backdrop").hidden = true; };
  el("btn-athena-intro-ok").onclick = () => {
    el("athena-intro-backdrop").hidden = true;
    AthenaBoard.tab = "themes";
    AthenaBoard.render();
  };
  el("athena-intro-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("athena-intro-backdrop")) el("athena-intro-backdrop").hidden = true;
  });
  el("btn-close-athena-review").onclick = closeAthenaReview;
  el("btn-athena-review-cancel").onclick = closeAthenaReview;
  el("btn-athena-review-file").onclick = () => AthenaFiler.file();
  el("athena-review-backdrop").addEventListener("mousedown", (e) => {
    if (e.target === el("athena-review-backdrop")) closeAthenaReview();
  });
  el("athena-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    AthenaBoard.tab = b.dataset.tab;
    AthenaBoard.render();
  });
  el("athena-gran").onchange = (e) => { AthenaBoard.gran = e.target.value; AthenaBoard.render(); };
  el("athena-from").onchange = (e) => { AthenaBoard.fromIdx = +e.target.value; AthenaBoard.render(); };
  el("athena-to").onchange = (e) => { AthenaBoard.toIdx = +e.target.value; AthenaBoard.render(); };
}
