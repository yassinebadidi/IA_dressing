# 👗 Météo-Dressing — pipeline IA automatisé (n8n)

Un pipeline qui consulte la météo, lit ta garde-robe en base de données et demande à un LLM (NVIDIA NIM) de composer une tenue personnalisée, **sans répétition**. Chaque tenue est enregistrée en base pour adapter les suggestions des jours suivants.

![Architecture](docs/architecture.png)

## Les 5 exigences du brief

| Exigence | Implémentation |
|---|---|
| Source de données externe | **Open-Meteo** (géocodage + prévisions horaires, gratuit, sans clé) + dataset garde-robe (78 pièces) |
| Étape LLM | **OPENAI API** prompt système + prompt utilisateur, sortie JSON |
| Automatisation n8n | Schedule 7h + 4 webhooks, 34 nœuds, retry, Error Trigger |
| Stockage BDD | **PostgreSQL (Supabase)** : `app_users`, `wardrobe_items`, `outfit_history`, `pipeline_errors` |
| Évaluation chiffrée | 8 contrôles auto par run, accuracy sur 30 scénarios annotés, répétitions sur 14 jours, grille humaine 1-5, notes utilisateurs |

Extras : mini-appli web servie par n8n, gestion d'erreurs + repli déterministe, boucle de feedback.

## Contenu

```
data/      wardrobe_dataset.csv (78 pièces) · build_seed.py
sql/       01_schema.sql (tables, fonctions, vues d'éval) · 02_seed.sql
n8n/       meteo_dressing_workflow.json · error_handler_workflow.json
           code/*.js (les 10 nœuds Code, lisibles) · build_workflow.py
eval/      scenarios.json (30 scénarios annotés) · evaluate.py
tests/     simulate.js · sequence_test.js · fake_n8n_server.js (tests hors-ligne)
docs/      architecture.svg / .png
```

## Installation (≈ 20 min)

### 1. Base de données (Supabase)
Supabase → **SQL Editor** → exécuter `sql/01_schema.sql` puis `sql/02_seed.sql`.
Utilisateurs créés : **1 Alex** (Paris, casual), **2 Sam** (Lyon, chic, frileux), **99 Eval Bot** (réservé à l'évaluation).

Pour modifier la garde-robe : éditer le CSV puis `python data/build_seed.py`, ou éditer directement la table `wardrobe_items` dans Supabase.

### 2. Clé NVIDIA
build.nvidia.com → se connecter → **Get API Key** (commence par `nvapi-`).

### 3. Credentials n8n (ne jamais mettre les secrets dans le workflow)
- **Postgres** nommé `Supabase Postgres` : Supabase → *Connect* → paramètres du **Session pooler** (host, port 5432, database `postgres`, user `postgres.xxxx`, mot de passe), SSL activé.
- **Header Auth** nommé `NVIDIA NIM API` : Name = `Authorization`, Value = `Bearer nvapi-...`

### 4. Import
1. Importer `n8n/error_handler_workflow.json`, ouvrir le nœud Postgres, choisir le credential, enregistrer.
2. Importer `n8n/meteo_dressing_workflow.json`, sélectionner les credentials dans les nœuds Postgres (7) et **LLM NVIDIA NIM**.
3. Settings du workflow principal → **Error workflow** = « Météo-Dressing — gestion des erreurs ».
4. Activer le workflow (sinon utiliser les URLs `webhook-test` après *Listen for test event*).

> Sur l'instance partagée de la formation, si un autre étudiant utilise déjà les chemins `outfit` / `dressing`, préfixez-les (ex : `ahmed-outfit`) dans les 4 nœuds Webhook **et** dans `08_page_appli.js` / `06_construire_reponse.js`.

### 5. Tester
```bash
# page de l'appli dans le navigateur
https://<votre-n8n>/webhook/dressing

# appel direct (JSON)
curl -X POST "https://<votre-n8n>/webhook/outfit" -H "Content-Type: application/json" \
  -d '{"user_id":1,"style":"chic","occasion":"travail","format":"json"}'

# simulation (non enregistrée dans l'historique)
curl -X POST "https://<votre-n8n>/webhook/outfit" -H "Content-Type: application/json" \
  -d '{"user_id":2,"simulate":"neige"}' --output tenue.html
```

Paramètres du webhook `POST /outfit` : `user_id`, `style` (casual, chic, business, streetwear, sport, boheme), `occasion` (quotidien, travail, sortie, rendez-vous, week-end, sport, voyage), `city`, `day_offset` (0 ou 1 = demain), `simulate` (pluie_froide, canicule, neige, vent_fort, printemps), `format` (html / json), `model` (autre modèle NVIDIA).

## Comment ça marche

1. **Normaliser** : valide style / occasion / date (fuseau Europe/Paris) ; valeurs inconnues → valeurs du profil + avertissement.
2. **Contexte BDD** (`get_outfit_context`) : profil, pièces disponibles avec *jours depuis le dernier port*, *nb de ports sur 14 j*, *note moyenne*, tenues récentes, pièces déjà proposées aujourd'hui.
3. **Open-Meteo** : géocodage de la ville (repli sur le profil si introuvable) + prévisions.
4. **Features météo** : ressenti pondéré matin/après-midi/soir, ajusté à la frilosité → bande thermique (très froid … très chaud), pluie, neige, vent, UV, écart de température → contraintes (plage de chaleur cible, couche/manteau obligatoires, protection pluie).
5. **Pré-filtrage** : exclusions dures (météo, *cooldown* : haut 4 j, bas 2 j) + score (style, formalité de l'occasion, rotation, notes utilisateur) → top 6 par catégorie. Le LLM ne peut choisir que ces pièces (pas d'invention).
6. **LLM NVIDIA** : prompt système (règles + format JSON) + prompt utilisateur (météo, profil, historique, combinaisons interdites, candidats).
7. **Validation** : parsing robuste, 8 contrôles, réparations tracées (parapluie oublié, combinaison déjà portée), **repli déterministe** si la sortie est inutilisable ou l'API en panne.
8. **Enregistrement** (`save_outfit`) : une régénération le même jour remplace la tenue officielle ; l'ancienne reste en base pour ne pas être reproposée.
9. **Feedback** : les étoiles (1-5) alimentent `avg_rating` → le score des pièces aimées monte, celui des pièces mal notées baisse.

## Évaluation (Jour 3)

```bash
pip install requests
export N8N_OUTFIT_URL="https://<votre-n8n>/webhook/outfit"
python eval/evaluate.py                       # 30 scénarios + séquence 14 jours (~5 min)
python eval/evaluate.py --model <autre/modele>  # comparer deux modèles NVIDIA
python eval/evaluate.py rubric eval/results/<dossier>/rubric_to_fill.csv
```

| Métrique | Définition |
|---|---|
| **Accuracy tenue** | % des 30 scénarios annotés où la tenue respecte *toutes* les contraintes (manteau, couche, pluie, chaussures imperméables, pièces interdites, style) |
| Accuracy bande thermique | la météo est-elle bien classée (froid, doux…) |
| Sorties IA utilisables | % de réponses LLM exploitables sans repli |
| Score des 8 contrôles | qualité de la sortie *brute* du LLM (avant réparation) |
| Hallucinations | % de réponses citant une pièce inexistante |
| Répétitions 14 j | nb de combinaisons haut+bas répétées (objectif 0) |
| Grille humaine | 4 critères notés 1-5 sur 20 tenues |

En direct dans Supabase :
```sql
select * from v_eval_summary;   -- synthèse
select * from v_eval_checks;    -- réussite par contrôle
select * from v_repetitions;    -- doit être vide
select * from pipeline_errors order by id desc;
```

Les scénarios dans `eval/scenarios.json` ont été annotés à l'avance : **relisez-les** et ajustez les étiquettes si vous n'êtes pas d'accord — c'est votre jeu de référence.


## Tests hors-ligne

Sans n8n ni clés, avec un PostgreSQL local :
```bash
createdb dressing && psql -d dressing -f sql/01_schema.sql -f sql/02_seed.sql
node tests/simulate.js         # scénarios : LLM valide, hallucination, API HS, ville/style inconnus, simulation
node tests/sequence_test.js    # 14 jours → 0 répétition
```
(Le script utilise `su postgres` ; adaptez la fonction `sql()` à votre machine.)

Après modification d'un fichier `n8n/code/*.js` : `python n8n/build_workflow.py` puis ré-importer.


## Limites et pistes d'amélioration

- Dataset de garde-robe unisexe et fictif → ajouter photos et import depuis un formulaire.
- Les scénarios d'évaluation utilisent des météos simulées (reproductibles) ; compléter avec des journées réelles notées.
- Le ressenti pondéré et les seuils de bandes sont des heuristiques à calibrer avec les notes utilisateurs.
- Envoi du conseil par e-mail / Telegram chaque matin (le run planifié s'arrête aujourd'hui à la BDD).
