# Calendrier multi-agendas → page style Notion Calendar + base Notion

Ce dépôt fait deux choses toutes les 20 minutes (GitHub Actions) :
1. lit tes agendas Google (adresse secrète iCal) et publie `docs/events.json`
2. (optionnel) crée / met à jour / archive les pages de ta base Notion « Calendrier »,
   avec une page par matière dans une base « Matières »

La page `docs/index.html` affiche le tout (GitHub Pages) et s'intègre dans Notion avec `/embed`.

## Installation

### 1. Dépôt GitHub
- Crée un dépôt (public pour GitHub Pages gratuit, ou privé si ton plan le permet) et envoie-y tout ce dossier.
- **Settings → Pages** : Source = *Deploy from a branch*, branche `main`, dossier `/docs`.
- Ta page sera sur `https://TON-PSEUDO.github.io/NOM-DU-DEPOT/`.
  ⚠ Dépôt public = `events.json` lisible par n'importe qui qui connaît l'URL (titres, lieux, descriptions).

### 2. Adresses iCal de tes agendas
Google Agenda (web) → ⚙ Paramètres → clique l'agenda → **Intégrer l'agenda** →
**Adresse secrète au format iCal**. Ne la partage pas.

Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**
- `CAL_URL_1` = adresse de l'agenda « Cours »
- `CAL_URL_2` = « Fériés » (calendrier des jours fériés), `CAL_URL_3` = « Perso » (ajoute-en d'autres dans `config.json` + `sync.yml`).
  Sans agenda Google des fériés, mets `"builtin": "holidays_fr"` à la place de `url_env` : le script calcule les fériés français (+ `"reunion": true` pour le 20 décembre)

Noms, couleurs et agendas à envoyer vers Notion (`sync_to_notion`) : `config.json`.

### 3. Notion (facultatif mais c'est ce que tu voulais)
1. https://www.notion.so/profile/integrations → **Nouvelle intégration** (interne) → copie le token → secret `NOTION_TOKEN`.
2. Crée une base **Matières** (colonne titre `Nom`).
3. Dans ta base **Calendrier**, ajoute ces propriétés :
   | Nom | Type |
   |---|---|
   | `ID Google` | Texte |
   | `Matière` | Relation → base Matières |
   | `Agenda` | Sélection |
   | `Type` | Sélection |
   | `Lieu` | Texte |
4. Sur chacune des deux bases : **⋯ → Connexions → ajoute ton intégration**.
5. Copie l'ID de chaque base (la partie de 32 caractères dans l'URL, avant `?v=`) dans
   `config.json` → `notion.calendar_db_id` et `notion.subjects_db_id`.

### 4. Premier lancement
Onglet **Actions → Sync agendas → Run workflow**. Ensuite c'est automatique.

### 5. Dans Notion
Tape `/embed` dans ta page « la vie » et colle l'URL GitHub Pages.
Ajoute `?theme=light` à l'URL pour le mode clair.

## Comment la matière est déduite
`4A INFO-TP1-RESEAUX INFORMATIQUES` → matière **Réseaux informatiques**, type **TP1**.
Les règles sont dans `config.json` → `subject_rules` (préfixes à retirer, coupure après `:`, alias avec accents).
Pour tester en local : `python3 -c "import sync; print(sync.parse_subject('4A-TD-ANGLAIS'))"`.

## Limites à connaître
- Le flux iCal de Google peut avoir quelques heures de retard : c'est Google, pas ce script.
- Un cron GitHub peut être retardé de plusieurs minutes.
- Si l'agenda « Cours » ne renvoie rien (erreur réseau), le script ne touche à rien plutôt que de tout archiver.
- Les événements supprimés dans Google sont **archivés** dans Notion (récupérables via la corbeille).

## Raccourcis de la page
`t` aujourd'hui · `←/→` précédent/suivant · `d` jour · `w` semaine · `m` mois · clic sur un agenda à gauche pour le masquer.
