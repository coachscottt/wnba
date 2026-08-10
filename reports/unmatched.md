# Unmatched report

Generated 2026-08-10 16:21 UTC by `run.py clean`.

## Overview

- `ok`: 779
- `stats_pending`: 238
- `voided`: 25
- `name_unmatched`: 4

**Match rate** (odds rows whose game has stats): 99.5% (804/808)
`stats_pending` rows (games newer than ingested stats) are excluded from the rate — they resolve when stats catch up.

## Match rate by book

- betonlineag: 98.5% (128/130)
- betrivers: 100.0% (294/294)
- draftkings: 100.0% (130/130)
- fanduel: 100.0% (125/125)
- williamhill_us: 98.4% (127/129)

## Match rate by month (capture time)

- 2026-07: 99.4% (682/686)
- 2026-08: 100.0% (122/122)

## Match rate by team (either side of the mapped game)

- Atlanta Dream: 100.0% (117/117)
- Chicago Sky: 100.0% (116/116)
- Connecticut Sun: 100.0% (98/98)
- Dallas Wings: 100.0% (119/119)
- Golden State Valkyries: 100.0% (52/52)
- Indiana Fever: 98.1% (105/107)
- Las Vegas Aces: 98.8% (164/166)
- Los Angeles Sparks: 100.0% (40/40)
- Minnesota Lynx: 100.0% (106/106)
- New York Liberty: 100.0% (154/154)
- Phoenix Mercury: 100.0% (112/112)
- Portland Fire: 96.3% (103/107)
- Seattle Storm: 100.0% (110/110)
- Toronto Tempo: 100.0% (106/106)
- Washington Mystics: 100.0% (106/106)

## Top 20 unmatched names by frequency

- 'Megan Gustafson': 4 rows

## Fuzzy proposals — approve via `data/external/name_approvals.csv`

| raw name | reason | candidates (name [player_id] score) |
|---|---|---|
| `Megan Gustafson` | no_exact | Megan DiLeo [3934218] roster/first-name |

## Unmatched odds rows (game has stats, name did not match)

- 'Megan Gustafson' on 2026-07-28 (betonlineag): 1 lines
- 'Megan Gustafson' on 2026-07-28 (williamhill_us): 1 lines
- 'Megan Gustafson' on 2026-07-31 (betonlineag): 1 lines
- 'Megan Gustafson' on 2026-07-31 (williamhill_us): 1 lines

## Player-games with no odds row (expected — not everyone gets props)

- 24566 played player-games have no prop line (odds coverage spans 15 of the ingested games; most of history predates odds collection — expected).
