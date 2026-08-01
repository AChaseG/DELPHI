"""World major-cities catalog → one local-news source per city.

For every city we register a Google News *city-scoped* search feed, set to the
country's own edition (hl/gl/ceid), scope="local". Two things happen:

  1. Local coverage of that city flows in immediately, and
  2. auto-discovery (discovery.py) reads the outlet named on each entry's
     <source> tag and promotes the city's real local papers/stations to
     first-class sources over subsequent cycles — validated, not guessed.

This is deliberately the only mechanism used: hand-listing thousands of
individual outlet RSS URLs can't be validated and rots quickly, whereas one
reliable feed format per city is self-maintaining and grows the catalog toward
each city's actual local press.

Data model: WORLD[region][iso2] = (language, [city, ...]). Language is the
country's dominant Google-News locale language; region is a continent bucket
used for the source's `region` field. Extend freely — the seeder is idempotent.
"""
from __future__ import annotations

import urllib.parse

# region -> ISO2 -> (google-news language code, [cities])
WORLD: dict[str, dict[str, tuple[str, list[str]]]] = {
    "Africa": {
        "DZ": ("ar", ["Algiers", "Oran", "Constantine"]),
        "AO": ("pt", ["Luanda", "Huambo"]),
        "BJ": ("fr", ["Cotonou", "Porto-Novo"]),
        "BW": ("en", ["Gaborone"]),
        "BF": ("fr", ["Ouagadougou", "Bobo-Dioulasso"]),
        "BI": ("fr", ["Bujumbura", "Gitega"]),
        "CM": ("fr", ["Douala", "Yaoundé", "Bamenda"]),
        "CV": ("pt", ["Praia"]),
        "CF": ("fr", ["Bangui"]),
        "TD": ("fr", ["N'Djamena"]),
        "CG": ("fr", ["Brazzaville", "Pointe-Noire"]),
        "CD": ("fr", ["Kinshasa", "Lubumbashi", "Goma"]),
        "CI": ("fr", ["Abidjan", "Yamoussoukro", "Bouaké"]),
        "DJ": ("fr", ["Djibouti"]),
        "EG": ("ar", ["Cairo", "Alexandria", "Giza", "Port Said"]),
        "GQ": ("es", ["Malabo"]),
        "ER": ("en", ["Asmara"]),
        "SZ": ("en", ["Mbabane"]),
        "ET": ("en", ["Addis Ababa", "Dire Dawa"]),
        "GA": ("fr", ["Libreville"]),
        "GM": ("en", ["Banjul"]),
        "GH": ("en", ["Accra", "Kumasi", "Tamale"]),
        "GN": ("fr", ["Conakry"]),
        "KE": ("en", ["Nairobi", "Mombasa", "Kisumu", "Nakuru"]),
        "LS": ("en", ["Maseru"]),
        "LR": ("en", ["Monrovia"]),
        "LY": ("ar", ["Tripoli", "Benghazi"]),
        "MG": ("fr", ["Antananarivo"]),
        "MW": ("en", ["Lilongwe", "Blantyre"]),
        "ML": ("fr", ["Bamako"]),
        "MR": ("ar", ["Nouakchott"]),
        "MU": ("en", ["Port Louis"]),
        "MA": ("fr", ["Casablanca", "Rabat", "Marrakesh", "Fes", "Tangier"]),
        "MZ": ("pt", ["Maputo", "Beira"]),
        "NA": ("en", ["Windhoek"]),
        "NE": ("fr", ["Niamey"]),
        "NG": ("en", ["Lagos", "Abuja", "Kano", "Ibadan", "Port Harcourt", "Benin City"]),
        "RW": ("en", ["Kigali"]),
        "SN": ("fr", ["Dakar", "Touba"]),
        "SL": ("en", ["Freetown"]),
        "SO": ("en", ["Mogadishu", "Hargeisa"]),
        "ZA": ("en", ["Johannesburg", "Cape Town", "Durban", "Pretoria", "Port Elizabeth"]),
        "SS": ("en", ["Juba"]),
        "SD": ("ar", ["Khartoum", "Omdurman"]),
        "TZ": ("en", ["Dar es Salaam", "Dodoma", "Mwanza"]),
        "TG": ("fr", ["Lomé"]),
        "TN": ("ar", ["Tunis", "Sfax"]),
        "UG": ("en", ["Kampala", "Gulu"]),
        "ZM": ("en", ["Lusaka", "Kitwe"]),
        "ZW": ("en", ["Harare", "Bulawayo"]),
    },
    "Asia": {
        "AF": ("en", ["Kabul", "Kandahar", "Herat"]),
        "BD": ("en", ["Dhaka", "Chittagong", "Khulna", "Rajshahi"]),
        "BT": ("en", ["Thimphu"]),
        "BN": ("en", ["Bandar Seri Begawan"]),
        "KH": ("en", ["Phnom Penh", "Siem Reap"]),
        "CN": ("zh", ["Beijing", "Shanghai", "Guangzhou", "Shenzhen", "Chengdu",
                      "Chongqing", "Wuhan", "Xi'an", "Hangzhou", "Nanjing"]),
        "HK": ("en", ["Hong Kong"]),
        "IN": ("en", ["Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai",
                      "Kolkata", "Ahmedabad", "Pune", "Jaipur", "Lucknow"]),
        "ID": ("id", ["Jakarta", "Surabaya", "Bandung", "Medan", "Semarang", "Makassar"]),
        "JP": ("ja", ["Tokyo", "Osaka", "Yokohama", "Nagoya", "Sapporo",
                      "Fukuoka", "Kyoto", "Kobe"]),
        "KZ": ("ru", ["Almaty", "Astana", "Shymkent"]),
        "KG": ("ru", ["Bishkek", "Osh"]),
        "LA": ("en", ["Vientiane"]),
        "MO": ("en", ["Macau"]),
        "MY": ("en", ["Kuala Lumpur", "George Town", "Johor Bahru", "Ipoh"]),
        "MV": ("en", ["Malé"]),
        "MN": ("en", ["Ulaanbaatar"]),
        "MM": ("en", ["Yangon", "Mandalay", "Naypyidaw"]),
        "NP": ("en", ["Kathmandu", "Pokhara"]),
        "KP": ("en", ["Pyongyang"]),
        "PK": ("en", ["Karachi", "Lahore", "Islamabad", "Faisalabad", "Rawalpindi", "Peshawar"]),
        "PH": ("en", ["Manila", "Quezon City", "Cebu City", "Davao City"]),
        "SG": ("en", ["Singapore"]),
        "KR": ("ko", ["Seoul", "Busan", "Incheon", "Daegu", "Daejeon", "Gwangju"]),
        "LK": ("en", ["Colombo", "Kandy"]),
        "TW": ("zh", ["Taipei", "Kaohsiung", "Taichung", "Tainan"]),
        "TJ": ("ru", ["Dushanbe"]),
        "TH": ("th", ["Bangkok", "Chiang Mai", "Nonthaburi", "Pattaya"]),
        "TM": ("ru", ["Ashgabat"]),
        "UZ": ("ru", ["Tashkent", "Samarkand"]),
        "VN": ("vi", ["Ho Chi Minh City", "Hanoi", "Da Nang", "Hai Phong"]),
    },
    "Middle East": {
        "BH": ("ar", ["Manama"]),
        "IR": ("en", ["Tehran", "Mashhad", "Isfahan", "Shiraz", "Tabriz"]),
        "IQ": ("ar", ["Baghdad", "Basra", "Mosul", "Erbil"]),
        "IL": ("en", ["Jerusalem", "Tel Aviv", "Haifa"]),
        "JO": ("ar", ["Amman", "Zarqa"]),
        "KW": ("ar", ["Kuwait City"]),
        "LB": ("ar", ["Beirut", "Tripoli"]),
        "OM": ("ar", ["Muscat"]),
        "PS": ("ar", ["Gaza", "Ramallah", "Hebron"]),
        "QA": ("ar", ["Doha"]),
        "SA": ("ar", ["Riyadh", "Jeddah", "Mecca", "Medina", "Dammam"]),
        "SY": ("ar", ["Damascus", "Aleppo", "Homs"]),
        "TR": ("tr", ["Istanbul", "Ankara", "Izmir", "Bursa", "Antalya", "Adana"]),
        "AE": ("en", ["Dubai", "Abu Dhabi", "Sharjah"]),
        "YE": ("ar", ["Sanaa", "Aden"]),
    },
    "Europe": {
        "AL": ("en", ["Tirana"]),
        "AT": ("de", ["Vienna", "Graz", "Linz", "Salzburg"]),
        "BY": ("ru", ["Minsk", "Gomel"]),
        "BE": ("fr", ["Brussels", "Antwerp", "Ghent"]),
        "BA": ("en", ["Sarajevo", "Banja Luka"]),
        "BG": ("bg", ["Sofia", "Plovdiv", "Varna"]),
        "HR": ("hr", ["Zagreb", "Split", "Rijeka"]),
        "CY": ("en", ["Nicosia", "Limassol"]),
        "CZ": ("cs", ["Prague", "Brno", "Ostrava"]),
        "DK": ("da", ["Copenhagen", "Aarhus", "Odense"]),
        "EE": ("en", ["Tallinn", "Tartu"]),
        "FI": ("fi", ["Helsinki", "Espoo", "Tampere", "Turku"]),
        "FR": ("fr", ["Paris", "Marseille", "Lyon", "Toulouse", "Nice",
                      "Nantes", "Strasbourg", "Bordeaux", "Lille"]),
        "DE": ("de", ["Berlin", "Hamburg", "Munich", "Cologne", "Frankfurt",
                      "Stuttgart", "Düsseldorf", "Leipzig", "Dresden"]),
        "GR": ("el", ["Athens", "Thessaloniki", "Patras"]),
        "HU": ("hu", ["Budapest", "Debrecen", "Szeged"]),
        "IS": ("en", ["Reykjavik"]),
        "IE": ("en", ["Dublin", "Cork", "Galway", "Limerick"]),
        "IT": ("it", ["Rome", "Milan", "Naples", "Turin", "Palermo",
                      "Genoa", "Bologna", "Florence", "Venice"]),
        "XK": ("en", ["Pristina"]),
        "LV": ("en", ["Riga", "Daugavpils"]),
        "LT": ("en", ["Vilnius", "Kaunas"]),
        "LU": ("fr", ["Luxembourg"]),
        "MT": ("en", ["Valletta"]),
        "MD": ("ro", ["Chișinău"]),
        "ME": ("en", ["Podgorica"]),
        "NL": ("nl", ["Amsterdam", "Rotterdam", "The Hague", "Utrecht", "Eindhoven"]),
        "MK": ("en", ["Skopje"]),
        "NO": ("no", ["Oslo", "Bergen", "Trondheim", "Stavanger"]),
        "PL": ("pl", ["Warsaw", "Kraków", "Łódź", "Wrocław", "Poznań", "Gdańsk"]),
        "PT": ("pt", ["Lisbon", "Porto", "Braga", "Coimbra"]),
        "RO": ("ro", ["Bucharest", "Cluj-Napoca", "Timișoara", "Iași"]),
        "RU": ("ru", ["Moscow", "Saint Petersburg", "Novosibirsk", "Yekaterinburg",
                      "Kazan", "Nizhny Novgorod", "Sochi", "Vladivostok"]),
        "RS": ("en", ["Belgrade", "Novi Sad", "Niš"]),
        "SK": ("sk", ["Bratislava", "Košice"]),
        "SI": ("sl", ["Ljubljana", "Maribor"]),
        "ES": ("es", ["Madrid", "Barcelona", "Valencia", "Seville", "Zaragoza",
                      "Málaga", "Bilbao", "Granada"]),
        "SE": ("sv", ["Stockholm", "Gothenburg", "Malmö", "Uppsala"]),
        "CH": ("de", ["Zurich", "Geneva", "Basel", "Bern", "Lausanne"]),
        "UA": ("uk", ["Kyiv", "Kharkiv", "Odesa", "Dnipro", "Lviv"]),
        "GB": ("en", ["London", "Manchester", "Birmingham", "Glasgow", "Leeds",
                      "Liverpool", "Edinburgh", "Bristol", "Cardiff", "Belfast"]),
    },
    "North America": {
        "BS": ("en", ["Nassau"]),
        "BZ": ("en", ["Belize City", "Belmopan"]),
        "CA": ("en", ["Toronto", "Montreal", "Vancouver", "Calgary", "Ottawa",
                      "Edmonton", "Winnipeg", "Quebec City", "Halifax"]),
        "CR": ("es", ["San José"]),
        "CU": ("es", ["Havana", "Santiago de Cuba"]),
        "DO": ("es", ["Santo Domingo", "Santiago"]),
        "SV": ("es", ["San Salvador"]),
        "GT": ("es", ["Guatemala City"]),
        "HT": ("fr", ["Port-au-Prince"]),
        "HN": ("es", ["Tegucigalpa", "San Pedro Sula"]),
        "JM": ("en", ["Kingston"]),
        "MX": ("es", ["Mexico City", "Guadalajara", "Monterrey", "Puebla",
                      "Tijuana", "Cancún", "Mérida"]),
        "NI": ("es", ["Managua"]),
        "PA": ("es", ["Panama City"]),
        "PR": ("es", ["San Juan"]),
        "TT": ("en", ["Port of Spain"]),
        "US": ("en", ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
                      "Philadelphia", "San Antonio", "San Diego", "Dallas",
                      "San Francisco", "Seattle", "Denver", "Boston", "Washington",
                      "Atlanta", "Miami", "Detroit", "Minneapolis", "New Orleans"]),
    },
    "South America": {
        "AR": ("es", ["Buenos Aires", "Córdoba", "Rosario", "Mendoza", "La Plata"]),
        "BO": ("es", ["La Paz", "Santa Cruz", "Cochabamba"]),
        "BR": ("pt", ["São Paulo", "Rio de Janeiro", "Brasília", "Salvador",
                      "Fortaleza", "Belo Horizonte", "Manaus", "Curitiba",
                      "Recife", "Porto Alegre"]),
        "CL": ("es", ["Santiago", "Valparaíso", "Concepción"]),
        "CO": ("es", ["Bogotá", "Medellín", "Cali", "Barranquilla", "Cartagena"]),
        "EC": ("es", ["Quito", "Guayaquil", "Cuenca"]),
        "GY": ("en", ["Georgetown"]),
        "PY": ("es", ["Asunción"]),
        "PE": ("es", ["Lima", "Arequipa", "Trujillo", "Cusco"]),
        "SR": ("nl", ["Paramaribo"]),
        "UY": ("es", ["Montevideo"]),
        "VE": ("es", ["Caracas", "Maracaibo", "Valencia"]),
    },
    "Oceania": {
        "AU": ("en", ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide",
                      "Canberra", "Gold Coast", "Hobart", "Darwin"]),
        "FJ": ("en", ["Suva"]),
        "NZ": ("en", ["Auckland", "Wellington", "Christchurch", "Hamilton"]),
        "PG": ("en", ["Port Moresby"]),
    },
}


def _feed_url(city: str, lang: str, cc: str) -> str:
    """Google News city-scoped local search feed for the country's edition."""
    q = urllib.parse.quote(city)
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={lang}-{cc}&gl={cc}&ceid={cc}:{lang}")


def language_for(country: str) -> str:
    """The Google News edition language this catalog uses for a country.

    Derived from the catalog itself rather than kept as a second list, so a
    watched place asks for news in the same language its country's own city
    feeds do. English when the country is unknown to the catalog.
    """
    cc = (country or "").upper()
    for countries in WORLD.values():
        entry = countries.get(cc)
        if entry:
            return entry[0]
    return "en"


def search_feed_url(query: str, country: str = "") -> str:
    """A Google News search feed for any query, in a country's edition.

    Shares _feed_url with the city catalog so there is one place that knows
    the shape of these URLs.
    """
    cc = (country or "").upper() or "US"
    return _feed_url(query, language_for(cc), cc)


def build_city_sources() -> list[dict]:
    """One local-news Source row per catalog city (Source column kwargs)."""
    rows: list[dict] = []
    for region, countries in WORLD.items():
        for cc, (lang, cities) in countries.items():
            for city in cities:
                rows.append({
                    "name": f"{city} — local news",
                    "rss_url": _feed_url(city, lang, cc),
                    "homepage": "https://news.google.com",
                    "country": cc,
                    "region": region,
                    "language": lang,
                    "scope": "local",
                    "categories": [],
                    "platform": "news",
                    "tier": 3,
                    "added_by": "city-catalog",
                })
    return rows


def city_count() -> int:
    return sum(len(cities) for cs in WORLD.values() for _, cities in cs.values())


def catalog_cities() -> list[tuple[str, str]]:
    """(city name, ISO2 country) for every catalog city — used to extend the
    geotagging gazetteer so these cities are recognized in article text."""
    out: list[tuple[str, str]] = []
    for countries in WORLD.values():
        for cc, (_lang, names) in countries.items():
            for name in names:
                out.append((name, cc))
    return out


# Native-script / alternate names for major world cities, so geotagging (and
# cross-language event clustering, which keys off canonical place names) works
# on non-English coverage. Keyed by the catalog's English city name.
CITY_ALIASES: dict[str, list[str]] = {
    "Tokyo": ["東京"], "Osaka": ["大阪"], "Kyoto": ["京都"], "Yokohama": ["横浜"],
    "Nagoya": ["名古屋"], "Sapporo": ["札幌"], "Fukuoka": ["福岡"], "Kobe": ["神戸"],
    "Beijing": ["北京"], "Shanghai": ["上海"], "Guangzhou": ["广州"], "Shenzhen": ["深圳"],
    "Chengdu": ["成都"], "Chongqing": ["重庆"], "Wuhan": ["武汉"], "Xi'an": ["西安"],
    "Hangzhou": ["杭州"], "Nanjing": ["南京"], "Hong Kong": ["香港"], "Taipei": ["台北"],
    "Kaohsiung": ["高雄"], "Seoul": ["서울"], "Busan": ["부산"], "Incheon": ["인천"],
    "Daegu": ["대구"], "Pyongyang": ["평양"],
    "Moscow": ["Москва"], "Saint Petersburg": ["Санкт-Петербург"], "Novosibirsk": ["Новосибирск"],
    "Yekaterinburg": ["Екатеринбург"], "Kazan": ["Казань"], "Vladivostok": ["Владивосток"],
    # Ukrainian declension alternates the stem vowel (Київ → Києвом), so the
    # alternated stem is listed alongside the nominative and the Russian form.
    "Kyiv": ["Київ", "Києв", "Киев"], "Kharkiv": ["Харків", "Харков", "Харьков"],
    "Odesa": ["Одеса", "Одесса"], "Lviv": ["Львів", "Львов"], "Minsk": ["Мінск", "Минск"],
    "Athens": ["Αθήνα"], "Thessaloniki": ["Θεσσαλονίκη"],
    "Cairo": ["القاهرة"], "Alexandria": ["الإسكندرية"], "Baghdad": ["بغداد"], "Basra": ["البصرة"],
    "Damascus": ["دمشق"], "Aleppo": ["حلب"], "Riyadh": ["الرياض"], "Jeddah": ["جدة"],
    "Mecca": ["مكة"], "Beirut": ["بيروت"], "Amman": ["عمّان"], "Tripoli": ["طرابلس"],
    "Tunis": ["تونس"], "Casablanca": ["الدار البيضاء"], "Rabat": ["الرباط"],
    "Sanaa": ["صنعاء"], "Doha": ["الدوحة"], "Kuwait City": ["الكويت"], "Dubai": ["دبي"],
    "Abu Dhabi": ["أبوظبي"], "Tehran": ["تهران"], "Mashhad": ["مشهد"], "Isfahan": ["اصفهان"],
    "Jerusalem": ["ירושלים", "القدس"], "Tel Aviv": ["תל אביב"],
    "Bangkok": ["กรุงเทพ", "กรุงเทพมหานคร"], "Chiang Mai": ["เชียงใหม่"],
    "Delhi": ["दिल्ली"], "Mumbai": ["मुंबई"], "Kolkata": ["कोलकाता"],
}
