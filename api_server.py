#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Petit serveur local pour le prototype "recherche d'annonces + résolution DPE".

- GET  /api/listings?city=Haubourdin&type=appartement&max_price=300000&min_surface=30
- GET  /api/listings?departments=59&type=appartement&max_price=300000
- GET  /api/geo/departements   (GeoJSON des départements + région)
- GET  /api/geo/communes?departments=59
- GET  /api/sources            (état des sources et des portails bloqués)
- POST /api/resolve            { city, type, surface, dpe, ges, street, ... }
- POST /api/analyze            { url }  → extraction de l'annonce + résolution DPE

Sources actives : Bien'ici, Logic-Immo (selon DataDome), Century21, Orpi,
Immonot, iad, Citya, ParuVendu, Nestenn, Laforêt. La production devra passer
par un agrégateur licencié ou des flux d'agences pour les portails protégés.
"""

import datetime
import hashlib
import html as htmlmod
import json
import os
from collections import Counter
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from run_test import fetch_dpe, haversine, norm, point_of, score

C21 = "https://www.century21.fr"
ORPI = "https://www.orpi.com"
IMMONOT = "https://www.immonot.com"
LOGIC = "https://www.logic-immo.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CACHE = {}
GEO_CACHE = {}
COMMUNES_CACHE = {}
SOURCE_LAST = {}
ENRICH_CACHE = {}
IMPORTED = []
DEP_GEOJSON = "https://raw.githubusercontent.com/gregoiredavid/france-geojson/master/departements.geojson"
DEP_META = "https://geo.api.gouv.fr/departements?fields=nom,code,region"

# Identifiants de régions Logic-Immo (stables, relevés sur logic-immo.com).
LOGIC_REGION_IDS = {
    "84": ("auvergne-rhone-alpes", "ad04fr31"),
    "27": ("bourgogne-franche-comte", "ad04fr13"),
    "53": ("bretagne", "ad04fr22"),
    "24": ("centre-val-de-loire", "ad04fr10"),
    "94": ("corse", "ad04fr34"),
    "44": ("grand-est", "ad04fr20"),
    "32": ("hauts-de-france", "ad04fr16"),
    "11": ("ile-de-france", "ad04fr5"),
    "28": ("normandie", "ad04fr14"),
    "75": ("nouvelle-aquitaine", "ad04fr27"),
    "76": ("occitanie", "ad04fr28"),
    "52": ("pays-de-la-loire", "ad04fr21"),
    "93": ("provence-alpes-cote-d-azur", "ad04fr33"),
}
LOGIC_DEPT_URLS = {}
LOGIC_CITY_URLS = {}
LOGIC_BLOCK_UNTIL = 0
# Raccourcis stables pour les villes de démonstration (évite un appel de découverte).
LOGIC_KNOWN_CITIES = {
    ("59", "haubourdin"): ("hauts-de-france", "haubourdin-59320", "ad08fr23556"),
    ("59", "lille"): ("hauts-de-france", "lille-59000", "ad08fr23619"),
    ("37", "tours"): ("centre-val-de-loire", "tours-37000", "ad08fr14783"),
}
LOGIC_KNOWN_DEPTS = {
    "59": ("hauts-de-france", "nord-59", "ad06fr60"),
    "37": ("centre-val-de-loire", "indre-et-loire-37", "ad06fr38"),
}

SOURCES_REGISTRY = [
    {"name": "Bien'ici", "mode": "API de recherche", "status": "actif",
     "note": "Agrège déjà de nombreuses agences (Citya, Guy Hoquet, Bonaparte…)."},
    {"name": "Logic-Immo", "mode": "HTML + JSON embarqué", "status": "actif",
     "note": "Portail du groupe Aviv (comme SeLoger). DataDome peut le protéger par rafales : la source est alors ignorée."},
    {"name": "Century21", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Réseau national d'agences."},
    {"name": "Orpi", "mode": "JSON-LD + HTML", "status": "actif",
     "note": "Réseau national d'agences."},
    {"name": "Immonot", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Annonces des notaires."},
    {"name": "iad", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Réseau national de mandataires."},
    {"name": "Citya", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Réseau national d'agences ; 14 annonces à Haubourdin."},
    {"name": "ParuVendu", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Portail de petites annonces ; 26 annonces à Haubourdin."},
    {"name": "Nestenn", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Réseau national d'agences."},
    {"name": "Laforêt", "mode": "HTML rendu serveur", "status": "actif",
     "note": "Réseau national d'agences."},
    {"name": "PAP", "mode": "Cloudflare Turnstile", "status": "bloqué",
     "note": "Partenariat ou flux XML d'agence nécessaire."},
    {"name": "SeLoger", "mode": "Import assisté (extension / WebView)", "status": "assisté",
     "note": "DataDome bloque les serveurs. Ouvre SeLoger dans ton navigateur, puis importe la page via l'extension Valentine."},
    {"name": "Leboncoin", "mode": "Import assisté (extension / WebView)", "status": "assisté",
     "note": "DataDome bloque les serveurs. Ouvre Leboncoin dans ton navigateur, puis importe la page via l'extension Valentine. API pro disponible pour une app publique."},
    {"name": "Figaro Immo", "mode": "Cloudflare", "status": "bloqué",
     "note": "Partenariat éditorial nécessaire."},
    {"name": "Ouest-France Immo", "mode": "Akamai", "status": "bloqué",
     "note": "Partenariat / flux régional nécessaire."},
]


def fetch_html(url, timeout=30):
    if url in CACHE:
        return CACHE[url]
    last_error = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read().decode("utf-8", errors="replace")
            CACHE[url] = data
            return data
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code in (403, 429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(0.8)
                continue
            raise
    raise last_error


def get_departements_meta():
    if "dep_meta" not in GEO_CACHE:
        data = json.loads(fetch_html(DEP_META, timeout=30))
        GEO_CACHE["dep_meta"] = {d["code"]: d for d in data}
    return GEO_CACHE["dep_meta"]


def get_departements_geo():
    if "dep_geo" not in GEO_CACHE:
        geo = json.loads(fetch_html(DEP_GEOJSON, timeout=60))
        meta = get_departements_meta()
        for f in geo.get("features", []):
            code = f.get("properties", {}).get("code")
            m = meta.get(code, {})
            reg = m.get("region") or {}
            f["properties"]["region_code"] = reg.get("code")
            f["properties"]["region_nom"] = reg.get("nom")
        GEO_CACHE["dep_geo"] = geo
    return GEO_CACHE["dep_geo"]


def dept_slug(name):
    return re.sub(r"[^a-z0-9]+", "_", norm(name)).strip("_")


def get_communes(departments):
    results = []
    for code in departments:
        if code not in COMMUNES_CACHE:
            url = (
                f"https://geo.api.gouv.fr/communes?codeDepartement={urllib.parse.quote(code)}"
                "&fields=nom,code,centre,population"
            )
            data = fetch_json_url(url)
            communes = []
            for c in data:
                centre = c.get("centre") or {}
                coords = centre.get("coordinates") or []
                if len(coords) != 2:
                    continue
                communes.append({
                    "nom": c.get("nom"),
                    "code": c.get("code"),
                    "lat": coords[1],
                    "lon": coords[0],
                    "population": c.get("population") or 0,
                    "department": code,
                })
            COMMUNES_CACHE[code] = communes
        results.extend(COMMUNES_CACHE[code])
    return results


def slugify_hyphen(name):
    return re.sub(r"[^a-z0-9]+", "-", norm(name)).strip("-")


def smart_title(name):
    """Titre lisible sans casser les particules (Hallennes-lez-Haubourdin)."""
    if not name:
        return name
    small = {"de", "du", "des", "la", "le", "les", "l", "d", "sur", "sous", "en", "aux", "au", "et"}
    words = re.split(r"([\s\-'])", str(name).strip())
    out = []
    first = True
    for w in words:
        if not w or re.fullmatch(r"[\s\-']", w):
            out.append(w)
            continue
        low = norm(w)
        if not first and low in small:
            out.append(low)
        else:
            out.append(w[:1].upper() + w[1:].lower())
        first = False
    return re.sub(r"\s+", " ", "".join(out)).strip()


def parse_price(text):
    m = re.search(r"(?<![\w])([\d][\d \t\xa0.,]*)[ \t\xa0]*(?:€|euros?|eur)\b", text or "", re.I)
    if not m:
        return None
    return to_float(m.group(1))


def passes_filters(row, type_, max_price=None, min_price=None, min_surface=None, max_surface=None,
                   min_rooms=None, max_rooms=None, dpe_max=None, ges_max=None):
    if type_ and row.get("type") and row["type"] != type_:
        return False
    if max_price and row.get("price") and row["price"] > max_price:
        return False
    if min_price and row.get("price") and row["price"] < min_price:
        return False
    if min_surface and (row.get("surface") is None or row["surface"] < min_surface):
        return False
    if max_surface and (row.get("surface") is None or row["surface"] > max_surface):
        return False
    if min_rooms and row.get("rooms") and row["rooms"] < min_rooms:
        return False
    if max_rooms and row.get("rooms") and row["rooms"] > max_rooms:
        return False
    if dpe_max and row.get("dpe") and row["dpe"] in "ABCDEFG":
        if "ABCDEFG".index(row["dpe"]) > "ABCDEFG".index(dpe_max):
            return False
    if ges_max and row.get("ges") and row["ges"] in "ABCDEFG":
        if "ABCDEFG".index(row["ges"]) > "ABCDEFG".index(ges_max):
            return False
    return True


def ban_city_center(city):
    key = "ban_center:" + norm(city)
    if key in GEO_CACHE:
        return GEO_CACHE[key]
    if not city:
        return None
    url = (
        "https://api-adresse.data.gouv.fr/search/?q="
        + urllib.parse.quote(city)
        + "&type=municipality&limit=1"
    )
    try:
        data = fetch_json_url(url)
        f = (data.get("features") or [None])[0]
        coords = ((f or {}).get("geometry") or {}).get("coordinates") or []
        center = {"lat": coords[1], "lon": coords[0]} if len(coords) == 2 else None
    except Exception:
        center = None
    GEO_CACHE[key] = center
    return center


def ban_city_info(city):
    key = "ban_city:" + norm(city)
    if key in GEO_CACHE:
        return GEO_CACHE[key]
    url = (
        "https://api-adresse.data.gouv.fr/search/?q="
        + urllib.parse.quote(city)
        + "&type=municipality&limit=1"
    )
    data = fetch_json_url(url)
    f = (data.get("features") or [None])[0]
    info = f.get("properties") if f else None
    GEO_CACHE[key] = info
    return info


def dept_name_slug(name):
    return slugify_hyphen(name)


# ---------------------------------------------------------------------------
# Bien'ici : API de recherche (source principale du prototype)
# ---------------------------------------------------------------------------
BIENICI_ADS = "https://www.bienici.com/realEstateAds.json"
BIENICI_PLACE = "https://res.bienici.com/place.json"
BAN_REVERSE = "https://api-adresse.data.gouv.fr/reverse/"


def fetch_json_url(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def bienici_place(q):
    url = (
        f"{BIENICI_PLACE}?q={urllib.parse.quote(q)}"
        "&type=city,delegated-city,department,postalCode,region&prefix=no"
    )
    return fetch_json_url(url)


def bienici_zone_ids(city=None, departments=None, cities=None):
    queries = []
    if cities:
        # Les villes sont plus précises que le département : elles priment.
        queries = list(cities)
    elif departments:
        meta = get_departements_meta()
        for code in departments:
            name = (meta.get(code) or {}).get("nom") or code
            queries.append(name)
    elif city:
        queries.append(city)
    ids = []
    for q in queries:
        try:
            data = bienici_place(q)
            ids.extend(data.get("zoneIds") or [])
        except Exception:
            continue
    return list(dict.fromkeys(ids))


def bienici_normalize(a):
    # Certaines annonces "programme neuf" exposent des fourchettes
    # (prix/surface listes) : ce ne sont pas des biens uniques.
    for field in ("price", "surfaceArea", "roomsQuantity"):
        if isinstance(a.get(field), list):
            return None
    type_ = "maison" if a.get("propertyType") == "house" else "appartement"
    blur = a.get("blurInfo") or {}
    pos = blur.get("position") or blur.get("centroid") or {}
    pub = a.get("publicationDate") or ""
    mod = a.get("modificationDate") or ""
    is_pub = bool(pub and not pub.startswith("1970"))
    listing_date = pub if is_pub else mod
    district = a.get("district") or {}
    desc = strip_tags(a.get("description") or "")
    photos = [p.get("url") for p in (a.get("photos") or []) if p.get("url")]
    rooms = a.get("roomsQuantity")
    raw_heat = norm(a.get("heating") or "")
    if "electri" in raw_heat:
        heating = "électrique"
    elif "gaz" in raw_heat:
        heating = "gaz"
    elif "fioul" in raw_heat:
        heating = "fioul"
    elif "bois" in raw_heat:
        heating = "bois"
    elif "pompe" in raw_heat or "pac" in raw_heat:
        heating = "pompe à chaleur"
    else:
        heating = a.get("heating")
    title = f"{'Maison' if type_ == 'maison' else 'Appartement'}"
    if rooms:
        title += f" {rooms} pièces"
    title += f" — {a.get('city') or ''}"
    return {
        "id": a.get("id"),
        "source": "Bien'ici",
        "title": title,
        "type": type_,
        "city": a.get("city"),
        "postal_code": a.get("postalCode"),
        "price": a.get("price"),
        "surface": a.get("surfaceArea"),
        "rooms": rooms,
        "bedrooms": a.get("bedroomsQuantity"),
        "year": a.get("yearOfConstruction"),
        "heating": heating,
        "floor": a.get("floor"),
        "dpe": a.get("energyClassification"),
        "ges": a.get("greenhouseGazClassification"),
        "dpe_date": a.get("energyPerformanceDiagnosticDate"),
        "publication_date": pub if is_pub else None,
        "modification_date": mod,
        "listing_date": listing_date,
        "listing_date_kind": "publication" if is_pub else "modification",
        "address_known": a.get("addressKnown"),
        "blur_type": blur.get("type"),
        "blur_radius": blur.get("radius"),
        "lat": pos.get("lat"),
        "lon": pos.get("lon"),
        "district": district.get("name") or district.get("libelle"),
        "description": desc[:800],
        "photos": photos[:6],
        "url": f"https://www.bienici.com/annonce/{a.get('id')}",
    }


def bienici_search(zone_ids, type_, max_price=None, min_price=None, min_area=None, max_area=None,
                   min_rooms=None, max_rooms=None, pages=4, dpe_max=None, ges_max=None):
    prop = "house" if type_ == "maison" else "flat"
    ads = []
    total = 0
    for page in range(max(1, pages)):
        filters = {
            "size": 100,
            "from": page * 100,
            "page": page + 1,
            "filterType": "buy",
            "propertyType": [prop],
            "sortBy": "publicationDate",
            "sortOrder": "desc",
            "onTheMarket": [True],
            "zoneIdsByTypes": {"zoneIds": zone_ids},
        }
        if max_price:
            filters["maxPrice"] = max_price
        if min_price:
            filters["minPrice"] = min_price
        if min_area:
            filters["minArea"] = min_area
        if max_area:
            filters["maxArea"] = max_area
        if dpe_max and dpe_max in "ABCDEFG":
            filters["energyClassification"] = list("ABCDEFG"[: "ABCDEFG".index(dpe_max) + 1])
        url = (
            f"{BIENICI_ADS}?filters={urllib.parse.quote(json.dumps(filters, separators=(',', ':')))}"
            "&extensionType=extendedIfNoResult&enableGoogleStructuredDataAggregates=true&leadingCount=2"
        )
        try:
            data = fetch_json_url(url)
        except Exception:
            break
        total = data.get("total") or 0
        batch = data.get("realEstateAds") or []
        ads.extend(batch)
        if len(ads) >= total or not batch:
            break
    results = [r for r in (bienici_normalize(a) for a in ads) if r]
    if min_price:
        results = [r for r in results if r.get("price") and r["price"] >= min_price]
    if min_rooms:
        results = [r for r in results if r.get("rooms") and r["rooms"] >= min_rooms]
    if max_rooms:
        results = [r for r in results if r.get("rooms") and r["rooms"] <= max_rooms]
    if dpe_max and dpe_max in "ABCDEFG":
        allowed = set("ABCDEFG"[: "ABCDEFG".index(dpe_max) + 1])
        results = [r for r in results if r.get("dpe") in allowed]
    if ges_max and ges_max in "ABCDEFG":
        allowed = set("ABCDEFG"[: "ABCDEFG".index(ges_max) + 1])
        results = [r for r in results if r.get("ges") in allowed]
    return results[:400], total


# ---------------------------------------------------------------------------
# Sources HTML / JSON-LD (sans contournement de CAPTCHA)
# ---------------------------------------------------------------------------
SOURCE_ORDER = ["Bien'ici", "SeLoger", "iad", "Citya", "ParuVendu", "Nestenn",
                "Laforêt", "Logic-Immo", "Century21", "Orpi", "Immonot"]
MAX_CITY_TARGETS = 2


def c21_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    urls = []
    city_targets = []
    if cities:
        city_targets = list(cities)
    elif city:
        city_targets = [city]
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            urls.append(f"{C21}/annonces/achat-{type_path}/v-{slugify_hyphen(c)}/")
    elif departments:
        meta = get_departements_meta()
        for code in departments[:4]:
            name = (meta.get(code) or {}).get("nom") or code
            urls.append(f"{C21}/annonces/achat-{type_path}/d-{code.lower()}_{dept_slug(name)}/")
    if not urls:
        return []

    def fetch(u):
        try:
            return fetch_html(u)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as ex:
        pages = list(ex.map(fetch, urls))

    target_names = {norm(c) for c in city_targets} if city_targets else set()
    rows = []
    for page in pages:
        if not page:
            continue
        for m in re.finditer(r'data-uid="(\d+)"', page):
            end = page.find('data-uid="', m.start() + 5)
            block = page[m.start():end if end != -1 else m.start() + 20000]
            href_m = re.search(r'href="(/trouver_logement/detail/\d+/?)"', block)
            if not href_m:
                continue
            text = strip_tags(block)
            label_m = re.search(r'uppercase">\s*([^<]+?)\s*</div>', block)
            label = label_m.group(1).strip() if label_m else ""
            city_m = re.match(r"(.+?)\s+(\d{2,3})$", label)
            row_city = smart_title(city_m.group(1)) if city_m else (city_targets[0] if city_targets else "")
            if not row_city:
                upper_m = re.search(r"([A-ZÉÈÀÂÎÔÛÇ][A-ZÉÈÀÂÎÔÛÇ'’\- ]{2,40})\s+(\d{2,3})\b", text)
                if upper_m:
                    row_city = smart_title(upper_m.group(1))
            row_city = row_city or "Nord"
            if target_names and norm(row_city) not in target_names:
                continue
            price_m = re.search(r'tw-text-2xl[^>]*>\s*([\d\s\xa0]+)\s*&nbsp;€', block)
            price = to_float(price_m.group(1)) if price_m else parse_price(text)
            surf_m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:2|\u00b2)", text)
            surface = to_float(surf_m.group(1)) if surf_m else None
            kind_m = re.search(r",\s*(maison|appartement)\b", text, re.I)
            kind = kind_m.group(1).lower() if kind_m else type_
            rooms_m = re.search(r"(\d+)\s*(?:pcs?|pi[eè]ces?)\b", text, re.I)
            rooms = to_int(rooms_m.group(1)) if rooms_m else None
            img_m = re.search(r'<img[^>]+src="([^"]+)"', block)
            photo = img_m.group(1) if img_m else None
            if photo and photo.startswith("/"):
                photo = C21 + photo
            row = {
                "id": f"c21-{m.group(1)}",
                "source": "Century21",
                "title": f"{'Maison' if kind == 'maison' else 'Appartement'}"
                         + (f" {rooms} pièces" if rooms else "") + f" — {row_city}",
                "type": kind,
                "city": row_city,
                "postal_code": None,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": None,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": None,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": "",
                "photos": [photo] if photo else [],
                "url": C21 + href_m.group(1),
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def orpi_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    urls = []
    city_targets = []
    if cities:
        city_targets = list(cities)
    elif city:
        city_targets = [city]
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            urls.append(f"{ORPI}/annonces-immobilieres-{slugify_hyphen(c)}/vente-{type_path}/")
    elif departments:
        meta = get_departements_meta()
        for code in departments[:4]:
            name = (meta.get(code) or {}).get("nom") or code
            urls.append(f"{ORPI}/annonces-immobilieres-{slugify_hyphen(name)}/vente-{type_path}/")
    if not urls:
        return []

    def fetch(u):
        try:
            return fetch_html(u)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as ex:
        pages = list(ex.map(fetch, urls))

    target_names = {norm(c) for c in city_targets} if city_targets else set()
    rows = []
    for page in pages:
        if not page:
            continue
        items = []
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>([\s\S]*?)</script>', page, re.I):
            try:
                data = json.loads(m.group(1).strip())
            except Exception:
                continue
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                items.extend(data.get("itemListElement") or [])
        for item in items:
            url = item.get("url") or (item.get("item") or {}).get("url") or ""
            if "/annonce-vente-" not in url:
                continue
            item_data = item.get("item") or {}
            url_m = re.search(
                r"/annonce-vente-(appartement|maison)-t(\d+)-(.+)-(\d{5})-([a-z0-9-]+)/?",
                url,
            )
            if not url_m:
                continue
            kind, rooms_s, city_slug, postal, uid = url_m.groups()
            row_city = smart_title(city_slug.replace("-", " "))
            if target_names and norm(row_city) not in target_names:
                continue
            offers = item_data.get("offers") or {}
            price = to_float(offers.get("price"))
            image = item_data.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            path = urllib.parse.urlparse(url).path
            idx = page.rfind(path)
            block = page[max(0, idx - 4000): idx + 5000] if idx != -1 else ""
            text = strip_tags(block)
            surface = None
            for sm in re.finditer(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:2|\u00b2)", text):
                val = to_float(sm.group(1))
                if val and 5 <= val <= 2000:
                    surface = val
                    break
            if not surface and price:
                pm = re.search(r"Soit\s+([\d\s\xa0.,]+)\s*€/m", text)
                if pm:
                    per = to_float(pm.group(1))
                    if per:
                        surface = round(price / per, 1)
            rooms = to_int(rooms_s)
            bedrooms = None
            bm = re.search(r"(\d+)\s*chambres?", text, re.I)
            if bm:
                bedrooms = to_int(bm.group(1))
            desc = ""
            dm = re.search(r"(?:pièces|chambres|m²)\s+(.{80,500}?)(?:\s+Favoris|\s+Message|$)", text)
            if dm:
                desc = dm.group(1).strip()
            row = {
                "id": f"orpi-{uid}",
                "source": "Orpi",
                "title": f"{'Maison' if kind == 'maison' else 'Appartement'}"
                         + (f" {rooms} pièces" if rooms else "") + f" — {row_city}",
                "type": kind,
                "city": row_city,
                "postal_code": postal,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": bedrooms,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": None,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": desc[:800],
                "photos": [image] if image else [],
                "url": url,
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def immonot_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    urls = []
    city_targets = []
    if cities:
        city_targets = list(cities)
    elif city:
        city_targets = [city]
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            info = ban_city_info(c)
            if not info or not info.get("postcode") or not info.get("citycode"):
                continue
            postal = info["postcode"]
            dept = postal[:2]
            meta = get_departements_meta().get(dept) or {}
            dept_slug_name = dept_name_slug(meta.get("nom") or dept)
            urls.append(
                f"{IMMONOT}/immobilier-notaire/annonces/Ccom_{info['citycode']}_{postal}/"
                f"achat-{type_path}-a-vendre-{postal}-{slugify_hyphen(c)}-{dept_slug_name}.html"
            )
    elif departments:
        meta = get_departements_meta()
        for code in departments[:3]:
            name = (meta.get(code) or {}).get("nom") or code
            urls.append(
                f"{IMMONOT}/immobilier-notaire/annonces/D{code}/vente-immobiliere-{dept_name_slug(name)}.html"
            )
    if not urls:
        return []

    def fetch(u):
        try:
            return fetch_html(u)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(urls))) as ex:
        pages = list(ex.map(fetch, urls))

    target_names = {norm(c) for c in city_targets} if city_targets else set()
    rows = []
    for page in pages:
        if not page:
            continue
        for m in re.finditer(r'<li class="card-immo-li"', page):
            end = page.find("</li>", m.start())
            block = page[m.start(): end + 5 if end != -1 else m.start() + 20000]
            href_m = re.search(r'href="(/immobilier-notaire/detail/[^"]+)"', block)
            if not href_m:
                continue
            text = strip_tags(block)
            kind_m = re.search(r"Achat\s*-\s*(maison|appartement)", text, re.I)
            kind = kind_m.group(1).lower() if kind_m else type_
            city_m = re.search(r"Achat\s*-\s*(?:maison|appartement)\s+(.+?)\s+(\d{5})", text, re.I)
            row_city = smart_title(city_m.group(1)) if city_m else (city_targets[0] if city_targets else "")
            postal = city_m.group(2) if city_m else None
            if not row_city:
                url_city_m = re.search(r"a-vendre-(\d{5})-([a-z0-9-]+)-[a-z]+\.html", href_m.group(1), re.I)
                if url_city_m:
                    postal = url_city_m.group(1)
                    row_city = smart_title(url_city_m.group(2).replace("-", " "))
            row_city = row_city or "Nord"
            if target_names and norm(row_city) not in target_names:
                continue
            price = parse_price(text)
            spans = re.findall(r'card-immo-caracteristiques">([\d.,]+)', block)
            surface = to_float(spans[0]) if spans else None
            rooms = to_int(spans[2]) if len(spans) > 2 else None
            bedrooms = to_int(spans[3]) if len(spans) > 3 else None
            img_m = re.search(r'<img[^>]+src="([^"]+)"', block)
            oid_m = re.search(r'data-annonce-oid="([^"]+)"', block)
            oid = oid_m.group(1) if oid_m else href_m.group(1).split("/")[-2]
            desc = text
            desc = re.sub(r"^\d+\s+photos\s*", "", desc, flags=re.I)
            desc = re.sub(r"^[\d\s\xa0]+€\s*", "", desc)
            desc = re.sub(r"^Achat\s*-\s*(?:maison|appartement)\s+.+?\d{5}\s*", "", desc, flags=re.I)
            desc = re.sub(r"\s*(?:\d+(?:[.,]\d+)?\s*m²\s*){1,3}\d+\s+\d+\s*Réf\s*:.*$", "", desc).strip()
            row = {
                "id": f"immonot-{oid}",
                "source": "Immonot",
                "title": f"{'Maison' if kind == 'maison' else 'Appartement'}"
                         + (f" {rooms} pièces" if rooms else "") + f" — {row_city}",
                "type": kind,
                "city": row_city,
                "postal_code": postal,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": bedrooms,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": None,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": desc[:800],
                "photos": [img_m.group(1)] if img_m else [],
                "url": IMMONOT + href_m.group(1),
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def iad_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    city_targets = list(cities) if cities else ([city] if city else [])
    targets = []
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            info = ban_city_info(c)
            if not info or not info.get("postcode"):
                continue
            targets.append((c, f"https://www.iadfrance.fr/annonces/{slugify_hyphen(c)}-{info['postcode']}/{type_path}"))
    elif departments:
        meta = get_departements_meta()
        for code in departments[:2]:
            name = (meta.get(code) or {}).get("nom") or code
            targets.append((None, f"https://www.iadfrance.fr/annonces/{slugify_hyphen(name)}-{code}/vente/{type_path}"))
    if not targets:
        return []

    def fetch(u):
        try:
            return fetch_html(u)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as ex:
        pages = list(ex.map(lambda t: fetch(t[1]), targets))

    rows = []
    for page, target_city in zip(pages, [t[0] for t in targets]):
        if not page:
            continue
        info = ban_city_info(target_city) if target_city else {}
        postal = info.get("postcode")
        city_slug = slugify_hyphen(target_city) if target_city else None
        seen = set()
        for m in re.finditer(r'href="(/annonce/[^"]+)"', page):
            link = m.group(1)
            if f"/{type_path}-vente-" not in link:
                continue
            if city_slug and f"-{city_slug}-" not in link:
                continue
            if link in seen:
                continue
            seen.add(link)
            idx = page.find(link)
            art_start = page.rfind("<article", 0, idx)
            art_end = page.find("</article>", idx)
            if art_start != -1 and art_end != -1:
                block = page[art_start:art_end + 10]
            else:
                block = page[max(0, idx - 7000): idx + 7000]
            text = strip_tags(block)
            price = None
            for pm in re.finditer(r"(\d{1,3}(?:[\s\xa0.]\d{3})+|\d{4,9})\s*€", text):
                val = to_float(pm.group(1))
                if val and 10000 <= val <= 10000000:
                    price = val
                    break
            title_m = re.search(r"<h2[^>]*>([\s\S]*?)</h2>", block)
            title = strip_tags(title_m.group(1)) if title_m else ""
            if not title:
                title_m = re.search(r"((?:Maison|Appartement)[^€]{0,100}?\(\d{5}\))", text)
                title = title_m.group(1).strip() if title_m else ""
            rooms_m = re.search(r"(\d+)\s*pi[eè]ces?", text, re.I)
            bed_m = re.search(r"(\d+)\s*chambres?", text, re.I)
            surface_m = re.search(r"(\d+)\s*m\s*(?:2|\u00b2)", text, re.I)
            img_m = re.search(r'(https://images\.iadfrance\.fr/property/broadcast/[^"\s]+)', block)
            if not img_m:
                img_m = re.search(r'(https://images\.iadfrance\.fr/[^"\s]+)', block)
            uid_m = re.search(r"/(r[\w-]+)/?$", link)
            row_city = smart_title(target_city) if target_city else ""
            row_postal = postal
            if not row_city and title:
                cm = re.search(r"à\s+(.+?)\s*\((\d{5})\)", title)
                if cm:
                    row_city = smart_title(cm.group(1))
                    row_postal = cm.group(2)
            row = {
                "id": f"iad-{uid_m.group(1) if uid_m else slugify_hyphen(link)}",
                "source": "iad",
                "title": title or f"{'Maison' if type_ == 'maison' else 'Appartement'}"
                                  + (f" {to_int(rooms_m.group(1))} pièces" if rooms_m else "")
                                  + (f" — {row_city}" if row_city else ""),
                "type": type_,
                "city": row_city,
                "postal_code": row_postal,
                "price": price,
                "surface": to_float(surface_m.group(1)) if surface_m else None,
                "rooms": to_int(rooms_m.group(1)) if rooms_m else None,
                "bedrooms": to_int(bed_m.group(1)) if bed_m else None,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": None,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": "",
                "photos": [htmlmod.unescape(img_m.group(1))] if img_m else [],
                "url": "https://www.iadfrance.fr" + link,
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def citya_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    city_targets = list(cities) if cities else ([city] if city else [])
    targets = []
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            info = ban_city_info(c)
            if not info or not info.get("postcode"):
                continue
            targets.append((c, f"https://www.citya.com/annonces/vente/{type_path}/"
                                f"{slugify_hyphen(c)}-{info['postcode']}"))
    elif departments:
        meta = get_departements_meta()
        for code in departments[:3]:
            name = (meta.get(code) or {}).get("nom") or code
            targets.append((None, f"https://www.citya.com/annonces/vente/{type_path}/"
                                  f"{slugify_hyphen(name)}-{code}"))
    if not targets:
        return []

    def fetch(u):
        try:
            return fetch_html(u, timeout=15)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as ex:
        pages = list(ex.map(lambda t: fetch(t[1]), targets))

    rows = []
    for page, target_city in zip(pages, [t[0] for t in targets]):
        if not page:
            continue
        info = ban_city_info(target_city) if target_city else {}
        for m in re.finditer(r'data-itemId="([^"]+)"', page):
            end = page.find('data-itemId="', m.start() + 5)
            block = page[m.start():end if end != -1 else m.start() + 20000]
            block_text = strip_tags(block)
            city_m = re.search(r"([A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\- ]{2,45})\s*\((\d{5})\)", block_text)
            row_city = smart_title(city_m.group(1)) if city_m else (
                smart_title(target_city) if target_city else "")
            row_postal = city_m.group(2) if city_m else (info.get("postcode") if info else None)
            href_m = re.search(r'href="(https://www\.citya\.com/annonces/[^"]+)"', block)
            if not href_m:
                href_m = re.search(r'href="(/annonces/[^"]+)"', block)
            if not href_m:
                continue
            name_m = re.search(r'data-itemName="([^"]*)"', block)
            cat_m = re.search(r'data-category="([^"]*)"', block)
            price_m = re.search(r'data-price="([\d.]+)"', block)
            name = name_m.group(1) if name_m else ""
            price = to_float(price_m.group(1)) if price_m else parse_price(strip_tags(block))
            surface = None
            sm = re.search(r"([\d.,]+)\s*m\s*(?:2|\u00b2)", name)
            if sm:
                surface = to_float(sm.group(1))
            rooms = None
            rm = re.search(r"(\d+)\s*pi[eè]ces?", name, re.I)
            if rm:
                rooms = to_int(rm.group(1))
            kind = "maison" if (cat_m and "maison" in cat_m.group(1).lower()) else "appartement"
            img_m = re.search(r'<img[^>]+(?:src|data-src)="([^"]+)"', block)
            photo = img_m.group(1) if img_m else None
            if photo and photo.startswith("/"):
                photo = "https://www.citya.com" + photo
            url = href_m.group(1)
            if url.startswith("/"):
                url = "https://www.citya.com" + url
            title = name or (
                f"{'Maison' if kind == 'maison' else 'Appartement'}"
                + (f" {rooms} pièces" if rooms else "")
            )
            if row_city and norm(row_city) not in norm(title):
                title += f" — {row_city}"
            row = {
                "id": f"citya-{m.group(1)}",
                "source": "Citya",
                "title": title,
                "type": kind,
                "city": row_city,
                "postal_code": row_postal,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": None,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": None,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": "",
                "photos": [photo] if photo else [],
                "url": url,
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def paruvendu_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    city_targets = list(cities) if cities else ([city] if city else [])
    targets = []
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            info = ban_city_info(c)
            if not info or not info.get("postcode"):
                continue
            targets.append((c, f"https://www.paruvendu.fr/immobilier/vente/{type_path}/"
                                f"{slugify_hyphen(c)}-{info['postcode']}/"))
    elif departments:
        meta = get_departements_meta()
        for code in departments[:3]:
            name = (meta.get(code) or {}).get("nom") or code
            targets.append((None, f"https://www.paruvendu.fr/immobilier/vente/{type_path}/"
                                  f"{slugify_hyphen(name)}-{code}/"))
    if not targets:
        return []

    def fetch(u):
        try:
            return fetch_html(u, timeout=15)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as ex:
        pages = list(ex.map(lambda t: fetch(t[1]), targets))

    rows = []
    for page, target_city in zip(pages, [t[0] for t in targets]):
        if not page:
            continue
        info = ban_city_info(target_city) if target_city else {}
        links = []
        for m in re.finditer(r'href="(/immobilier/vente/' + type_path + r'/[^"?#]+)"', page):
            if m.group(1) not in links:
                links.append(m.group(1))
        for link in links:
            positions = [m.start() for m in re.finditer(re.escape(link), page)]
            idx = positions[-1] if positions else -1
            if idx < 0:
                continue
            before = page[max(0, idx - 5000):idx]
            after = page[idx:idx + 5000]
            prices = re.findall(
                r'([\d]{1,3}(?:[\s\xa0.]\d{3})+|\d{4,9})\s*€',
                htmlmod.unescape(before),
            )
            price = to_float(prices[-1]) if prices else None
            if not price:
                continue
            surface = None
            sm = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:<sup>2</sup>|2|\u00b2)", after, re.I)
            if sm:
                surface = to_float(sm.group(1))
            rooms = None
            rm = re.search(r"(\d+)\s*pi[eè]ce", after, re.I)
            if rm:
                rooms = to_int(rm.group(1))
            imgs = [
                x for x in re.findall(r'<img[^>]+src="(https://img\.paruvendu\.fr/[^"]+)"', before)
                if "/_static_/" not in x and "media_ext" in x
            ]
            photo = htmlmod.unescape(imgs[-1]) if imgs else None
            title_m = re.search(r'alt="([^"]*(?:Appartement|Maison)[^"]*)"', before)
            title = htmlmod.unescape(title_m.group(1)).strip() if title_m else ""
            blob = htmlmod.unescape(strip_tags(before + after))
            city_m = re.search(r"([A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\- ]{2,45})\s*\((\d{2,3})\)", blob)
            row_city = smart_title(city_m.group(1)) if city_m else (
                smart_title(target_city) if target_city else "")
            row_postal = info.get("postcode") if info else None
            title = title or (
                f"{'Maison' if type_ == 'maison' else 'Appartement'}"
                + (f" {rooms} pièces" if rooms else "")
            )
            if row_city and norm(row_city) not in norm(title):
                title += f" — {row_city}"
            dpe_m = re.search(r"\bDPE\s*:\s*([A-G])\b", blob, re.I)
            dpe = dpe_m.group(1).upper() if dpe_m else None
            row = {
                "id": f"paruvendu-{link.rstrip('/').split('/')[-1]}",
                "source": "ParuVendu",
                "title": title,
                "type": type_,
                "city": row_city,
                "postal_code": row_postal,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": None,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": dpe,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": "",
                "photos": [photo] if photo else [],
                "url": "https://www.paruvendu.fr" + link,
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def nestenn_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    city_targets = list(cities) if cities else ([city] if city else [])
    if not city_targets:
        return []
    targets = []
    for c in city_targets[:MAX_CITY_TARGETS]:
        info = ban_city_info(c)
        if not info or not info.get("postcode"):
            continue
        targets.append((c, f"https://nestenn.com/vente/{type_path}/"
                            f"{slugify_hyphen(c)}-{info['postcode']}"))
    if not targets:
        return []

    def fetch(u):
        try:
            return fetch_html(u, timeout=15)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as ex:
        pages = list(ex.map(lambda t: fetch(t[1]), targets))

    rows = []
    for page, target_city in zip(pages, [t[0] for t in targets]):
        if not page:
            continue
        info = ban_city_info(target_city) or {}
        city_slug = slugify_hyphen(target_city)
        for m in re.finditer(r'class="flex flex_column bien_item"', page):
            end = page.find('class="flex flex_column bien_item"', m.start() + 5)
            block = page[m.start():end if end != -1 else m.start() + 20000]
            href_m = re.search(r'href="([^"]+)"', block)
            if not href_m or "location" in href_m.group(1):
                continue
            link = href_m.group(1)
            if f"-{city_slug}-" not in link:
                continue
            text = strip_tags(block)
            price = parse_price(text)
            if not price or "photoVendu" in block:
                continue
            surface = None
            sm = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:2|\u00b2)", text, re.I)
            if sm:
                surface = to_float(sm.group(1))
            rooms = None
            rm = re.search(r"(\d+)\s*pi[eè]ces?", text, re.I)
            if rm:
                rooms = to_int(rm.group(1))
            bedrooms = None
            bm = re.search(r"(\d+)\s*chambres?", text, re.I)
            if bm:
                bedrooms = to_int(bm.group(1))
            dpe = None
            dm = re.search(r"dpe_([a-g])\.svg", block, re.I)
            if dm:
                dpe = dm.group(1).upper()
            img_m = re.search(r"background-image:\s*url\('([^']+)'\)", block)
            desc_m = re.search(r'descriptionBien">([\s\S]*?)</div>', block)
            desc = strip_tags(desc_m.group(1)) if desc_m else ""
            row = {
                "id": f"nestenn-{link.rstrip('/').split('/')[-1]}",
                "source": "Nestenn",
                "title": f"{'Maison' if type_ == 'maison' else 'Appartement'}"
                         + (f" {rooms} pièces" if rooms else "")
                         + f" — {smart_title(target_city)}",
                "type": type_,
                "city": smart_title(target_city),
                "postal_code": info.get("postcode"),
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": bedrooms,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": dpe,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": desc[:800],
                "photos": [img_m.group(1)] if img_m else [],
                "url": link if link.startswith("http") else "https://nestenn.com/" + link,
            }
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def laforet_search(type_, city=None, departments=None, cities=None, **filters):
    type_path = "appartement" if type_ == "appartement" else "maison"
    city_targets = list(cities) if cities else ([city] if city else [])
    if not city_targets:
        return []
    targets = []
    for c in city_targets[:MAX_CITY_TARGETS]:
        info = ban_city_info(c)
        if not info or not info.get("postcode"):
            continue
        targets.append((c, f"https://www.laforet.com/ville/achat-{type_path}-"
                            f"{slugify_hyphen(c)}-{info['postcode']}"))
    if not targets:
        return []

    def fetch(u):
        try:
            return fetch_html(u, timeout=15)
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as ex:
        pages = list(ex.map(lambda t: fetch(t[1]), targets))

    rows = []
    for page, target_city in zip(pages, [t[0] for t in targets]):
        if not page:
            continue
        info = ban_city_info(target_city) or {}
        for m in re.finditer(r'data-gtm-item-id-param="(\d+)"', page):
            end = page.find('data-gtm-item-id-param="', m.start() + 5)
            block = page[max(0, m.start() - 3000):end if end != -1 else m.start() + 12000]
            name_m = re.search(r'data-gtm-item-name-param="([^"]*)"', block)
            city_m = re.search(r'data-gtm-item-city-param="([^"]*)"', block)
            zip_m = re.search(r'data-gtm-item-zipcode-param="([^"]*)"', block)
            price_m = re.search(r'data-gtm-item-price-param="([\d.]+)"', block)
            size_m = re.search(r'data-gtm-item-size-param="([\d.]+)"', block)
            rooms_m = re.search(r'data-gtm-item-rooms-nb-param="([\d.]+)"', block)
            row_city = smart_title(city_m.group(1)) if city_m else smart_title(target_city)
            if city_m and norm(row_city) != norm(target_city):
                continue
            href_m = re.search(r'href="(https://www\.laforet\.com/[^"]*' + m.group(1) + r'[^"]*)"', block)
            img_m = re.search(r'<img[^>]+(?:src|data-src)="([^"]+)"', block)
            photo = htmlmod.unescape(img_m.group(1)) if img_m else None
            if photo and photo.startswith("/"):
                photo = "https://www.laforet.com" + photo
            row = {
                "id": f"laforet-{m.group(1)}",
                "source": "Laforêt",
                "title": htmlmod.unescape(name_m.group(1)) if name_m else
                         f"{'Maison' if type_ == 'maison' else 'Appartement'} — {row_city}",
                "type": type_,
                "city": row_city,
                "postal_code": zip_m.group(1) if zip_m else info.get("postcode"),
                "price": to_float(price_m.group(1)) if price_m else None,
                "surface": to_float(size_m.group(1)) if size_m else None,
                "rooms": to_int(rooms_m.group(1)) if rooms_m else None,
                "bedrooms": None,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": None,
                "ges": None,
                "dpe_date": None,
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": "",
                "photos": [photo] if photo else [],
                "url": href_m.group(1) if href_m else "",
            }
            if not row["url"]:
                continue
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def logic_department_url(code, type_="appartement"):
    global LOGIC_BLOCK_UNTIL
    if time.time() < LOGIC_BLOCK_UNTIL:
        return None
    type_path = "appartement" if type_ == "appartement" else "maison"
    known = LOGIC_KNOWN_DEPTS.get(code)
    if known:
        region_slug, dept_slug, ad_id = known
        return f"{LOGIC}/recherche-immo/vente/{type_path}/{region_slug}/{dept_slug}/{ad_id}"
    key = (code, type_path)
    if key in LOGIC_DEPT_URLS:
        return LOGIC_DEPT_URLS[key]
    meta = get_departements_meta().get(code) or {}
    region = (meta.get("region") or {}).get("code")
    region_info = LOGIC_REGION_IDS.get(str(region))
    if not region_info:
        return None
    region_slug, region_id = region_info
    region_url = f"{LOGIC}/recherche-immo/vente/{type_path}/{region_slug}/{region_id}"
    try:
        html = fetch_html(region_url)
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code in (403, 429):
            LOGIC_BLOCK_UNTIL = time.time() + 600
        return None
    for m in re.finditer(
        r'href="([^"]*/recherche-immo/vente/' + type_path + r'/[^"]*ad06fr\d+[^"]*)"', html
    ):
        u = m.group(1)
        if f"-{code}/" in u:
            LOGIC_DEPT_URLS[key] = u
            return u
    return None


def logic_city_url(city, dept_code, type_="appartement"):
    global LOGIC_BLOCK_UNTIL
    if time.time() < LOGIC_BLOCK_UNTIL:
        return None
    type_path = "appartement" if type_ == "appartement" else "maison"
    known = LOGIC_KNOWN_CITIES.get((dept_code, slugify_hyphen(city)))
    if known:
        region_slug, city_slug, ad_id = known
        return f"{LOGIC}/recherche-immo/vente/{type_path}/{region_slug}/{city_slug}/{ad_id}"
    key = (dept_code, norm(city), type_path)
    if key in LOGIC_CITY_URLS:
        return LOGIC_CITY_URLS[key]
    dept_url = logic_department_url(dept_code, type_)
    if not dept_url:
        return None
    try:
        html = fetch_html(dept_url)
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code in (403, 429):
            LOGIC_BLOCK_UNTIL = time.time() + 600
        return None
    target = f"/{slugify_hyphen(city)}-"
    for m in re.finditer(
        r'href="([^"]*/recherche-immo/vente/' + type_path + r'/[^"]*ad08fr\d+[^"]*)"', html
    ):
        u = m.group(1)
        if target in u:
            LOGIC_CITY_URLS[key] = u
            return u
    return None


def logic_unquote_fetcher(raw):
    try:
        inner = raw[raw.index('JSON.parse("') + len('JSON.parse("'):raw.rindex('")')]
        decoded = json.loads('"' + inner.replace("\n", "\\n") + '"')
        return json.loads(decoded)
    except Exception:
        return None


def logic_normalize_ad(obj):
    if not obj:
        return None
    loc = obj.get("location") or {}
    addr = loc.get("address") or {}
    facts = {}
    for f in (obj.get("hardFacts") or {}).get("facts") or []:
        facts[f.get("type")] = f.get("splitValue") or f.get("value")
    price = to_float(((obj.get("hardFacts") or {}).get("price") or {}).get("value"))
    surface = to_float(facts.get("livingSpace"))
    rooms = to_int(facts.get("numberOfRooms"))
    bedrooms = to_int(facts.get("numberOfBedrooms"))
    raw_type = ((obj.get("rawData") or {}).get("propertyType") or "").upper()
    kind = "maison" if raw_type == "HOUSE" else "appartement"
    desc = ((obj.get("mainDescription") or {}).get("description") or "")[:800]
    headline = (obj.get("mainDescription") or {}).get("headline") or ""
    images = [
        img.get("url") for img in ((obj.get("gallery") or {}).get("images") or [])
        if img.get("url")
    ]
    pub = ((obj.get("metadata") or {}).get("creationDate") or "")[:10]
    mod = ((obj.get("metadata") or {}).get("updateDate") or "")[:10]
    dpe = obj.get("energyClass")
    if dpe not in list("ABCDEFG"):
        dpe = None
    row_city = addr.get("city") or ""
    title = headline or (
        f"{'Maison' if kind == 'maison' else 'Appartement'}"
        + (f" {rooms} pièces" if rooms else "")
        + (f" — {row_city}" if row_city else "")
    )
    return {
        "id": f"logicimmo-{obj.get('id')}",
        "source": "Logic-Immo",
        "title": title,
        "type": kind,
        "city": row_city,
        "postal_code": addr.get("zipCode"),
        "price": price,
        "surface": surface,
        "rooms": rooms,
        "bedrooms": bedrooms,
        "year": None,
        "heating": None,
        "floor": None,
        "dpe": dpe,
        "ges": None,
        "dpe_date": None,
        "publication_date": pub or None,
        "modification_date": mod or None,
        "listing_date": pub or mod or None,
        "listing_date_kind": "publication" if pub else "modification",
        "address_known": bool(loc.get("isAddressPublished")),
        "blur_type": None,
        "blur_radius": None,
        "lat": None,
        "lon": None,
        "district": addr.get("district"),
        "description": desc,
        "photos": images[:6],
        "url": obj.get("url"),
    }


def logic_parse_search(page):
    m = re.search(r'<script id="__UFRN_FETCHER__">([\s\S]*?)</script>', page)
    if not m:
        return []
    data = logic_unquote_fetcher(m.group(1))
    if not data:
        return []
    serp = ((data.get("data") or {}).get("classified-serp-data") or {})
    by_id = serp.get("classifiedsData") or {}
    rows = []
    for cid in serp.get("classifieds") or []:
        row = logic_normalize_ad(by_id.get(cid))
        if row:
            rows.append(row)
    return rows


def logic_immo_search(type_, city=None, departments=None, cities=None, **filters):
    urls = []
    city_targets = []
    if cities:
        city_targets = list(cities)
    elif city:
        city_targets = [city]
    if city_targets:
        for c in city_targets[:MAX_CITY_TARGETS]:
            info = ban_city_info(c)
            if not info or not info.get("postcode"):
                continue
            dept = info["postcode"][:2]
            u = logic_city_url(c, dept, type_)
            if u:
                urls.append((c, u))
    elif departments:
        for code in departments[:3]:
            u = logic_department_url(code, type_)
            if u:
                urls.append((None, u))
    if not urls:
        if time.time() < LOGIC_BLOCK_UNTIL:
            raise RuntimeError("Logic-Immo protégé par DataDome : réessai différé.")
        return []

    def fetch(u):
        global LOGIC_BLOCK_UNTIL
        try:
            return fetch_html(u)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (403, 429):
                LOGIC_BLOCK_UNTIL = time.time() + 600
            return ""

    with ThreadPoolExecutor(max_workers=min(3, len(urls))) as ex:
        pages = list(ex.map(lambda x: fetch(x[1]), urls))
    if urls and not any(pages) and time.time() < LOGIC_BLOCK_UNTIL:
        raise RuntimeError("Logic-Immo protégé par DataDome : réessai différé.")

    target_names = {norm(c) for c in city_targets} if city_targets else set()
    rows = []
    for (target_city, _), page in zip(urls, pages):
        for row in logic_parse_search(page):
            if target_names and norm(row.get("city")) not in target_names:
                continue
            if row.get("type") != type_:
                continue
            if passes_filters(row, type_, **filters):
                rows.append(row)
    return rows


def parse_polygon(raw):
    if not raw:
        return None
    points = []
    for part in str(raw).split("|"):
        bits = part.split(",")
        if len(bits) != 2:
            continue
        lat, lon = to_float(bits[0]), to_float(bits[1])
        if lat is not None and lon is not None:
            points.append((lat, lon))
    return points if len(points) >= 3 else None


def point_in_polygon(lat, lon, points):
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        yi, xi = points[i]
        yj, xj = points[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def ring_contains(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def department_for_point(lat, lon):
    """Département contenant un point, à partir du GeoJSON déjà chargé."""
    try:
        geo = get_departements_geo()
    except Exception:
        return None
    for feature in geo.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "Polygon":
            polygons = [geometry.get("coordinates") or []]
        elif geometry.get("type") == "MultiPolygon":
            polygons = geometry.get("coordinates") or []
        else:
            continue
        for polygon in polygons:
            if not polygon or not ring_contains(lon, lat, polygon[0]):
                continue
            if any(ring_contains(lon, lat, hole) for hole in polygon[1:]):
                continue
            return (feature.get("properties") or {}).get("code")
    return None


def filter_polygon_rows(rows, polygon):
    """Filtre une zone : coordonnées exactes, sinon centre de la ville."""
    exact = []
    approximate_candidates = []
    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        if lat is not None and lon is not None:
            if point_in_polygon(float(lat), float(lon), polygon):
                exact.append(row)
            continue
        approximate_candidates.append(row)

    cities = sorted({
        str(row.get("city")).strip()
        for row in approximate_candidates
        if row.get("city")
    })
    centers = {}
    if cities:
        with ThreadPoolExecutor(max_workers=min(8, len(cities))) as ex:
            for city, center in zip(cities, ex.map(ban_city_center, cities)):
                centers[city] = center

    approximate = []
    unlocated = []
    for row in approximate_candidates:
        center = centers.get(str(row.get("city")).strip())
        if center and point_in_polygon(center["lat"], center["lon"], polygon):
            row["lat"] = center["lat"]
            row["lon"] = center["lon"]
            row["position_approx"] = True
            approximate.append(row)
        else:
            unlocated.append(row)
    return exact + approximate, len(approximate), len(unlocated)


def merge_listings(rows):
    merged = []
    index = {}
    for row in rows:
        if not row:
            continue
        photos = row.get("photos") or []
        photo = photos[0].split("?")[0] if photos else ""
        desc = norm(row.get("description") or "")
        key = None
        if photo:
            key = "photo:" + photo
        elif len(desc) >= 120:
            key = "desc:" + desc[:240]
        # iad est aussi diffusé par Bien'ici : on rapproche la référence interne.
        ref = None
        if row.get("source") == "iad":
            m = re.search(r"-r(\d+)", row.get("id") or "")
            if m:
                ref = m.group(1)
        elif row.get("source") == "Bien'ici" and str(row.get("id") or "").startswith("iad-france-"):
            ref = str(row.get("id")).split("-")[-1]
        if ref:
            key = "iad-ref:" + ref
        if key and key in index:
            existing = merged[index[key]]
            sources = set(existing.get("sources") or [existing.get("source")])
            sources.add(row.get("source"))
            existing["sources"] = [s for s in SOURCE_ORDER if s in sources]
            existing["source_count"] = len(existing["sources"])
            existing["duplicate_count"] = existing.get("duplicate_count", 1) + 1
            for field in ("surface", "rooms", "bedrooms", "dpe", "ges", "dpe_date",
                          "publication_date", "description", "district", "postal_code"):
                if not existing.get(field) and row.get(field):
                    existing[field] = row[field]
            continue
        row["sources"] = [row.get("source")]
        row["source_count"] = 1
        row["duplicate_count"] = 1
        index[key] = len(merged) if key else None
        merged.append(row)
    return merged


def bienici_search_cities(cities, type_, max_price=None, min_price=None, min_area=None,
                          max_area=None, min_rooms=None, max_rooms=None,
                          dpe_max=None, ges_max=None):
    """Une requête Bien'ici par ville : évite qu'une grande ville noie les petites."""
    def one(city_name):
        try:
            zones = bienici_zone_ids(None, None, [city_name])
            if not zones:
                return []
            rows, _ = bienici_search(
                zones, type_,
                max_price=max_price, min_price=min_price,
                min_area=min_area, max_area=max_area,
                min_rooms=min_rooms, max_rooms=max_rooms,
                dpe_max=dpe_max, ges_max=ges_max,
                pages=2,
            )
            return rows
        except Exception as e:
            print("[bienici-city]", city_name, e, flush=True)
            return []

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(cities)))) as ex:
        batches = list(ex.map(one, cities))
    out = []
    seen = set()
    for rows in batches:
        for row in rows:
            if row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            out.append(row)
    return out


def ban_reverse(lon, lat):
    url = f"{BAN_REVERSE}?lon={lon}&lat={lat}&limit=1"
    data = fetch_json_url(url)
    f = (data.get("features") or [None])[0]
    return f["properties"] if f else None


def days_between(d1, d2):
    if not d1 or not d2:
        return None
    try:
        a = datetime.date.fromisoformat(str(d1)[:10])
        b = datetime.date.fromisoformat(str(d2)[:10])
        return (a - b).days
    except ValueError:
        return None


def strip_tags(s):
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def to_float(s):
    if s is None:
        return None
    try:
        return float(re.sub(r"\s+", "", str(s).replace("\xa0", " ")).replace(",", "."))
    except ValueError:
        return None


def to_int(s):
    v = to_float(s)
    return int(v) if v is not None else None


def parse_detail(url, page):
    h1 = ""
    m = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", page, re.I)
    if m:
        h1 = strip_tags(m.group(1))
    text = strip_tags(page)
    desc = ""
    m = re.search(r'<meta name="description" content="([^"]*)"', page, re.I)
    if m:
        desc = htmlmod.unescape(m.group(1))

    type_ = "appartement" if re.search(r"\bappartement\b", h1, re.I) else (
        "maison" if re.search(r"\bmaison\b", h1, re.I) else "appartement"
    )
    surface = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*2", h1, re.I)
    if m:
        surface = to_float(m.group(1))
    rooms = None
    m = re.search(r"(\d+)\s*pi[eè]ces?", h1, re.I)
    if m:
        rooms = to_int(m.group(1))

    price = None
    m = re.search(r"Prix(?:\s+de\s+vente)?\s*:\s*([\d\s.,]+)\s*€", text, re.I)
    if not m:
        m = re.search(r"\b(\d{2,3}(?:[\s.]\d{3})+)\s*€", text)
    if m:
        price = to_float(m.group(1))

    dpe = None
    m = re.search(r"classe\s+[eé]nergie\s*([A-G])", text, re.I)
    if m:
        dpe = m.group(1).upper()
    if not dpe:
        m = re.search(r"DPE\s*[:\-]?\s*(?:classe\s+[eé]nergie\s*)?([A-G])\b", text, re.I)
        if m:
            dpe = m.group(1).upper()
    ges = None
    m = re.search(r"classe\s+GES\s*([A-G])", text, re.I)
    if m:
        ges = m.group(1).upper()

    floor = None
    m = re.search(r"(\d+)\s*(?:er|e|ème)?\s*étage", text, re.I)
    if m:
        floor = to_int(m.group(1))
    elif re.search(r"\bRDC\b|rez-de-chaussée", text, re.I):
        floor = 0

    year = None
    m = re.search(r"(?:construite?|construction|édifiée?|bâtie?)\s*(?:en|:)?\s*((?:18|19|20)\d{2})", text, re.I)
    if m:
        year = to_int(m.group(1))

    heating = None
    m = re.search(r"Chauffage\s*:\s*([^|]{3,60})", text, re.I)
    if m:
        heating = m.group(1).strip()
    if not heating:
        m = re.search(r"chauffage\s+(individuel|collectif|gaz|électrique|fioul|bois|pompe à chaleur)", text, re.I)
        if m:
            heating = m.group(1).strip()

    # Ville : la fin du h1, ex. "HAUBOURDIN - 59"
    city = None
    m = re.search(r"([A-ZÉÈÀÂÎÔÛÇ][A-ZÉÈÀÂÎÔÛÇ'’\- ]{2,40})\s*-\s*\d{2,3}\s*$", h1)
    if m:
        city = m.group(1).strip().title()

    # Rue : uniquement dans la description (pas dans le pied de page agence)
    street = None
    m = re.search(
        r"\b((?:rue|avenue|boulevard|place|impasse|allée|quai|chemin|route)\s+"
        r"[A-ZÉÈÀÂÎÔÛÇ][\w'’\-]+(?:\s+[A-ZÉÈÀÂÎÔÛÇ][\w'’\-]+){0,4})",
        desc,
    )
    if m:
        street = m.group(1).strip()

    return {
        "url": url,
        "title": h1,
        "type": type_,
        "city": city,
        "price": price,
        "surface": surface,
        "rooms": rooms,
        "dpe": dpe,
        "ges": ges,
        "floor": floor,
        "year": year,
        "heating": heating,
        "street": street,
        "description": desc[:600],
        "source": "Century21",
    }


def get_listings(city, type_, max_price=None, min_price=None, min_surface=None, max_surface=None,
                 min_rooms=None, max_rooms=None, dpe_max=None, ges_max=None,
                 departments=None, cities=None, polygon=None):
    filters = dict(
        max_price=max_price, min_price=min_price,
        min_surface=min_surface, max_surface=max_surface,
        min_rooms=min_rooms, max_rooms=max_rooms,
        dpe_max=dpe_max, ges_max=ges_max,
    )
    # Dès qu'une zone est sélectionnée, elle prime sur le champ Ville.
    if departments or cities:
        city = None
    zones = []
    try:
        zones = bienici_zone_ids(city, departments, cities)
    except Exception as e:
        print("[bienici-zone]", e, flush=True)

    def run(name, fn):
        started = time.time()
        try:
            rows = fn() or []
            rows = [r for r in rows if passes_filters(r, type_, **filters)]
            return name, rows, {
                "name": name, "count": len(rows), "ok": True,
                "ms": int((time.time() - started) * 1000),
            }
        except Exception as e:
            print(f"[{name}]", e, flush=True)
            return name, [], {
                "name": name, "count": 0, "ok": False,
                "ms": int((time.time() - started) * 1000),
                "error": str(e)[:200],
            }

    # Les sources HTML sont plus lentes : sur une grande sélection, on interroge
    # la ville prioritaire (celle saisie en premier) et Bien'ici couvre toutes
    # les autres ville par ville. Cela évite les timeouts en cascade.
    html_cities = cities
    if cities and len(cities) > 4:
        html_cities = cities[:1]
    elif cities and len(cities) > 1:
        html_cities = cities[:2]

    tasks = []
    if cities and len(cities) > 1:
        tasks.append(("Bien'ici", lambda: bienici_search_cities(
            cities, type_,
            max_price=max_price,
            min_price=min_price,
            min_area=min_surface,
            max_area=max_surface,
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            dpe_max=dpe_max,
            ges_max=ges_max,
        )))
    elif zones:
        tasks.append(("Bien'ici", lambda: bienici_search(
            zones, type_,
            max_price=max_price,
            min_price=min_price,
            min_area=min_surface,
            max_area=max_surface,
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            dpe_max=dpe_max,
            ges_max=ges_max,
            pages=4,
        )[0]))
    else:
        SOURCE_LAST["Bien'ici"] = {"name": "Bien'ici", "count": 0, "ok": False,
                                  "ms": 0, "error": "zone introuvable"}
    tasks.append(("Logic-Immo", lambda: logic_immo_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("Century21", lambda: c21_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("Orpi", lambda: orpi_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("Immonot", lambda: immonot_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("iad", lambda: iad_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("Citya", lambda: citya_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("ParuVendu", lambda: paruvendu_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("Nestenn", lambda: nestenn_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))
    tasks.append(("Laforêt", lambda: laforet_search(
        type_, city=city, departments=departments, cities=html_cities, **filters)))

    results = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        for name, rows, meta in ex.map(lambda t: run(t[0], t[1]), tasks):
            results.append((name, rows, meta))
            SOURCE_LAST[name] = meta

    all_rows = []
    sources = []
    for name, rows, meta in results:
        sources.append(meta)
        all_rows.extend(rows)

    imported_rows = imported_for_query(
        type_, city, departments, cities, filters, polygon
    )
    if imported_rows:
        all_rows.extend(imported_rows)
        for src_name in sorted({str(r.get("source") or "SeLoger") for r in imported_rows}):
            src_rows = [r for r in imported_rows if str(r.get("source") or "SeLoger") == src_name]
            meta_import = {
                "name": src_name, "count": len(src_rows), "ok": True,
                "ms": 0, "imported": True,
            }
            sources.append(meta_import)
            SOURCE_LAST[src_name] = meta_import

    merged = merge_listings(all_rows)
    unlocated_count = 0
    approx_count = 0
    if polygon:
        merged, approx_count, unlocated_count = filter_polygon_rows(merged, polygon)
    for meta in sources:
        meta["merged_count"] = sum(1 for r in merged if r.get("source") == meta["name"])
        if polygon:
            meta["count"] = sum(
                1 for r in merged
                if meta["name"] in (r.get("sources") or [r.get("source")])
            )
    return merged, sources, unlocated_count, approx_count


def ban_insee(city):
    q = urllib.parse.quote(city)
    url = f"https://api-adresse.data.gouv.fr/search/?q={q}&type=municipality&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    f = (data.get("features") or [None])[0]
    if not f:
        return None
    return f["properties"]["citycode"]


def street_query(street, city_label):
    stop = {
        "rue", "avenue", "boulevard", "bd", "place", "impasse", "allée", "allee",
        "quai", "route", "chemin", "square", "villa", "du", "de", "des", "la",
        "le", "les", "au", "aux", "d", "l", "sur", "sous", "et",
    }
    for w in re.split(r"[^\w]+", city_label or "", flags=re.UNICODE):
        if w:
            stop.add(norm(w))
    words = [w for w in re.split(r"[^\w]+", street or "", flags=re.UNICODE) if w]
    candidates = [w for w in words if len(w) >= 4 and norm(w) not in stop]
    candidates.sort(key=len, reverse=True)
    return candidates[0] if candidates else ""


def enrich_url_energy(url):
    """Complète une annonce avec le DPE/GES de sa page de détail (à la demande)."""
    if not url:
        return {}
    if url in ENRICH_CACHE:
        return ENRICH_CACHE[url]
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    source_name = {
        "citya.com": "Citya",
        "paruvendu.fr": "ParuVendu",
        "nestenn.com": "Nestenn",
        "laforet.com": "Laforêt",
        "century21.fr": "Century21",
        "orpi.com": "Orpi",
        "immonot.com": "Immonot",
        "iadfrance.fr": "iad",
        "logic-immo.com": "Logic-Immo",
    }.get(host[4:] if host.startswith("www.") else host, host)
    result = {}
    try:
        if any(domain in host for domain in (
            "bienici.com", "seloger.com", "leboncoin.fr", "pap.fr",
            "figaro.fr", "ouestfrance-immo.com",
        )):
            return {}
        html = fetch_html(url, timeout=12)
        listing = generic_detail_listing(html, url, source_name) or {}
        result = {
            "dpe": listing.get("dpe"),
            "ges": listing.get("ges"),
            "dpe_date": listing.get("dpe_date"),
            "dpe_status": listing.get("dpe_status"),
            "surface": listing.get("surface"),
            "rooms": listing.get("rooms"),
            "street": listing.get("street"),
            "city": listing.get("city"),
            "postal_code": listing.get("postal_code"),
            "type": listing.get("type"),
            "publication_date": listing.get("publication_date"),
            "modification_date": listing.get("modification_date"),
            "description": listing.get("description"),
            "source": source_name,
        }
    except Exception as e:
        print("[enrich]", url[:80], e, flush=True)
    if result:
        ENRICH_CACHE[url] = result
    return result


def detect_import_source(url, fallback="SeLoger"):
    host = urllib.parse.urlparse(str(url or "")).netloc.lower()
    if "leboncoin" in host:
        return "Leboncoin"
    if "seloger" in host:
        return "SeLoger"
    return fallback if fallback in ("SeLoger", "Leboncoin") else "SeLoger"


def parse_import_html(html, url="", source=None):
    """Extrait des annonces d'une page importée (SeLoger/Leboncoin via navigateur/WebView)."""
    source = detect_import_source(url, source or "SeLoger")
    id_prefix = "leboncoin" if source == "Leboncoin" else "seloger"
    site_root = "https://www.leboncoin.fr" if source == "Leboncoin" else "https://www.seloger.com"
    site_host = urllib.parse.urlparse(site_root).netloc
    rows = []
    seen = set()

    def add(row):
        key = row.get("url") or row.get("id")
        if not key or key in seen:
            return
        seen.add(key)
        rows.append(row)

    # 1) JSON-LD (souvent présent sur les pages de détail et certains listing pages).
    for data in jsonld_blocks(html):
        for obj in walk_json(data):
            typ = obj.get("@type")
            if isinstance(typ, list):
                typ = " ".join(typ)
            typ = str(typ or "")
            if typ not in ("RealEstateListing", "Product", "Apartment", "House",
                           "SingleFamilyResidence", "Offer", "Residence"):
                continue
            offers = obj.get("offers") if isinstance(obj.get("offers"), dict) else {}
            price = to_float(offers.get("price") or offers.get("lowPrice") or obj.get("price"))
            surface = to_float((obj.get("floorSize") or {}).get("value")
                               if isinstance(obj.get("floorSize"), dict) else None)
            if not price and not surface:
                continue
            addr = obj.get("address") if isinstance(obj.get("address"), dict) else {}
            name = obj.get("name") or obj.get("description") or ""
            link = obj.get("url") or url
            image = obj.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            geo = obj.get("geo") if isinstance(obj.get("geo"), dict) else {}
            plain_name = strip_tags(name)
            surface = surface or extract_surface(plain_name)
            rooms = to_int(obj.get("numberOfRooms")) or extract_rooms(plain_name)
            bedrooms = to_int(obj.get("numberOfBedrooms")) or extract_bedrooms(plain_name)
            row = {
                "id": id_prefix + "-" + hashlib.sha1(str(link or name).encode("utf-8")).hexdigest()[:12],
                "source": source,
                "title": plain_name[:180] or f"Bien — {addr.get('addressLocality') or ''}",
                "type": extract_type(name + " " + typ) or "appartement",
                "city": smart_title(addr.get("addressLocality") or "") or None,
                "postal_code": addr.get("postalCode"),
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": bedrooms,
                "year": None,
                "heating": None,
                "floor": None,
                "dpe": extract_dpe(strip_tags(html[:120000])),
                "ges": extract_ges(strip_tags(html[:120000])),
                "dpe_date": None,
                "publication_date": iso_date(obj.get("datePosted")),
                "modification_date": None,
                "listing_date": iso_date(obj.get("datePosted")),
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": to_float(geo.get("latitude")),
                "lon": to_float(geo.get("longitude")),
                "district": None,
                "description": strip_tags(obj.get("description") or "")[:800],
                "photos": [htmlmod.unescape(image)] if image else [],
                "url": link,
            }
            add(row)

    link_paths = r"(?:/ad/|/annonces/|/annonce/)" if source == "Leboncoin" else r"(?:/annonces/|/annonce/)"
    LINK_PATTERN = r'href="([^"]*' + link_paths + r'[^"]*)"'

    # 2) Cartes HTML : liens d'annonces et texte autour.
    if not rows:
        for m in re.finditer(LINK_PATTERN, html, re.I):
            link = htmlmod.unescape(m.group(1))
            if link.startswith("/"):
                link = site_root + link
            if site_host not in link:
                continue
            block = html[max(0, m.start() - 9000):m.start() + 9000]
            text = strip_tags(block)
            price = parse_price(text)
            surface = extract_surface(text)
            if not price and not surface:
                continue
            city, postal = extract_city_postal(text)
            title_m = re.search(r"<h[12][^>]*>([\s\S]*?)</h[12]>", block, re.I)
            title = strip_tags(title_m.group(1)) if title_m else ""
            img_m = re.search(r'<img[^>]+(?:src|data-src)="([^"]+)"', block, re.I)
            photo = htmlmod.unescape(img_m.group(1)) if img_m else None
            if photo and photo.startswith("/"):
                photo = site_root + photo
            kind = extract_type(title) or extract_type(text) or extract_type(link) or "appartement"
            add({
                "id": id_prefix + "-" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:12],
                "source": source,
                "title": title or f"{'Maison' if kind == 'maison' else 'Appartement'}"
                                  + (f" {extract_rooms(text)} pièces" if extract_rooms(text) else "")
                                  + (f" — {city}" if city else ""),
                "type": kind,
                "city": city,
                "postal_code": postal,
                "price": price,
                "surface": surface,
                "rooms": extract_rooms(text),
                "bedrooms": extract_bedrooms(text),
                "year": extract_year(text),
                "heating": extract_heating(text),
                "floor": extract_floor(text),
                "dpe": extract_dpe(text),
                "ges": extract_ges(text),
                "dpe_date": extract_dpe_date(text),
                "publication_date": None,
                "modification_date": None,
                "listing_date": None,
                "listing_date_kind": "publication",
                "address_known": False,
                "blur_type": None,
                "blur_radius": None,
                "lat": None,
                "lon": None,
                "district": None,
                "description": "",
                "photos": [photo] if photo else [],
                "url": link,
            })
            if len(rows) >= 60:
                break

    # 3) Page de détail sans JSON-LD exploitable.
    if not rows:
        detail = generic_detail_listing(html, url, source)
        if detail.get("price") or detail.get("surface"):
            detail["id"] = id_prefix + "-" + hashlib.sha1((url or detail.get("title") or "").encode("utf-8")).hexdigest()[:12]
            detail["source"] = source
            add(detail)
    return rows


def store_imported(rows):
    global IMPORTED
    by_id = {r.get("id"): r for r in IMPORTED if r.get("id")}
    for row in rows:
        if row.get("id"):
            by_id[row["id"]] = row
    IMPORTED = list(by_id.values())[-500:]
    return rows


def imported_for_query(type_, city, departments, cities, filters, polygon):
    target_cities = {norm(c) for c in (cities or [])}
    depts = set(departments or [])
    out = []
    for row in IMPORTED:
        if type_ and row.get("type") != type_:
            continue
        if target_cities:
            if norm(row.get("city")) not in target_cities:
                continue
        elif depts:
            postal = str(row.get("postal_code") or "")
            if postal[:2] not in depts:
                continue
        elif city and norm(row.get("city")) != norm(city):
            continue
        if not passes_filters(row, type_, **filters):
            continue
        if polygon:
            if row.get("lat") is None or row.get("lon") is None:
                continue
            if not point_in_polygon(float(row["lat"]), float(row["lon"]), polygon):
                continue
        out.append(row)
    return out


def group_dpe_candidates(rows):
    """Regroupe les DPE d'un même immeuble : une seule adresse, plusieurs diagnostics."""
    groups = {}
    order = []
    for candidate in rows:
        key = norm(candidate.get("address") or "")
        if not key:
            key = f"{candidate.get('lat')},{candidate.get('lon')}"
        if key in groups:
            existing = groups[key]
            existing["dpe_count"] = existing.get("dpe_count", 1) + 1
            existing.setdefault("dpe_surfaces", []).append(candidate.get("surface"))
            continue
        item = dict(candidate)
        item["dpe_count"] = 1
        groups[key] = item
        order.append(key)
    return [groups[k] for k in order]


def resolve(payload):
    city = payload.get("city") or ""
    type_ = payload.get("type") or "appartement"
    surface = to_float(payload.get("surface")) or 0
    dpe = (payload.get("dpe") or "").upper() or None
    ges = (payload.get("ges") or "").upper() or None
    floor = payload.get("floor")
    year = payload.get("year")
    heating = payload.get("heating")
    street = payload.get("street") or ""
    lat = to_float(payload.get("lat"))
    lon = to_float(payload.get("lon"))
    blur_type = payload.get("blur_type")
    blur_radius = to_float(payload.get("blur_radius"))
    listing_date = payload.get("publication_date") or payload.get("modification_date")
    listing_dpe_date = payload.get("dpe_date")
    dpe_status = None

    # La plupart des sources HTML n'affichent pas le DPE dans la carte :
    # on va le chercher dans la page de détail au moment de la résolution.
    url = payload.get("url") or ""
    if url:
        enriched = enrich_url_energy(url)
        dpe = dpe or (enriched.get("dpe") or "").upper() or None
        ges = ges or (enriched.get("ges") or "").upper() or None
        surface = surface or to_float(enriched.get("surface")) or 0
        street = street or enriched.get("street") or ""
        listing_date = listing_date or enriched.get("publication_date") or enriched.get("modification_date")
        listing_dpe_date = listing_dpe_date or enriched.get("dpe_date")
        dpe_status = enriched.get("dpe_status")
        city = city or enriched.get("city") or ""
        type_ = enriched.get("type") or type_
    city = city or "Haubourdin"
    if type_ == "maison":
        # L'étage n'a pas de sens pour une maison.
        floor = None

    insee = ban_insee(city)
    if not insee:
        return {"error": f"Commune introuvable : {city}"}

    # Position exacte fournie par l'annonce : géocodage inverse, pas de matching DPE.
    if lat is not None and lon is not None and blur_type == "exact":
        try:
            addr = ban_reverse(lon, lat)
        except Exception:
            addr = None
        if addr:
            gap = days_between(listing_dpe_date, listing_date)
            return {
                "city": city,
                "insee": insee,
                "candidates": 1,
                "total_api": 0,
                "used_street": False,
                "street_query": None,
                "mode": "exact",
                "listing_date": listing_date,
                "listing_dpe": dpe,
                "listing_ges": ges,
                "listing_surface": surface,
                "dpe_status": dpe_status,
                "enriched_from_url": bool(url and ENRICH_CACHE.get(url)),
                "listing_dpe_date": listing_dpe_date,
                "listing_dpe_gap_days": gap,
                "dpe_near_count": 1 if gap is not None and abs(gap) <= 180 else 0,
                "dpe_near_total": 1,
                "results": [{
                    "address": addr.get("label"),
                    "score": 100,
                    "reasons": [
                        "position exacte de l'annonce",
                        "adresse retrouvée par coordonnées",
                    ],
                    "dpe": dpe,
                    "ges": ges,
                    "surface": surface,
                    "date": listing_dpe_date,
                    "numero_dpe": None,
                    "lat": lat,
                    "lon": lon,
                    "distance_m": 0,
                    "days_from_listing": gap,
                    "dpe_near_listing": (abs(gap) <= 180) if gap is not None else None,
                    "source": "coordonnées Bien'ici",
                }],
            }

    delta = max(4, round(surface * 0.1)) if surface else 30
    lo = max(9, (surface or 9) - delta)
    hi = (surface or 300) + delta
    filters = [
        f"code_insee_ban:{insee}",
        f"type_batiment:{type_}",
        f"surface_habitable_logement:[{lo} TO {hi}]",
    ]
    if dpe:
        filters.append(f"etiquette_dpe:{dpe}")
    if ges:
        filters.append(f"etiquette_ges:{ges}")

    qs = " AND ".join(filters)
    q = street_query(street, city) if street else None
    rows, total = fetch_dpe(qs, q=q)
    used_street = bool(q and rows)
    if not rows and q:
        rows, total = fetch_dpe(qs, q=None)
        used_street = False
    # Aucun DPE avec la classe ou la surface exacte : on élargit pour proposer
    # des adresses plausibles (le score reflétera l'écart).
    if not rows:
        wide_lo = max(9, (surface or 30) * 0.72)
        wide_hi = (surface or 120) * 1.38
        loose = [
            f"code_insee_ban:{insee}",
            f"type_batiment:{type_}",
            f"surface_habitable_logement:[{wide_lo:.0f} TO {wide_hi:.0f}]",
        ]
        loose_qs = " AND ".join(loose)
        rows, total = fetch_dpe(loose_qs, q=q)
        used_street = bool(q and rows)
        if not rows and q:
            rows, total = fetch_dpe(loose_qs, q=None)
            used_street = False

    L = {
        "surface": surface or 0,
        "dpe": dpe,
        "ges": ges,
        "type": type_,
        "floor": floor,
        "year": year,
        "heating": heating,
        "residence": None,
    }
    scored = []
    for c in rows:
        sc, reasons = score(c, L)
        pt = point_of(c)
        cand_lat = pt[0] if pt else None
        cand_lon = pt[1] if pt else None
        gap = days_between(c.get("date_etablissement_dpe"), listing_date)
        if gap is not None and abs(gap) <= 180:
            sc = min(99, sc + 4)
            reasons.append(f"DPE établi {abs(gap)} j {'avant' if gap < 0 else 'après'} l'annonce")
        dist = None
        if lat is not None and lon is not None and cand_lat is not None and cand_lon is not None:
            dist = round(haversine(lat, lon, cand_lat, cand_lon), 1)
        scored.append({
            "address": c.get("adresse_ban"),
            "score": sc,
            "reasons": reasons,
            "dpe": c.get("etiquette_dpe"),
            "ges": c.get("etiquette_ges"),
            "surface": c.get("surface_habitable_logement") or c.get("surface_habitable_immeuble"),
            "date": c.get("date_etablissement_dpe"),
            "numero_dpe": c.get("numero_dpe"),
            "lat": cand_lat,
            "lon": cand_lon,
            "distance_m": dist,
            "days_from_listing": gap,
            "dpe_near_listing": (abs(gap) <= 180) if gap is not None else None,
        })

    # Zone approximative fournie par l'annonce : on garde les DPE proches du point.
    if lat is not None and lon is not None and blur_type == "disk":
        radius = (blur_radius or 300) + 50
        near = [x for x in scored if x["distance_m"] is not None and x["distance_m"] <= radius]
        if near:
            scored = near

    scored.sort(key=lambda x: (-x["score"], x["distance_m"] if x["distance_m"] is not None else 1e12))
    grouped = group_dpe_candidates(scored)

    return {
        "city": city,
        "insee": insee,
        "candidates": len(grouped),
        "dpe_total": len(scored),
        "total_api": total,
        "used_street": used_street,
        "street_query": q,
        "mode": "disk" if (lat is not None and lon is not None and blur_type == "disk") else "city",
        "listing_date": listing_date,
        "listing_dpe": dpe,
        "listing_ges": ges,
        "listing_surface": surface,
        "dpe_status": dpe_status,
        "enriched_from_url": bool(url and ENRICH_CACHE.get(url)),
        "listing_dpe_date": listing_dpe_date,
        "listing_dpe_gap_days": days_between(listing_dpe_date, listing_date),
        "dpe_near_count": sum(1 for x in scored if x.get("dpe_near_listing")),
        "dpe_near_total": len(scored),
        "results": grouped[:8],
    }


FR_MONTHS = {
    "janv": 1, "janvier": 1, "fev": 2, "fevr": 2, "février": 2, "fevrier": 2,
    "mars": 3, "avr": 4, "avril": 4, "mai": 5, "juin": 6, "juil": 7,
    "juillet": 7, "aout": 8, "août": 8, "sept": 9, "septembre": 9,
    "oct": 10, "octobre": 10, "nov": 11, "novembre": 11, "dec": 12, "décembre": 12,
}


def iso_date(value):
    if not value:
        return None
    s = str(value).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\s+([A-Za-zéûôà]+)\s+(\d{4})", s, re.I)
    if m:
        month = FR_MONTHS.get(norm(m.group(2))[:4]) or FR_MONTHS.get(norm(m.group(2)))
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    return None


def meta_content(html, key):
    m = re.search(
        r'<meta[^>]+(?:property|name)="' + re.escape(key) + r'"[^>]+content="([^"]*)"',
        html, re.I,
    )
    return htmlmod.unescape(m.group(1)) if m else None


def jsonld_blocks(html):
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>([\s\S]*?)</script>', html, re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        out.append(data)
    return out


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk_json(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_json(v)


def extract_dpe(text):
    for pat in (
        r"classe\s+[ée]nergie\s*[:\-]?\s*([A-G])\b",
        r"[ée]tiquette\s+[ée]nergie(?:-climat)?\s*[:\-]?\s*([A-G])\b",
        r"\bDPE\s*[:\-]\s*([A-G])\b",
        r"\(DPE\)\s*([A-G])\b",
        r"diagnostic de performance [ée]nerg[ée]tique[^.\n]{0,80}?classe\s+([A-G])\b",
        r"performance [ée]nerg[ée]tique\s*[:\-]?\s*(?:classe\s+)?([A-G])\b",
    ):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).upper()
    return None


def extract_ges(text):
    for pat in (
        r"classe\s+GES\s*[:\-]?\s*([A-G])\b",
        r"classe\s+climat\s*[:\-]?\s*([A-G])\b",
        r"\(GES\)\s*([A-G])\b",
        r"\bGES\s*[:\-]\s*([A-G])\b",
        r"gaz à effet de serre[^.\n]{0,80}?classe\s*([A-G])\b",
    ):
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).upper()
    return None


def extract_surface(text):
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:2|\u00b2)", text or "", re.I):
        val = to_float(m.group(1))
        if val and 5 <= val <= 3000:
            return val
    return None


def extract_rooms(text):
    m = re.search(r"\bT\s*[-]?\s*(\d{1,2})\b", text or "", re.I)
    if m:
        return to_int(m.group(1))
    m = re.search(r"(\d{1,2})\s*pi[eè]ces?\b", text or "", re.I)
    return to_int(m.group(1)) if m else None


def extract_bedrooms(text):
    m = re.search(r"(\d{1,2})\s*chambres?\b", text or "", re.I)
    return to_int(m.group(1)) if m else None


def extract_price(text):
    vals = []
    for m in re.finditer(r"(\d{1,3}(?:[\s\xa0.]\d{3})+|\d{4,9})\s*€", text or ""):
        val = to_float(m.group(1))
        if val and 10000 <= val <= 10000000:
            vals.append(val)
    return vals[0] if vals else None


GENERIC_CITY_WORDS = {
    "maison", "appartement", "studio", "villa", "bien", "terrain", "immeuble",
    "local", "propriete", "vente", "location", "programme", "residence",
}


def clean_city_candidate(value):
    words = [w for w in re.split(r"\s+", str(value or "").strip()) if w]
    cleaned = []
    for word in words:
        raw = word.strip(" ,.;:")
        if not raw:
            continue
        if raw.isupper() and len(raw) <= 6:
            # EUR, DPE, GES, TTC, CC… ne sont pas des communes.
            continue
        if raw.lower() in GENERIC_CITY_WORDS:
            continue
        cleaned.append(raw)
    return " ".join(cleaned[-4:]).strip()


def extract_city_postal(text):
    for pat in (
        r"([A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\- ]{2,45})[ \t]*\((\d{5})\)",
        r"([A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\-]+(?:[ -][A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\-]+){0,3})[ \t]+(\d{5})\b",
        r"(\d{5})[ \t]+([A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\- ]{2,45})",
    ):
        m = re.search(pat, text or "")
        if not m:
            continue
        if m.group(1).isdigit():
            postal, city = m.group(1), m.group(2)
        else:
            city, postal = m.group(1), m.group(2)
        city = clean_city_candidate(city)
        if city:
            return smart_title(city), postal
    m = re.search(r"\b(\d{5})\b", text or "")
    return None, (m.group(1) if m else None)


def extract_street(text):
    m = re.search(
        r"\b((?:rue|avenue|boulevard|place|impasse|all[ée]e|quai|chemin|route)\s+"
        r"[A-ZÉÈÀÂÎÔÛÇ][\w'’\-]+(?:\s+[A-ZÉÈÀÂÎÔÛÇ][\w'’\-]+){0,4})",
        text or "", re.I,
    )
    return m.group(1).strip() if m else None


def extract_type(text):
    t = norm(text or "")
    if "maison" in t or "villa" in t:
        return "maison"
    if "appartement" in t or "studio" in t or "duplex" in t:
        return "appartement"
    return None


def extract_floor(text):
    m = re.search(r"(\d{1,2})\s*(?:er|e|ème)?\s*étage", text or "", re.I)
    if m:
        return to_int(m.group(1))
    if re.search(r"\bRDC\b|rez-de-chauss[ée]e", text or "", re.I):
        return 0
    return None


def extract_year(text):
    m = re.search(
        r"(?:ann[ée]e de construction|construite?|construction|édifiée?|bâtie?)\s*(?:en|:)?\s*((?:18|19|20)\d{2})",
        text or "", re.I,
    )
    return to_int(m.group(1)) if m else None


def extract_heating(text):
    m = re.search(r"(?:chauffage|type de chauffage)\s*[:\-]\s*([^|\n]{3,70})", text or "", re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"chauffage\s+(individuel|collectif|gaz|électrique|fioul|bois|pompe à chaleur)", text or "", re.I)
    return m.group(1).strip() if m else None


def extract_dpe_date(text):
    m = re.search(
        r"DPE[^0-9]{0,40}(\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{4}-\d{2}-\d{2})",
        text or "", re.I,
    )
    return iso_date(m.group(1)) if m else None


def extract_detail_energy(html, source_name):
    text = strip_tags(html)
    src = norm(source_name)
    dpe = ges = dpe_date = None
    status = None

    if "citya" in src:
        m = re.search(r"Diagnostic de performance énergétique, classe\s*([A-G])", text, re.I)
        if m:
            dpe = m.group(1).upper()
        m = re.search(r"Indice d'émission de gaz à effet de serre, classe\s*([A-G])", text, re.I)
        if m:
            ges = m.group(1).upper()
    elif "paruvendu" in src:
        m = re.search(r"[ÉE]TIQUETTE\s+[ÉE]NERGIE-CLIMAT\s*([A-G])", text, re.I)
        if not m:
            m = re.search(r"DPE\)\s*(?:[A-G]\s*){7}([A-G])", text, re.I)
        if m:
            dpe = m.group(1).upper()
        m = re.search(r"info\s+([A-G])\s+Indice d'émissions?", text, re.I)
        if m:
            ges = m.group(1).upper()
        m = re.search(r"Fait le\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
        if m:
            dpe_date = iso_date(m.group(1))
    elif "nestenn" in src:
        m = re.search(r"Classe\s+[ÉE]nergie\s*:\s*([A-G])", text, re.I)
        if m:
            dpe = m.group(1).upper()
        m = re.search(r"Classe\s+GES\s*:\s*([A-G])", text, re.I)
        if m:
            ges = m.group(1).upper()
    elif "orpi" in src:
        m = re.search(r"Classe\s+Energie\s*:\s*([A-G])", text, re.I)
        if m:
            dpe = m.group(1).upper()
        m = re.search(r"Classe\s+Climat\s*:\s*([A-G])", text, re.I)
        if m:
            ges = m.group(1).upper()
    elif "century" in src or "c21" in src:
        m = re.search(r"classe\s+[ée]nergie\s*([A-G])", text, re.I)
        if m:
            dpe = m.group(1).upper()
        m = re.search(r"classe\s+GES\s*([A-G])", text, re.I)
        if m:
            ges = m.group(1).upper()
        m = re.search(r"Date du DPE\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
        if m:
            dpe_date = iso_date(m.group(1))
    elif "immonot" in src:
        classes = re.findall(r'data-dpe-classe="([A-G])"', html, re.I)
        if classes:
            dpe = classes[0].upper()
            if len(classes) > 1:
                ges = classes[1].upper()
        m = re.search(r"Diagnostic réalisé le\s*([0-9]{1,2}\s+\w+\s+\d{4})", text, re.I)
        if m:
            dpe_date = iso_date(m.group(1))
    elif "iad" in src:
        m = re.search(r"Diagnostic de performance énergétique \(DPE\)\s*([A-G])", text, re.I)
        if m:
            dpe = m.group(1).upper()
        m = re.search(r"Indice d'émission de gaz à effet de serre \(GES\)\s*([A-G])", text, re.I)
        if m:
            ges = m.group(1).upper()
    elif "laforet" in src or "laforêt" in src:
        if re.search(r"DPE\s+NON\s+SOUMIS", text, re.I):
            status = "non_soumis"
        else:
            m = re.search(r"\bDPE\s*[:\-]?\s*([A-G])\b", text, re.I)
            if m:
                dpe = m.group(1).upper()

    dpe = dpe or extract_dpe(text)
    ges = ges or extract_ges(text)
    dpe_date = dpe_date or extract_dpe_date(text)
    if re.search(r"DPE\s+NON\s+SOUMIS", text, re.I):
        status = "non_soumis"
    return {"dpe": dpe, "ges": ges, "dpe_date": dpe_date, "dpe_status": status}


def generic_detail_listing(html, url, source_name):
    title = meta_content(html, "og:title") or ""
    if not title:
        m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
        title = strip_tags(m.group(1)) if m else ""
    desc = meta_content(html, "og:description") or meta_content(html, "description") or ""
    image = meta_content(html, "og:image")
    text = strip_tags(html)
    blob = f"{title} {desc}"

    price = None
    surface = None
    rooms = None
    bedrooms = None
    postal = None
    city = None
    street = None
    date_posted = None
    floor = None
    year = None
    heating = None
    lat = None
    lon = None
    kind = None

    for data in jsonld_blocks(html):
        for obj in walk_json(data):
            typ = obj.get("@type")
            if isinstance(typ, list):
                typ = " ".join(typ)
            typ = str(typ or "")
            if typ in ("RealEstateListing", "Product", "Apartment", "House", "SingleFamilyResidence", "Offer", "Residence"):
                price = price or to_float((obj.get("offers") or {}).get("price") if isinstance(obj.get("offers"), dict) else obj.get("price"))
                surface = surface or to_float((obj.get("floorSize") or {}).get("value") if isinstance(obj.get("floorSize"), dict) else None)
                rooms = rooms or to_int(obj.get("numberOfRooms"))
                bedrooms = bedrooms or to_int(obj.get("numberOfBedrooms"))
                date_posted = date_posted or iso_date(obj.get("datePosted"))
                addr = obj.get("address") if isinstance(obj.get("address"), dict) else {}
                if addr:
                    postal = postal or addr.get("postalCode")
                    city = city or addr.get("addressLocality")
                    street = street or addr.get("streetAddress")
                if not image and obj.get("image"):
                    img = obj.get("image")
                    image = img[0] if isinstance(img, list) else (img.get("url") if isinstance(img, dict) else img)
                off = obj.get("itemOffered")
                if isinstance(off, dict):
                    surface = surface or to_float((off.get("floorSize") or {}).get("value") if isinstance(off.get("floorSize"), dict) else None)
                    rooms = rooms or to_int(off.get("numberOfRooms"))
                    kind = kind or extract_type(str(off.get("@type") or off.get("name") or ""))
                kind = kind or extract_type(str(obj.get("name") or "") + " " + str(obj.get("@type") or ""))

    price = price or extract_price(blob) or extract_price(text[:4000])
    surface = surface or extract_surface(blob)
    rooms = rooms or extract_rooms(blob)
    bedrooms = bedrooms or extract_bedrooms(blob)
    kind = kind or extract_type(title) or extract_type(desc)
    city2, postal2 = extract_city_postal(blob)
    city = city or city2
    postal = postal or postal2
    street = street or extract_street(desc) or extract_street(text[:8000])
    date_posted = date_posted or iso_date(meta_content(html, "article:published_time"))
    floor = extract_floor(blob)
    year = extract_year(blob)
    heating = extract_heating(blob)
    dpe = extract_dpe(blob)
    ges = extract_ges(blob)
    dpe_date = extract_dpe_date(blob)
    energy = extract_detail_energy(html, source_name)
    dpe = energy.get("dpe") or dpe
    ges = energy.get("ges") or ges
    dpe_date = energy.get("dpe_date") or dpe_date
    dpe_status = energy.get("dpe_status")

    # Coordonnées exactes si la page expose un JSON-LD geo.
    for data in jsonld_blocks(html):
        for obj in walk_json(data):
            geo = obj.get("geo") if isinstance(obj.get("geo"), dict) else None
            if geo:
                lat = to_float(geo.get("latitude"))
                lon = to_float(geo.get("longitude"))
            if lat is not None and lon is not None:
                break

    return {
        "id": None,
        "source": source_name,
        "title": title or (f"{'Maison' if kind == 'maison' else 'Appartement'}"
                           + (f" {rooms} pièces" if rooms else "")
                           + (f" — {city}" if city else "")),
        "type": kind or "appartement",
        "city": city,
        "postal_code": postal,
        "price": price,
        "surface": surface,
        "rooms": rooms,
        "bedrooms": bedrooms,
        "year": year,
        "heating": heating,
        "floor": floor,
        "dpe": dpe,
        "ges": ges,
        "dpe_date": dpe_date,
        "dpe_status": dpe_status,
        "publication_date": date_posted,
        "modification_date": None,
        "listing_date": date_posted,
        "listing_date_kind": "publication",
        "address_known": bool(street),
        "blur_type": "exact" if (lat is not None and lon is not None) else None,
        "blur_radius": None,
        "lat": lat,
        "lon": lon,
        "district": None,
        "description": desc[:800],
        "photos": [image] if image else [],
        "url": url,
    }


def logic_detail_listing(html, url):
    m = re.search(r'<script id="__UFRN_LIFECYCLE_SERVERREQUEST__">([\s\S]*?)</script>', html)
    if not m:
        return generic_detail_listing(html, url, "Logic-Immo")
    data = logic_unquote_fetcher(m.group(1))
    if not data:
        return generic_detail_listing(html, url, "Logic-Immo")
    c = (((data.get("app_cldp") or {}).get("data") or {}).get("classified") or {})
    if not c:
        return generic_detail_listing(html, url, "Logic-Immo")
    sections = c.get("sections") or {}
    loc = sections.get("location") or {}
    addr = loc.get("address") or {}
    geometry = loc.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    lat = coords[1] if len(coords) == 2 else None
    lon = coords[0] if len(coords) == 2 else None
    facts = {}
    for f in (sections.get("hardFacts") or {}).get("facts") or []:
        facts[f.get("type")] = f.get("splitValue") or f.get("value")
    price = extract_price(json.dumps(
        (sections.get("hardFacts") or {}).get("price") or {},
        ensure_ascii=False,
    ))
    surface = to_float(facts.get("livingSpace")) or extract_surface(
        json.dumps(sections.get("hardFacts") or {}, ensure_ascii=False))
    rooms = to_int(facts.get("numberOfRooms"))
    bedrooms = to_int(facts.get("numberOfBedrooms"))
    kind = "maison" if str((c.get("rawData") or {}).get("propertyType")) == "HOUSE" else "appartement"
    desc = ((sections.get("description") or {}).get("description")
            or (sections.get("mainDescription") or {}).get("description") or "")
    text = strip_tags(html)
    dpe = extract_dpe(text)
    ges = extract_ges(text)
    dpe_date = extract_dpe_date(text)
    images = []
    for img in ((sections.get("gallery") or {}).get("images")
                or ((c.get("domains") or {}).get("medias") or {}).get("images") or []):
        if img.get("url"):
            images.append(img["url"])
    pub = iso_date((c.get("metadata") or {}).get("creationDate"))
    mod = iso_date((c.get("metadata") or {}).get("updateDate"))
    title = ((sections.get("mainDescription") or {}).get("headline")
             or (sections.get("hardFacts") or {}).get("title") or "")
    return {
        "id": f"logicimmo-{c.get('id')}",
        "source": "Logic-Immo",
        "title": title or f"{'Maison' if kind == 'maison' else 'Appartement'}"
                          + (f" {rooms} pièces" if rooms else "")
                          + (f" — {addr.get('city')}" if addr.get("city") else ""),
        "type": kind,
        "city": addr.get("city"),
        "postal_code": addr.get("zipCode"),
        "price": price,
        "surface": surface,
        "rooms": rooms,
        "bedrooms": bedrooms,
        "year": extract_year(text),
        "heating": extract_heating(text),
        "floor": extract_floor(text),
        "dpe": dpe,
        "ges": ges,
        "dpe_date": dpe_date,
        "publication_date": pub,
        "modification_date": mod,
        "listing_date": pub or mod,
        "listing_date_kind": "publication" if pub else "modification",
        "address_known": bool(loc.get("isAddressPublished")),
        "blur_type": "exact" if (lat is not None and lon is not None and loc.get("isAddressPublished")) else None,
        "blur_radius": None,
        "lat": lat,
        "lon": lon,
        "district": addr.get("district"),
        "description": desc[:800],
        "photos": images[:6],
        "url": url,
    }


def parse_orpi_detail(html, url):
    row = generic_detail_listing(html, url, "Orpi")
    title = meta_content(html, "og:title") or ""
    m = re.search(r"^(Maison|Appartement)\s+(.+?)\s+\d+(?:[.,]\d+)?\s*m\s*(?:2|\u00b2)", title, re.I)
    if m:
        row["type"] = "maison" if m.group(1).lower() == "maison" else "appartement"
        row["city"] = smart_title(m.group(2).strip())
        row["title"] = (
            ("Maison" if row["type"] == "maison" else "Appartement")
            + (f" {extract_rooms(title)} pièces" if extract_rooms(title) else "")
            + f" — {row['city']}"
        )
    if not row.get("surface"):
        row["surface"] = extract_surface(title)
    if not row.get("rooms"):
        row["rooms"] = extract_rooms(title)
    if not row.get("price"):
        row["price"] = extract_price(meta_content(html, "og:description") or "") or extract_price(title)
    if not row.get("city"):
        pm = re.search(r"-(\d{5})-", urllib.parse.urlparse(url).path)
        if pm:
            row["postal_code"] = pm.group(1)
            info = ban_city_info(pm.group(1))
            if info:
                row["city"] = info.get("city")
    row["type"] = row.get("type") or extract_type(title) or "appartement"
    return row


def parse_immonot_detail(html, url):
    row = generic_detail_listing(html, url, "Immonot")
    title = meta_content(html, "og:title") or ""
    m = re.search(r"à vendre\s+(.+?)\s*\((\d{5})\)", title, re.I)
    if m:
        row["city"] = smart_title(m.group(1))
        row["postal_code"] = m.group(2)
    row["type"] = extract_type(title) or row.get("type") or "maison"
    row["rooms"] = row.get("rooms") or extract_rooms(title)
    row["surface"] = row.get("surface") or extract_surface(title)
    row["price"] = row.get("price") or extract_price(title)
    if row.get("city"):
        row["title"] = (
            ("Maison" if row["type"] == "maison" else "Appartement")
            + (f" {row['rooms']} pièces" if row.get("rooms") else "")
            + f" — {row['city']}"
        )
    return row


def extract_c21_detail(html, url):
    row = parse_detail(url, html)
    text = strip_tags(html)
    row["dpe_date"] = extract_dpe_date(text)
    row["publication_date"] = iso_date(re.search(
        r"(?:publi[ée]e?|mise en ligne)[^0-9]{0,20}(\d{1,2}[/.]\d{1,2}[/.]\d{4})",
        text, re.I).group(1)) if re.search(
        r"(?:publi[ée]e?|mise en ligne)[^0-9]{0,20}(\d{1,2}[/.]\d{1,2}[/.]\d{4})",
        text, re.I) else None
    row["listing_date"] = row["publication_date"]
    row["listing_date_kind"] = "publication"
    row["source"] = "Century21"
    if row.get("city"):
        row["title"] = (
            ("Maison" if row.get("type") == "maison" else "Appartement")
            + (f" {row.get('rooms')} pièces" if row.get("rooms") else "")
            + f" — {row['city']}"
        )
    row["photos"] = [meta_content(html, "og:image")] if meta_content(html, "og:image") else []
    row["postal_code"] = None
    row["blur_type"] = None
    row["lat"] = None
    row["lon"] = None
    row["address_known"] = bool(row.get("street"))
    return row


def bienici_url_listing(url):
    path = urllib.parse.urlparse(url).path
    ad_id = path.rstrip("/").split("/")[-1]
    if not ad_id:
        return None
    u = (
        "https://www.bienici.com/realEstateAds-one.json?filters="
        + urllib.parse.quote(json.dumps({"onTheMarket": [True]}, separators=(",", ":")))
        + "&onlyRealEstateAd=" + urllib.parse.quote(ad_id)
    )
    data = fetch_json_url(u)
    if not data or not data.get("id"):
        return None
    row = bienici_normalize(data)
    if not row:
        return None
    row["url"] = url
    row["source"] = "Bien'ici"
    return row


ALLOWED_ANALYZE_HOSTS = (
    "bienici.com", "logic-immo.com", "century21.fr", "orpi.com", "immonot.com",
    "iadfrance.fr", "citya.com", "paruvendu.fr", "nestenn.com", "laforet.com",
    "immobilier.notaires.fr", "superimmo.com", "foncia.fr",
    "guyhoquet-immobilier.fr", "squarehabitat.fr",
)
BLOCKED_ANALYZE_HOSTS = {
    "pap.fr": "Cloudflare Turnstile",
    "seloger.com": "DataDome",
    "leboncoin.fr": "DataDome",
    "figaro.fr": "Cloudflare",
    "ouestfrance-immo.com": "Akamai",
    "entreparticuliers.com": "Cloudflare",
}


def analyze_url(url):
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    bare = host[4:] if host.startswith("www.") else host
    source_name = {
        "bienici.com": "Bien'ici",
        "logic-immo.com": "Logic-Immo",
        "century21.fr": "Century21",
        "orpi.com": "Orpi",
        "immonot.com": "Immonot",
        "iadfrance.fr": "iad",
        "citya.com": "Citya",
        "paruvendu.fr": "ParuVendu",
        "nestenn.com": "Nestenn",
        "laforet.com": "Laforêt",
        "pap.fr": "PAP",
        "seloger.com": "SeLoger",
        "leboncoin.fr": "Leboncoin",
        "figaro.fr": "Figaro Immo",
        "ouestfrance-immo.com": "Ouest-France Immo",
        "entreparticuliers.com": "Entreparticuliers",
    }.get(bare, bare)
    for domain, blocker in BLOCKED_ANALYZE_HOSTS.items():
        if bare == domain or bare.endswith("." + domain):
            return {
                "error": f"{source_name} bloque l'analyse automatique ({blocker}).",
                "blocked": True,
                "source": source_name,
                "advice": (
                    "Copie le texte de l'annonce dans l'outil DPE manuel, ou cherche la même "
                    "référence sur Logic-Immo/Bien'ici. On ne contourne pas les CAPTCHA."
                ),
            }
    if not any(bare == d or bare.endswith("." + d) for d in ALLOWED_ANALYZE_HOSTS):
        return {
            "error": f"Site non pris en charge : {bare}.",
            "blocked": False,
            "advice": "Utilise un lien Bien'ici, Logic-Immo, Century21, Orpi ou Immonot.",
        }
    try:
        if bare == "bienici.com":
            listing = bienici_url_listing(url)
            if not listing:
                return {"error": "Annonce Bien'ici introuvable ou retirée.", "source": "Bien'ici"}
        else:
            html = fetch_html(url, timeout=35)
            low = html[:40000].lower()
            if any(x in low for x in ("captcha-delivery", "datadome", "just a moment", "access denied")):
                return {
                    "error": f"{source_name} affiche un CAPTCHA sur cette page.",
                    "blocked": True,
                    "source": source_name,
                    "advice": "Copie le texte de l'annonce dans l'outil DPE manuel.",
                }
            if bare == "logic-immo.com":
                listing = logic_detail_listing(html, url)
            elif bare == "century21.fr":
                listing = extract_c21_detail(html, url)
            elif bare == "orpi.com":
                listing = parse_orpi_detail(html, url)
            elif bare == "immonot.com":
                listing = parse_immonot_detail(html, url)
            elif bare == "laforet.com":
                listing = generic_detail_listing(html, url, source_name)
                path = urllib.parse.urlparse(url).path
                m = re.search(r"/acheter/([^/]+)/", path)
                if m:
                    listing["city"] = smart_title(m.group(1).replace("-", " "))
                if listing.get("city") and not listing.get("postal_code"):
                    info = ban_city_info(listing["city"])
                    if info:
                        listing["postal_code"] = info.get("postcode")
            else:
                listing = generic_detail_listing(html, url, source_name)
    except Exception as e:
        return {"error": f"Impossible de lire la page : {e}", "source": source_name}

    if not listing:
        return {"error": "Aucune annonce détectée sur cette page.", "source": source_name}
    if not listing.get("city") and listing.get("postal_code"):
        info = ban_city_info(listing["postal_code"])
        if info:
            listing["city"] = info.get("city")
            listing["postal_code"] = listing["postal_code"] or info.get("postcode")
    if not listing.get("city"):
        return {
            "error": "Ville introuvable dans la page.",
            "source": source_name,
            "listing": listing,
            "advice": "Vérifie que le lien pointe bien vers une annonce complète, ou utilise l'outil DPE manuel.",
        }

    warnings = []
    if not listing.get("surface"):
        warnings.append("Surface non détectée : la recherche DPE sera plus large.")
    if not listing.get("dpe"):
        warnings.append("DPE non affiché : le matching repose sur la ville, la surface et le type.")
    payload = {
        "city": listing.get("city"),
        "type": listing.get("type") or "appartement",
        "surface": listing.get("surface"),
        "dpe": listing.get("dpe"),
        "ges": listing.get("ges"),
        "floor": listing.get("floor"),
        "year": listing.get("year"),
        "heating": listing.get("heating"),
        "street": listing.get("street"),
        "lat": listing.get("lat"),
        "lon": listing.get("lon"),
        "blur_type": listing.get("blur_type"),
        "blur_radius": listing.get("blur_radius"),
        "publication_date": listing.get("publication_date"),
        "modification_date": listing.get("modification_date"),
        "dpe_date": listing.get("dpe_date"),
    }
    try:
        resolved = resolve(payload)
    except Exception as e:
        resolved = {"error": str(e), "results": []}
    return {
        "url": url,
        "source": source_name,
        "listing": listing,
        "resolve": resolved,
        "warnings": warnings,
    }


STATIC_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".zip": "application/zip",
}


class Handler(BaseHTTPRequestHandler):
    def _serve_static(self, parsed):
        path = urllib.parse.unquote(parsed.path or "/")
        if path in ("", "/"):
            path = "/search.html"
        target = os.path.normpath(os.path.join(STATIC_ROOT, path.lstrip("/").replace("/", os.sep)))
        if not target.startswith(STATIC_ROOT + os.sep) and target != STATIC_ROOT:
            self._json({"error": "forbidden"}, 403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "search.html")
        ext = os.path.splitext(target)[1].lower()
        if ext not in STATIC_TYPES or not os.path.isfile(target):
            self._json({"error": "not found"}, 404)
            return
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            self._json({"error": str(exc)}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[ext])
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control",
            "no-cache" if ext in (".html", ".js", ".css", ".webmanifest") else "public, max-age=3600",
        )
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({"ok": True})
            return
        if parsed.path == "/api/geo/departements":
            try:
                self._json(get_departements_geo())
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/geo/place":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0]
            try:
                self._json(bienici_place(q))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/geo/communes":
            qs = urllib.parse.parse_qs(parsed.query)
            departments = [x for x in (qs.get("departments") or [""])[0].split(",") if x]
            try:
                self._json({"departments": departments, "communes": get_communes(departments)})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/geo/communes-in-polygon":
            qs = urllib.parse.parse_qs(parsed.query)
            polygon = parse_polygon((qs.get("polygon") or [""])[0])
            if not polygon:
                self._json({"error": "Zone invalide."}, 400)
                return
            try:
                depts = set()
                for lat, lon in polygon:
                    dept = department_for_point(lat, lon)
                    if dept:
                        depts.add(dept)
                lat0 = sum(p[0] for p in polygon) / len(polygon)
                lon0 = sum(p[1] for p in polygon) / len(polygon)
                dept0 = department_for_point(lat0, lon0)
                if dept0:
                    depts.add(dept0)
                communes = get_communes(sorted(depts))
                inside = [
                    c for c in communes
                    if point_in_polygon(float(c["lat"]), float(c["lon"]), polygon)
                ]
                inside.sort(key=lambda c: -(c.get("population") or 0))
                self._json({
                    "departments": sorted(depts),
                    "communes": inside,
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/geo/communes-in-radius":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            radius = to_float((qs.get("radius") or ["10"])[0]) or 10
            lat = to_float((qs.get("lat") or [None])[0])
            lon = to_float((qs.get("lon") or [None])[0])
            try:
                if lat is None or lon is None:
                    center = ban_city_center(q)
                    if not center:
                        self._json({"error": f"Ville introuvable : {q}"}, 404)
                        return
                    lat, lon = center["lat"], center["lon"]
                depts = set()
                geo = get_departements_geo()
                for feature in geo.get("features", []):
                    code = (feature.get("properties") or {}).get("code")
                    geometry = feature.get("geometry") or {}
                    coords = []

                    def collect(node):
                        if isinstance(node, list) and node and isinstance(node[0], (int, float)):
                            coords.append((float(node[1]), float(node[0])))
                        elif isinstance(node, list):
                            for item in node:
                                collect(item)

                    collect(geometry.get("coordinates") or [])
                    if not coords or not code:
                        continue
                    lats = [p[0] for p in coords]
                    lons = [p[1] for p in coords]
                    blat = (min(lats) + max(lats)) / 2
                    blon = (min(lons) + max(lons)) / 2
                    brad = max(
                        haversine_km(blat, blon, la, lo)
                        for la in (min(lats), max(lats))
                        for lo in (min(lons), max(lons))
                    )
                    if haversine_km(lat, lon, blat, blon) <= radius + brad:
                        depts.add(code)
                communes = get_communes(sorted(depts))
                inside = [
                    c for c in communes
                    if c.get("lat") is not None and c.get("lon") is not None
                    and haversine_km(lat, lon, float(c["lat"]), float(c["lon"])) <= radius
                ]
                inside.sort(key=lambda c: -(c.get("population") or 0))
                self._json({
                    "center": {"lat": lat, "lon": lon},
                    "radius_km": radius,
                    "departments": sorted(depts),
                    "communes": inside,
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/sources":
            imported_counts = {}
            for row in IMPORTED:
                src = str(row.get("source") or "SeLoger")
                imported_counts[src] = imported_counts.get(src, 0) + 1
            self._json({
                "sources": SOURCES_REGISTRY,
                "last": SOURCE_LAST,
                "imported": imported_counts,
            })
            return
        if parsed.path == "/api/listings":
            qs = urllib.parse.parse_qs(parsed.query)
            city = (qs.get("city") or [""])[0]
            type_ = (qs.get("type") or ["appartement"])[0]
            max_price = to_float((qs.get("max_price") or [None])[0])
            min_price = to_float((qs.get("min_price") or [None])[0])
            min_surface = to_float((qs.get("min_surface") or [None])[0])
            max_surface = to_float((qs.get("max_surface") or [None])[0])
            min_rooms = to_int((qs.get("min_rooms") or [None])[0])
            max_rooms = to_int((qs.get("max_rooms") or [None])[0])
            dpe_max = ((qs.get("dpe_max") or [""])[0] or "").upper() or None
            ges_max = ((qs.get("ges_max") or [""])[0] or "").upper() or None
            departments = [x for x in (qs.get("departments") or [""])[0].split(",") if x]
            cities = [x for x in (qs.get("cities") or [""])[0].split(",") if x]
            polygon = parse_polygon((qs.get("polygon") or [""])[0])
            if polygon and not city and not departments and not cities:
                # Zone dessinée sans ville : on part du département du centroïde.
                lat0 = sum(p[0] for p in polygon) / len(polygon)
                lon0 = sum(p[1] for p in polygon) / len(polygon)
                dept = department_for_point(lat0, lon0)
                if not dept:
                    try:
                        addr = ban_reverse(lon0, lat0)
                    except Exception:
                        addr = None
                    postcode = (addr or {}).get("postcode") or ""
                    dept = postcode[:2] if postcode else ""
                    if not dept and (addr or {}).get("city"):
                        city = addr["city"]
                if dept:
                    departments = [dept]
                elif not city:
                    self._json({"error": "Impossible de localiser la zone dessinée."}, 400)
                    return
            if not city and not departments and not cities:
                city = "Haubourdin"
            try:
                listings, sources, unlocated_count, approx_count = get_listings(
                    city, type_,
                    max_price=max_price,
                    min_price=min_price,
                    min_surface=min_surface,
                    max_surface=max_surface,
                    min_rooms=min_rooms,
                    max_rooms=max_rooms,
                    dpe_max=dpe_max,
                    ges_max=ges_max,
                    departments=departments or None,
                    cities=cities or None,
                    polygon=polygon,
                )
                city_counts = Counter(
                    r.get("city") for r in listings if r.get("city")
                )
                self._json({
                    "city": city,
                    "departments": departments,
                    "cities": cities,
                    "type": type_,
                    "count": len(listings),
                    "city_counts": dict(city_counts.most_common()),
                    "sources": sources,
                    "listings": listings,
                    "polygon_filtered": bool(polygon),
                    "unlocated_count": unlocated_count,
                    "approx_count": approx_count,
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        self._serve_static(parsed)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/import":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                html = payload.get("html") or ""
                url = payload.get("url") or ""
                source = payload.get("source") or ""
                if not html:
                    self._json({"error": "HTML manquant."}, 400)
                    return
                rows = parse_import_html(html, url, source)
                store_imported(rows)
                self._json({
                    "count": len(rows),
                    "source": detect_import_source(url, source or "SeLoger"),
                    "listings": rows[:20],
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path == "/api/analyze":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                url = (payload.get("url") or "").strip()
                if not url:
                    self._json({"error": "URL manquante."}, 400)
                    return
                self._json(analyze_url(url))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if parsed.path != "/api/resolve":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            self._json(resolve(payload))
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print("[api]", fmt % args, flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"API sur http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
