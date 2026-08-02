# TAB-style feature ideas

Things AU TAB app users rely on that we could add to Dog Rater Live (product backlog — not yet built).

## Priority

### High value for tip flow
1. ~~**Odds + flucs on picks**~~ — **DONE** (Sportsbet public feed on pick cells: `$4.8↓`; place in pick detail)
2. ~~**Scratchings board**~~ — **DONE** (field SCR + Sportsbet late outs; tip-hit warning)
3. ~~**Jump countdown alerts**~~ — **DONE** (live banner, next 20 min, updates every 5s)
4. **Results with dividends** — $ prices, not just horse names
5. **Same-race multi ideas** — e.g. top 2 to run 1–2 from our tips

### Form / card depth
6. **Speed map / on-pace note** — likely leaders vs backmarkers
7. **Form extras** — gear changes, days since last run, weight vs last start, track/distance record

### Bet-shaped views (without placing bets)
8. **Quaddie / Early Quaddie helper** — our tips laid out for the legs
9. **Value flag** — tip looks overs vs market (needs odds)

### Trust / habit
10. **Watchlist / my races** + **tip performance ledger** (ROI-ish even without real stakes)

## Nice later (lower priority)
- Live vision / stream
- Cash out / account / deposit
- Mystery bets / randomisers
- Full Fixed vs Tote shopping
- Meeting weather + rail next to track rating
- Jockey/trainer combo at this track
- Banker / flexi-style packing for multis

## Suggested next build
**Results with dividends** ($ prices on winners).

## Notes
- Live odds / late outs use **Sportsbet’s public racing API** (TAB API not reachable without auth from this app).
- Fluc arrows: ↓ shortened · ↑ drifted · → steady (vs opening in recent fluc list).
- Scratch detection on Acceptances uses whole-word `SCR` (avoids false hits on names like SCRUFFY).
