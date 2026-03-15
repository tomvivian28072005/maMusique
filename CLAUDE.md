# Clom — Contexte pour Claude

## Qui est Tom

Tom est un jeune dev français, autodidacte, qui code son propre lecteur de musique. Il communique en français, va droit au but, et préfère les solutions simples. Quand il dit "finis ce que tu fesais" ou "pourquoi arreter ?", ça veut dire : continue sans poser de questions. Il ne veut pas de récap inutile ni de demandes de confirmation pour des trucs évidents.

**Style de collaboration :**
- Réponses concises, pas de blabla
- Faire les choses sans demander permission à chaque étape
- Quand il donne une tâche, enchaîner jusqu'au bout (build + commit + push) sauf s'il dit le contraire
- Il teste en live et donne du feedback visuel (screenshots, logs)
- Les diagnostics IDE (linter) sur main.py sont des faux positifs, les ignorer

## Le projet

**Clom** est un lecteur de musique personnel. L'utilisateur installe un .exe, ça lance un serveur FastAPI local, et l'app s'ouvre dans le navigateur comme une vraie app desktop.

### Stack
- **Backend** : FastAPI + uvicorn, Python 3.14, SQLite via SQLAlchemy
- **Frontend** : `index.html` unique (vanilla JS + Tailwind CDN), tout inline
- **Téléchargement** : yt-dlp + ffmpeg (MP3 192kbps) + mutagen (tags ID3)
- **Packaging** : PyInstaller (--noconsole) → Inno Setup (.exe installeur)
- **Lancement prod** : `launcher.py` → démarre uvicorn + ouvre le navigateur
- **Lancement dev** : `maMusique.bat` sur le Bureau (avec --reload)

### Fichiers clés
| Fichier | Rôle |
|---------|------|
| `main.py` | App FastAPI (~1250 lignes) : toutes les routes API, logique download, import, mise à jour |
| `index.html` | SPA frontend (~4800 lignes) : HTML + CSS + JS inline |
| `database.py` | Modèles SQLAlchemy (Track, Playlist, PlaylistTrack) + CRUD |
| `launcher.py` | Point d'entrée PyInstaller : redirige stdout/stderr, lance uvicorn, ouvre navigateur |
| `build.py` | Script de build : PyInstaller + copie outils (yt-dlp, ffmpeg, node) + Inno Setup |
| `installer.iss` | Config Inno Setup (installeur Windows) |
| `docs/index.html` | Landing page GitHub Pages (tomvivian28072005.github.io/maMusique) |

### Design
- Dark theme, fond `#050508`, accent bleu `#2563eb` (blue-600 Tailwind)
- Font Outfit
- Mots courts dans les menus : Modifier, Ajouter, Retirer, Supprimer
- Volume slider logarithmique (`x^1.4`)
- Marquee texte lent, boucle unidirectionnelle avec pause 1s début + fin
- Coeur favoris en bleu (pas rouge !)

## Workflow de release

Chaque nouvelle version suit ce processus exact :

1. **Modifier le code** (la feature ou le fix)
2. **Bumper la version** dans 3 fichiers :
   - `main.py` : `APP_VERSION = "x.y.z"`
   - `installer.iss` : `AppVersion=x.y.z`
   - `docs/index.html` : lien de téléchargement `releases/download/vx.y.z/Clom-setup.exe`
3. **Build** : `./venv/Scripts/python build.py` (~2-3 min, produit `dist/Clom-setup.exe` ~161 Mo)
4. **Commit + push** : `git add` les fichiers modifiés, commit avec message en français, push
5. **Tom crée la release** sur GitHub manuellement et y joint `Clom-setup.exe`

## Système de mise à jour automatique

L'app vérifie au démarrage s'il y a une nouvelle version sur GitHub Releases :
- Comparaison **sémantique** (tuple numérique, pas string)
- Si nouvelle version : bouton "Mise à jour vX.Y.Z" dans la sidebar (au-dessus de Bibliothèque)
- Clic → POST `/api/update` → télécharge l'installeur → crée un batch → kill le serveur
- Le batch attend, lance l'installeur en `/SILENT`, puis relance `Clom.exe`
- L'ancienne page désactive son listener `pagehide` pour ne pas tuer le nouveau serveur
- Le nouveau serveur a une grace period de 10s où il ignore les shutdown

## Mécanisme shutdown/reload

Problème résolu : comment distinguer un reload de page d'une fermeture d'onglet ?
- `pagehide` → `sendBeacon('/api/shutdown')` (délai 3s)
- Au chargement → `POST /api/cancel-shutdown` (annule si c'était un reload)
- Pendant une MAJ → le listener est désactivé (`_updateInProgress = true`)
- Grace period 10s au démarrage du serveur (ignore les shutdown parasites)

## Subprocess Windows

Tous les `subprocess.run()` utilisent `creationflags=_NO_WINDOW` (`0x08000000`) pour éviter les fenêtres console qui pop (yt-dlp, ffmpeg).

## Historique des versions

| Version | Feature |
|---------|---------|
| 0.1.0 | Premier installeur fonctionnel, onboarding, vérification MAJ |
| 0.1.1 | Bouton coeur (favoris) dans le player |
| 0.1.2 | Coeur en bleu + bouton MAJ dans la sidebar |
| 0.1.3 | Suppression doublon "Ajouter" dans le menu |
| 0.1.4 | Relance auto du serveur après MAJ |
| 0.1.5 | Comparaison sémantique des versions + grace period serveur |
| 0.1.6 | Fix shutdown parasite (ancienne page ne tue plus le nouveau serveur) |

## Phases futures (plan global)

### Fait
- Phase 0 : Proto fonctionnel (bugs, CSS, responsive, accessibilité, menus)
- Phase 1 partielle : Git + GitHub, landing page GitHub Pages
- Phase 2 partielle : Installeur Windows, auto-update fonctionnel

### À faire
- **PWA** : manifest.json, service worker, bouton "Installer"
- **Accès mobile** : écouter sur `0.0.0.0` + QR code (accès depuis le téléphone via Wi-Fi)
- **Google OAuth** : comptes utilisateurs, multi-user, données séparées par user
- **Onboarding** amélioré pour les nouveaux utilisateurs
- **Versioning** propre : CHANGELOG.md, numéro de version visible dans l'app
- **CI/CD** : build automatique du .exe via GitHub Actions
- **Stores** (plus tard) : Microsoft Store, Google Play

## Pièges connus

- **PyInstaller --noconsole** : met `sys.stdout`/`sys.stderr` à `None`, ce qui crash uvicorn. `launcher.py` redirige vers `Clom.log`.
- **Linter IDE** : les diagnostics sur `main.py` sont des faux positifs (imports venv non résolus, types SQLAlchemy). Les ignorer.
- **`pagehide` + reload** : `pagehide` se déclenche aussi sur un reload, pas seulement quand on ferme l'onglet. D'où le système de délai 3s + cancel.
- **Inno Setup `/SILENT`** : la section `[Run]` avec `postinstall` ne s'exécute PAS en mode silencieux. C'est le batch qui relance l'app.
- **YouTube** : zone grise légale. Présenter comme "lecteur de musique personnel", le téléchargement YT comme fonctionnalité optionnelle.

## Commandes utiles

```bash
# Dev (depuis le dossier du projet)
./venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 9000 --reload

# Build complet (PyInstaller + Inno Setup)
./venv/Scripts/python build.py

# L'installeur produit se trouve dans dist/Clom-setup.exe
```

## Repo GitHub
- **URL** : github.com/tomvivian28072005/maMusique
- **Pages** : tomvivian28072005.github.io/maMusique (landing page)
- **Branche** : main uniquement
