# Operator football competition verification matrix

`OPERATOR_COMPETITION_REGISTRY_VERSION = 3`

Principal visible catalogue: **30** rows.
Operator UI/state uses canonical codes only. Venue identifiers below are backend evidence, not operator-selectable tickers.

Retrieved 2026-09-20 from read-only public metadata:
- Polymarket Gamma `GET /sports` (469 sports). FA Cup sport `efa` series is **10314**;
  the 2026-09-16 snapshot `10307` is no longer listed and is not claimed.
- Kalshi `GET /series?category=Sports`
- Matchbook: label-alias matching only (no competition IDs)

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

## Deliberately excluded neighbours

- Kalshi: `KXUCLW*` women's UCL, `KXMLSAST*` MLS All-Star, `KXDENSUPERLIGA*` Danish Superliga,
  `KXBUNDESLIGA2*`, `KXSERIEAW*`, `KXSERIEB*`, `KXBRASILEIROB*`/`C*`, `KXLIGUE2*`,
  `KXEREDIVISIEW*`, `KXJ2LEAGUE*`, `KXCONMEBOLSUD*` Sudamericana.
- Polymarket: `bl2` 2. Bundesliga, `itsb` Serie B, `clf` Club Friendlies, `ja2` J2, `fr2` Ligue 2,
  `uwcl` Women's Champions League, `tur2` Turkey 1. Lig.
- Matchbook labels still unmatched: EFL Trophy / Vertu Trophy, club friendlies, women's/youth cups.

Default selected startup scope remains the original eight competitions.
