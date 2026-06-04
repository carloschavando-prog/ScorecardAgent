# ScorecardAgent

Weekly EOS Pulse scorecard for On Par Entertainment. Renders a single
self-contained `index.html` from Supabase data.

## Generate

```
python3 generate.py                    # default: P5W4 2026 vs P5W4 2025
python3 generate.py 2026-06-01 2026-06-07 2025-05-26 2025-06-01 P6W1
```

Writes `index.html` in this directory.

## Data sources

- `labor_daily` — kitchen / FOH hours + cost (7Shifts pipeline)
- `sales` — raw GoTab POS line items
- `ts_events` — Tripleseat private events

Categories that map to the scorecard are listed at the top of `generate.py`.

## Deploy

Static site on Vercel:

```
vercel --prod
```

## Pending data sources

The following scorecard rows currently render as **TBD** until a feed lands:

- Food COS % / Beverage COS % (cost of goods)
- Employee count
- Google review count
- Voids & comps
- Staff attendance issues
