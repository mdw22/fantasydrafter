# Draft Board

React front end for the fantasy draft tool. Consumes `cheat_sheet.csv`
(produced by the Python pipeline: `compute_fantasy_points.py` →
`projection_model.py` → `vbd_and_tiers.py`) and displays it as a
draft-day cheat sheet: an overall ranked list plus a per-position tab
with tier groupings.

## Setup

```
npm install
```

## Getting your data into the app

The app reads from `public/data/cheat_sheet.json`, which is generated
from your `cheat_sheet.csv` — it isn't committed as real data, since it
changes every time you rerun the projection pipeline.

```
python scripts/csv_to_json.py /path/to/cheat_sheet.csv
```

Run this any time you regenerate `cheat_sheet.csv`, before `npm run dev`
or `npm run build` — the app won't pick up new projections otherwise.

## Local development

```
npm run dev
```

## Deploying to GitHub Pages

1. Update `base` in `vite.config.js` to match your actual repo name
   (e.g. `/fantasydrafter/`) — this is required for assets to resolve
   correctly once hosted; the default `/fantasydrafter/` is a placeholder.
2. Build and deploy:
   ```
   npm run build
   npm run deploy
   ```
   (`npm run deploy` uses the `gh-pages` package to push `dist/` to the
   `gh-pages` branch of your repo — make sure GitHub Pages is configured
   to serve from that branch in your repo settings.)

## Notes on the data model

- **Tiers are position-scoped.** "Tier 1" for QBs and "Tier 1" for RBs
  aren't comparable — they're each computed relative to that position's
  own player pool. Because of this, the "Overall" tab shows a flat list
  ranked by `vbd_rank_overall` rather than grouping by tier; tier
  grouping only appears on the per-position tabs, where it's meaningful.
- **`vbd_vs_adp_proxy_overall`** is the model's cross-position VBD rank
  compared to a proxy for "market" draft position (based on each
  player's prior-season finish, since real ADP data isn't available).
  Positive (▲) means the model values the player earlier than their
  prior finish suggests; negative (▼) means the opposite.