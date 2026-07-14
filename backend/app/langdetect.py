"""Lightweight, dependency-free language detection for article text.

Article language drives auto-translation and the language filter, but it was
inherited from the *source's* configured tag — which is wrong for aggregators
(one Google News feed carries many languages) and auto-discovered outlets
(default "en"). So foreign articles were tagged English and never translated.

`detect()` reads the text instead. Two tiers:

  1. Script detection (Cyrillic, Greek, CJK, Arabic, Devanagari, Hebrew, Thai)
     is unambiguous by Unicode block — always trusted.
  2. Latin-script languages are separated by function-word frequency, and only
     override the source's tag on a clear margin (short headlines are noisy —
     when in doubt we keep the source's language rather than guess wrong).
"""
from __future__ import annotations

import re
from collections import Counter

# Frequent function words per Latin-script UI language. Chosen to discriminate:
# words shared across several languages ("de", "a", "la") pull their weight
# only in combination, and the margin rule below guards against ties.
_STOP: dict[str, set[str]] = {
    "en": set("the of and to in a is that for on with as it was by are be at from this "
              "have not or an but has had who will".split()),
    "es": set("el la de que y en los las un una por con para no se su del al es lo como "
              "más pero sus le ya o".split()),
    "fr": set("le la les de des un une et en que qui dans pour pas sur au aux du est ne "
              "se ce il elle par plus avec".split()),
    "de": set("der die das und in den von zu mit ist im auf für dem nicht ein eine als "
              "auch sich wird bei aus nach werden".split()),
    "it": set("il lo la gli le di che e in un una per non con si sono come più ma anche "
              "della dei delle nel alla".split()),
    "pt": set("o a os as de que e do da em um uma para com não se por mais como mas "
              "dos das no na ao à".split()),
    "nl": set("de het een en van in is dat op te met voor niet zijn er aan ook als maar "
              "door om dan naar".split()),
    "pl": set("i w na z do nie że się jest to o jak co po dla od za ale przez oraz "
              "który przy tym".split()),
    "sv": set("och att det som en är på för med av den till inte har de ett om men "
              "han var sig så".split()),
    "tr": set("ve bir bu için ile de da çok en daha olarak gibi kadar ancak ya ki "
              "ama sonra göre".split()),
}


def _script(text: str) -> str | None:
    """Dominant non-Latin script → language, or None for Latin/mixed."""
    c: Counter = Counter()
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:               c["ja"] += 1     # kana (definitive JP)
        elif 0xAC00 <= o <= 0xD7A3:             c["ko"] += 1     # hangul
        elif 0x4E00 <= o <= 0x9FFF:             c["han"] += 1    # CJK ideographs
        elif 0x0400 <= o <= 0x04FF:             c["cyr"] += 1
        elif 0x0370 <= o <= 0x03FF:             c["el"] += 1
        elif 0x0600 <= o <= 0x06FF:             c["ar"] += 1
        elif 0x0900 <= o <= 0x097F:             c["hi"] += 1
        elif 0x0590 <= o <= 0x05FF:             c["he"] += 1
        elif 0x0E00 <= o <= 0x0E7F:             c["th"] += 1
        elif ch.isalpha() and o < 0x0250:       c["latin"] += 1
    letters = sum(c.values())
    if letters < 8:
        return None
    if c["ja"]:                       return "ja"           # any kana ⇒ Japanese
    if c["ko"]:                       return "ko"
    if c["cyr"] >= 0.3 * letters:
        return "uk" if re.search(r"[іїєґІЇЄҐ]", text) else "ru"
    for lang in ("el", "ar", "hi", "he", "th"):
        if c[lang] >= 0.3 * letters:
            return lang
    if c["han"] >= 0.2 * letters:     return "zh"           # Han without kana ⇒ Chinese
    return None


def detect(text: str, default: str = "en") -> str:
    """Best-effort language code (2-letter). Falls back to `default` when the
    signal is weak, so a wrong guess never overrides a plausible source tag."""
    text = (text or "").strip()
    if not text:
        return default
    scripted = _script(text)
    if scripted:
        return scripted
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    if len(words) < 4:
        return default
    scores = {lang: sum(w in sw for w in words) for lang, sw in _STOP.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_n = ranked[0]
    second_n = ranked[1][1] if len(ranked) > 1 else 0
    # Real evidence (≥3 function words) and a strict lead. Falling back to the
    # source tag on a tie would re-tag foreign articles as the (often "en")
    # source language — the very bug this fixes — so a clear winner takes it.
    if best_n >= 3 and best_n > second_n:
        return best
    return default
