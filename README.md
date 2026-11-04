# Veille Appels d'Offres — Assemblage ingénierie

Application web de veille automatisée sur les appels d'offres (AO) publiés par les bailleurs de fonds internationaux, développée pour le bureau d'études **Assemblage ingénierie** (faisabilité, efficacité énergétique, AMO).

---

## Lancement

```bash
python main.py
```

Le serveur démarre sur **http://localhost:8000** et s'ouvre automatiquement dans le navigateur. Les dépendances Python manquantes sont installées automatiquement au premier lancement.

**Variables d'environnement (optionnel)** : copier `.env.example` en `.env` et renseigner les identifiants GIZ/DTVP pour activer la connexion automatique à l'espace documentaire GIZ.

---

## Architecture

```
ao_veille/
├── main.py              # Point d'entrée : deps, init DB, scheduler, uvicorn
├── api.py               # FastAPI — routes HTTP + serveur de fichiers statiques
├── scheduler.py         # Collecte périodique (quotidienne à 07:00) + état partagé
├── database.py          # SQLite — init, insertion, requêtes, expiration
├── filters.py           # Scoring de pertinence (aucun filtrage — tout est conservé)
├── config.py            # Paramètres : mots-clés, secteurs, chemin DB, heure scheduler
├── .env.example         # Modèle de variables d'environnement (GIZ credentials)
├── sources/
│   ├── bm.py            # Banque Mondiale — API JSON v2 procnotices
│   ├── undp.py          # PNUD — flux RSS 1.0 (RDF/XML)
│   ├── giz.py           # GIZ — scraping HTML avec pagination
│   ├── ebrd.py          # BERD/ECEPP — scraping HTML table server-rendered
│   ├── afd.py           # AFD — scraping dgMarket (biddingDocumentsList + PDF notice)
│   ├── detail.py        # Enrichissement : télécharge et parse les TOR/notices par bailleur
│   └── dns_utils.py     # Fallback DNS-over-HTTPS (réseaux d'entreprise)
├── static/
│   ├── index.html                # Interface veille (SPA vanilla JS)
│   ├── analyse.html              # Interface Analyse AO (triage + détail enrichi)
│   ├── pertinentes.html          # Interface Offres pertinentes (assignation équipe)
│   ├── params.html               # Interface Paramètres (équipe, SMTP)
│   ├── logo_Ai_rouge_HD.png      # Logo Assemblage ingénierie (charte)
│   └── sigle_A_rouge_HD.png      # Sigle .A (watermark charte)
└── ao_database.db       # Base SQLite (créée au premier lancement)
```

---

## Sources de données

| Bailleur | Source | Méthode | Fréquence |
|---|---|---|---|
| **Banque Mondiale** | `search.worldbank.org/api/v2/procnotices` | API JSON | Quotidienne |
| **PNUD** | `procurement-notices.undp.org/rss_feeds/rss.xml` | RSS 1.0 / RDF | Quotidienne |
| **GIZ** | `ausschreibungen.giz.de` | Scraping HTML (pagination complète) | Quotidienne |
| **BERD** | `ecepp.ebrd.com/delta/noticeSearchResults.html` | Scraping HTML table `#noticeResultsTable` | Quotidienne |
| **AFD** | `dgmarket.com/tenders/...` | Scraping HTML + téléchargement PDF notice | Quotidienne |

### Détails techniques par source

**Banque Mondiale** — Requête sans filtre de type (`rows=500`, trié par deadline croissante, deadline ≥ aujourd'hui). Filtre langue : Anglais, Français, Espagnol, Portugais uniquement.

**PNUD** — RSS 1.0 en namespace RDF (`http://purl.org/rss/1.0/`). Champs extraits via Clark notation `{ns}tag`. Types exclus : RFI, IC, RFQ (voir `config.UNDP_EXCLUDE_TYPES`).

**GIZ** — Table HTML avec en-têtes allemands (`Bezeichnung`, `Frist`, `Veröffentlicht`). Pagination via paramètre `selectedTablePagePROJECT_RESULT=N`. Délai de politesse 0,8 s entre pages.

**BERD** — Table `#noticeResultsTable` avec 10 colonnes dont 4 cachées DataTables. Les colonnes 7 et 8 contiennent des horodatages compacts (`YYYYMMDDhhmm`) utilisés pour les dates. Le suffixe de fuseau horaire "Heure britannique" / "UK Time" est nettoyé avant parsing. Types exclus : Contract Award Notice, Addendum, Corrigendum.

**AFD** — Scraping de dgMarket via `_HARDCODED_DNS` (IP AWS codée en dur dans `dns_utils.py` pour contourner les DNS d'entreprise). Téléchargement de la notice PDF via POST ou GET selon l'URL. Si la réponse est HTML (page de login), le document est ajouté comme lien direct plutôt que parsé.

> **Vérification SSL désactivée pour l'AFD** — `afd.dgmarket.com` présente une chaîne de certificat dont le maillon intermédiaire est absent : `certifi` (le bundle CA de `requests`) refuse de la valider (`CERTIFICATE_VERIFY_FAILED`), alors que Chrome l'accepte via le magasin Windows. `_make_session()` (dans `afd.py`) pose donc `session.verify = False` — l'hôte reste correct (SNI + connexion TCP), seule la vérification de la chaîne est levée. À retirer si l'AFD corrige son certificat. Le champ `type_avis` des AO AFD n'est pas renseigné par la source : l'AFD n'apparaît donc pas dans le tableau de filtres « Type AO ».

---

## Enrichissement des AO (`sources/detail.py`)

Le module `detail.py` est appelé à la demande depuis l'interface **Analyse AO**. Pour chaque AO, il va chercher les données détaillées directement sur le site du bailleur et extrait :

| Bailleur | Ce qui est récupéré |
|---|---|
| **Banque Mondiale** | Description de la mission + critères de sélection (page `/procurement/notices/{id}`) |
| **PNUD** | Description complète (API `procurement-notices.undp.org/view?id=`) |
| **GIZ** | PDF TOR depuis l'onglet "Tender Documents" → section "Specification for Tenders" / "Leistungsbeschreibungen" |
| **BERD** | Description depuis la section `.completeentry` de la page d'avis |
| **AFD** | Texte de la notice PDF (via `pdfplumber`) ou du document DOCX |

**Navigation GIZ** :
1. Page d'avis (`projectForwarding.do?pid=…`) → redirect vers `.../project/{code}/de/overview`
2. Onglet "Tender Documents" (lien contenant `teilnahmeunterlagen` / `tender document`)
3. Section "Specification for Tenders" (= `Leistungsbeschreibungen` en allemand)
4. Clic sur l'icône disque (`<span onclick="window.location.href='…pdf'">`) → téléchargement du TOR

**Extraction de texte** — `_bytes_to_text()` détecte automatiquement le format par Content-Type et extension : PDF via `pdfplumber`, DOCX via `python-docx`, HTML via BeautifulSoup, texte brut sinon.

---

## Fallback DNS (`sources/dns_utils.py`)

Certains réseaux d'entreprise ne résolvent pas les domaines publics (ex. `afd.dgmarket.com`). Le module patche `socket.getaddrinfo` avec deux mécanismes :

1. **Overrides permanents** (`_HARDCODED_DNS`) — IPs codées en dur pour les domaines cassés (récupérées depuis le cache DNS Chrome `chrome://net-internals/#dns`).
2. **Fallback DoH** — En cas de `NameResolutionError`, interroge `https://dns.google/resolve` pour obtenir l'IP, puis réessaie la requête. Thread-safe via `contextvars`.

---

## Base de données

SQLite (`ao_database.db`) en mode journal **MEMORY**. Table principale `notices` :

| Colonne | Type | Description |
|---|---|---|
| `id` | TEXT PK | Identifiant unique (`BERD-42771404`, `UNDP-12345`…) |
| `bailleur` | TEXT | Banque Mondiale / PNUD / GIZ / BERD / AFD |
| `titre` | TEXT | Intitulé de la mission |
| `projet` | TEXT | Nom du projet associé |
| `secteur` | TEXT | Secteur détecté : Éducation, Santé, Culture, Autre |
| `type_avis` | TEXT | Type brut tel que retourné par la source |
| `mode_selection` | TEXT | Méthode de passation (BM uniquement) |
| `langue` | TEXT | Anglais / Français / Espagnol / Portugais |
| `date_publication` | TEXT | ISO date `YYYY-MM-DD` |
| `date_limite` | TEXT | ISO date `YYYY-MM-DD` |
| `pays` | TEXT | Pays bénéficiaire |
| `lien` | TEXT | URL de l'avis original |
| `pertinence` | TEXT | Haute / Moyenne / À vérifier |
| `texte_complet` | TEXT | Texte brut pour scoring (non transmis à l'UI) |
| `actif` | INTEGER | 0 si deadline dépassée |

**Déduplication** : `INSERT OR IGNORE` sur `id`. Un AO déjà en base n'est jamais écrasé.  
**Expiration** : `mark_expired()` passe `actif=0` pour les AO dont `date_limite < aujourd'hui`.  
**FTS5** : table virtuelle `notices_fts` pour la recherche plein texte (dégradation gracieuse si SQLite sans FTS5).

### Mode journal MEMORY (compatibilité Google Drive)

La base est stockée sur un **Drive partagé Google Drive**. Le mode WAL (par défaut SQLite) y est **incompatible** : les fichiers annexes `-wal` / `-shm` ne sont pas synchronisés de façon atomique → corruption (`database disk image is malformed`) lors d'une coupure réseau. Le mode TRUNCATE laisse lui aussi un fichier `-journal` que Google Drive peut verrouiller au démarrage (« Accès refusé »).

Solution retenue (`get_connection()` dans `database.py`) : `PRAGMA journal_mode=MEMORY` — le journal de rollback est tenu **en RAM**, aucun fichier annexe n'est écrit sur le disque, donc aucun conflit possible avec la synchronisation Google Drive. La connexion utilise aussi `timeout=30` pour patienter si la base est momentanément verrouillée par le scheduler.

**Récupération automatique** : au démarrage, `init_db()` teste l'ouverture de la base. Si elle est corrompue ou verrouillée, elle (et ses fichiers `-journal` / `-wal` / `-shm`) est supprimée et recréée à neuf — l'application redémarre alors sans intervention manuelle (les AO sont recollectés au prochain cycle).

### Cache des détails AO (`notice_details`)

Pour éviter de relire les sites sources à **chaque rechargement** des pages Analyse AO et Offres pertinentes, le résultat de `fetch_detail()` est mis en cache côté serveur :

| Colonne | Type | Description |
|---|---|---|
| `notice_id` | TEXT PK | Identifiant de l'AO |
| `detail_json` | TEXT | Détail enrichi sérialisé en JSON |
| `fetched_at` | TEXT | Horodatage de la récupération |

Trois états :
- **Aucune entrée** → on interroge la source, puis on met en cache.
- **Données utiles** (`description`, `full_text`, `criteria_section`, `documents`…) → servies depuis le cache, aucune requête externe.
- **`{_failed: true}`** → la récupération précédente a échoué ; on n'insiste pas à chaque chargement (affichage « source inaccessible » + bouton 🔄 Réessayer).

Le paramètre `?force=true` court-circuite le cache et force une relecture (bouton « 🔄 Actualiser »). Fonctions associées dans `database.py` : `get_notice_detail_cache()`, `set_notice_detail_cache()`, `clear_notice_detail_cache()`.

---

## Scoring de pertinence

Le module `filters.py` attribue un score à chaque AO **sans en écarter aucun**. Le filtrage est entièrement délégué à l'interface.

| Niveau | Condition |
|---|---|
| **Haute** | Mot-clé fort (`STRONG_KEYWORDS`) **et** secteur cible identifié (`TARGET_SECTORS`) |
| **Moyenne** | Mot-clé fort **ou** secteur cible (pas les deux) |
| **À vérifier** | Aucun signal détecté automatiquement |

**Mots-clés forts** (`config.STRONG_KEYWORDS`) : feasibility, étude de faisabilité, energy efficiency, AMO, technical assistance, green building, energy audit, advisory services…

**Secteurs cibles** (`config.TARGET_SECTORS`) : Éducation, Santé, Culture — détectés par liste de mots-clés multilingues (FR/EN/ES/PT).

---

## API REST

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/` | Interface veille (`index.html`) |
| `GET` | `/analyse` | Interface Analyse AO (`analyse.html`) |
| `GET` | `/pertinentes` | Interface Offres pertinentes (`pertinentes.html`) |
| `GET` | `/params` | Interface Paramètres (`params.html`) |
| `GET` | `/api/notices` | Liste des AO actifs (JSON) |
| `GET` | `/api/notices/stats` | Statistiques par bailleur / langue / secteur |
| `GET` | `/api/refresh/status` | État de la collecte en cours |
| `POST` | `/api/refresh` | Déclenche une collecte immédiate |
| `GET` | `/api/debug/afd` | Diagnostic du scraper AFD (connexion, tables HTML, parsing) |
| `GET` | `/api/ao-detail/{id}` | Données enrichies d'un AO — servi depuis le cache `notice_details` ; `?force=true` force une relecture de la source |
| `DELETE` | `/api/ao-detail/{id}/cache` | Vide le cache d'un AO (relecture à la prochaine ouverture) |
| `GET` | `/api/keywords` | Groupes de mots-clés pour l'UI |
| `GET` | `/static/*` | Fichiers statiques (logo, sigle…) |

---

## Interface web

SPA vanilla JavaScript, charte graphique **Assemblage ingénierie** (Open Sans, rouge `#E30513`, violet `#30323E`).

### Page Veille (`index.html`)

#### Panneau de filtres

Cliquer sur le **titre d'un filtre** (Bailleur, Langue, Type AO, Pertinence, Tri, Publication) coche ou décoche toutes ses cases d'un coup. Dans le tableau Type AO, cliquer sur l'en-tête de colonne (**BM**, **PNUD**, **GIZ**, **BERD**) fait de même pour cette colonne uniquement.

| Filtre | Type | Comportement | Défaut |
|---|---|---|---|
| **Bailleur** | Cases à cocher | BM / PNUD / GIZ / BERD / AFD | Tous cochés |
| **Langue** | Cases à cocher | Anglais / Français / Espagnol / Portugais | Tous cochés |
| **Type d'AO** | Tableau 4 colonnes (par bailleur) | Valeur `bailleur\|\|abbr` — filtrage indépendant par source | Voir détail ci-dessous |
| **Pertinence** | Cases à cocher + légende | Haute / Moyenne / À vérifier | Haute ✅ · Moyenne ✅ · À vérifier ☐ |
| **Tri** | Cases à cocher | Non pertinent / Pré-Tri / Pertinent / Non trié | Non trié ✅ uniquement |
| **Publication** | Cases à cocher | Publiés (≤ aujourd'hui) / À venir (> aujourd'hui) | Tous cochés |
| **Délai** | Sélecteurs de date | Deadline ≥ date et ≤ date | Vide |
| **Mots-clés** | Cases dynamiques | Logique OU sur titre + projet | Tous décochés |
| **Recherche libre** | Champ texte | Logique ET sur titre + projet + pays | Vide |

**Types AO décochés par défaut** (`TYPE_DEFAULT_OFF` dans `index.html`) :

| Bailleur | Types décochés |
|---|---|
| **BM** | Invitation à soumissionner (ITB) · Individual consultant (IC) |
| **PNUD** | ITB · Appel à propositions (CFP) · Expression d'intérêt (EOI) · Accord-cadre long terme (LTA) |
| **GIZ** | Marché attribué (`VgVVergebener Auftrag`) · Appel d'offres travaux (`VOB/A/Ausschreibung`) |
| **BERD** | Invitation à soumissionner 1 tour · Invitation à soumissionner 2 tours · Avis de présélection · Liste des présélectionnés · GPN (avis général de passation) |

> **Filtre « Individual consultant (IC) » de la BM** — cas particulier : il ne repose pas sur `type_avis` mais sur la colonne `mode_selection` (un AO BM est « IC » si `mode_selection` contient « individual consultant »). La case n'apparaît que si au moins un AO BM correspond, et elle est **décochée par défaut**.

#### Tableau de résultats

- Tri côté client par clic sur colonne (défaut : date_limite croissante)
- Colonnes : ⭐ · 🔄 · Bailleur · Titre · Secteur · Type · Mode · Langue · Publication · Deadline · Pays · 🔗
- Deadline < 7 jours : texte en rouge gras
- Lignes **Haute** : fond rouge clair `#F9E1E3`
- Lignes **Moyenne** : fond gris `#F2F2F2`

#### Triage

Boutons 🗑️ / 🔄 / ⭐ sur chaque ligne. État stocké **côté serveur** dans la colonne `triage` de la table `notices` (endpoints `GET`/`POST /api/triage`), partagé entre tous les utilisateurs et toutes les pages. Une migration unique récupère l'ancien état `localStorage` (clé `ao_veille_triage`) au premier chargement. Les AO marqués 🔄 (pré-tri) apparaissent dans la page Analyse, les ⭐ (pertinents) dans la page Offres pertinentes.

---

### Page Analyse AO (`analyse.html`)

Affiche uniquement les AO marqués **Pré-Tri** (🔄) depuis la page Veille. Pour chaque AO, les données enrichies sont chargées en parallèle (par lots de 3) via `/api/ao-detail/{id}`. La source n'est lue qu'**une seule fois** : les résultats sont mis en cache côté serveur (table `notice_details`), donc les rechargements suivants sont instantanés et fonctionnent même hors connexion. Un badge « 📁 En cache · [date] » et un bouton « 🔄 Actualiser » permettent de forcer une relecture (`?force=true`). Les sources injoignables sont marquées `_failed` et ne sont pas réinterrogées à chaque chargement (bouton « 🔄 Réessayer »).

#### Structure d'une carte

```
┌─────────────────────────────────────────────────────────────────┐
│ STRIP : [badge] [dot] Titre de la mission     [🗑️][🔄][⭐] [↗] │
├──────────────────┬──────────────────────────────────────────────┤
│ GAUCHE (180px)   │  DROITE (flex)                               │
│ 🌍 Pays          │  📝 MISSION                                  │
│ ⏰ Deadline      │  Texte extrait du TOR / notice…              │
│ 📁 Type          │                                              │
│ ⚙️ Mode          │  📄 DOCUMENTS                                │
│ 💬 Langue        │  [lien vers PDF]                             │
│ 🏛 Secteur       │                                              │
├──────────────────┴──────────────────────────────────────────────┤
│ ✅ CRITÈRES DE SÉLECTION (si disponibles)                       │
│ Texte extrait du TOR…                                           │
└─────────────────────────────────────────────────────────────────┘
```

Le triage (🗑️ / 🔄 / ⭐) dans cette page est synchronisé avec la page Veille via la base SQLite (voir ci-dessous).

---

## Configuration (`config.py`)

| Paramètre | Valeur par défaut | Description |
|---|---|---|
| `DB_PATH` | `ao_database.db` (même dossier) | Chemin de la base SQLite |
| `SCHEDULER_TIME` | `"07:00"` | Heure de collecte quotidienne automatique |
| `UNDP_EXCLUDE_TYPES` | `["RFI", "IC", "RFQ"]` | Types PNUD exclus de la collecte |
| `STRONG_KEYWORDS` | — | Mots-clés métier pour scoring Haute/Moyenne |
| `TARGET_SECTORS` | — | Secteurs cibles et leurs mots-clés |
| `KEYWORD_DISPLAY_GROUPS` | — | Groupes affichés dans le panneau UI |

---

## Dépendances Python

```
fastapi
uvicorn[standard]
requests
beautifulsoup4
lxml
python-dateutil
schedule
pdfplumber
python-docx
```

Installation automatique au premier `python main.py` via `requirements.txt`.
