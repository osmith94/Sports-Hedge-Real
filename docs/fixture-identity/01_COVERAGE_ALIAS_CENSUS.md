# Fixture-identity census for newly selectable football competitions

Issue #434. Follow-up to draft PR #415 (MLS / Liga MX competition-aware aliases).
This census does **not** modify #415.

Data class: **live read-only provider metadata** plus **authoritative league/federation
participant lists**. Tests that consume this census are deterministic fixture/demo
providers, not live quotes.

Retrieved at: `2026-09-21T06:20:20Z`.

Method:

- Kalshi Trade API v2 unauthenticated `GET /series/{ticker}` and `GET /events`
  (no status filter; most current match-level rows were `settled` on a Monday).
- Polymarket Gamma unauthenticated `GET /sports` and `GET /events?series_id=`.
- Matchbook unauthenticated `GET /edge/rest/lookups/sports` and
  `GET /edge/rest/events` (`sport-ids=15`, `states=open,suspended` then `closed`).
- No login POST. No venue writes. No credentials stored.
- Authoritative senior-club universes from TFF, Liga Portugal licensing,
  LFP/Wikipedia 2026–27 tables, Eredivisie / SPFL / Belgian Pro League /
  EFL League One / CBF / AFA / SAFF 2026/26–27 lists.

Sanitized label extract: `backend/tests/fixtures/fixture_identity_census/provider_observed_labels.json`.

## 1. Canonical identity rules (from #415; not reimplemented here)

#415's competition-aware model is the design target:

- globally safe / unique aliases may be global;
- generic aliases must be competition-scoped;
- unknown aliases remain unchanged / fail closed;
- no global stripping of identity-bearing words;
- no fuzzy alias creation from similarity alone.

Owner-live (`588c8b4`) already has unique-alias `SeniorClub.aliases` and a
fail-closed FC/CF/AFC/SC/Calcio/BC remainder strip. It does **not** yet have
`SeniorClub.generic_aliases` or `resolve_team_name_for_competition`.

This follow-up therefore:

- adds **unique aliases only** to the owner-live registry;
- leaves generic city / same-token labels unresolved until #415 lands;
- does not change PAPER EventMatcher 0.80, scanner, capture, economics,
  settlement, or provider concurrency.

## 2. Coverage matrix

| Code | Competition | Operator selectable | Curated senior-club registry before this PR | Unique-alias registry in this PR | Generic aliases (need #415) | Matchbook label 2026-09-21 | Kalshi match-level series | Polymarket Gamma |
|---|---|---|---|---|---|---|---|---|
| `premier_league` | English Premier League | yes | yes (#316) | already present | none new | `England Premier League` | `KXEPLGAME` + BTTS/TOTAL/FTTS | `epl=10188` |
| `championship` | EFL Championship | yes | yes (#316) | already present | none new | `England Championship` | `KXEFLCHAMPIONSHIP*` | `elc=10355` |
| `la_liga` | La Liga | yes | yes (#316) | already present | none new | `Spain La Liga` | `KXLALIGA*` | `lal=10193` |
| `bundesliga` | Bundesliga | yes | yes (#316) | already present | none new | not in this snapshot | `KXBUNDESLIGA*` | `bun=10194` |
| `serie_a` | Serie A | yes | yes (#316) | already present | none new | `Italy Serie A` outright | `KXSERIEA*` | `sea=10203` |
| `mls` | MLS | yes | **#415 only** | not in this PR | `Miami` | `US Major League Soccer` (#415) | `KXMLS*` | `mls=10189` |
| `liga_mx` | Liga MX | yes | **#415 only** | not in this PR | `Leon`, `Queretaro` | `Mexico Liga MX` | `KXLIGAMX*` | `mex=10290` |
| `super_lig` | Turkish Süper Lig | yes | **missing** | **added (unique)** | `Istanbul`, sponsor-only remainder | no current match events | `KXSUPERLIGGAME/BTTS/TOTAL` | `tur=10292` |
| `primeira_liga` | Primeira Liga | yes | **missing** | **added (unique)** | `Sporting`, `Nacional`, `Lisbon` | outright `Primeira Liga 2026/27 BT` (no competition tag) | `KXLIGAPORTUGALGAME/BTTS/TOTAL` | `por=10330` |
| `ligue_1` | Ligue 1 | yes | **missing** | **added (unique)** | `Paris` | no current match events | `KXLIGUE1GAME/BTTS/TOTAL/FTTS` | `fl1=10195` |
| `eredivisie` | Eredivisie | yes | **missing** | **added (unique)** | `Eindhoven`, `Alkmaar`, `Sparta`, `Tokyo`-style city tokens | no current match events | `KXEREDIVISIEGAME/BTTS/TOTAL` | `ere=10286` |
| `scottish_premiership` | Scottish Premiership | yes | **missing** | **added (unique)** | bare `Dundee` (vs Dundee United) | `Scotland Premiership` outright | `KXSCOTTISHPREMGAME/BTTS/TOTAL` | `scop=10674` |
| `belgian_pro_league` | Belgian Pro League | yes | **missing** | **added (unique)** | `Standard`, `Brugge`/`Bruges` | no current match events | `KXBELGIANPLGAME/BTTS/TOTAL` | `bel1=12351` |
| `league_one` | EFL League One | yes | **missing** | **added (unique)** | overlapping English city tokens already Championship-scoped as unique | `England League 1` outright | `KXEFLL1GAME/BTTS/TOTAL` | `el1=11435` |
| `brasileirao` | Brazilian Série A | yes | **missing** | **added (unique)** | bare `Vitoria`, `Nacional` | `Brazilian Serie A` outright | `KXBRASILEIROGAME/BTTS/TOTAL/FTTS` | `bra=10359` |
| `argentina_primera` | Argentine Primera | yes | **missing** | **added (unique)** | `Rosario`, `Tucuman`, `Mendoza`, `Estudiantes`, `Gimnasia`, `Independiente` | `Argentina Liga Profesional de Fútbol` **match events present** | `KXARGPREMDIVGAME/BTTS/TOTAL` | `arg=10312` |
| `saudi_pro_league` | Saudi Pro League | yes | **missing** | **added (unique)** | `AL Suqoor` (unresolved nickname) | none | `KXSAUDIPLGAME/BTTS/TOTAL` | `spl=10361` |
| `j1_league` | J1 League | yes | **missing** | **added (unique)** | `Tokyo`, `Yokohama`, `Osaka`, `Kobe` city-only where two clubs share the city | none | `KXJLEAGUEGAME/BTTS/TOTAL` | `jap=10360` |
| `champions_league` | UEFA Champions League | yes | **missing** | reuse domestic union | cup-only clubs stay uncurated | `UEFA Champions League` outright | `KXUCLGAME/BTTS/TOTAL/FTTS` | `ucl=10204` |
| `europa_league` | UEFA Europa League | yes | **missing** | reuse domestic union | cup-only clubs stay uncurated | none | `KXUELGAME/BTTS/TOTAL` | `uel=10209` |
| `conference_league` | UEFA Conference League | yes | **missing** | reuse domestic union | cup-only clubs stay uncurated | none | `KXUECLGAME/BTTS/TOTAL` | `col=10437` |
| `copa_del_rey` | Copa del Rey | yes | **missing** | reuse La Liga | Spanish lower-division sides stay uncurated | none | `KXCOPADELREY*` | `cdr=10316` |
| `dfb_pokal` | DFB-Pokal | yes | **missing** | reuse Bundesliga | German lower-division sides stay uncurated | none | `KXDFBPOKAL*` | `dfb=10317` |
| `coppa_italia` | Coppa Italia | yes | **missing** | reuse Serie A | Italian lower-division sides stay uncurated | none | `KXCOPPAITALIA*` | `itc=10287` |
| `copa_libertadores` | Copa Libertadores | yes | **missing** | reuse Brazil + Argentina | `Barcelona` (Ecuador vs La Liga), `Nacional` (Uruguay vs Madeira) | none | `KXCONMEBOLLIB*` | `lib=10289` |

Existing curated coverage (do not regress): EPL, Championship, La Liga, Bundesliga, Serie A, plus English cup reuse and national teams.

Missing before this PR: the eleven domestic leagues listed in #434 plus the seven cup/continental scopes.

MLS / Liga MX remain #415's job.

## 3. Provider series identifiers already in the catalogue

These are already claimed on owner-live `target_competitions.py` from the 2026-09-20
provider matrix. This census re-checked them on 2026-09-21; they still resolve.

| Code | Matchbook aliases (catalogue) | Kalshi prefixes | Polymarket |
|---|---|---|---|
| `super_lig` | super lig, süper lig, turkish super lig, turkey süper lig, … | `KXSUPERLIGGAME/BTTS/TOTAL` title `Turkish Super Lig Game` | sport `tur` series `10292` name `Süper Lig` |
| `primeira_liga` | primeira liga, liga portugal, liga portugal betclic, … | `KXLIGAPORTUGALGAME/BTTS/TOTAL` title `Liga Portugal Game` | sport `por` series `10330` name `Primeira Liga` |
| `ligue_1` | ligue 1, french ligue 1, ligue 1 mc donalds, … | `KXLIGUE1GAME/BTTS/TOTAL/FTTS` | sport `fl1` series `10195` |
| `eredivisie` | eredivisie, dutch eredivisie, … | `KXEREDIVISIEGAME/BTTS/TOTAL` (short `KXEREDIVISIE` also matches Vrouwen; not used) | sport `ere` series `10286` |
| `scottish_premiership` | scottish premiership, william hill premiership, … | `KXSCOTTISHPREMGAME/BTTS/TOTAL` | sport `scop` series `10674` |
| `belgian_pro_league` | belgian pro league, jupiler pro league, … | `KXBELGIANPLGAME/BTTS/TOTAL` | sport `bel1` series `12351` |
| `league_one` | league one, efl league one, sky bet league one, … | `KXEFLL1GAME/BTTS/TOTAL` | sport `el1` series `11435` |
| `brasileirao` | brasileirao, brazilian serie a, campeonato brasileiro série a, … | `KXBRASILEIROGAME/BTTS/TOTAL/FTTS` (short ticker also matches B/C; not used) | sport `bra` series `10359` |
| `argentina_primera` | liga profesional, argentine primera, … | `KXARGPREMDIVGAME/BTTS/TOTAL` | sport `arg` series `10312` |
| `saudi_pro_league` | saudi pro league, roshn saudi league, … | `KXSAUDIPLGAME/BTTS/TOTAL` | sport `spl` series `10361` |
| `j1_league` | j1 league, j-league, meiji yasuda j1, … | `KXJLEAGUEGAME/BTTS/TOTAL` (`KXJ2LEAGUE*` not claimed) | sport `jap` series `10360` |

## 4. Current senior participants + provider-observed aliases

Aliases below are **observed**, not invented. Historical 2025/26 leftovers that
still appear on Kalshi (relegated clubs) are listed as leftover evidence and
are **not** added as current-universe canonicals unless they remain unique
identity-bearing names with no collision.

### 4.1 Turkish Süper Lig (priority 1)

TFF table 2026-09-21 (2 matches played): Alanyaspor, Amed, Başakşehir,
Beşiktaş, Çorum, Erzurumspor, Eyüpspor, Fenerbahçe, Galatasaray, Gaziantep,
Gençlerbirliği, Göztepe, Kasımpaşa, Kocaelispor, Konyaspor, Rizespor,
Samsunspor, Trabzonspor.

Provider-observed unique forms:

| Canonical | Unique aliases (add) | Fail closed |
|---|---|---|
| Galatasaray | Galatasaray A.Ş., Galatasaray Istanbul | Istanbul |
| Fenerbahce | Fenerbahçe, Fenerbahçe A.Ş., Fenerbahce Istanbul | Istanbul |
| Besiktas | Beşiktaş, Beşiktaş A.Ş., Besiktas Istanbul | Istanbul |
| Trabzonspor | Trabzonspor A.Ş. | |
| Samsunspor | Samsunspor A.Ş. | |
| Alanyaspor | Corendon Alanyaspor, CORENDON Alanyaspor | |
| Goztepe | Göztepe, Göztepe A.Ş., Goztepe Izmir | Izmir |
| Eyupspor | Eyüpspor | |
| Konyaspor | Tümosan Konyaspor, TÜMOSAN Konyaspor | |
| Rizespor | Caykur Rizespor, Çaykur Rizespor, Çaykur Rizespor A.Ş. | |
| Istanbul Basaksehir | Başakşehir, Istanbul Basaksehir, İstanbul Başakşehir FK, Rams Başakşehir FK, Basaksehir | Istanbul, Rams |
| Genclerbirligi | Gençlerbirliği, Genclerbirligi SK, Gençlerbirliği SK | |
| Kocaelispor | Kocaeli | |
| Gaziantep | Gaziantep FK, Gaziantep Futbol Kulübü A.Ş. | |
| Kasimpasa | Kasımpaşa, Kasımpaşa A.Ş., Kasimpasa Istanbul | Istanbul |
| Amed | Amed Sportif, Amed Sportif Faaliyetler, Amed SFK | |
| Erzurumspor | Erzurumspor FK, Erzurum | |
| Corum | Çorum FK, Arca Çorum FK, Corum FK | |

Kalshi example: `KXSUPERLIGGAME-26SEP20GOZRIZ` title `Goztepe Izmir vs Rizespor`.
Polymarket example: `Rams Başakşehir FK vs. Gençlerbirliği SK - Total Corners`.

Kalshi leftovers (not 2026/27 TFF table): Antalyaspor, Kayserispor, Karagumruk /
Fatih Karagumruk Istanbul. Left as UNKNOWN current-universe clubs.

### 4.2 Primeira Liga (priority 2)

Liga Portugal licensed 2026/27 Primeira side: Académico de Viseu, Alverca,
Arouca, Benfica, Casa Pia, Estoril, Estrela da Amadora, Famalicão, FC Porto,
Gil Vicente, Marítimo, Moreirense, Nacional, Rio Ave, Santa Clara, SC Braga,
Sporting CP, Vitória SC.

| Canonical | Unique aliases (add) | Fail closed |
|---|---|---|
| Benfica | SL Benfica, Sport Lisboa e Benfica | Lisbon |
| Sporting CP | Sporting Lisbon | Sporting, Lisbon |
| Porto | FC Porto | Porto as a *generic city token is still added* because Kalshi GAME titles use `Porto` for this club and no other current Primeira club is named Porto. Portimonense is Liga 2. Revisit if Portimonense is promoted. |
| Braga | SC Braga | |
| Guimaraes | Vitória SC, Vitoria SC, Vitoria SC Guimaraes, Vitória de Guimarães | Vitoria (Brazil collision) |
| Gil Vicente | Gil Vicente FC, Gil Vicente Barcelos, Vicente Barcelos | |
| Casa Pia | Casa Pia AC, Casa Pia Lisbon | Lisbon |
| Estoril | Estoril Praia, GD Estoril Praia | |
| Estrela Amadora | CF Estrela da Amadora, Estrela da Amadora | |
| Famalicao | FC Famalicão, FC Famalicao | |
| Alverca | FC Alverca, FC Alverca SAD | |
| Arouca | FC Arouca | |
| Rio Ave | Rio Ave FC | |
| Santa Clara | CD Santa Clara, Santa Clara Azores | |
| Nacional Madeira | CD Nacional, Nacional da Madeira | **Nacional** (Libertadores Club Nacional) |
| Maritimo | CS Marítimo, Marítimo | |
| Academico Viseu | Académico de Viseu, Académico de Viseu FC, Viseu | |
| Moreirense | Moreirense FC | |

Kalshi example: `KXLIGAPORTUGALGAME-26SEP20FCPBEN` title `Porto vs SL Benfica`.
Polymarket also used `Sport Lisboa e Benfica` and `Sporting CP`.

Leftovers (2025/26): AVS Futebol SAD, Tondela / CD Tondela. Not added.

### 4.3 Ligue 1

2026/27 (18): Angers, Auxerre, Brest, Le Havre, Le Mans, Lens, Lille, Lorient,
Lyon, Marseille, Monaco, Nice, Paris FC, Paris Saint-Germain, Rennes,
Strasbourg, Toulouse, Troyes.

Exact Kalshi sibling (2026-09-20):

- `KXLIGUE1GAME-26SEP20OLMPSG` `Marseille vs PSG`
- `KXLIGUE1FTTS-26SEP20OLMPSG` `Olympique Marseille vs Paris Saint-Germain: First Team to Score`

| Canonical | Unique aliases | Fail closed |
|---|---|---|
| Paris Saint-Germain | PSG | **Paris** |
| Paris FC | | **Paris** |
| Marseille | Olympique Marseille, Olympique de Marseille | |
| Lyon | Olympique Lyonnais, Olympique Lyon | |
| Lille | Lille OSC, LOSC Lille | |
| Monaco | AS Monaco, AS Monaco FC | |
| Nice | OGC Nice | |
| Lens | RC Lens, Racing Club De Lens | |
| Strasbourg | Strasbourg Alsace, RC Strasbourg, RC Strasbourg Alsace | |
| Rennes | Stade Rennais, Stade Rennais FC, Stade Rennes | |
| Brest | Stade Brest, Stade Brest 29, Stade Brestois 29 | |
| Angers | Angers SCO | |
| Auxerre | AJ Auxerre | |
| Toulouse | Toulouse FC | |
| Le Havre | Le Havre AC | |
| Lorient | FC Lorient | |
| Le Mans | Le Mans FC | |
| Troyes | ESTAC Troyes, ES Troyes AC | |

Leftovers: Metz, Nantes, Saint-Etienne (relegated). Not added.

### 4.4 Eredivisie

2026/27 (18): ADO Den Haag, Ajax, AZ, Cambuur, Excelsior, Feyenoord,
Fortuna Sittard, Go Ahead Eagles, Groningen, Heerenveen, NEC, PEC Zwolle,
PSV, Sparta Rotterdam, Telstar, Twente, Utrecht, Willem II.

| Canonical | Unique aliases | Fail closed |
|---|---|---|
| Ajax | Ajax Amsterdam, AFC Ajax | |
| PSV Eindhoven | PSV | Eindhoven |
| Feyenoord | Feyenoord Rotterdam | |
| AZ Alkmaar | AZ, Alkmaar | Alkmaar as unique is observed (`Alkmaar vs Enschede`); kept because no other Alkmaar club is in the registry |
| Twente | FC Twente, FC Twente Enschede, Enschede | Enschede city-only against unknown clubs |
| Sparta Rotterdam | | **Sparta** (Sparta Prague) |
| NEC Nijmegen | NEC, Nijmegen | |
| Go Ahead Eagles | GA Eagles | |
| Utrecht | FC Utrecht | |
| Groningen | FC Groningen | |
| Heerenveen | SC Heerenveen | |
| Fortuna Sittard | Sittard | |
| Excelsior | Excelsior Rotterdam | |
| PEC Zwolle | Zwolle | |
| Telstar | SC Telstar, Telstar 1963 | |
| Willem II | Willem II Tilburg | |
| ADO Den Haag | Den Haag | |
| Cambuur | SC Cambuur, SC Cambuur-Leeuwarden | |

### 4.5 Scottish Premiership

2026/27 (12): Aberdeen, Celtic, Dundee, Dundee United, Falkirk,
Heart of Midlothian, Hibernian, Kilmarnock, Motherwell, Rangers,
St Johnstone, St Mirren.

| Canonical | Unique aliases | Fail closed |
|---|---|---|
| Celtic | Celtic FC | |
| Rangers | Rangers FC | |
| Dundee | Dundee FC | bare `Dundee` is **the canonical** for Dundee FC. `Dundee United` must stay a different canonical. |
| Dundee United | Dundee United FC | |
| Heart of Midlothian | Hearts, Heart of Midlothian FC | |
| Hibernian | Hibernian FC, Hibs | |
| Aberdeen | Aberdeen FC | |
| Motherwell | Motherwell FC | |
| Kilmarnock | Kilmarnock FC | |
| Falkirk | Falkirk FC | |
| St Mirren | St. Mirren, St Mirren FC | |
| St Johnstone | St. Johnstone, St Johnstone FC | |

Same-city rule: `Dundee` ≠ `Dundee United`.

### 4.6 Belgian Pro League

2026/27 (18): Anderlecht, Antwerp, Beveren, Cercle Brugge, Charleroi,
Club Brugge, Genk, Gent, Kortrijk, La Louvière, Lommel, Mechelen,
OH Leuven, Sint-Truiden, Standard Liège, Union SG, Westerlo, Zulte Waregem.

| Canonical | Unique aliases | Fail closed |
|---|---|---|
| Club Brugge | | **Brugge**, **Bruges** |
| Cercle Brugge | | **Brugge**, **Bruges** |
| Anderlecht | RSC Anderlecht | |
| Union Saint-Gilloise | Union Gilloise, Union SG | Union |
| Standard Liege | Standard Liège | **Standard** |
| Gent | KAA Gent | |
| Genk | KRC Genk | |
| Royal Antwerp | Royal Antwerp FC, Antwerp | |
| Charleroi | Royal Charleroi, Royal Charleroi SC | |
| Mechelen | Yellow-Red KV Mechelen | |
| Westerlo | KVC Westerlo | |
| Sint-Truiden | St. Truidense, St. Truidense VV | |
| OH Leuven | Leuven, Oud-Heverlee Leuven | |
| Zulte Waregem | SV Zulte Waregem | |
| La Louviere | RAAL La Louviere, La Louvière | |
| Dender leftover / Beveren / Lommel / Kortrijk | FCV Dender EH, Lommel SK | |

Same-city rule: Club Brugge ≠ Cercle Brugge. Gent ≠ Genk.

### 4.7 EFL League One

2026/27 (24): AFC Wimbledon, Barnsley, Blackpool, Bradford City, Bromley,
Burton Albion, Cambridge United, Doncaster Rovers, Huddersfield Town,
Leicester City, Leyton Orient, Luton Town, Mansfield Town, Milton Keynes Dons,
Notts County, Oxford United, Peterborough United, Plymouth Argyle, Reading,
Sheffield Wednesday, Stevenage, Stockport County, Wigan Athletic,
Wycombe Wanderers.

Overlapping Championship canonicals are reused (`Barnsley`, `Blackpool`,
`Huddersfield Town`, `Leicester City`, `Luton Town`, `Oxford United`,
`Peterborough United`, `Plymouth Argyle`, `Reading`, `Sheffield Wednesday`,
`Wigan Athletic`). New unique aliases only for League One-only clubs.

Notts County is **not** Nottingham Forest. `Notts` is observed; it is unique
enough vs Forest.

### 4.8 Brazilian Série A

2026 (20): Athletico Paranaense, Atlético Mineiro, Bahia, Botafogo,
Chapecoense, Corinthians, Coritiba, Cruzeiro, Flamengo, Fluminense, Grêmio,
Internacional, Mirassol, Palmeiras, Red Bull Bragantino, Remo, Santos,
São Paulo, Vasco da Gama, Vitória.

Unique legal/state suffixes from Kalshi FTTS titles are added
(`CR Flamengo RJ`, `SE Palmeiras SP`, …). Bare `Vitoria` is **not** added
(Vitória SC Guimarães). Bare `Nacional` is **not** added.

### 4.9 Argentine Primera División

2026 (30), including same-city / same-token pairs that must stay distinct:

- Estudiantes (LP) ≠ Estudiantes (RC)
- Gimnasia (LP) ≠ Gimnasia (M)
- Independiente (Avellaneda) ≠ Independiente Rivadavia
- Newell's Old Boys ≠ Rosario Central (`Rosario` fail closed)
- Atlético Tucumán (`Tucuman` fail closed)

Matchbook currently lists Liga Profesional match events
(`Aldosivi vs Atlético Tucumán`, `Lanus vs Estudiantes de La Plata`).

### 4.10 Saudi Pro League

2026/27 (18): Abha, Al-Ahli, Al-Diriyah, Al-Ettifaq, Al-Faisaly, Al-Fateh,
Al-Fayha, Al-Hazem, Al-Hilal, Al-Ittihad, Al-Khaleej, Al-Kholood, Al-Nassr,
Al-Qadsiah, Al-Riyadh, Al-Shabab, Al-Taawoun, Neom.

Observed unique forms: `Al Hilal`, `Al Nassr`, `Al Nassr Club`,
`Al Ahli Saudi`, `Al-Ittihad Club`, `Al Qadsiah`, `Neom SC`,
`Al Ettifaq Saudi Club`, `Al Fateh Saudi Club`, `Al Riyadh Saudi Club`.

`AL Suqoor` appears as a Kalshi GAME/TOTAL nickname and is **not** mapped.

### 4.11 J1 League

Provider long forms observed: Kashima Antlers, Urawa Red Diamonds,
Kawasaki Frontale, Yokohama F. Marinos, Yokohama FC, FC Tokyo, Tokyo Verdy,
Vissel Kobe, Gamba Osaka, Cerezo Osaka, Nagoya Grampus, Sanfrecce Hiroshima,
Kashiwa Reysol, Avispa Fukuoka, Kyoto Sanga, Machida Zelvia,
Shimizu S-Pulse, Fagiano Okayama, Albirex Niigata, Shonan Bellmare,
Mito Hollyhock, JEF United Chiba.

Fail closed: `Tokyo` (FC Tokyo vs Tokyo Verdy), `Yokohama` (Marinos vs Yokohama FC),
`Osaka` (Gamba vs Cerezo). Unique nicknames `Gamba`, `Cerezo`, `Avispa`,
`Kashima`, `Urawa`, `Marinos` are observed and unique in this registry.

## 5. Ambiguous aliases that must fail closed

Do **not** add these as global unique aliases:

| Token | Why |
|---|---|
| `Istanbul` | Galatasaray, Fenerbahçe, Beşiktaş, Başakşehir, Kasımpaşa |
| `Paris` | PSG vs Paris FC. Kalshi GAME used `Paris`; FTTS used `Paris Saint-Germain` / `Paris FC` |
| `Sporting` | Sporting CP vs Sporting Kansas City (MLS/#415) |
| `Nacional` | CD Nacional (Madeira) vs Club Nacional (Uruguay, Libertadores) |
| `Sparta` | Sparta Rotterdam vs Sparta Prague |
| `Standard` | Standard Liège vs other Standard clubs |
| `Brugge` / `Bruges` | Club Brugge vs Cercle Brugge |
| `Union` | Union SG vs Union Berlin vs Unión Santa Fe |
| `Barcelona` on Libertadores | Barcelona SC (Ecuador) vs FC Barcelona |
| `Estudiantes` / `Gimnasia` / `Independiente` / `Rosario` | multiple Argentine clubs |
| `Tucuman` / `Mendoza` / `Junin` | city tokens covering more than one club or losing legal form |
| `Vitoria` | EC Vitória (Brazil) vs Vitória SC (Portugal) |
| `Tokyo` / `Yokohama` / `Osaka` | two J1 clubs each |
| `AL Suqoor` | Kalshi nickname; club not identified from evidence |
| `Chelsea Women`, `Galatasaray U21`, `Ajax Vrouwen`, `PAOK B` | youth / women / reserves |
| Sponsor-only remainder (`Rams`, `Corendon`, `Tümosan`, `Arca`) | identity-bearing only when attached to the club name |

Youth / women / reserve examples actually observed in this Matchbook snapshot:

- `AFC Bournemouth U21 vs Stoke City U21` (`England Premier League 2`)
- `Bolton Wanderers FC U21 vs Queens Park Rangers U21` (`England Professional Development League`)
- `PAOK B vs Nestos Chrysoupoli FC` (`Greece Super League 2`)
- `PFC Ludogorets Razgrad II`

Those must not collapse onto senior canonicals.

## 6. Recommended registry architecture

**Domestic leagues:** keep competition-keyed `SeniorClub` tables, as #316/#415
already do. Unique aliases are global. Generic aliases wait for #415's
`generic_aliases` + `resolve_team_name_for_competition`.

**Cups / continental:** do **not** duplicate domestic club rows. Reuse:

- Copa del Rey → La Liga senior set (lower-division Spanish sides stay uncurated)
- DFB-Pokal → Bundesliga senior set
- Coppa Italia → Serie A senior set
- UCL / UEL / UECL → union of curated domestic tables (same canonical club ID
  across Super Lig and UCL for Galatasaray)
- Copa Libertadores → Brazil + Argentina union, **without** copying La Liga
  `Barcelona` onto Barcelona SC

A season-scoped participant list for UCL would be more precise (qualifiers,
playoff losers), but it churns every round and is not required for unique-alias
collapse of clubs already in a domestic table. Unknown cup participants fail
closed. That is cleaner than inventing a second club table.

Do **not** lower EventMatcher 0.92 on owner-live. PAPER 0.80 stays #415.

## 7. Tests required

For every implemented domestic competition:

1. Exact provider aliases collapse to one fixture / one canonical team.
2. Generic aliases stay unresolved (fail closed) without #415.
3. Same-city / similar-name clubs remain distinct.
4. Youth / women / reserves fail closed.
5. Super Lig vs UCL with the same clubs + kickoff must not match *solely*
   because labels look similar (`competition_score=0` → 0.90 < 0.92).
6. Inclusive 5-minute kickoff tolerance unchanged (300s; 5:00 matches, 5:01 does not).
7. EPL / La Liga / Bundesliga / Serie A identity tests stay green.

## 8. Implementation boundary

Included after this evidence commit, only if additive and unique:

- static `SeniorClub` tables + cup reuse mappings;
- deterministic identity tests.

Not included:

- #415 generic_aliases / PAPER 0.80 / MLS / Liga MX;
- scanner / capture / economics / settlement / concurrency;
- venue writes; owner-live movement; merge.
