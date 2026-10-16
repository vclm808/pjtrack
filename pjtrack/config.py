"""Static configuration: tracked airports, region, business-jet type codes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "pjtrack.db"
EVENTS_CSV = ROOT / "events.csv"
SCOPE_FILE = ROOT / "scope.txt"
ENV_FILE = ROOT / ".env"

# Middle East airports with meaningful business-aviation traffic.
# country = ISO-3166 alpha-2. tier 1 = fetched first during backfill.
AIRPORTS = {
    # UAE
    "OMDB": ("AE", 1), "OMDW": ("AE", 1), "OMAA": ("AE", 1), "OMAD": ("AE", 1), "OMSJ": ("AE", 2),
    # Saudi Arabia
    "OERK": ("SA", 1), "OEJN": ("SA", 1), "OEDF": ("SA", 2), "OEMA": ("SA", 2),
    # Qatar
    "OTHH": ("QA", 1), "OTBD": ("QA", 2),
    # Kuwait, Bahrain, Oman
    "OKKK": ("KW", 1), "OBBI": ("BH", 1), "OOMS": ("OM", 2),
    # Israel
    "LLBG": ("IL", 1), "LLER": ("IL", 2),
    # Jordan, Lebanon, Syria
    "OJAI": ("JO", 1), "OJAM": ("JO", 2), "OLBA": ("LB", 1), "OSDI": ("SY", 2),
    # Iran
    "OIII": ("IR", 1), "OIIE": ("IR", 1), "OIMM": ("IR", 2), "OISS": ("IR", 2),
    # Iraq
    "ORBI": ("IQ", 1), "ORER": ("IQ", 2),
    # Egypt
    "HECA": ("EG", 1), "HESH": ("EG", 2),
    # Turkey
    "LTFM": ("TR", 1), "LTBA": ("TR", 1), "LTFJ": ("TR", 2),
    # Cyprus (common evacuation/hop point)
    "LCLK": ("CY", 2),
    # Yemen
    "OYSN": ("YE", 2),
}

COUNTRY_NAMES = {
    "AE": "UAE", "SA": "Saudi Arabia", "QA": "Qatar", "KW": "Kuwait", "BH": "Bahrain",
    "OM": "Oman", "IL": "Israel", "JO": "Jordan", "LB": "Lebanon", "SY": "Syria",
    "IR": "Iran", "IQ": "Iraq", "EG": "Egypt", "TR": "Turkey", "CY": "Cyprus", "YE": "Yemen",
}

# Display names for common destinations outside the region (labels only, not tracked).
DEST_NAMES = {
    "GB": "United Kingdom", "FR": "France", "CH": "Switzerland", "DE": "Germany", "IT": "Italy",
    "ES": "Spain", "GR": "Greece", "AT": "Austria", "NL": "Netherlands", "MC": "Monaco", "US": "United States",
    "RU": "Russia", "IN": "India", "PK": "Pakistan", "MA": "Morocco", "AZ": "Azerbaijan", "GE": "Georgia",
    "MV": "Maldives", "SC": "Seychelles", "TH": "Thailand", "SG": "Singapore", "CN": "China", "AL": "Albania",
    "ME": "Montenegro", "MT": "Malta", "PT": "Portugal", "KE": "Kenya", "ZA": "South Africa", "ET": "Ethiopia",
}

# Bounding box used by the live tracker to decide "in region" (lat_min, lat_max, lon_min, lon_max).
REGION_BBOX = (12.0, 42.5, 25.0, 63.5)

# ICAO aircraft type designators for business jets. Airliner-type VIP jets (BBJ/ACJ)
# share codes with commercial aircraft and are deliberately excluded.
BIZJET_TYPES = {
    # Gulfstream
    "GLF2", "GLF3", "GLF4", "GLF5", "GLF6", "GA5C", "GA6C", "GA7C", "GA8C", "G280", "GALX", "G150", "ASTR",
    # Bombardier
    "GLEX", "GL5T", "GL7T", "GL8T", "CL30", "CL35", "CL60",
    "LJ31", "LJ35", "LJ40", "LJ45", "LJ55", "LJ60", "LJ70", "LJ75",
    # Dassault
    "FA10", "FA20", "FA50", "F900", "F2TH", "FA6X", "FA7X", "FA8X",
    # Embraer
    "E50P", "E55P", "E545", "E550", "E35L",
    # Cessna / Textron
    "C500", "C501", "C510", "C525", "C25A", "C25B", "C25C", "C25M", "C550", "C551", "C560", "C56X",
    "C650", "C680", "C68A", "C700", "C750", "H25A", "H25B", "H25C", "HA4T", "BE40", "PRM1",
    # Others
    "HDJT", "PC24", "EA50", "SF50", "PRM1",
}

# Live tracker: centres of 250 nm circles covering the region's main airports (lat, lon).
LIVE_POINTS = [
    (24.8, 55.0),   # Dubai / Abu Dhabi / Sharjah
    (25.9, 50.8),   # Doha / Bahrain / Dammam
    (24.8, 46.7),   # Riyadh
    (22.5, 39.6),   # Jeddah / Medina
    (29.4, 47.8),   # Kuwait / Basra
    (23.5, 58.3),   # Muscat
    (32.5, 35.3),   # Israel / Jordan / Lebanon / Syria / Cyprus
    (29.0, 31.8),   # Cairo / Sharm el-Sheikh
    (40.6, 29.5),   # Istanbul
    (34.5, 44.0),   # Baghdad / Erbil
    (35.0, 51.5),   # Tehran
    (30.0, 54.0),   # Shiraz / southern Iran
    (36.2, 59.6),   # Mashhad
    (15.4, 44.2),   # Sanaa
]
LIVE_INTERVAL_S = 300          # seconds between regional polling cycles
LIVE_SWEEP_S = 3600            # seconds between global per-type sweeps (follows jets out of region)
OPENSKY_DAILY_BUDGET = 3600    # leave headroom under the 4000 standard-tier credits
