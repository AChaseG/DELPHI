# Benchmarks

Delphi is fast on a thousand articles and slow on a quarter of a million, and
only the second number is worth optimizing against. These two scripts build an
archive the size of a real one and time the work Delphi does most, so a change
that costs speed is noticed the day it lands rather than months later on a
database nobody can reproduce.

```sh
python bench/corpus.py  --db /tmp/bench.db --articles 120000   # ~45s, ~340 MB
python bench/measure.py --db /tmp/bench.db --baseline bench/baseline.json
```

`measure.py` exits non-zero if anything got materially slower. Add `--out
today.json` to keep the numbers.

## What is measured

**search** — the query shapes behind feeds, alerts and the Home board: a common
word, an uncommon one, written boolean searches, words combined with countries
and importance and time windows, the 200-article draws the grouped columns make,
and the case where a page can never be filled, which takes the deepest route
through the search.

**ingest** — the per-article work of a poll tick: which story an article belongs
to, how many other outlets are carrying it, and whether it trips any of thirty
saved alerts. This is the work that competes with serving pages, because it all
happens on the same machine.

**payload** — what a browser downloads, compressed, before Delphi runs at all.

## Why the corpus is synthetic

It has to be reproducible. `corpus.py` is seeded, so the same arguments produce
the same archive on any machine on any day, which is what makes yesterday's
measurements comparable to today's. Real articles would be better in every way
except that one, and that one is the point.

Its vocabulary is deliberately narrower than real news. Headlines share words
far more often than they do in life, which is the hard case for anything that
indexes by word — the measurements are pessimistic, not flattering.

Build a fresh corpus for each run: the recency cases (`the last 6h`, the
72-hour clustering window) are measured against a corpus built relative to
*now*, and `measure.py` warns if it is given a stale one.

## Comparing across machines

The daily run may not land on the same hardware twice, so two fixed workloads —
one pure Python, one pure SQLite — are timed alongside everything else, and a
comparison scales by how much faster or slower this machine is than the one that
recorded the baseline.

That correction is rough. It exists to stop a slow VM being reported as a
regression, not to make small differences meaningful. Run-to-run spread on
identical code is under 10%; the default threshold is 1.4× *and* at least 3ms
of absolute difference, comfortably outside the noise. Anything quieter than
that is not worth acting on and is not reported.

## Updating the baseline

`bench/baseline.json` is the record of what Delphi currently costs. Update it
when a change makes something genuinely faster — in the same commit as the
change, so the numbers in the commit message and the numbers in the file agree.
Never update it to make a regression go away; a regression that is a deliberate
trade belongs in the commit message, explained.
