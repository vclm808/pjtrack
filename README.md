# pjtrack — Middle East private-jet departure tracker

Personal research tool: do business-jet departures from Middle East airports rise around
conflicts and large events, and where do those jets go? Aggregate analysis only; the
aircraft owner/operator field is deliberately not stored.

Pure Python 3 standard library, no dependencies. Data lives in `data/pjtrack.db` (SQLite).

## Commands

```sh
python3 -m pjtrack refdata              # aircraft-type DB + airports (run first; refresh monthly)
python3 -m pjtrack backfill --days 90   # OpenSky history; resumable; stops at the daily credit budget
python3 -m pjtrack live                 # adsb.lol live tracker loop (leave running)
python3 -m pjtrack report --days 30     # terminal summary
python3 -m pjtrack dashboard            # writes data/dashboard.html
python3 -m pjtrack status
```

## Data sources

| Source | Used for | Limits |
|---|---|---|
| OpenSky Network `/flights/departure` (OAuth2 creds in `.env`) | Historical departures per airport/day, with estimated destination | 4,000 credits/day; one 2-day window per airport = 30 credits. 33 airports ≈ 500 credits/day to keep current; the rest goes to backfill (~6 days of history per day of quota). Data finalises nightly, so the newest day fetched is today−2. |
| adsb.lol `/v2/point` + `/v2/type` | Live departures/arrivals, including blocked (LADD/PIA) aircraft | Free and unfiltered, but rate-limited and dependent on volunteer receivers |
| ADS-B Exchange `basic-ac-db` | icao24 → aircraft type, used to identify business jets | — |
| OurAirports | Airport coordinates/countries | — |

Every outbound request is checked against `scope.txt`.

## Coverage caveats (read before drawing conclusions)

- **Receiver coverage in the Gulf is thin on both free networks.** Riyadh (OERK) returned zero OpenSky
  departures on every day tested. Airports that never return data are flagged "blind" and excluded from all
  counts, so they never appear as fake zeros (`report` lists them).
- Business jets are identified by ICAO type code (`config.BIZJET_TYPES`). VIP airliners (BBJ, ACJ, VIP 747s)
  share type codes with commercial aircraft and are **not** counted.
- An aircraft whose transponder is missing from the type DB is not counted.
- Counts measure *observed* departures. Compare trends against each airport's own baseline, never
  absolute numbers between airports.

## Events

Add rows to `events.csv`: `date,countries,category,title` (countries = ISO-2 codes joined with `;`, or `ALL`).
The event study computes departures ÷ 28-day median baseline for days −7..+7, then tests the mean over
days −3..+1 against 2,000 random placebo dates from the same series (one-sided p-value).
It needs at least ~5 weeks of history before an event to compute a baseline.

## Backtest: could departures have flagged past events?

```sh
python3 -m pjtrack backtest --archive --priority 1 --workers 8   # stream adsb.lol archive days (no quota)
python3 -m pjtrack backtest --fetch --priority 1                 # OpenSky days for QA/BH/JO/LB (quota-limited)
python3 -m pjtrack backtest                                      # report + data/backtest_auto.html
python3 -m pjtrack backtest --plan                               # OpenSky credits still needed
```

Events, controls and holiday confounders live in `backtest_events.csv`. In `auto` mode each country
comes from the one source that covered it historically: adsb.lol archive for UAE (OMDB/OMDW/OMAD/OMAA/OMSJ),
Israel and Cyprus; OpenSky for Qatar, Bahrain, Jordan and Lebanon. Saudi Arabia, Iran, Iraq and Kuwait
have no usable free coverage. Archive days are ~2.5-3.4 GB each and are streamed, not stored.

## Analysis method

- Daily counts per country are only reported for days with complete data: every non-blind tracked airport
  was fetched from OpenSky, or the live tracker ran for ≥70% of the day. Other days are blank, not zero.
- Anomaly = |z| ≥ 2.5 (Poisson-scaled vs. 28-day median, skipping the previous week) **and** ≥ 5 flights
  from baseline.
- OpenSky and live legs for the same aircraft/airport within 2 h are merged (OpenSky preferred).
