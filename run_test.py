#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test réel : 10 annonces Immonot à Tours.

Deux passes :
  A. "sans rue" : on ne donne que ville + type + surface + DPE/GES.
  B. "avec rue" : on ajoute le nom de rue/place extrait de l'annonce (q=...),
     ce qui est réaliste : beaucoup d'annonces donnent la rue ou le quartier.

On mesure si le bon DPE / la bonne rue sort dans le top 1/3/5.
"""

import csv
import datetime
import json
import math
import re
import time
import unicodedata
from urllib.parse import quote, parse_qs, urlparse
from urllib.request import Request, urlopen

API = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe03existant/lines"
FIELDS = [
    "numero_dpe", "adresse_ban", "code_insee_ban", "nom_commune_ban",
    "surface_habitable_logement", "surface_habitable_immeuble",
    "etiquette_dpe", "etiquette_ges", "type_batiment",
    "annee_construction", "periode_construction",
    "numero_etage_appartement", "nombre_appartement",
    "complement_adresse_batiment", "complement_adresse_logement",
    "date_etablissement_dpe", "_geopoint",
    "type_energie_principale_chauffage", "methode_application_dpe",
]

LISTINGS = [
    {
        "id": 1, "label": "Appartement Bd Heurteloup",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011846_37007-2707/achat-appartement-a-vendre-37000-tours-indre-et-loire.html",
        "type": "appartement", "surface": 55, "dpe": "E", "ges": "E",
        "rooms": 3, "floor": 3, "year": None, "heating": "gaz",
        "ground_label": "Boulevard Heurteloup, Tours",
        "street": "Heurteloup", "street_query": "Heurteloup",
        "ground_lat": 47.391872, "ground_lon": 0.697842, "radius": 150,
    },
    {
        "id": 2, "label": "Appartement Place Foire au Roi",
        "url": "https://www.immonot.com/immobilier-notaire/detail/4000000370211926/achat-appartement-a-vendre-37000-tours-indre-et-loire.html",
        "type": "appartement", "surface": 61, "dpe": "D", "ges": "D",
        "rooms": 2, "floor": 2, "year": None, "heating": None,
        "ground_label": "Place Foire le Roi, Tours",
        "street": "Foire", "street_query": "Foire",
        "ground_lat": 47.396464, "ground_lon": 0.689949, "radius": 150,
    },
    {
        "id": 3, "label": "Appartement Avenue Grammont",
        "url": "https://www.immonot.com/immobilier-notaire/detail/4000000370212067/achat-appartement-a-vendre-37000-tours-indre-et-loire.html",
        "type": "appartement", "surface": 102, "dpe": "D", "ges": "C",
        "rooms": 4, "floor": 7, "year": None, "heating": None,
        "ground_label": "Avenue de Grammont, Tours",
        "street": "Grammont", "street_query": "Grammont",
        "ground_lat": 47.381415, "ground_lon": 0.692448, "radius": 200,
    },
    {
        "id": 4, "label": "Appartement 42 Rue Traversière",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011846_37007-2861/achat-appartement-a-vendre-37000-tours-indre-et-loire.html",
        "type": "appartement", "surface": 30, "dpe": "D", "ges": "D",
        "rooms": 1, "floor": 0, "year": 1998, "heating": "collectif",
        "ground_label": "42 Rue Traversière, Tours",
        "street": "Traversière", "street_query": "Traversière",
        "ground_lat": 47.392647, "ground_lon": 0.699496, "radius": 100,
    },
    {
        "id": 5, "label": "Appartement Rue du Cluzel",
        "url": "https://www.immonot.com/immobilier-notaire/detail/4000003700113402/achat-appartement-a-vendre-37000-tours-indre-et-loire.html",
        "type": "appartement", "surface": 75, "dpe": "C", "ges": "A",
        "rooms": 3, "floor": 1, "year": None, "heating": "électrique",
        "ground_label": "Rue du Cluzel, Tours",
        "street": "Cluzel", "street_query": "Cluzel",
        "ground_lat": 47.382635, "ground_lon": 0.685114, "radius": 150,
    },
    {
        "id": 6, "label": "Appartement 16 Rue Germaine Taillefer",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011846_37007-2837/achat-appartement-a-vendre-37200-tours-indre-et-loire.html",
        "type": "appartement", "surface": 30, "dpe": "C", "ges": "B",
        "rooms": 1, "floor": 1, "year": 2010, "heating": None,
        "ground_label": "16 Rue Germaine Tailleferre, Tours",
        "street": "Taillefer", "street_query": "Taillefer",
        "ground_lat": 47.368384, "ground_lon": 0.678131, "radius": 100,
    },
    {
        "id": 7, "label": "Appartement Rue Auguste Chevalier",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011905_37009-2675/achat-appartement-a-vendre-37000-tours-indre-et-loire.html",
        "type": "appartement", "surface": 88, "dpe": "F", "ges": "F",
        "rooms": 3, "floor": None, "year": None, "heating": "gaz",
        "ground_label": "Rue Auguste Chevallier, Tours",
        "street": "Cheval", "street_query": "Chevalier",
        "ground_lat": 47.379881, "ground_lon": 0.679107, "radius": 150,
    },
    {
        "id": 8, "label": "Maison Quartier Velpeau",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011905_37009-2710/achat-maison-a-vendre-37000-tours-indre-et-loire.html",
        "type": "maison", "surface": 92, "dpe": "F", "ges": "E",
        "rooms": 5, "floor": None, "year": None, "heating": None,
        "ground_label": "Quartier Velpeau, Tours (Place Velpeau)",
        "street": None, "street_query": None,
        "ground_lat": 47.386914, "ground_lon": 0.702460, "radius": 500,
    },
    {
        "id": 9, "label": "Maison Tours Nord (près Leclerc)",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011902_37072-672/achat-maison-a-vendre-37100-tours-indre-et-loire.html",
        "type": "maison", "surface": 81, "dpe": "D", "ges": "D",
        "rooms": 4, "floor": None, "year": None, "heating": None,
        "ground_label": "Tours Nord (Place du Nord)",
        "street": None, "street_query": None,
        "ground_lat": 47.421845, "ground_lon": 0.681399, "radius": 500,
    },
    {
        "id": 10, "label": "Maison Saint-Symphorien",
        "url": "https://www.immonot.com/immobilier-notaire/detail/0000011898_37037-27/achat-maison-a-vendre-37100-tours-indre-et-loire.html",
        "type": "maison", "surface": 107, "dpe": "D", "ges": "B",
        "rooms": 5, "floor": None, "year": None, "heating": None,
        "ground_label": "Saint-Symphorien, Tours (Place du Faubourg)",
        "street": None, "street_query": None,
        "ground_lat": 47.403048, "ground_lon": 0.694044, "radius": 500,
    },
]


def fetch_json(url):
    req = Request(url, headers={
        "User-Agent": "ValentineTest/1.0 (test immobilier)",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_dpe(qs, q=None, max_rows=5000):
    select = ",".join(FIELDS)
    qpart = f"&q={quote(q, safe='')}" if q else ""
    url = f"{API}?size=1000&select={quote(select, safe='')}&qs={quote(qs, safe='')}{qpart}"
    rows = []
    total = 0
    for _ in range(5):
        data = fetch_json(url)
        total = data.get("total", 0)
        rows.extend(data.get("results", []))
        if len(rows) >= total or not data.get("next"):
            break
        after = parse_qs(urlparse(data["next"]).query).get("after", [None])[0]
        if not after:
            break
        url = f"{API}?size=1000&select={quote(select, safe='')}&qs={quote(qs, safe='')}&after={quote(after, safe='')}{qpart}"
    return rows[:max_rows], total


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_of(c):
    raw = str(c.get("_geopoint") or "")
    parts = raw.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def year_of(c):
    if c.get("annee_construction"):
        try:
            return int(c["annee_construction"])
        except (TypeError, ValueError):
            pass
    p = str(c.get("periode_construction") or "")
    m = re.search(r"(\d{4})\s*[-–]\s*(\d{4})", p)
    if m:
        return round((int(m.group(1)) + int(m.group(2))) / 2)
    m = re.search(r"(\d{4})", p)
    return int(m.group(1)) if m else None


def floor_of(c):
    txt = f"{c.get('complement_adresse_logement') or ''} {c.get('complement_adresse_batiment') or ''}"
    m = re.search(r"[EÉ]tage\s*[:\-–]?\s*(\d+)", txt, re.I)
    if m:
        return int(m.group(1))
    if re.search(r"\bRDC\b|rez-de-chaussée", txt, re.I):
        return 0
    try:
        n = int(c.get("numero_etage_appartement"))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def norm(s):
    s = str(s or "").lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def score(c, L):
    s = 0
    reasons = []
    try:
        surf = float(c.get("surface_habitable_logement") or c.get("surface_habitable_immeuble") or 0)
    except (TypeError, ValueError):
        surf = 0
    if surf:
        d = abs(surf - L["surface"])
        if d <= 1:
            s += 28; reasons.append("surface quasi identique")
        elif d <= 3:
            s += 20; reasons.append(f"surface à {d:.1f} m²")
        elif d <= 5:
            s += 12; reasons.append(f"surface à {d:.1f} m²")
        elif d <= 8:
            s += 5; reasons.append(f"surface à {d:.1f} m²")
        else:
            s -= 6
    if L["dpe"] and c.get("etiquette_dpe") == L["dpe"]:
        s += 15; reasons.append(f"DPE {L['dpe']}")
    if L["ges"] and c.get("etiquette_ges") == L["ges"]:
        s += 10; reasons.append(f"GES {L['ges']}")
    if c.get("type_batiment") == L["type"]:
        s += 10; reasons.append("type de bien")
    cy = year_of(c)
    if L["year"] and cy:
        d = abs(cy - L["year"])
        if d <= 2:
            s += 12; reasons.append("année de construction")
        elif d <= 5:
            s += 8; reasons.append("année proche")
        elif d <= 10:
            s += 3
    cf = floor_of(c)
    if L["floor"] is not None and cf is not None:
        if cf == L["floor"]:
            s += 10; reasons.append("étage identique")
        elif abs(cf - L["floor"]) <= 1:
            s += 4; reasons.append("étage proche")
    if L["heating"] and c.get("type_energie_principale_chauffage"):
        if norm(L["heating"]) in norm(c["type_energie_principale_chauffage"]):
            s += 6; reasons.append("chauffage")
    date = c.get("date_etablissement_dpe")
    if date:
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(date[:10])).days / 365.25
            if age <= 2:
                s += 5; reasons.append("DPE récent")
            elif age <= 5:
                s += 3
        except ValueError:
            pass
    return max(0, min(99, round(s))), reasons


def run_pass(L, use_street=False):
    delta = max(3, round(L["surface"] * 0.1))
    lo = max(9, L["surface"] - delta)
    hi = L["surface"] + delta
    filters = [
        "code_insee_ban:37261",
        f"type_batiment:{L['type']}",
        f"surface_habitable_logement:[{lo} TO {hi}]",
    ]
    if L["dpe"]:
        filters.append(f"etiquette_dpe:{L['dpe']}")
    if L["ges"]:
        filters.append(f"etiquette_ges:{L['ges']}")

    attempts = [
        ("DPE+GES+surface", list(filters)),
        ("DPE+surface", [f for f in filters if not f.startswith("etiquette_ges")]),
        ("surface seule", [f for f in filters if not f.startswith("etiquette_dpe") and not f.startswith("etiquette_ges")]),
    ]
    rows, total, used = [], 0, None
    for name, flt in attempts:
        qs = " AND ".join(flt)
        q = L["street_query"] if use_street else None
        rows, total = fetch_dpe(qs, q=q)
        used = name
        if rows:
            break

    scored = []
    for c in rows:
        sc, reasons = score(c, L)
        pt = point_of(c)
        dist = haversine(L["ground_lat"], L["ground_lon"], pt[0], pt[1]) if pt else None
        scored.append({"c": c, "score": sc, "reasons": reasons, "dist": dist})
    scored.sort(key=lambda x: (-x["score"], x["dist"] if x["dist"] is not None else 1e12))

    rank_radius = None
    rank_street = None
    for i, x in enumerate(scored, 1):
        if rank_radius is None and x["dist"] is not None and x["dist"] <= L["radius"]:
            rank_radius = i
        if L["street"] and rank_street is None and norm(L["street"]) in norm(x["c"].get("adresse_ban")):
            rank_street = i
        if rank_radius and (rank_street or not L["street"]):
            break

    top = scored[:3]
    return {
        "pass": "B_avec_rue" if use_street else "A_sans_rue",
        "q": L["street_query"] if use_street else None,
        "candidates": len(rows),
        "total_api": total,
        "filter_used": used,
        "rank_radius": rank_radius,
        "rank_street": rank_street,
        "top1_score": top[0]["score"] if top else None,
        "top1_address": top[0]["c"].get("adresse_ban") if top else None,
        "top1_distance": round(top[0]["dist"], 1) if top and top[0]["dist"] is not None else None,
        "top1_reasons": top[0]["reasons"] if top else [],
        "top3": [
            {
                "address": x["c"].get("adresse_ban"),
                "score": x["score"],
                "distance": round(x["dist"], 1) if x["dist"] is not None else None,
                "dpe": x["c"].get("etiquette_dpe"),
                "ges": x["c"].get("etiquette_ges"),
                "surface": x["c"].get("surface_habitable_logement"),
                "date": x["c"].get("date_etablissement_dpe"),
            }
            for x in top
        ],
        "top1_ok": bool(rank_radius == 1 or rank_street == 1),
        "top3_ok": bool((rank_radius is not None and rank_radius <= 3) or (rank_street is not None and rank_street <= 3)),
        "top5_ok": bool((rank_radius is not None and rank_radius <= 5) or (rank_street is not None and rank_street <= 5)),
    }


def summarize(results, key):
    rows = [r[key] for r in results if r.get(key)]
    if not rows:
        return None
    return {
        "n": len(rows),
        "top1": sum(1 for x in rows if x["top1_ok"]),
        "top3": sum(1 for x in rows if x["top3_ok"]),
        "top5": sum(1 for x in rows if x["top5_ok"]),
    }


def run():
    results = []
    for L in LISTINGS:
        print(f"[{L['id']:2d}] {L['label']}...", flush=True)
        a = run_pass(L, use_street=False)
        b = run_pass(L, use_street=True) if L["street_query"] else None
        results.append({
            "id": L["id"], "label": L["label"], "url": L["url"],
            "type": L["type"], "surface": L["surface"], "dpe": L["dpe"], "ges": L["ges"],
            "floor": L["floor"], "year": L["year"],
            "ground_label": L["ground_label"],
            "ground_lat": L["ground_lat"], "ground_lon": L["ground_lon"], "radius": L["radius"],
            "A": a, "B": b,
        })
        print(f"     A: {a['candidates']:4d} cand. | rue #{a['rank_street']} | rayon #{a['rank_radius']} | top1 {a['top1_address']}", flush=True)
        if b:
            print(f"     B: {b['candidates']:4d} cand. | rue #{b['rank_street']} | rayon #{b['rank_radius']} | top1 {b['top1_address']}", flush=True)
        time.sleep(0.3)

    with open("test-results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    with open("test-results.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "annonce", "type", "surface", "dpe", "ges", "cand_A", "rue_A", "rayon_A", "top1_A", "dist_A", "top3_A", "cand_B", "rue_B", "rayon_B", "top1_B", "dist_B", "top3_B"])
        for r in results:
            a, b = r["A"], r["B"] or {}
            w.writerow([
                r["id"], r["label"], r["type"], r["surface"], r["dpe"], r["ges"],
                a["candidates"], a["rank_street"], a["rank_radius"], a["top1_address"], a["top1_distance"], "oui" if a["top3_ok"] else "non",
                b.get("candidates", ""), b.get("rank_street", ""), b.get("rank_radius", ""), b.get("top1_address", ""), b.get("top1_distance", ""), "oui" if b.get("top3_ok") else ("non" if b else ""),
            ])

    sa = summarize(results, "A")
    sb = summarize(results, "B")
    print("\n=== RESULTATS ===")
    print(f"Passe A (sans rue) : top1 {sa['top1']}/{sa['n']} | top3 {sa['top3']}/{sa['n']} | top5 {sa['top5']}/{sa['n']}")
    if sb:
        print(f"Passe B (avec rue) : top1 {sb['top1']}/{sb['n']} | top3 {sb['top3']}/{sb['n']} | top5 {sb['top5']}/{sb['n']}")
    print("\nDétail :")
    for r in results:
        a, b = r["A"], r["B"]
        print(f"{r['id']:2d}. {r['label']:40s} | A: rue #{str(a['rank_street']):>4s} rayon #{str(a['rank_radius']):>4s} top1 {str(a['top1_distance']):>7s} m | B: rue #{str(b['rank_street']) if b else '-':>4s} rayon #{str(b['rank_radius']) if b else '-':>4s} top1 {str(b['top1_distance']) if b else '-':>7s} m")


if __name__ == "__main__":
    run()
