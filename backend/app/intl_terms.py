"""Multilingual news terms so non-English articles categorize and score.

Categorization and breaking-signal scoring were English-only, so the ~500
foreign-language city feeds landed in "world" and under-scored. This adds
high-confidence, unambiguous terms in the languages that dominate the catalog,
for the categories where cross-language coverage matters most (disasters,
conflict, health emergencies, politics). It is deliberately conservative — a
missing term just leaves the English fallback, but a wrong term would
mis-tag, so only clear, well-known words are included. Extend freely.

_CONCEPTS drives both outputs: (category, breaking_weight, [terms…]). A term
may serve several languages (e.g. "terremoto" es/it/pt, "地震" zh/ja).
"""
from __future__ import annotations

from collections import defaultdict

# (category, breaking_weight 0=none, [terms across languages])
_CONCEPTS: list[tuple[str, int, list[str]]] = [
    ("disaster", 12, ["terremoto", "sismo", "séisme", "tremblement de terre",
                      "erdbeben", "землетрясение", "زلزال", "地震", "지진"]),
    ("disaster", 14, ["tsunami", "цунами", "تسونامي", "津波", "海啸", "쓰나미"]),
    ("disaster", 8, ["inundación", "inundações", "inundação", "enchente",
                     "inondation", "überschwemmung", "hochwasser", "alluvione",
                     "наводнение", "فيضان", "洪水", "홍수"]),
    ("disaster", 8, ["incendio", "incêndio", "incendie", "brand", "waldbrand",
                     "пожар", "حريق", "火災", "火灾", "화재", "산불"]),
    ("disaster", 10, ["huracán", "ouragan", "tempête", "unwetter", "uragano",
                      "furacão", "tempestade", "ураган", "шторм", "عاصفة",
                      "台风", "台風", "태풍"]),
    ("disaster", 10, ["erupción", "éruption", "vulkanausbruch", "eruzione",
                      "erupção", "извержение", "ثوران", "火山", "噴火", "분화"]),
    ("disaster", 8, ["evacuación", "évacuation", "evakuierung", "evacuazione",
                     "evacuação", "эвакуация", "إجلاء", "避難", "疏散", "대피"]),
    ("conflict", 10, ["guerra", "guerre", "krieg", "война", "حرب", "戦争",
                      "战争", "전쟁"]),
    ("conflict", 9, ["ataque", "atentado", "attaque", "attentat", "angriff",
                     "anschlag", "attacco", "нападение", "هجوم", "攻撃", "襲撃",
                     "袭击", "공격"]),
    ("conflict", 12, ["explosión", "explosion", "esplosione", "explosão",
                      "взрыв", "انفجار", "爆発", "爆炸", "폭발"]),
    ("conflict", 12, ["bombardeo", "bombardement", "bombardamento",
                      "bombardeio", "бомбардировка", "قصف", "空爆", "轰炸", "폭격"]),
    ("conflict", 8, ["muertos", "mortos", "morts", "tués", "getötet", "tote",
                     "morti", "погибли", "убиты", "قتلى", "死亡", "遇难", "사망"]),
    ("health", 10, ["brote", "epidemia", "épidémie", "epidemie", "surto",
                    "эпидемия", "вспышка", "وباء", "تفشي", "疫情", "流行", "발병"]),
    ("health", 9, ["virus", "vírus", "вирус", "فيروس", "ウイルス", "病毒", "바이러스"]),
    ("politics", 5, ["elección", "elecciones", "eleição", "eleições", "élection",
                     "élections", "wahl", "wahlen", "elezioni", "выборы",
                     "انتخابات", "選挙", "选举", "선거"]),
    ("politics", 14, ["golpe de estado", "coup d'état", "putsch", "colpo di stato",
                      "golpe", "переворот", "انقلاب", "クーデター", "政变", "쿠데타"]),
    ("politics", 5, ["protesta", "manifestación", "manifestation", "protest",
                     "demonstration", "manifestação", "protesto", "protestation",
                     "протест", "احتجاج", "抗議", "デモ", "抗议", "시위"]),
    ("politics", 6, ["emergencia", "urgence", "notstand", "notfall", "emergenza",
                     "emergência", "чрезвычайное", "طوارئ", "緊急", "非常事態",
                     "紧急", "비상"]),
    ("politics", 7, ["sanciones", "sanctions", "sanktionen", "sanzioni",
                     "sanções", "санкции", "عقوبات", "制裁", "제재"]),
    ("business", 4, ["economía", "économie", "wirtschaft", "economia",
                     "экономика", "اقتصاد", "経済", "经济", "경제"]),
    ("business", 7, ["inflación", "inflation", "inflação", "inflazione",
                     "инфляция", "تضخم", "インフレ", "通胀", "인플레이션"]),
]

# Outlet-provided RSS section tags → our category (high precision when present).
TAG_SYNONYMS: dict[str, str] = {}
for _en, _cat in {
    "politics": "politics", "world": "world", "business": "business",
    "economy": "economy", "economic": "economy", "technology": "technology",
    "tech": "technology", "science": "science", "health": "health",
    "environment": "environment", "climate": "environment", "sport": "sports",
    "sports": "sports", "entertainment": "entertainment", "culture": "culture",
    "arts": "culture", "crime": "crime", "war": "conflict", "conflict": "conflict",
    "disaster": "disaster", "weather": "disaster",
    # common non-English section names
    "política": "politics", "politique": "politics", "politik": "politics",
    "politica": "politics", "политика": "politics", "سياسة": "politics",
    "economía": "economy", "économie": "economy", "wirtschaft": "business",
    "economia": "economy", "экономика": "economy", "اقتصاد": "economy",
    "mundo": "world", "monde": "world", "welt": "world", "mondo": "world",
    "мир": "world", "عالم": "world", "国際": "world", "国际": "world",
    "deportes": "sports", "esportes": "sports",
    "スポーツ": "sports", "体育": "sports", "스포츠": "sports",
    "salud": "health", "santé": "health", "gesundheit": "health",
    "saúde": "health", "здоровье": "health", "صحة": "health",
    "tecnología": "technology", "technologie": "technology",
    "tecnologia": "technology", "테크": "technology", "科技": "technology",
    "ciencia": "science", "wissenschaft": "science", "наука": "science",
    "cultura": "culture", "kultur": "culture",
    "sucesos": "crime", "faits divers": "crime", "происшествия": "crime",
}.items():
    TAG_SYNONYMS[_en.lower()] = _cat


def _build():
    breaking: dict[str, int] = {}
    cats: dict[str, list[str]] = defaultdict(list)
    for cat, weight, terms in _CONCEPTS:
        for t in terms:
            if weight:
                breaking[t] = max(breaking.get(t, 0), weight)
            cats[cat].append(t)
    return breaking, {c: sorted(set(v)) for c, v in cats.items()}


BREAKING_INTL, CATEGORY_INTL = _build()
