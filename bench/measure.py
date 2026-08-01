"""Time the work Delphi does most, and say what got slower.

Everything here runs in-process against a corpus built by bench/corpus.py — no
server, no network — so it behaves the same in a fresh container as on a
laptop. Three kinds of work are measured:

  search    the query shapes behind feeds, alerts and the Home board
  ingest    the per-article work of a poll tick: clustering, corroboration,
            and matching new articles against saved alerts
  payload   what the browser has to download, compressed, before Delphi runs

    python bench/corpus.py  --db /tmp/bench.db --articles 120000
    python bench/measure.py --db /tmp/bench.db --out today.json
    python bench/measure.py --db /tmp/bench.db --baseline bench/baseline.json

Machines differ, and the daily run may not land on the same one twice. So two
fixed workloads — one pure Python, one pure SQLite — are timed alongside
everything else, and a comparison scales by how much faster or slower this
machine is than the one that recorded the baseline. That correction is rough;
it is there to stop a slow VM being reported as a regression, not to make
small differences meaningful. Anything under `--threshold` is not reported.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", required=True, help="corpus built by bench/corpus.py")
parser.add_argument("--out", help="write the measurements here as JSON")
parser.add_argument("--baseline", help="compare against this file and report regressions")
parser.add_argument("--threshold", type=float, default=1.4,
                    help="report a measurement this many times slower than baseline")
parser.add_argument("--runs", type=int, default=3, help="best of N")
args = parser.parse_args()

sys.path.insert(0, str(ROOT))
os.environ["NEWS_DB_PATH"] = args.db

from sqlalchemy import func, select, text  # noqa: E402

from backend.app import clustering, matching  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.ingest import RecentClusters, evaluate_alerts  # noqa: E402
from backend.app.matching import CriteriaMatcher, article_text  # noqa: E402
from backend.app.models import Article, Event, utcnow  # noqa: E402


def best(fn, runs=None) -> float:
    """Milliseconds for the fastest of N runs — the one least disturbed by
    whatever else the machine was doing."""
    times = []
    for _ in range(runs or args.runs):
        started = time.perf_counter()
        fn()
        times.append((time.perf_counter() - started) * 1000)
    return round(min(times), 2)


# ---------- how fast is this machine, in the abstract ----------

def calibrate(db) -> dict:
    def cpu():
        total = 0
        for i in range(400_000):
            total += i * i % 7
        return total

    def disk():
        return db.execute(text(
            "SELECT count(*), sum(importance) FROM articles")).one()

    return {"cpu_ms": best(cpu, runs=3), "sqlite_ms": best(disk, runs=3)}


# ---------- searching ----------

SEARCHES = [
    ("everyday word", {"keywords": ["earthquake"]}, "newest", 40),
    ("everyday word by importance", {"keywords": ["earthquake"]}, "importance", 40),
    ("two words", {"keywords": ["election", "protest"]}, "newest", 40),
    ("written search", {"queries": ["(tokyo OR osaka) AND earthquake"]}, "newest", 40),
    ("written search with NOT", {"queries": ["earthquake AND NOT verdict"]}, "newest", 40),
    ("word and country", {"keywords": ["earthquake"], "countries": ["JP"]}, "newest", 40),
    ("word and importance", {"keywords": ["earthquake"], "min_importance": 80}, "newest", 40),
    ("word in the last 6h", {"keywords": ["earthquake"], "hours": 6}, "newest", 40),
    ("exclusion", {"keywords": ["strike"], "exclude_keywords": ["harvest"]}, "newest", 40),
    ("rare phrase", {"queries": ['"aardvark dispatch"']}, "newest", 40),
    ("wildcard, index cannot help", {"keywords": ["earthq*"]}, "newest", 40),
    ("no text at all", {"min_importance": 60}, "importance", 40),
    ("categories", {"categories": ["politics"]}, "newest", 40),
    # What the grouped columns ask for: 200 articles to cluster into events.
    ("grouped column", {"categories": ["politics"]}, "newest", 200),
    ("grouped column with words", {"keywords": ["earthquake", "flood"]}, "newest", 200),
    # A page that can never be filled — the deepest route through the search.
    ("nothing can match", {"keywords": ["earthquake"], "countries": ["ZZ"]}, "newest", 40),
]


def measure_search(db) -> dict:
    out = {}
    for label, criteria, sort, limit in SEARCHES:
        out[label] = best(lambda c=criteria, s=sort, n=limit:
                          matching.query_articles(db, c, sort=s, limit=n))
    return out


# ---------- the work of a poll tick ----------

ALERT_CRITERIA = [
    {"keywords": ["earthquake", "flood"]},
    {"keywords": ["election"], "countries": ["US"]},
    {"queries": ["(tokyo OR osaka) AND earthquake"]},
    {"keywords": ["inflation", "tariff"], "exclude_keywords": ["harvest"]},
    {"min_importance": 80},
    {"categories": ["politics"], "keywords": ["summit"]},
] * 5                                   # thirty saved alerts


def measure_ingest(db) -> dict:
    arriving = db.scalars(select(Article).order_by(Article.id.desc()).limit(200)).all()

    window = db.scalar(select(func.max(Article.published_at)))
    recent_rows = db.execute(
        select(Article.source_id, Article.cluster_tokens).where(
            Article.published_at >= window - timedelta(hours=48),
            Article.cluster_tokens != "")).all()
    recent = RecentClusters([(r[0], r[1]) for r in recent_rows])

    live_rows = list(db.scalars(select(Event).where(
        Event.updated_at >= window - timedelta(hours=clustering.WINDOW_HOURS))))
    live = clustering.LiveEvents(live_rows)

    matchers = [CriteriaMatcher(c) for c in ALERT_CRITERIA]

    def corroborate():
        for a in arriving:
            recent.corroboration(a.source_id, a.cluster_tokens)

    def cluster():
        for a in arriving:
            live.best_match(a)

    def alerts():
        for a in arriving:
            body = article_text(a)
            for m in matchers:
                m.matches(a, a.source, text=body)

    return {
        "corroboration, 200 articles": best(corroborate),
        "clustering, 200 articles": best(cluster),
        "alert matching, 200 articles x 30 alerts": best(alerts),
        "_context": {"recent headlines": len(recent_rows), "live events": len(live_rows)},
    }


# ---------- what the browser downloads ----------

def measure_payload() -> dict:
    out = {}
    for rel in ("frontend/index.html", "frontend/js/app.js", "frontend/js/builder.js",
                "frontend/css/styles.css"):
        raw = (ROOT / rel).read_bytes()
        out[rel] = {"raw_kb": round(len(raw) / 1024, 1),
                    "gzip_kb": round(len(gzip.compress(raw, 6)) / 1024, 1)}
    out["first load gzip_kb"] = round(
        sum(v["gzip_kb"] for k, v in out.items() if isinstance(v, dict)), 1)
    return out


# ---------- comparing ----------

def flatten(section: dict, prefix: str) -> dict:
    return {f"{prefix}/{k}": v for k, v in section.items()
            if isinstance(v, (int, float)) and not k.startswith("_")}


def compare(now: dict, before: dict, threshold: float) -> list[str]:
    """Everything that got materially slower, corrected for machine speed."""
    if now["corpus"]["articles"] != before["corpus"]["articles"]:
        return [f"corpus differs ({before['corpus']['articles']} articles in the "
                f"baseline, {now['corpus']['articles']} here) — not comparable"]

    ratios = []
    for key in ("cpu_ms", "sqlite_ms"):
        was, is_now = before["calibration"].get(key), now["calibration"].get(key)
        if was and is_now:
            ratios.append(is_now / was)
    speed = statistics.fmean(ratios) if ratios else 1.0    # >1 = slower machine

    problems = []
    for section in ("search", "ingest"):
        mine = flatten(now[section], section)
        theirs = flatten(before.get(section, {}), section)
        for key, value in mine.items():
            was = theirs.get(key)
            if not was:
                continue
            corrected = value / speed
            # Small absolute numbers move around for reasons that have nothing
            # to do with the code, so they need a real gap to count.
            if corrected > was * threshold and corrected - was > 3:
                problems.append(
                    f"{key}: {was:.1f}ms → {value:.1f}ms"
                    + (f" ({corrected:.1f}ms once this machine's speed is taken "
                       f"into account, {speed:.2f}x the baseline's)" if abs(speed - 1) > 0.05
                       else ""))

    was_kb = before.get("payload", {}).get("first load gzip_kb")
    now_kb = now["payload"]["first load gzip_kb"]
    if was_kb and now_kb > was_kb * 1.1:
        problems.append(f"payload/first load: {was_kb}KB → {now_kb}KB compressed")
    return problems


with SessionLocal() as db:
    articles = db.scalar(select(func.count(Article.id))) or 0
    if articles < 1000:
        raise SystemExit(f"{args.db} has {articles} articles — run bench/corpus.py first")
    newest = db.scalar(select(func.max(Article.published_at)))
    stale_hours = (utcnow() - newest).total_seconds() / 3600 if newest else 0

    result = {
        "corpus": {"articles": articles,
                   "events": db.scalar(select(func.count(Event.id))) or 0,
                   "newest article, hours ago": round(stale_hours, 1)},
        "calibration": calibrate(db),
        "search": measure_search(db),
        "ingest": measure_ingest(db),
        "payload": measure_payload(),
    }

if stale_hours > 24:
    print(f"warning: the newest article is {stale_hours / 24:.0f} days old, so the "
          f"recency cases measure an empty window. Build a fresh corpus.\n")

print(f"corpus: {result['corpus']['articles']} articles, "
      f"{result['corpus']['events']} events")
print(f"machine: {result['calibration']['cpu_ms']:.0f}ms of arithmetic, "
      f"{result['calibration']['sqlite_ms']:.0f}ms of SQLite\n")
for section in ("search", "ingest"):
    print(section)
    for key, value in result[section].items():
        if isinstance(value, (int, float)):
            print(f"  {key:<44}{value:>9.1f}ms")
    print()
print(f"first load, compressed: {result['payload']['first load gzip_kb']}KB")

if args.out:
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nwritten to {args.out}")

if args.baseline:
    before = json.loads(Path(args.baseline).read_text())
    problems = compare(result, before, args.threshold)
    print("\n" + ("nothing got slower" if not problems else "SLOWER THAN BASELINE:"))
    for line in problems:
        print(f"  {line}")
    raise SystemExit(1 if problems else 0)
