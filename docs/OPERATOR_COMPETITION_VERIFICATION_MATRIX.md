# Operator football competition verification matrix

`OPERATOR_COMPETITION_REGISTRY_VERSION = 9`

Principal visible catalogue: **37** rows.
Operator UI/state uses canonical codes only. Venue identifiers below are backend evidence, not operator-selectable tickers.

Retrieved 2026-09-22 from read-only public metadata:
- Polymarket Gamma `GET /sports` (469 sports). UEFA Nations League sport `unl` series is **11446**.
  FA Cup sport `efa` series is **10314**; the 2026-09-16 snapshot `10307` is no longer listed and is not claimed.
- Kalshi `GET /series?category=Sports`
- Matchbook: label-alias matching only (no competition IDs). Live soccer COMPETITION tags for Nations League were `UEFA Nations League A/B/D`. League C was not on the 2026-09-22 open snapshot and is not registered.

Selectable rows require **VERIFIED_ALL_3** (Matchbook aliases + Kalshi match-level GAME/BTTS/TOTAL and FTTS where present + Polymarket Gamma series).
Partial rows remain visible and disabled. Identifiers were not guessed.

| Code | Display name | Group | Selectable | Status | Matchbook | Kalshi | Polymarket | Evidence / reason |
|---|---|---|---|---|---|---|---|---|
| `premier_league` | English Premier League | England | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXEPLGAME,KXEPLBTTS,KXEPLTOTAL,KXEPLFTTS; Gamma epl/10188 |
| `championship` | EFL Championship | England | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXEFLCHAMPIONSHIPGAME,KXEFLCHAMPIONSHIPBTTS,KXEFLCHAMPIONSHIPTOTAL; Gamma elc/10355 |
| `la_liga` | Spain La Liga | Spain | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXLALIGAGAME,KXLALIGABTTS,KXLALIGATOTAL,KXLALIGAFTTS; Gamma lal/10193 |
| `carabao_cup` | Carabao Cup | England | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXEFLCUPGAME,KXEFLCUPBTTS,KXEFLCUPTOTAL,KXEFLCUPFTTS; Gamma efl/10329 |
| `fa_cup` | FA Cup | England | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXFACUPGAME,KXFACUPBTTS,KXFACUPTOTAL,KXFACUPFTTS; Gamma efa/10314 (live 2026-09-20; 2026-09-16 snapshot 10307 is no longer listed) |
| `international_friendlies` | International Friendlies | International | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXINTLFRIENDLYGAME,KXINTLFRIENDLYBTTS,KXINTLFRIENDLYTOTAL; Gamma fif/10238 |
| `bundesliga` | Bundesliga | Germany | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXBUNDESLIGAGAME,KXBUNDESLIGABTTS,KXBUNDESLIGATOTAL,KXBUNDESLIGAFTTS; Gamma bun/10194 |
| `serie_a` | Serie A | Italy | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXSERIEAGAME,KXSERIEABTTS,KXSERIEATOTAL,KXSERIEAFTTS; Gamma sea/10203 |
| `champions_league` | UEFA Champions League | UEFA | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXUCLGAME,KXUCLBTTS,KXUCLTOTAL,KXUCLFTTS; Gamma ucl/10204 |
| `europa_league` | UEFA Europa League | UEFA | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXUELGAME,KXUELBTTS,KXUELTOTAL; Gamma uel/10209 |
| `conference_league` | UEFA Conference League | UEFA | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXUECLGAME,KXUECLBTTS,KXUECLTOTAL; Gamma col/10437 |
| `uefa_nations_league` | UEFA Nations League | UEFA | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi match-level KXUEFANLGAME,KXUEFANLBTTS,KXUEFANLTOTAL,KXUEFANLFTTS (GET /series 2026-09-22; GAME has live 24–26 Sep fixtures). Spread/1H/team-total/exact-score/advance/MOV observed but not admitted. Season series KXUEFANL is not a fixture. Gamma unl/11446 match events only; group/champion/relegation outrights have series=null and stay out of the fixture pipeline. Matchbook live COMPETITION tags UEFA Nations League A/B/D. Neighbor CONCACAF Nations League / KXCONCACAFNL / Gamma conl=10673 unmatched. Not in the default eight. |
| `super_lig` | Turkish Süper Lig | Turkey | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXSUPERLIGGAME,KXSUPERLIGBTTS,KXSUPERLIGTOTAL; Gamma tur/10292 |
| `mls` | Major League Soccer | USA / Canada | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXMLSGAME,KXMLSBTTS,KXMLSTOTAL,KXMLSFTTS; Gamma mls/10189 |
| `league_one` | EFL League One | England | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXEFLL1GAME,KXEFLL1BTTS,KXEFLL1TOTAL; Gamma el1/11435 |
| `league_two` | EFL League Two | England | no | PARTIAL | verified | unverified | verified | Kalshi match-level series not verified |
| `copa_del_rey` | Copa del Rey | Spain | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXCOPADELREYGAME,KXCOPADELREYBTTS,KXCOPADELREYTOTAL,KXCOPADELREYFTTS; Gamma cdr/10316 |
| `dfb_pokal` | DFB-Pokal | Germany | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXDFBPOKALGAME,KXDFBPOKALBTTS,KXDFBPOKALTOTAL,KXDFBPOKALFTTS; Gamma dfb/10317 |
| `coppa_italia` | Coppa Italia | Italy | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXCOPPAITALIAGAME,KXCOPPAITALIABTTS,KXCOPPAITALIATOTAL,KXCOPPAITALIAFTTS; Gamma itc/10287 |
| `ligue_1` | Ligue 1 | France | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXLIGUE1GAME,KXLIGUE1BTTS,KXLIGUE1TOTAL,KXLIGUE1FTTS; Gamma fl1/10195 |
| `eredivisie` | Eredivisie | Netherlands | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXEREDIVISIEGAME,KXEREDIVISIEBTTS,KXEREDIVISIETOTAL; Gamma ere/10286 |
| `primeira_liga` | Primeira Liga | Portugal | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXLIGAPORTUGALGAME,KXLIGAPORTUGALBTTS,KXLIGAPORTUGALTOTAL; Gamma por/10330 |
| `scottish_premiership` | Scottish Premiership | Scotland | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXSCOTTISHPREMGAME,KXSCOTTISHPREMBTTS,KXSCOTTISHPREMTOTAL; Gamma scop/10674 |
| `belgian_pro_league` | Belgian Pro League | Belgium | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXBELGIANPLGAME,KXBELGIANPLBTTS,KXBELGIANPLTOTAL; Gamma bel1/12351 |
| `liga_mx` | Liga MX | Mexico | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXLIGAMXGAME,KXLIGAMXBTTS,KXLIGAMXTOTAL,KXLIGAMXFTTS; Gamma mex/10290 |
| `brasileirao` | Brazilian Série A | Brazil | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXBRASILEIROGAME,KXBRASILEIROBTTS,KXBRASILEIROTOTAL,KXBRASILEIROFTTS; Gamma bra/10359 |
| `argentina_primera` | Argentine Primera División | Argentina | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXARGPREMDIVGAME,KXARGPREMDIVBTTS,KXARGPREMDIVTOTAL; Gamma arg/10312 |
| `copa_libertadores` | Copa Libertadores | South America | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXCONMEBOLLIBGAME,KXCONMEBOLLIBBTTS,KXCONMEBOLLIBTOTAL; Gamma lib/10289 |
| `saudi_pro_league` | Saudi Pro League | Saudi Arabia | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXSAUDIPLGAME,KXSAUDIPLBTTS,KXSAUDIPLTOTAL; Gamma spl/10361 |
| `j1_league` | J1 League | Japan | yes | VERIFIED_ALL_3 | verified | verified | verified | Kalshi KXJLEAGUEGAME,KXJLEAGUEBTTS,KXJLEAGUETOTAL; Gamma jap/10360 |
| `south_african_premiership` | South African Premiership | South Africa | no | PARTIAL | verified | unverified | verified | Kalshi match-level series not verified |
| `nfl` | NFL | NFL | yes | VERIFIED_ALL_3 | verified | verified | verified | Registered full-game families: KXNFLGAME, KXNFLSPREAD, KXNFLTOTAL (half-point families only); Gamma nfl/12185; Matchbook American Football + NFL competition tag. Not in the default eight. Exceptional tie/cancel/suspend differences are an audit caveat. Catalogue eligibility follows registration. Venue orders stay separately gated. |
| `nba` | NBA | NBA | yes | VERIFIED_ALL_3 | verified | verified | verified | PAPER: KXNBAGAME,KXNBASPREAD,KXNBATOTAL (full-game half-point families only). Gamma nba/10345. Matchbook basketball sport-id 4 + NBA tag 406202315670010. Selectable with zero fixtures. Not in the default eight. Only Kalshi↔Polymarket GAME_WINNER is PAPER-admitted. Exceptional lifecycle is not a PAPER blocker for that cell. Spreads/totals and all Matchbook NBA pairs stay fail-closed because ordinary full-game/OT game-book evidence is not established. WNBA/NCAAB/Summer League are not NBA. |
| `ncaab` | NCAA Men's Basketball | College Basketball | yes | VERIFIED_ALL_3 | verified | verified | verified | Issue #508. Kalshi KXNCAAMBGAME,KXNCAAMBSPREAD,KXNCAAMBTOTAL (full-game half-point structural families only; **no PAPER pair admitted**). Gamma cbb/10470 (0 open games on 2026-09-22). Matchbook Basketball sport-id 4; **no NCAA/NCAAB competition tag** — WNBA/NBA listings are rejected_non_ncaab_basketball. Selectable with a healthy zero-fixture off-season. Not default. NCAAW is not selectable. Census 2026-09-22. |
| `mlb` | MLB | MLB | yes | VERIFIED_ALL_3 | verified | verified | verified | Stage 1. Kalshi KXMLBGAME and KXMLBTOTAL only (KXMLBSPREAD, F5, inning, series, futures not registered). Gamma mlb series id 3. Matchbook Baseball sport-id 3 + competition tag 1494669213760003. Selectable, not default. Owner-approved PAPER comparison for structural Game Winner and exact x.5 Total Runs (2026-09-26). Not live-execution eligible. Spring training, minors, college, KBO, NPB, WBC, and World Series outrights are not MLB fixtures. |
| `atp` | ATP | Tennis | yes | VERIFIED_ALL_3 | verified | verified | verified | Stage 1 ATP singles discovery only. Public 2026-09-24: Kalshi series KXATPMATCH (not KXATPGAME, not challenger/doubles/set/game/outright series). Gamma atp/10365. Matchbook sport-id 9 name Tennis (Table Tennis is a different sport). Admitted tournament labels from the same player pairs: ATP Hangzhou = Hangzhou Open, ATP Chengdu = Chengdu Open. Structurally identical singles Match Winner is registered. Retirement/walkover settlement differences are not an admission blocker. Catalogue eligibility follows registration. Venue orders stay separately gated. Not in the default eight. Coverage is these admitted tournaments only, not every ATP event. |
| `wta` | WTA | Tennis | yes | VERIFIED_ALL_3 | verified | verified | verified | Stage 1 WTA singles discovery only. Public 2026-09-24: Kalshi series KXWTAMATCH. Gamma wta/10366. Matchbook sport-id 9. Admitted labels: WTA Singapore = Singapore Open, WTA Seoul (Matchbook label only; Korea Open was not proven as the same tournament). Structurally identical singles Match Winner is registered. Retirement/walkover settlement differences are not an admission blocker. Catalogue eligibility follows registration. Venue orders stay separately gated. Not in the default eight. Coverage is these admitted tournaments only, not every WTA event. |

## Deliberately excluded neighbours

- Kalshi: `KXUCLW*` women's UCL, `KXMLSAST*` MLS All-Star, `KXDENSUPERLIGA*` Danish Superliga,
  `KXBUNDESLIGA2*`, `KXSERIEAW*`, `KXSERIEB*`, `KXBRASILEIROB*`/`C*`, `KXLIGUE2*`,
  `KXEREDIVISIEW*`, `KXJ2LEAGUE*`, `KXCONMEBOLSUD*` Sudamericana,
  `KXCONCACAFNL` CONCACAF Nations League outrights, `KXUEFANL` UEFA Nations League
  season series, `KXUEFANL1H*` / `KXUEFANLSPREAD` / `KXUEFANLSCORE` /
  `KXUEFANLTEAMTOTAL` / `KXUEFANLADVANCE` / `KXUEFANLMOV`.
- Polymarket: `bl2` 2. Bundesliga, `itsb` Serie B, `clf` Club Friendlies, `ja2` J2, `fr2` Ligue 2,
  `uwcl` Women's Champions League, `tur2` Turkey 1. Lig, `conl` CONCACAF Nations League,
  Nations League group/champion/relegation events with `series=null`.
- Matchbook labels still unmatched: EFL Trophy / Vertu Trophy, club friendlies, women's/youth cups,
  UEFA Women's Nations League, CONCACAF Nations League.
  UEFA Nations League C is an exact Matchbook alias of UEFA Nations League,
  observed 2026-09-26 alongside A/B/D. It is not a separate competition.

Default selected startup scope remains the original eight competitions. NFL, NBA, NCAA Men's Basketball, MLB, ATP, and WTA are selectable and are not default. ATP and WTA singles Match Winner, and MLB Game Winner plus exact x.5 Total Runs, are registered where the Approved Match Register admits them. Catalogue eligibility follows that registration. Venue orders stay separately gated and default off. NCAAB stays unapproved: the census did not prove the ordinary contract. ATP/WTA tournament coverage is the evidence-backed allowlist only (Hangzhou, Chengdu, Singapore, Seoul).
