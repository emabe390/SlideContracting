# Pi Instructions — SlideContracting

## First Action on Every Session

**Read `PROJECT_HISTORY.md`** in the repository root before attempting any task. It contains the full architectural history, known limitations, and prior decisions.

## Execution Context

The Python scraper (`main.py`) is **NOT executed in this repository's local directory** (`C:/Users/Aitesh/SlideContracting`). It runs on a **separate, remote machine**.

## Database Location

The SQLite database (`contracts.db`) is created and maintained on the machine where the scraper actually runs. Clearing or resetting data requires action on that remote host, not in this repo.

## Git Workflow

Because the remote scraper auto-pulls and auto-pushes `contracts.json` every cycle, the remote branch may have moved ahead of your local clone. **Always pull with rebase before you push:**

```bash
git pull --rebase origin main
git push origin main
```

If you push without rebasing first, you will create a merge conflict that the headless scraper cannot resolve on its next auto-pull.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Headless scraper, classifier, exporter, git sync |
| `index.html` | GitHub Pages frontend (cards, SSO, ESI window opener) |
| `configuration.py` | Secrets (ignored by git) |
| `contracts.json` | Generated data with `{"updated_at", "contracts": [...]}` |
| `PROJECT_HISTORY.md` | Full session history, architecture, decisions |
| `DEPLOYMENT.md` | Local-only copy of deployment notes (gitignored) |
