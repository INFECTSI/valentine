# Valentine — retrouver l'adresse d'une annonce immobilière

Ce dossier contient deux choses :

| Fichier | Rôle |
|---|---|
| `PLAN.md` | Le plan complet : données, algorithmes, architecture, sites d'annonces, droit, roadmap, budget. |
| `demo.html` | L'outil « adresse » : on saisit une annonce/des critères DPE, il interroge l'ADEME, score les candidats, affiche une carte IGN et Street View. |
| `search.html` | Le prototype « recherche d'annonces » multi-sources : recherche par critères **ou par lien d'annonce**, tri, filtres, carte optionnelle, puis résolution DPE et Street View. |
| `api_server.py` | Le backend local : agrège les 10 sources, analyse une URL d'annonce et résout les DPE. |
| `import.html` | Import assisté d’une page SeLoger ou Leboncoin (coller le HTML). |
| `se-loger-extension/` | Extension Chrome/Edge « Importer cette page » pour SeLoger et Leboncoin (équivalent WebView). |
| `DEPLOYMENT.md` | Mise en ligne : VPS, Caddy/Nginx, Docker, HTTPS, PWA et checklist mobile. |
| `IOS_APP.md` | Trois options iOS : PWA installable, app Capacitor pour l’App Store, app SwiftUI native. |
| `install-ios.html` | Page d’installation iPhone avec QR code et étapes Safari. |
| `manifest.webmanifest`, `icon-*.png`, `sw.js` | Installation du site comme application + cache de la coquille. |

## Lancer le prototype complet (recherche d'annonces)

1. Sers les fichiers statiques :

```bash
cd C:\Users\pierr\Desktop\Valentine
python -m http.server 8765
```

2. Dans un autre terminal, lance l'API :

```bash
python api_server.py
```

3. Ouvre <http://localhost:8765/search.html>.
4. Saisis une ville, un budget et une surface, puis **« Rechercher »**. Pour plusieurs villes, clique sur **« Rechercher sur la carte »** : le département suffit, et la recherche se lance automatiquement. Coche **« Afficher les villes »** pour afficher **toutes les communes** du département et en sélectionner plusieurs.
5. Clique sur **« Voir l'adresse »** si l'annonce expose une position exacte, sinon sur **« Trouver l'adresse »**.
6. Ou ouvre l'onglet **« Coller un lien d'annonce »**, colle une URL Bien'ici / Logic-Immo / Century21 / Orpi / Immonot / iad, puis **« Analyser l'annonce »**.
7. Pour dessiner une zone libre : **« Rechercher sur la carte »** → **« Dessiner une zone »**, clique les sommets, puis **« Terminer la zone »**.

Les annonces viennent de **dix sources** :

| Source | Accès |
|---|---|
| **Bien'ici** | API de recherche (dates, DPE, coordonnées) |
| **Logic-Immo** | HTML + JSON embarqué (DataDome peut bloquer par rafales) |
| **Century21** | HTML rendu serveur |
| **Orpi** | JSON-LD + HTML |
| **Immonot** | HTML rendu serveur (notaires) |
| **iad** | HTML rendu serveur (mandataires) |
| **Citya** | HTML rendu serveur (14 annonces à Haubourdin) |
| **ParuVendu** | HTML rendu serveur (25 annonces à Haubourdin) |
| **Nestenn** | HTML rendu serveur (réseau d'agences) |
| **Laforêt** | HTML rendu serveur (réseau d'agences) |

Le tri **« Pertinence »** mélange volontairement les sources (une carte de chaque source à tour de rôle) pour éviter l'impression d'un monopole Bien'ici. Les tris date/prix/surface restent disponibles.

En production, il faudra un agrégateur licencié (Stream.estate, Melo, Ubiflow, Netty…) ou des flux d'agences : l'API Bien'ici n'est pas un canal public officiel.

La carte utilise les contours des départements (france-geojson) et les régions (geo.api.gouv.fr). Elle est **optionnelle** : la recherche par ville ne nécessite aucune carte. L'API résout chaque ville/département en **zone ID Bien'ici** et interroge jusqu'à 400 annonces, puis fusionne les autres sources.

## Ouvrir la démo DPE seule

**Option 1 — double-clic** : ouvre `demo.html` dans ton navigateur.

**Option 2 — serveur local** (si ton navigateur bloque les appels depuis `file://`) :

```bash
cd C:\Users\pierr\Desktop\Valentine
python -m http.server 8765
```

Puis ouvre <http://localhost:8765/demo.html>.

## Ce que fait l'outil DPE (`demo.html`)

1. Tu saisis une ville / un code postal, un type de bien, une surface, une classe DPE/GES, éventuellement l'année, l'étage, le chauffage et le nom de résidence.
2. Tu peux coller le texte de l'annonce : la démo extrait automatiquement le **numéro DPE**, la surface, le DPE, le GES, l'étage, l'année, le chauffage, le nombre de pièces, le nom de résidence et la **rue/lieu-dit**.
3. Si un **numéro DPE à 13 caractères** est présent, la démo va directement chercher l'adresse dans la base ADEME.
4. Sinon, elle interroge l'API DPE avec les filtres (commune, type, surface ±10 %, DPE/GES), et utilise la **rue** comme filtre plein texte. Si la rue ne donne aucun DPE, elle élargit à la commune puis filtre dans un **rayon de secours** (500 m par défaut) autour du lieu.
5. Elle récupère jusqu'à 5 000 candidats, les score et affiche le top 40.
6. Chaque candidat a :
   - un score de correspondance et les raisons du score ;
   - l'adresse BAN, la surface, le DPE/GES, l'année, l'étage, le chauffage ;
   - un lien **Street View**, **Google Maps**, **fiche DPE** et **DVF**.
7. Une carte IGN affiche les candidats, avec un code couleur selon le score.

## Positions exactes et zones

- Si l'annonce expose une **position exacte** (`blurInfo.type = exact`), l'app la géocode à l'envers avec la BAN : on obtient l'adresse exacte (ex. `44 Rue Claude Thion` à Tours) sans matching DPE.
- Si elle expose une **zone** (`disk` avec un rayon, souvent 50-250 m), l'app filtre les DPE candidats dans ce rayon (+50 m) puis les classe. C'est ce qui fait remonter 3 candidats au lieu de 100+ pour la maison de Haubourdin.
- Sinon, recherche au niveau de la commune.

## Dates annonce ↔ DPE

- `publicationDate` (ou `modificationDate` si la publication est inconnue) et `energyPerformanceDiagnosticDate` sont affichées sur chaque annonce.
- L'app calcule l'écart : « DPE 85 j avant l'annonce ».
- Un badge « DPE proche de l'annonce » apparaît si l'écart est ≤ 180 j.
- En haut des résultats : « DPE proche de la date de l'annonce (±180 j) : 9/11 (82 %) ».
- Sur l'exemple Haubourdin : maison 62 m² publiée le 04/11/2026, DPE du 11/08/2026 (85 j avant). Le meilleur candidat DPE est au **16 Rue Léon Ridez**, avec exactement la même date de DPE.

## Carte optionnelle et toutes les villes

La carte n'est plus imposée : elle s'ouvre avec **« Rechercher sur la carte »** et ne rajoute aucun clic au parcours normal.

1. Clique sur un ou plusieurs départements : la recherche se lance automatiquement (debounce de 850 ms).
2. Coche **« Afficher les villes »** : **toutes les communes** du département sont chargées, sans filtre de population.
3. Clique sur une ville pour l'ajouter ou la retirer.
4. Le champ « Ajouter une ville » reste disponible pour les petites communes.
5. **« Dessiner une zone »** : clique les sommets sur la carte, puis **« Terminer la zone »**. Valentine sélectionne **toutes les communes dont le centre est dans la zone**, affiche leurs points et lance la recherche dans ces villes avec toutes les sources. Aucune source n'est masquée.
6. Si des villes sont sélectionnées, elles priment sur les départements dans la recherche.
7. En multi-villes, Bien'ici est interrogé **ville par ville** pour éviter qu'une grande ville écrase les petites. Les sources HTML sont limitées à la ville saisie en priorité (ou aux deux premières) pour rester rapides.
8. Les résultats affichent un compteur **par ville** : `Haubourdin 74`, `Lille 188`, etc. Le nombre de sources contributrices est aussi distingué du nombre de sources interrogées.

## Tri, filtres et affinage

La recherche principale accepte : prix min/max, surface min/max, pièces min/max, DPE max, GES max.

Une fois les résultats affichés :

- **tri visible** : plus récentes, prix croissant, prix décroissant, surface ;
- **filtres rapides** : DPE récent (±180 j), adresse exacte, bon DPE (A–C), sans doublons, ≤ 200 000 €, 3 pièces + ;
- **tous les filtres** : prix, surface, pièces, DPE/GES, position exacte, doublons ;
- **filtres par source** : Toutes, Bien'ici, Logic-Immo, Century21, Orpi, Immonot, iad ;
- **« Afficher plus de biens »** : rendu par tranches de 24 cartes pour rester fluide.

## Analyser une annonce par URL

L'onglet **« Coller un lien d'annonce »** accepte :

- Bien'ici (`/annonce/{id}`) : l'API `realEstateAds-one.json` donne le bien complet, y compris la zone exacte et le DPE ;
- Logic-Immo, Century21, Orpi, Immonot, iad, Citya, ParuVendu, Nestenn, Laforêt : extraction HTML / JSON-LD (prix, surface, pièces, ville, DPE/GES, description, photo, coordonnées si disponibles).

Ensuite, l'outil lance automatiquement la résolution DPE et affiche les adresses probables avec Street View, Satellite et fiche DPE. Un bouton **« Chercher dans cette ville »** relance la recherche multi-sources autour du bien analysé.

Les portails protégés (SeLoger, Leboncoin, PAP, Figaro, Ouest-France) renvoient un message clair : l'analyse automatique est bloquée par CAPTCHA, il faut copier le texte dans l'outil DPE ou passer par un partenariat. SeLoger et Leboncoin peuvent aussi être importés en mode assisté (extension navigateur / WebView), sans contournement serveur.

## SeLoger et Leboncoin assistés (extension navigateur / WebView)

SeLoger et Leboncoin sont protégés par DataDome : le serveur ne peut pas lire leurs pages. La solution
est la même logique qu'une **WebView visible** : c'est le navigateur de l'utilisateur qui lit la page,
après le contrôle, puis l'envoie à l'API locale.

### Où le voir dans le site

- Bouton **« Importer SeLoger / Leboncoin »** en haut à droite, avec un badge du nombre d’annonces importées.
- Onglets pointillés **« ＋ SeLoger · importer »** et **« ＋ Leboncoin · importer »** sous les résultats.
- Ligne dans **Sources & données** avec un bouton **Importer**.
- Après l’import, relance la recherche : les annonces apparaissent avec les autres sources.

### Extension Chrome / Edge

1. Ouvre `chrome://extensions` → **Mode développeur**.
2. **Charger l'extension non empaquetée** → dossier `se-loger-extension`.
3. Ouvre SeLoger ou Leboncoin, passe le contrôle si besoin.
4. Clique sur **« Importer cette page (SeLoger) »** ou **« … (Leboncoin) »** en bas à droite.
5. Reviens sur Valentine et relance la recherche. La source correspondante apparaît.

### Sans extension

Ouvre `import.html`, choisis la source, colle le HTML (`F12` → Elements → clic droit sur
`<html>` → Copy outerHTML), puis clique sur **« Importer dans Valentine »**.

Aucun contournement de CAPTCHA côté serveur : l'import se fait dans la session de l'utilisateur.
Pour l'app mobile, le même principe s'implémente avec une WebView visible et un pont JavaScript.

## Street View avec adresse visible

Le bouton Street View ouvre une **fenêtre intégrée** avec l'adresse affichée en permanence dans l'en-tête **et en overlay directement sur le panorama**.

- Sans clé Google Maps, l'intégration peut être vide : le bouton **« Ouvrir dans Google Maps »** reprend l'adresse dans la recherche.
- Pour la visite intégrée, colle une **clé Google Maps Embed API** dans le champ prévu, puis « Enregistrer la clé » (stockée dans le navigateur). Elle doit être restreinte à `localhost:8765` pour la démo.
- Créer la clé : Google Cloud Console → activer « Maps Embed API » → créer une clé → restreindre les référents à `http://localhost:8765/*` → coller dans la fenêtre Street View.
- L'adresse reste aussi copiable via « Copier l'adresse ».

## Assistant IA

Le bouton **« ✦ Assistant IA »** en bas à droite ouvre un panneau qui peut piloter le site :

- lancer une recherche (« cherche une maison à Lille sous 250 000 € avec 3 chambres ») ;
- sélectionner toutes les villes dans un rayon (« 10 km autour de Haubourdin ») ;
- filtrer par source, trier, ouvrir un DPE, analyser un lien, ouvrir l’import SeLoger/Leboncoin ;
- répondre sur les résultats affichés (DPE, prix au m², comparaison).

Deux modes :

1. **Mode local, sans clé** : compréhension des commandes courantes, aucune donnée ne sort du navigateur.
2. **Avec une clé API** : bouton ⚙ → OpenAI, OpenRouter, Mistral, Groq, Ollama local ou URL compatible OpenAI. La clé reste dans `localStorage` et part directement vers le fournisseur choisi, jamais vers l’API Valentine.

Le contexte envoyé au modèle contient uniquement la recherche en cours, les compteurs de sources et les 10 premiers biens (prix, surface, ville, DPE, source). Pas de clé, pas d’appel réseau.

## Doublons

- Le serveur fusionne les doublons **exacts** : même photo principale, même description + mêmes métadonnées, ou **même référence iad** entre Bien'ici et iad.
- Le frontend complète avec un rapprochement **inter-sources plus large** : même ville, même type, prix et surface quasi identiques, pièces/chambres cohérentes, description proche. Les badges de toutes les sources sont conservés.
- Le filtre « Sans doublons » est activé par défaut ; un badge indique les doublons fusionnés.
- Test sur 400 annonces à Tours : 400 IDs uniques, **0 doublon** par photo/description ; 4 grappes ressemblantes étaient des **lots différents** (Citya/Netty) et ne doivent pas être fusionnées.
- Sur le Nord : 26 annonces iad récupérées, dont 25 déjà présentes dans Bien'ici et fusionnées par référence ; 1 seule reste spécifique à iad.
- Les maisons ont un rapprochement un peu plus large que les appartements, pour éviter de rater deux annonces du même bien publiées par deux agences.

## Sources et solutions pour les portails bloqués

| Source | État | Remarque |
|---|---|---|
| **Bien'ici** | actif | API de recherche, dates + DPE + coordonnées |
| **Logic-Immo** | actif mais protégé | HTML + JSON embarqué ; DataDome peut bloquer par rafales |
| **Century21** | actif | HTML rendu serveur |
| **Orpi** | actif | JSON-LD + HTML |
| **Immonot** | actif | notaires |
| **iad** | actif | mandataires |
| **Citya** | actif | réseau d'agences |
| **ParuVendu** | actif | petites annonces |
| **Nestenn** | actif | réseau d'agences |
| **Laforêt** | actif | réseau d'agences |
| **PAP** | bloqué | Cloudflare Turnstile |
| **SeLoger** | assisté | import via extension navigateur / WebView (DataDome bloqué côté serveur) |
| **Leboncoin** | assisté | import via extension navigateur / WebView (DataDome bloqué côté serveur) ; API pro pour une app publique |
| **Figaro Immo** | bloqué | Cloudflare |
| **Ouest-France Immo** | bloqué | Akamai |

Solutions propres pour aller plus loin :

1. **Agrégateur licencié** : Stream.estate, Melo, Ubiflow ou Netty fournissent un flux multi-portails sous contrat.
2. **Flux XML d'agences et de réseaux** : Century21, Orpi, Foncia, Citya, Guy Hoquet, Laforêt, Nestenn…
3. **API professionnelles** : SeLoger Pro (groupe Aviv), Leboncoin Pro, PAP Pro.
4. **Données ouvertes** : DPE ADEME, BDNB, DVF, BAN, cadastre, INSEE, RNIC.
5. **Extension navigateur** : analyser une page depuis la session de l'utilisateur, sans contourner le CAPTCHA côté serveur.

Le scraping des portails protégés n'est pas une option viable : il faut un partenariat ou un flux.

## Mise en ligne et mobile

Le guide complet est dans **`DEPLOYMENT.md`** : VPS, service systemd, Caddy ou Nginx, Docker, HTTPS, PWA et checklist mobile.

Points clés :

- **Gratuit** : Render Free (sans carte, veille après 15 min). Voir `DEPLOYMENT.md`.
- En production, servir le site et l’API sur la **même origine** ; le frontend appelle alors `/api` automatiquement.
- En local, l’API reste sur `http://localhost:8766`.
- Pour changer d’API sans modifier le code : `localStorage.setItem('valentine.apiBase', 'https://api.example.com')`.
- Le site s’installe comme application (PWA) sur Android et iPhone.
- L’extension navigateur vise l’API locale `127.0.0.1:8766` ; pour une version publique, passer par l’import HTML ou une WebView de l’app mobile.

Tests responsive à faire : **390 px** et **500 px**. L’assistant passe en plein écran sous 560 px, la carte reste utilisable au doigt, et les modales DPE / import / sources se réempilent verticalement.

## Tests effectués

- **Carcassonne**, appartement 45 m², DPE C : 929 candidats ; en ajoutant GES B, étage 2, année 1975 et « Résidence Les Jardins », 167 candidats et un top 1 à 85/100 sur « Résidence les Jardins d'Aude ».
- **Paris 11e**, appartement 45 m², DPE C : 1 389 candidats, pagination API OK, carte OK.
- **Recherche directe** par numéro DPE `2611E0031228S` : adresse `25 Rue de Belfort 11000 Carcassonne`, score 100.
- **Haubourdin**, appartement ≤ 300 000 €, 30 m²+ : **75 biens uniques** (Bien'ici 32, ParuVendu 25, Citya 14, iad 4, Orpi 1, Immonot 1, Laforêt 1).
- **Nord**, appartement ≤ 300 000 €, 30 m²+ : **466 biens uniques** (Bien'ici 393, iad 26, ParuVendu 22, Citya 19, Century21 15, Orpi 14, Immonot 3 ; iad 25 doublons fusionnés).
- **Zone dessinée** autour de Haubourdin : **75 biens** (1 ville sélectionnée) avec toutes les sources.
- **Zone plus large** Haubourdin/Loos/Sequedin : **161 biens, 15 villes** (Haubourdin 74, Loos 58, Wattignies 21…), avec Citya/ParuVendu/Orpi/iad/Immonot/Laforêt en plus de Bien'ici.
- **Haubourdin**, maison 62 m², ≤ 215 000 € : le DPE de l'annonce est 85 j avant la publication, et le meilleur candidat est au **16 Rue Léon Ridez**, avec la même date de DPE.
- **Analyse par URL Bien'ici** (`iad-france-2102380`) : 62 m², DPE D, 178 000 € → **16 Rue Léon Ridez**, score 72, même DPE du 11/08/2026.
- **Analyse par URL Orpi** : appartement 113 m², 239 900 € → 13 candidats, premier à 64 Rue Sadi Carnot.
- **Analyse par URL C21** : maison 228,87 m², DPE G, 260 600 € → 33 candidats après élargissement de la surface.
- **DPE détecté au clic** sur les sources qui ne l'affichent pas dans la carte :
  - Citya → DPE E · GES D → 2 adresses (68 Rue du General Dame).
  - ParuVendu → DPE E · GES D · diagnostic du 19/10/2021 → 2 adresses.
  - C21 → DPE G · GES F · diagnostic du 17/09/2024.
  - Orpi → DPE D · GES B → 5 adresses.
  - Immonot → DPE D · GES C · diagnostic du 06/11/2026 → 3 adresses.
  - iad → DPE D · GES D.
  - Laforêt → « DPE non soumis » (aucun diagnostic requis).
- **Lille**, appartement 30-80 m², ≤ 300 000 € : **400 annonces** (plafond de pagination du prototype).
- **Tours**, annonce avec position exacte : coordonnées `47.382661, 0.684881` → **44 Rue Claude Thion 37000 Tours** via géocodage inverse BAN.
- **Statistique DPE** : sur les 12 maisons de Haubourdin, **9/11 annonces datées** ont un DPE à ±180 j de la date de publication (82 %).

## Tester sur 10 annonces

Un test réel a déjà été fait sur **10 annonces Immonot à Tours** : voir `TEST_RESULTS.md`.

Résumé :

- **sans la rue** : top 1 3/10, top 3 4/10 ;
- **avec la rue/le lieu extrait de l'annonce** : top 1 7/7, top 3 7/7.

Conclusion : **la rue est le signal le plus rentable**. La démo a maintenant un champ « Rue / lieu-dit » et l'utilise comme filtre plein texte.

Pour refaire le test : `TEST_PROTOCOL.md` + `test-annonces.csv`.

## Street View dans l'outil DPE manuel

Le lien Street View recale le point DPE sur la route la plus proche (OSRM) et utilise l'URL `cbll`, plus fiable que `map_action=pano`. Si le panorama est noir, c'est que la zone n'a pas d'image : utilise **Satellite** ou **Google Maps**.

## Limites

- Le DPE donne l'adresse de **l'immeuble** ou de la parcelle, pas toujours celle de l'appartement exact.
- La base DPE ne couvre pas les biens sans DPE (neuf, etc.).
- Les données DPE sont brutes : surface, année ou étage peuvent être erronés.
- L'analyse d'URL fonctionne sur les sources accessibles. Les portails protégés (SeLoger, Leboncoin, PAP, Figaro, Ouest-France) renvoient un message explicite, sans contournement. SeLoger peut être importé en mode assisté via l'extension navigateur ou `import.html`.
- Logic-Immo peut être protégé par DataDome : la source est alors ignorée pendant 10 minutes et les autres sources continuent.
- Le scraping massif des portails n'est pas inclus (et n'est pas recommandé).
- L'API Bien'ici utilisée ici est une **API interne de démonstration** : sa réutilisation en production doit être encadrée (CGU, partenariat, licence).
- La visite Street View intégrée nécessite une **clé Google Maps Embed API** ; sans clé, l'iframe peut être vide, mais l'adresse reste affichée et le bouton « Ouvrir dans Google Maps » fonctionne.
- La carte des villes charge **toutes les communes** du département quand « Afficher les villes » est coché.

## Prochaines étapes

1. Tester la démo sur 5-10 annonces réelles et noter le taux de succès par source.
2. Ajouter l'OCR de la vignette DPE et le parsing des iframes de carte pour les annonces sans DPE texte.
3. Construire le backend FastAPI + PostGIS avec le dump DPE, la BDNB, la DVF et le RNIC.
4. Créer l'app mobile Flutter avec le partage depuis les applis d'annonces (le lien arrive directement dans l'analyseur).
5. Ajouter le feedback utilisateur et les alertes nouvelles annonces / baisses de prix.
6. Brancher un agrégateur licencié ou des flux d'agences pour les portails protégés.

Voir `PLAN.md` pour le détail.
