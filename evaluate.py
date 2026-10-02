"""
Évaluation de Météo-Dressing — appelle le VRAI pipeline n8n (webhook) et mesure :

  A. Jeu étiqueté (30 scénarios météo annotés à la main, eval/scenarios.json)
     - accuracy "tenue correcte" : la tenue respecte TOUTES les contraintes annotées
     - accuracy de la bande thermique (feature engineering météo)
     - taux de réussite par contrainte, taux de sorties IA utilisables,
       taux d'hallucination (pièce inexistante), score moyen des 8 contrôles, latence
  B. Séquence de 14 jours consécutifs (même utilisateur) :
     - nb de combinaisons haut+bas répétées sur 14 j (objectif : 0)
     - diversité (pièces distinctes / pièces portées)
  C. Grille d'évaluation humaine (rubric 1-5) pré-remplie pour 20 tenues

Usage :
  pip install requests
  export N8N_OUTFIT_URL="https://<votre-n8n>/webhook/outfit"
  python eval/evaluate.py                    # A + B + C
  python eval/evaluate.py --skip-sequence    # A + C seulement (plus rapide)
  python eval/evaluate.py --model mistralai/mistral-small-24b-instruct   # comparer un autre modèle NVIDIA
  python eval/evaluate.py rubric eval/results/<dossier>/rubric_to_fill.csv   # calculer la note humaine
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
EVAL_USER = 99
RUBRIC_CRITERIA = ["adaptation_meteo", "coherence_style", "harmonie_couleurs", "clarte_conseil"]


# ----------------------------------------------------------------------------- appels
def call_pipeline(url, payload, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            r = requests.post(url, json=payload, timeout=180)
            elapsed = int((time.time() - t0) * 1000)
            data = r.json()
            data["_http_status"] = r.status_code
            data["_roundtrip_ms"] = elapsed
            return data
        except Exception as e:  # réseau, JSON invalide, timeout...
            last = e
            time.sleep(3 * (attempt + 1))
    return {"error": f"Appel impossible : {last}", "_http_status": 0}


# ----------------------------------------------------------------------------- contraintes
def check_constraints(result, labels):
    items = result.get("items") or []
    subs = {i.get("subcategory") for i in items}
    cats = {i.get("category") for i in items}
    outer = [i for i in items if i.get("category") == "outerwear"]
    shoes = [i for i in items if i.get("category") == "shoes"]
    res = {}
    for need in labels["must_have"]:
        if need == "outerwear":
            res[need] = "outerwear" in cats
        elif need == "mid_layer":
            res[need] = "mid_layer" in cats
        elif need == "rain_protection":
            res[need] = any(o.get("waterproof") for o in outer) or "parapluie" in subs
        elif need == "waterproof_shoes":
            res[need] = any(s.get("waterproof") for s in shoes)
    res["no_forbidden_piece"] = not (subs & set(labels["must_not_subcategories"]))
    core = [i for i in items if i.get("category") in ("base_top", "bottom", "shoes")]
    res["style_match"] = sum(1 for i in core if labels["style"] in (i.get("styles") or [])) >= 2
    res["complete_outfit"] = {"base_top", "bottom", "shoes"} <= cats
    return res


def pct(x, n):
    return round(100 * x / n, 1) if n else 0.0


# ----------------------------------------------------------------------------- A. jeu étiqueté
def run_labeled(url, model, outdir):
    scenarios = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    rows, raw = [], []
    for s in scenarios:
        payload = {"user_id": EVAL_USER, "source": "eval_dry", "format": "json", "style": s["style"],
                   "occasion": s["occasion"], "weather_override": s["weather_override"]}
        if model:
            payload["model"] = model
        r = call_pipeline(url, payload)
        raw.append({"scenario": s["id"], "response": r})
        if r.get("error") and not r.get("items"):
            print(f"  {s['id']} ❌ {r.get('error')}")
            rows.append({"id": s["id"], "description": s["description"], "error": r.get("error"), "correct": False})
            continue
        cons = check_constraints(r, s["labels"])
        checks = r.get("checks") or {}
        row = {
            "id": s["id"], "description": s["description"], "style": s["style"],
            "expected_band": s["labels"]["expected_band"], "predicted_band": (r.get("weather") or {}).get("band"),
            "correct": all(cons.values()), **{f"c_{k}": v for k, v in cons.items()},
            "generation_mode": r.get("generation_mode"), "llm_rule_score": r.get("rule_score"),
            **{f"llm_{k}": v for k, v in checks.items()},
            "repairs": "; ".join(r.get("repairs") or []), "latency_ms": r.get("latency_ms"),
            "title": r.get("title"), "items": " | ".join(i["name"] for i in r.get("items") or []),
            "advice": r.get("advice"),
        }
        rows.append(row)
        print(f"  {s['id']} {'✅' if row['correct'] else '⚠️ '} {row['predicted_band']:<9} {row['generation_mode']:<8} "
              f"score IA {row['llm_rule_score']}  {s['description']}")
    (outdir / "labeled_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    write_csv(outdir / "labeled_results.csv", rows)
    return rows


# ----------------------------------------------------------------------------- B. séquence
def run_sequence(url, model, outdir, days):
    # date de départ aléatoire dans le futur : chaque run d'éval a son propre historique
    start = date(2031, 1, 1) + timedelta(days=60 * random.randint(0, 400))
    weather_cycle = [
        {"tmin": 10, "tmax": 18, "morning": 11, "afternoon": 18, "evening": 14, "precip_prob": 10, "precip_mm": 0, "wind_kmh": 12, "uv": 4, "code": 2},
        {"tmin": 9, "tmax": 15, "morning": 9, "afternoon": 15, "evening": 12, "precip_prob": 80, "precip_mm": 4, "wind_kmh": 20, "uv": 2, "code": 63},
        {"tmin": 12, "tmax": 21, "morning": 13, "afternoon": 21, "evening": 16, "precip_prob": 0, "precip_mm": 0, "wind_kmh": 10, "uv": 5, "code": 1},
    ]
    rows = []
    for d in range(days):
        day = start + timedelta(days=d)
        w = dict(weather_cycle[d % len(weather_cycle)])
        w["feels_min"], w["feels_max"] = min(w["morning"], w["evening"]), w["afternoon"]
        payload = {"user_id": EVAL_USER, "source": "eval", "format": "json", "outfit_date": day.isoformat(),
                   "style": "casual", "occasion": "quotidien", "weather_override": w}
        if model:
            payload["model"] = model
        r = call_pipeline(url, payload)
        items = r.get("items") or []
        top = next((i["id"] for i in items if i["category"] == "base_top"), None)
        bottom = next((i["id"] for i in items if i["category"] == "bottom"), None)
        rows.append({"date": day.isoformat(), "combo": f"{top}-{bottom}", "item_ids": [i["id"] for i in items],
                     "mode": r.get("generation_mode"), "repairs": "; ".join(r.get("repairs") or []),
                     "items": " | ".join(i["name"] for i in items)})
        print(f"  J{d + 1:02d} {day} {rows[-1]['combo']:<9} {rows[-1]['items'][:90]}")
    repeats = 0
    for i, r in enumerate(rows):
        if any(p["combo"] == r["combo"] for p in rows[max(0, i - 14):i]):
            repeats += 1
    worn = [x for r in rows for x in r["item_ids"]]
    top_repeat_4d = 0
    for i, r in enumerate(rows):
        t = r["combo"].split("-")[0]
        if any(p["combo"].split("-")[0] == t for p in rows[max(0, i - 4):i]):
            top_repeat_4d += 1
    write_csv(outdir / "sequence_results.csv", [{**r, "item_ids": " ".join(map(str, r["item_ids"]))} for r in rows])
    return {"days": days, "combo_repeats_14d": repeats, "top_reused_within_4d": top_repeat_4d,
            "distinct_items": len(set(worn)), "items_worn": len(worn),
            "diversity_pct": pct(len(set(worn)), len(worn)),
            "repairs_triggered": sum(1 for r in rows if r["repairs"])}


# ----------------------------------------------------------------------------- C. rubric humaine
def write_rubric(outdir, labeled_rows, n=20):
    rows = []
    for r in [x for x in labeled_rows if x.get("items")][:n]:
        rows.append({"id": r["id"], "scenario": r["description"], "style": r["style"], "tenue": r["items"],
                     "conseil": r["advice"], **{c: "" for c in RUBRIC_CRITERIA}, "commentaire": ""})
    write_csv(outdir / "rubric_to_fill.csv", rows)


def score_rubric(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    print(f"\nGrille humaine : {path}  ({len(rows)} tenues)")
    all_scores = []
    for c in RUBRIC_CRITERIA:
        vals = [int(r[c]) for r in rows if r.get(c, "").strip().isdigit()]
        if vals:
            all_scores += vals
            print(f"  {c:<20} {statistics.mean(vals):.2f} / 5   (n={len(vals)})")
    if all_scores:
        print(f"  {'MOYENNE GLOBALE':<20} {statistics.mean(all_scores):.2f} / 5")
    else:
        print("  Aucune note saisie : remplissez les colonnes 1-5 puis relancez.")


# ----------------------------------------------------------------------------- rapport
def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def summarize(rows, seq, model):
    ok = [r for r in rows if "error" not in r]
    n = len(rows)
    lat = sorted(r["latency_ms"] for r in ok if r.get("latency_ms") is not None)
    cons_keys = sorted({k for r in ok for k in r if k.startswith("c_")})
    llm_keys = sorted({k for r in ok for k in r if k.startswith("llm_") and k != "llm_rule_score"})
    s = {
        "model": model or "défaut du workflow",
        "scenarios": n,
        "outfit_accuracy_pct": pct(sum(1 for r in rows if r.get("correct")), n),
        "band_accuracy_pct": pct(sum(1 for r in ok if r["expected_band"] == r["predicted_band"]), n),
        "llm_usable_pct": pct(sum(1 for r in ok if r["generation_mode"] == "llm"), n),
        "llm_avg_rule_score_pct": round(100 * statistics.mean([r["llm_rule_score"] or 0 for r in ok]), 1) if ok else 0,
        "hallucination_pct": pct(sum(1 for r in ok if r.get("llm_ids_valid") is False), n),
        "repairs_pct": pct(sum(1 for r in ok if r.get("repairs")), n),
        "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        "latency_p95_ms": lat[int(len(lat) * 0.95) - 1] if lat else None,
        "constraints_pass_pct": {k[2:]: pct(sum(1 for r in ok if r.get(k)), len([r for r in ok if k in r])) for k in cons_keys},
        "llm_checks_pass_pct": {k[4:]: pct(sum(1 for r in ok if r.get(k)), len(ok)) for k in llm_keys},
        "errors": [r["id"] for r in rows if "error" in r],
    }
    if seq:
        s["sequence"] = seq
    return s


def report_md(s):
    L = [f"# Rapport d'évaluation — Météo-Dressing", f"_Généré le {datetime.now():%Y-%m-%d %H:%M} — modèle : `{s['model']}`_", "",
         "## Résultats clés", "",
         "| Métrique | Valeur |", "|---|---|",
         f"| **Accuracy tenue (toutes contraintes annotées respectées, {s['scenarios']} scénarios)** | **{s['outfit_accuracy_pct']} %** |",
         f"| Accuracy bande thermique (features météo) | {s['band_accuracy_pct']} % |",
         f"| Sorties IA directement utilisables | {s['llm_usable_pct']} % |",
         f"| Score moyen des 8 contrôles automatiques (sortie brute du LLM) | {s['llm_avg_rule_score_pct']} % |",
         f"| Taux d'hallucination (pièce inexistante) | {s['hallucination_pct']} % |",
         f"| Tenues corrigées automatiquement | {s['repairs_pct']} % |",
         f"| Latence LLM p50 / p95 | {s['latency_p50_ms']} ms / {s['latency_p95_ms']} ms |", ""]
    if "sequence" in s:
        q = s["sequence"]
        L += ["## Anti-répétition (séquence de jours consécutifs)", "",
              "| Métrique | Valeur |", "|---|---|",
              f"| Jours simulés | {q['days']} |",
              f"| Combinaisons haut+bas répétées sur 14 j | **{q['combo_repeats_14d']}** |",
              f"| Hauts reportés à moins de 4 j | {q['top_reused_within_4d']} |",
              f"| Diversité (pièces distinctes / portées) | {q['distinct_items']} / {q['items_worn']} ({q['diversity_pct']} %) |",
              f"| Réparations anti-répétition déclenchées | {q['repairs_triggered']} |", ""]
    L += ["## Détail par contrainte annotée", "", "| Contrainte | Réussite |", "|---|---|"]
    L += [f"| {k} | {v} % |" for k, v in s["constraints_pass_pct"].items()]
    L += ["", "## Détail des contrôles sur la sortie brute du LLM", "", "| Contrôle | Réussite |", "|---|---|"]
    L += [f"| {k} | {v} % |" for k, v in s["llm_checks_pass_pct"].items()]
    L += ["", "## Évaluation humaine", "",
          "Remplir `rubric_to_fill.csv` (notes 1-5 sur 4 critères) puis lancer :",
          "`python eval/evaluate.py rubric <chemin>/rubric_to_fill.csv`"]
    return "\n".join(L)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "rubric":
        return score_rubric(sys.argv[2])
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("N8N_OUTFIT_URL"), help="URL du webhook POST /outfit")
    ap.add_argument("--model", default=None, help="modèle NVIDIA à tester (sinon celui du workflow)")
    ap.add_argument("--skip-sequence", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    if not a.url:
        sys.exit("Définissez N8N_OUTFIT_URL ou passez --url (ex : https://.../webhook/outfit)")
    outdir = HERE / "results" / datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"A. Jeu étiqueté (30 scénarios) -> {a.url}")
    rows = run_labeled(a.url, a.model, outdir)
    seq = None
    if not a.skip_sequence:
        print(f"\nB. Séquence de {a.days} jours consécutifs")
        seq = run_sequence(a.url, a.model, outdir, a.days)
    write_rubric(outdir, rows)
    s = summarize(rows, seq, a.model)
    (outdir / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "report.md").write_text(report_md(s), encoding="utf-8")
    print("\n" + report_md(s))
    print(f"\nFichiers : {outdir}")


if __name__ == "__main__":
    main()
