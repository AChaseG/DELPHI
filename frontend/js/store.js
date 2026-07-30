/* Local store: the feed cache, kept on the reader's own machine.

   The in-memory cache made a *switch* between boards instant but died with the
   tab, so every reload re-queried every column — around thirty requests to
   repaint a board the reader had just been looking at, all of them landing on
   one small server. IndexedDB moves that work to the machine that is already
   sitting in front of the person: a reload paints from local disk in
   milliseconds and asks the server for nothing until something is actually
   stale, and the news stays readable when the server is down.

   Three rules keep this from becoming its own problem:

     · Bounded. At most MAX_ENTRIES columns and MAX_BYTES of articles; the
       least recently written go first. A cache is not an archive.
     · Scoped to the account. Records carry the account key they were written
       for and a foreign key is ignored and cleared, so signing in as someone
       else on a shared machine never shows their news.
     · Forgettable. Signing out erases it. This is somebody's reading history
       sitting on a disk that may not be theirs alone.

   Everything here degrades to a no-op: private-browsing modes and full disks
   make IndexedDB throw, and the app must not care. */
const Store = {
  DB: "delphi",
  VERSION: 1,
  STORE: "feeds",
  MAX_ENTRIES: 120,
  MAX_BYTES: 8 * 1024 * 1024,

  _db: null,
  _broken: false,

  async _open() {
    if (this._db) return this._db;
    if (this._broken || !self.indexedDB) return null;
    try {
      this._db = await new Promise((resolve, reject) => {
        const req = indexedDB.open(this.DB, this.VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains(this.STORE))
            db.createObjectStore(this.STORE, { keyPath: "key" });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
        req.onblocked = () => reject(new Error("blocked"));
      });
      return this._db;
    } catch (e) {
      this._broken = true;    // no storage available; carry on without it
      return null;
    }
  },

  async _tx(mode, fn) {
    const db = await this._open();
    if (!db) return null;
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(this.STORE, mode);
        const out = fn(tx.objectStore(this.STORE));
        tx.oncomplete = () => resolve(out && out.result !== undefined ? out.result : out);
        tx.onerror = tx.onabort = () => reject(tx.error);
      });
    } catch (e) {
      return null;
    }
  },

  /* Every cached column for this account, as [{key, items, at}]. Records
     belonging to another account are dropped on the way past. */
  async load(accountKey) {
    const rows = await this._tx("readonly", (s) => s.getAll());
    if (!rows || !rows.length) return [];
    const mine = rows.filter((r) => r.account === accountKey);
    if (mine.length !== rows.length) await this.clear();
    return mine.filter((r) => Array.isArray(r.items));
  },

  async put(accountKey, key, items, at) {
    await this._tx("readwrite", (s) =>
      s.put({ key, account: accountKey, items, at, bytes: roughBytes(items) }));
    this._evictSoon(accountKey);
  },

  async remove(key) {
    await this._tx("readwrite", (s) => s.delete(key));
  },

  async clear() {
    await this._tx("readwrite", (s) => s.clear());
  },

  /* Trim after writing rather than before, so a save is never blocked on it. */
  _evictTimer: null,
  _evictSoon(accountKey) {
    clearTimeout(this._evictTimer);
    this._evictTimer = setTimeout(() => this._evict(accountKey), 4000);
  },

  async _evict(accountKey) {
    const rows = await this._tx("readonly", (s) => s.getAll());
    if (!rows) return;
    // Oldest write first: what you looked at least recently is what goes.
    rows.sort((a, b) => (a.at || 0) - (b.at || 0));
    let bytes = rows.reduce((n, r) => n + (r.bytes || 0), 0);
    const doomed = [];
    while (rows.length && (rows.length > this.MAX_ENTRIES || bytes > this.MAX_BYTES)) {
      const gone = rows.shift();
      bytes -= gone.bytes || 0;
      doomed.push(gone.key);
    }
    for (const key of doomed) await this.remove(key);
  },
};

/* Enough to keep the cache inside a budget without serializing twice. */
function roughBytes(items) {
  let n = 0;
  for (const it of items || []) {
    const arts = it.articles || [it];
    for (const a of arts) n += (a.title || "").length + (a.summary || "").length + 200;
  }
  return n * 2;   // UTF-16
}
