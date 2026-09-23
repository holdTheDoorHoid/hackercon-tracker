"""Locations, distances and travel estimates.

Every conference gets a lat/lon. Most come from the built-in table below so
the app works offline; anything unknown is looked up once through the free
OpenStreetMap geocoder and cached in data/geocache.json.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

from .db import DATA_DIR

GEOCACHE = DATA_DIR / "geocache.json"

# (lat, lon) keyed by "city|region|country" in lowercase. Region may be "".
CITY_COORDS: dict[str, tuple[float, float]] = {
    # --- Pennsylvania / New Jersey / Delaware (home turf) ---
    "philadelphia|pa|us": (39.9526, -75.1652),
    "scranton|pa|us": (41.4090, -75.6624),
    "wilkes-barre|pa|us": (41.2459, -75.8813),
    "harrisburg|pa|us": (40.2732, -76.8867),
    "pittsburgh|pa|us": (40.4406, -79.9959),
    "johnstown|pa|us": (40.3267, -78.9220),
    "lancaster|pa|us": (40.0379, -76.3055),
    "state college|pa|us": (40.7934, -77.8600),
    "glassboro|nj|us": (39.7029, -75.1118),
    "atlantic city|nj|us": (39.3643, -74.4229),
    "wall|nj|us": (40.1590, -74.0596),
    "newark|de|us": (39.6837, -75.7497),
    "wilmington|de|us": (39.7391, -75.5398),
    "long island|ny|us": (40.7891, -73.1350),
    "syracuse|ny|us": (43.0481, -76.1474),
    # --- Northeast ---
    "new york|ny|us": (40.7128, -74.0060),
    "new york city|ny|us": (40.7128, -74.0060),
    "nyc|ny|us": (40.7128, -74.0060),
    "brooklyn|ny|us": (40.6782, -73.9442),
    "rochester|ny|us": (43.1566, -77.6088),
    "buffalo|ny|us": (42.8864, -78.8784),
    "albany|ny|us": (42.6526, -73.7562),
    "fairfield|ct|us": (41.1408, -73.2613),
    "hartford|ct|us": (41.7658, -72.6734),
    "boston|ma|us": (42.3601, -71.0589),
    "cambridge|ma|us": (42.3736, -71.1097),
    "somerville|ma|us": (42.3876, -71.0995),
    "portland|me|us": (43.6591, -70.2568),
    "providence|ri|us": (41.8240, -71.4128),
    "manchester|nh|us": (42.9956, -71.4548),
    "burlington|vt|us": (44.4759, -73.2121),
    "baltimore|md|us": (39.2904, -76.6122),
    "national harbor|md|us": (38.7824, -77.0166),
    "washington|dc|us": (38.9072, -77.0369),
    "arlington|va|us": (38.8816, -77.0910),
    "richmond|va|us": (37.5407, -77.4360),
    "roanoke|va|us": (37.2710, -79.9414),
    "norfolk|va|us": (36.8508, -76.2859),
    "charlottesville|va|us": (38.0293, -78.4767),
    # --- Southeast ---
    "charlotte|nc|us": (35.2271, -80.8431),
    "raleigh|nc|us": (35.7796, -78.6382),
    "durham|nc|us": (35.9940, -78.8986),
    "asheville|nc|us": (35.5951, -82.5515),
    "greenville|sc|us": (34.8526, -82.3940),
    "charleston|sc|us": (32.7765, -79.9311),
    "atlanta|ga|us": (33.7490, -84.3880),
    "augusta|ga|us": (33.4735, -82.0105),
    "savannah|ga|us": (32.0809, -81.0912),
    "nashville|tn|us": (36.1627, -86.7816),
    "franklin|tn|us": (35.9251, -86.8689),
    "knoxville|tn|us": (35.9606, -83.9207),
    "memphis|tn|us": (35.1495, -90.0490),
    "chattanooga|tn|us": (35.0456, -85.3097),
    "birmingham|al|us": (33.5186, -86.8104),
    "huntsville|al|us": (34.7304, -86.5861),
    "louisville|ky|us": (38.2527, -85.7585),
    "lexington|ky|us": (38.0406, -84.5037),
    "orlando|fl|us": (28.5383, -81.3792),
    "tampa|fl|us": (27.9506, -82.4572),
    "st. petersburg|fl|us": (27.7676, -82.6403),
    "miami|fl|us": (25.7617, -80.1918),
    "miami beach|fl|us": (25.7907, -80.1300),
    "fort lauderdale|fl|us": (26.1224, -80.1373),
    "fort myers|fl|us": (26.6406, -81.8723),
    "jacksonville|fl|us": (30.3322, -81.6557),
    "melbourne|fl|us": (28.0836, -80.6081),
    "cape canaveral|fl|us": (28.3922, -80.6077),
    "kennedy space center|fl|us": (28.5729, -80.6490),
    "new orleans|la|us": (29.9511, -90.0715),
    "little rock|ar|us": (34.7465, -92.2896),
    "bentonville|ar|us": (36.3729, -94.2088),
    # --- Midwest ---
    "chicago|il|us": (41.8781, -87.6298),
    "peoria|il|us": (40.6936, -89.5890),
    "bloomington|in|us": (39.1653, -86.5264),
    "indianapolis|in|us": (39.7684, -86.1581),
    "fort wayne|in|us": (41.0793, -85.1394),
    "columbus|oh|us": (39.9612, -82.9988),
    "cleveland|oh|us": (41.4993, -81.6944),
    "cincinnati|oh|us": (39.1031, -84.5120),
    "dayton|oh|us": (39.7589, -84.1916),
    "fairborn|oh|us": (39.8209, -84.0194),
    "detroit|mi|us": (42.3314, -83.0458),
    "grand rapids|mi|us": (42.9634, -85.6681),
    "milwaukee|wi|us": (43.0389, -87.9065),
    "madison|wi|us": (43.0731, -89.4012),
    "minneapolis|mn|us": (44.9778, -93.2650),
    "st paul|mn|us": (44.9537, -93.0900),
    "saint paul|mn|us": (44.9537, -93.0900),
    "des moines|ia|us": (41.5868, -93.6250),
    "davenport|ia|us": (41.5236, -90.5776),
    "omaha|ne|us": (41.2565, -95.9345),
    "kansas city|mo|us": (39.0997, -94.5786),
    "kansas city|ks|us": (39.1141, -94.6275),
    "overland park|ks|us": (38.9822, -94.6708),
    "st. louis|mo|us": (38.6270, -90.1994),
    "springfield|mo|us": (37.2090, -93.2923),
    "madison|sd|us": (44.0061, -97.1140),
    "deadwood|sd|us": (44.3767, -103.7296),
    "sioux falls|sd|us": (43.5460, -96.7313),
    "tulsa|ok|us": (36.1540, -95.9928),
    "glenpool|ok|us": (35.9551, -96.0089),
    "oklahoma city|ok|us": (35.4676, -97.5164),
    # --- South / Texas ---
    "austin|tx|us": (30.2672, -97.7431),
    "san antonio|tx|us": (29.4241, -98.4936),
    "dallas|tx|us": (32.7767, -96.7970),
    "dallas/fort worth|tx|us": (32.8998, -97.0403),
    "houston|tx|us": (29.7604, -95.3698),
    "el paso|tx|us": (31.7619, -106.4850),
    # --- Mountain / West ---
    "denver|co|us": (39.7392, -104.9903),
    "boulder|co|us": (40.0150, -105.2705),
    "colorado springs|co|us": (38.8339, -104.8214),
    "salt lake city|ut|us": (40.7608, -111.8910),
    "sandy|ut|us": (40.5649, -111.8390),
    "logan|ut|us": (41.7370, -111.8338),
    "park city|ut|us": (40.6461, -111.4980),
    "st. george|ut|us": (37.0965, -113.5684),
    "saint george|ut|us": (37.0965, -113.5684),
    "boise|id|us": (43.6150, -116.2023),
    "idaho falls|id|us": (43.4917, -112.0339),
    "missoula|mt|us": (46.8721, -113.9940),
    "bozeman|mt|us": (45.6770, -111.0429),
    "albuquerque|nm|us": (35.0844, -106.6504),
    "santa fe|nm|us": (35.6870, -105.9378),
    "phoenix|az|us": (33.4484, -112.0740),
    "mesa|az|us": (33.4152, -111.8315),
    "scottsdale|az|us": (33.4942, -111.9261),
    "las vegas|nv|us": (36.1699, -115.1398),
    "reno|nv|us": (39.5296, -119.8138),
    # --- Pacific ---
    "los angeles|ca|us": (34.0522, -118.2437),
    "pasadena|ca|us": (34.1478, -118.1445),
    "hawthorne|ca|us": (33.9164, -118.3526),
    "orange county|ca|us": (33.7175, -117.8311),
    "san diego|ca|us": (32.7157, -117.1611),
    "san francisco|ca|us": (37.7749, -122.4194),
    "san jose|ca|us": (37.3382, -121.8863),
    "santa clara|ca|us": (37.3541, -121.9552),
    "san mateo|ca|us": (37.5630, -122.3255),
    "vallejo|ca|us": (38.1041, -122.2566),
    "sacramento|ca|us": (38.5816, -121.4944),
    "rocklin|ca|us": (38.7907, -121.2358),
    "fresno|ca|us": (36.7378, -119.7871),
    "portland|or|us": (45.5152, -122.6784),
    "seattle|wa|us": (47.6062, -122.3321),
    "spokane|wa|us": (47.6588, -117.4260),
    "anchorage|ak|us": (61.2181, -149.9003),
    "honolulu|hi|us": (21.3069, -157.8583),
    # --- Canada ---
    "montreal|qc|ca": (45.5017, -73.5673),
    "quebec city|qc|ca": (46.8139, -71.2080),
    "ottawa|on|ca": (45.4215, -75.6972),
    "toronto|on|ca": (43.6532, -79.3832),
    "london|on|ca": (42.9849, -81.2453),
    "halifax|ns|ca": (44.6488, -63.5752),
    "fredericton|nb|ca": (45.9636, -66.6431),
    "st. john's|nl|ca": (47.5615, -52.7126),
    "winnipeg|mb|ca": (49.8951, -97.1384),
    "regina|sk|ca": (50.4452, -104.6189),
    "saskatoon|sk|ca": (52.1332, -106.6700),
    "calgary|ab|ca": (51.0447, -114.0719),
    "edmonton|ab|ca": (53.5461, -113.4938),
    "vancouver|bc|ca": (49.2827, -123.1207),
    "victoria|bc|ca": (48.4284, -123.3656),
    "whitehorse|yt|ca": (60.7212, -135.0568),
    # --- Europe / elsewhere (the ones on the list) ---
    "london||gb": (51.5074, -0.1278),
    "manchester||gb": (53.4808, -2.2426),
    "leeds||gb": (53.8008, -1.5491),
    "lancaster||gb": (54.0466, -2.8007),
    "bristol||gb": (51.4545, -2.5879),
    "sheffield||gb": (53.3811, -1.4701),
    "glasgow||gb": (55.8642, -4.2518),
    "belfast||gb": (54.5973, -5.9301),
    "cheltenham||gb": (51.8994, -2.0783),
    "exeter||gb": (50.7184, -3.5339),
    "bournemouth||gb": (50.7192, -1.8808),
    "cardiff||gb": (51.4816, -3.1791),
    "aberystwyth||gb": (52.4153, -4.0829),
    "jersey||gb": (49.2144, -2.1312),
    "dublin||ie": (53.3498, -6.2603),
    "galway||ie": (53.2707, -9.0568),
    "reykjavik||is": (64.1466, -21.9426),
    "paris||fr": (48.8566, 2.3522),
    "brest||fr": (48.3904, -4.4861),
    "lille||fr": (50.6292, 3.0573),
    "calais||fr": (50.9513, 1.8587),
    "brussels||be": (50.8503, 4.3517),
    "ghent||be": (51.0543, 3.7174),
    "gent||be": (51.0543, 3.7174),
    "hasselt||be": (50.9307, 5.3378),
    "amsterdam||nl": (52.3676, 4.9041),
    "haarlem||nl": (52.3874, 4.6462),
    "groningen||nl": (53.2194, 6.5665),
    "delft||nl": (52.0116, 4.3571),
    "luxembourg||lu": (49.6116, 6.1319),
    "belval||lu": (49.5030, 5.9470),
    "dommeldange||lu": (49.6340, 6.1360),
    "zurich||ch": (47.3769, 8.5417),
    "lausanne||ch": (46.5197, 6.6323),
    "solothurn||ch": (47.2088, 7.5323),
    "berlin||de": (52.5200, 13.4050),
    "hamburg||de": (53.5511, 9.9937),
    "frankfurt||de": (50.1109, 8.6821),
    "munich||de": (48.1351, 11.5820),
    "dresden||de": (51.0504, 13.7373),
    "hannover||de": (52.3759, 9.7320),
    "wuppertal||de": (51.2562, 7.1508),
    "minden||de": (52.2895, 8.9146),
    "vienna||at": (48.2082, 16.3738),
    "innsbruck||at": (47.2692, 11.4041),
    "prague||cz": (50.0755, 14.4378),
    "vrchlabi||cz": (50.6272, 15.6094),
    "budapest||hu": (47.4979, 19.0402),
    "krakow||pl": (50.0647, 19.9450),
    "bialystok||pl": (53.1325, 23.1688),
    "warsaw||pl": (52.2297, 21.0122),
    "stockholm||se": (59.3293, 18.0686),
    "gothenburg||se": (57.7089, 11.9746),
    "goteborg||se": (57.7089, 11.9746),
    "umea||se": (63.8258, 20.2630),
    "oslo||no": (59.9139, 10.7522),
    "kristiansand||no": (58.1599, 8.0182),
    "copenhagen||dk": (55.6761, 12.5683),
    "helsinki||fi": (60.1699, 24.9384),
    "tallinn||ee": (59.4370, 24.7536),
    "lisbon||pt": (38.7223, -9.1393),
    "porto||pt": (41.1579, -8.6291),
    "barcelona||es": (41.3874, 2.1686),
    "malaga||es": (36.7213, -4.4214),
    "seville||es": (37.3891, -5.9845),
    "rome||it": (41.9028, 12.4964),
    "bergamo||it": (45.6983, 9.6773),
    "trieste||it": (45.6495, 13.7768),
    "caserta||it": (41.0730, 14.3330),
    "cagliari||it": (39.2238, 9.1217),
    "malta||mt": (35.8989, 14.5146),
    "zadar||hr": (44.1194, 15.2314),
    "sarajevo||ba": (43.8563, 18.4131),
    "tirana||al": (41.3275, 19.8187),
    "novi sad||rs": (45.2671, 19.8335),
    "chisinau||md": (47.0105, 28.8638),
    "bucharest||ro": (44.4268, 26.1025),
    "tel aviv||il": (32.0853, 34.7818),
    "marrakesh||ma": (31.6295, -7.9811),
    "amman||jo": (31.9454, 35.9284),
    "manama||bh": (26.2285, 50.5860),
    "accra||gh": (5.6037, -0.1870),
    "singapore||sg": (1.3521, 103.8198),
    "kochi||in": (9.9312, 76.2673),
    "mumbai||in": (19.0760, 72.8777),
    "tokyo||jp": (35.6762, 139.6503),
    "kyoto||jp": (35.0116, 135.7681),
    "seoul||kr": (37.5665, 126.9780),
    "guangzhou||cn": (23.1291, 113.2644),
    "curitiba||br": (-25.4284, -49.2733),
    "canberra||au": (-35.2809, 149.1300),
    "perth||au": (-31.9505, 115.8605),
}

# Common region spellings -> two-letter codes
REGION_ALIASES = {
    "pennsylvania": "pa", "new jersey": "nj", "delaware": "de", "new york": "ny", "connecticut": "ct",
    "massachusetts": "ma", "maine": "me", "maryland": "md", "virginia": "va", "north carolina": "nc",
    "south carolina": "sc", "georgia": "ga", "florida": "fl", "tennessee": "tn", "ohio": "oh",
    "michigan": "mi", "illinois": "il", "indiana": "in", "wisconsin": "wi", "minnesota": "mn",
    "iowa": "ia", "missouri": "mo", "kansas": "ks", "nebraska": "ne", "south dakota": "sd",
    "oklahoma": "ok", "texas": "tx", "colorado": "co", "utah": "ut", "arizona": "az", "nevada": "nv",
    "california": "ca", "oregon": "or", "washington": "wa", "montana": "mt", "idaho": "id",
    "new mexico": "nm", "louisiana": "la", "alabama": "al", "kentucky": "ky", "arkansas": "ar",
    "ontario": "on", "ont": "on", "quebec": "qc", "alberta": "ab", "british columbia": "bc",
    "manitoba": "mb", "saskatchewan": "sk", "nova scotia": "ns", "new brunswick": "nb",
    "newfoundland and labrador": "nl", "yukon": "yt",
}

COUNTRY_ALIASES = {
    "usa": "US", "us": "US", "united states": "US", "u.s.": "US", "u.s.a.": "US",
    "canada": "CA", "ca": "CA", "uk": "GB", "gb": "GB", "united kingdom": "GB", "uk (n ireland)": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "ireland": "IE", "iceland": "IS",
    "france": "FR", "fr": "FR", "belgium": "BE", "be": "BE", "netherlands": "NL", "nl": "NL",
    "the netherlands": "NL", "luxembourg": "LU", "lu": "LU", "germany": "DE", "de": "DE",
    "switzerland": "CH", "austria": "AT", "czech republic": "CZ", "czechia": "CZ", "cr": "CZ",
    "hungary": "HU", "poland": "PL", "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "estonia": "EE", "portugal": "PT", "spain": "ES", "italy": "IT", "malta": "MT", "croatia": "HR",
    "bosnia": "BA", "albania": "AL", "serbia": "RS", "moldova": "MD", "moldova, republic of": "MD",
    "romania": "RO", "israel": "IL", "morocco": "MA", "jordan": "JO", "bahrain": "BH", "ghana": "GH",
    "singapore": "SG", "india": "IN", "in": "IN", "japan": "JP", "south korea": "KR", "korea": "KR",
    "china": "CN", "brazil": "BR", "australia": "AU", "mexico": "MX", "nepal": "NP", "turkey": "TR",
    "azerbaijan": "AZ",
}

US_STATE_SET = set(v for k, v in REGION_ALIASES.items())


def norm_country(c: str | None) -> str | None:
    if not c:
        return None
    c = c.strip()
    return COUNTRY_ALIASES.get(c.lower(), c.upper() if len(c) == 2 else c)


def norm_region(r: str | None) -> str:
    if not r:
        return ""
    r = r.strip().lower().strip(".")
    return REGION_ALIASES.get(r, r)


def split_location(text: str | None) -> tuple[str | None, str | None, str | None]:
    """'Scranton, PA' -> ('Scranton', 'PA', None). Handles 'Fort Lauderdale, FL, USA' too."""
    if not text:
        return None, None, None
    parts = [p.strip() for p in re.split(r"[,/]", text) if p.strip()]
    if not parts:
        return None, None, None
    city = parts[0]
    region = None
    country = None
    if len(parts) >= 2:
        second = parts[1]
        if norm_country(second) in ("US", "CA") and second.lower() not in REGION_ALIASES and len(second) != 2:
            country = norm_country(second)
        elif second.lower() in REGION_ALIASES or (len(second) == 2 and second.isalpha()):
            region = second.upper() if len(second) == 2 else REGION_ALIASES.get(second.lower(), second).upper()
        else:
            country = norm_country(second)
    if len(parts) >= 3:
        country = norm_country(parts[2]) or country
    return city, region, country


def lookup(city: str | None, region: str | None, country: str | None) -> tuple[float, float] | None:
    if not city:
        return None
    c = unicodedata.normalize("NFKD", city.strip().lower()).encode("ascii", "ignore").decode()
    c = c.replace("saint ", "st. ").replace("st ", "st. ").replace("’", "'")
    r = norm_region(region)
    k = norm_country(country)
    k = (k or "").lower()
    cands = [f"{c}|{r}|{k}", f"{c}||{k}"]
    if not k:
        cands += [f"{c}|{r}|us", f"{c}||us", f"{c}|{r}|ca"]
    for key in cands:
        if key in CITY_COORDS:
            return CITY_COORDS[key]
    # last resort: any entry whose city matches
    for key, v in CITY_COORDS.items():
        if key.split("|")[0] == c and (not k or key.endswith("|" + k)):
            return v
    return None


def _load_cache() -> dict:
    try:
        return json.loads(GEOCACHE.read_text())
    except Exception:
        return {}


def geocode(city, region, country) -> tuple[float, float] | None:
    """Built-in table first, then cached OpenStreetMap lookup."""
    hit = lookup(city, region, country)
    if hit:
        return hit
    if not city:
        return None
    key = f"{city}|{region or ''}|{country or ''}".lower()
    cache = _load_cache()
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None
    try:
        import httpx
        q = ", ".join(p for p in (city, region, country) if p)
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"q": q, "format": "json", "limit": 1},
                      headers={"User-Agent": "hackercon-tracker/1.0 (conference planning tool)"}, timeout=15)
        data = r.json()
        val = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
    except Exception:
        val = None
    cache[key] = val
    try:
        GEOCACHE.parent.mkdir(parents=True, exist_ok=True)
        GEOCACHE.write_text(json.dumps(cache, indent=1))
    except Exception:
        pass
    return val


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def travel_estimate(conf: dict, settings: dict) -> dict:
    """Return {'mode': 'drive'|'fly'|'online'|'unknown', 'miles', 'drive_hours', 'international'}."""
    if conf.get("online"):
        return {"mode": "online", "miles": 0, "drive_hours": 0, "international": False, "label": "Online"}
    lat, lon = conf.get("lat"), conf.get("lon")
    if lat is None or lon is None:
        return {"mode": "unknown", "miles": None, "drive_hours": None, "international": False, "label": "Location unknown"}
    if not settings.get("home_lat") or not settings.get("home_lon"):
        return {"mode": "unknown", "miles": None, "drive_hours": None, "international": False, "label": "Set your home city in Settings"}
    hlat, hlon = float(settings["home_lat"]), float(settings["home_lon"])
    miles = haversine_miles(hlat, hlon, lat, lon)
    road = miles * float(settings["road_factor"])
    hours = road / float(settings["avg_mph"])
    intl = (conf.get("country") or "US") not in ("US",)
    far_intl = intl and (conf.get("country") or "") not in ("CA", "MX")
    if hours <= float(settings["drive_max_hours"]) and not far_intl:
        mode = "drive"
        label = f"Drive ~{hours:.1f} h ({road:.0f} mi)"
    else:
        mode = "fly"
        label = f"Fly ({miles:.0f} mi straight line)"
        if far_intl:
            label = f"International flight ({miles:.0f} mi)"
    return {"mode": mode, "miles": round(miles), "road_miles": round(road), "drive_hours": round(hours, 1),
            "international": intl, "far_international": far_intl, "label": label}
