from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.text import normalize_text


class TargetCompetitionCode(StrEnum):
    PREMIER_LEAGUE = "premier_league"
    CHAMPIONSHIP = "championship"
    LEAGUE_ONE = "league_one"
    LEAGUE_TWO = "league_two"
    LA_LIGA = "la_liga"
    COPA_DEL_REY = "copa_del_rey"
    CARABAO_CUP = "carabao_cup"
    FA_CUP = "fa_cup"
    INTERNATIONAL_FRIENDLIES = "international_friendlies"
    BUNDESLIGA = "bundesliga"
    DFB_POKAL = "dfb_pokal"
    SERIE_A = "serie_a"
    COPPA_ITALIA = "coppa_italia"
    LIGUE_1 = "ligue_1"
    EREDIVISIE = "eredivisie"
    PRIMEIRA_LIGA = "primeira_liga"
    SCOTTISH_PREMIERSHIP = "scottish_premiership"
    BELGIAN_PRO_LEAGUE = "belgian_pro_league"
    CHAMPIONS_LEAGUE = "champions_league"
    EUROPA_LEAGUE = "europa_league"
    CONFERENCE_LEAGUE = "conference_league"
    UEFA_NATIONS_LEAGUE = "uefa_nations_league"
    SUPER_LIG = "super_lig"
    MLS = "mls"
    LIGA_MX = "liga_mx"
    BRASILEIRAO = "brasileirao"
    ARGENTINA_PRIMERA = "argentina_primera"
    COPA_LIBERTADORES = "copa_libertadores"
    SAUDI_PRO_LEAGUE = "saudi_pro_league"
    J1_LEAGUE = "j1_league"
    SOUTH_AFRICAN_PREMIERSHIP = "south_african_premiership"
    NFL = "nfl"
    NBA = "nba"
    NCAAB = "ncaab"
    MLB = "mlb"
    ATP = "atp"
    WTA = "wta"


class VenueMappingStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


PRINCIPAL_OPERATOR_COMPETITION_COUNT = 37
VERIFIED_ALL_3 = "VERIFIED_ALL_3"
PARTIAL_PROVIDER_MAPPING = "PARTIAL"
PROVIDER_MATRIX_RETRIEVED_AT = "2026-09-22"


class TargetCompetition(BaseModel):
    code: TargetCompetitionCode
    display_name: str
    aliases: tuple[str, ...] = Field(default_factory=tuple)
    polymarket_gamma_series_id: str | None = None
    polymarket_gamma_sport: str | None = None
    kalshi_series_prefixes: tuple[str, ...] = Field(default_factory=tuple)


# Provider coverage is claimed only from read-only metadata. Empty series_id /
# empty Kalshi prefixes means Sports Hedge must not invent that venue's markets.
# Selectable operator rows require VERIFIED_ALL_3: Matchbook label aliases +
# Kalshi match-level series + Polymarket Gamma series. Partial rows stay visible
# and disabled.
#
# Public Gamma GET /sports (retrieved 2026-09-16, FA Cup re-checked 2026-09-20):
#   epl=10188, elc=10355, lal=10193, efl=10329 (EFL CUP), efa=10314 (FA Cup;
#   2026-09-16 snapshot was 10307, which is no longer listed),
#   fif=10238 (FIFA Friendlies), bun=10194 (Bundesliga), sea=10203 (Serie A).
# Public Gamma GET /sports (retrieved 2026-09-20) additional football series:
#   ucl=10204, uel=10209, col=10437, tur=10292, mls=10189,
#   el1=11435 (League One), el2=11436 (League Two), cdr=10316, dfb=10317,
#   itc=10287, fl1=10195, ere=10286, por=10330, scop=10674, bel1=12351,
#   mex=10290, bra=10359, arg=10312, lib=10289, spl=10361, jap=10360,
#   saf1=12360 (South Africa Premiership).
# Public Gamma GET /sports (retrieved 2026-09-22): unl=11446 UEFA Nations League.
# Near-neighbor Gamma series left unmatched: bl2 (2. Bundesliga), itsb (Serie B),
# clf (Club Friendlies), ecu1 (LigaPro Serie A), uwcl (Women's UCL),
# tur2 (Turkey 1. Lig), ja2/j2100 (J2), bra2/bra3, fr2 (Ligue 2),
# conl=10673 CONCACAF Nations League.
# Polymarket group/champion/relegation outrights (e.g. event 994203
# uefa-nations-league-winner-2026-27) have series=null / sport=null and are
# not in Gamma series 11446. They stay out of the fixture pipeline.
#
# Public Kalshi GET /series category=Sports (retrieved 2026-09-20) match-level
# GAME/BTTS/TOTAL/(FTTS where present). Short prefixes are not used when they
# would also match a neighbour (women's, All-Star, 2. Bundesliga, Serie B/C,
# Ligue 2, J2, Sudamericana).
# Public Kalshi GET /series category=Sports (retrieved 2026-09-22) UEFA
# Nations League match-level GAME/BTTS/TOTAL/FTTS:
#   KXUEFANLGAME, KXUEFANLBTTS, KXUEFANLTOTAL, KXUEFANLFTTS.
# Observed but not admitted (not already soccer-approved families):
#   KXUEFANLSPREAD, KXUEFANL1H*, KXUEFANLTEAMTOTAL, KXUEFANLSCORE,
#   KXUEFANLADVANCE, KXUEFANLMOV. Season series KXUEFANL is not a fixture.
# Neighbor KXCONCACAFNL is CONCACAF outrights, not UEFA fixtures.
# Not present on that listing and therefore not invented:
#   EFL League Two match-level series, South African Premiership match-level series.
#
# Aliases include observed Matchbook / Gamma / Kalshi label shapes. Matching is
# exact after normalize_text; unknown labels fail closed. Matchbook has no
# competition-ID filter — verification is label-alias matching only.
TARGET_COMPETITIONS: tuple[TargetCompetition, ...] = (
    TargetCompetition(
        code=TargetCompetitionCode.PREMIER_LEAGUE,
        display_name="English Premier League",
        aliases=(
            "premier league",
            "english premier league",
            "england premier league",
            "eng premier league",
            "the premier league",
            "epl",
            "barclays premier league",
            "barclays premiership",
            "premier league england",
            "premier league 2025/26",
            "premier league 2026/27",
            "premier league 2026",
            "english premier league 2025/26",
            "english premier league 2026/27",
        ),
        polymarket_gamma_series_id="10188",
        polymarket_gamma_sport="epl",
        kalshi_series_prefixes=("KXEPL",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CHAMPIONSHIP,
        display_name="EFL Championship",
        aliases=(
            "championship",
            "the championship",
            "efl championship",
            "english championship",
            "england championship",
            "sky bet championship",
            "skybet championship",
            "english football league championship",
            "efl champ",
            "championship 2025/26",
            "efl championship 2025/26",
            "efl championship 2026/27",
        ),
        polymarket_gamma_series_id="10355",
        polymarket_gamma_sport="elc",
        kalshi_series_prefixes=("KXEFLCHAMPIONSHIP",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LA_LIGA,
        display_name="Spain La Liga",
        aliases=(
            "la liga",
            "laliga",
            "la liga santander",
            "la liga ea sports",
            "laliga ea sports",
            "primera division",
            "primera división",
            "spanish primera",
            "spanish primera division",
            "spanish la liga",
            "spain la liga",
            "spain primera division",
            "primera división de españa",
            "la liga 2025/26",
            "la liga 2026/27",
        ),
        polymarket_gamma_series_id="10193",
        polymarket_gamma_sport="lal",
        kalshi_series_prefixes=("KXLALIGA",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CARABAO_CUP,
        display_name="Carabao Cup",
        aliases=(
            "carabao cup",
            "the carabao cup",
            "efl cup",
            "the efl cup",
            "league cup",
            "the league cup",
            "english league cup",
            "england league cup",
            "football league cup",
            "english football league cup",
            "carabao cup 2026/27",
            "efl cup 2026/27",
            "league cup 2026/27",
        ),
        polymarket_gamma_series_id="10329",
        polymarket_gamma_sport="efl",
        kalshi_series_prefixes=("KXEFLCUP",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.FA_CUP,
        display_name="FA Cup",
        aliases=(
            "fa cup",
            "the fa cup",
            "emirates fa cup",
            "the emirates fa cup",
            "english fa cup",
            "england fa cup",
            "fa cup 2026/27",
            "emirates fa cup 2026/27",
        ),
        polymarket_gamma_series_id="10314",
        polymarket_gamma_sport="efa",
        kalshi_series_prefixes=("KXFACUP",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.INTERNATIONAL_FRIENDLIES,
        display_name="International Friendlies",
        aliases=(
            "international friendlies",
            "international friendly",
            "fifa friendlies",
            "fifa friendly",
            "senior international friendlies",
            "senior international friendly",
            "mens international friendlies",
            "men's international friendlies",
            "senior mens international friendlies",
            "senior men's international friendlies",
            "international friendly matches",
        ),
        polymarket_gamma_series_id="10238",
        polymarket_gamma_sport="fif",
        kalshi_series_prefixes=("KXINTLFRIENDLY",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.BUNDESLIGA,
        display_name="Bundesliga",
        aliases=(
            "bundesliga",
            "german bundesliga",
            "germany bundesliga",
            "1 bundesliga",
            "bundesliga 1",
            "fussball bundesliga",
            "fußball-bundesliga",
            "bundesliga 2026/27",
            "german bundesliga 2026/27",
        ),
        polymarket_gamma_series_id="10194",
        polymarket_gamma_sport="bun",
        # Match-level prefixes only. Short KXBUNDESLIGA also matches 2. Bundesliga.
        kalshi_series_prefixes=(
            "KXBUNDESLIGAGAME",
            "KXBUNDESLIGABTTS",
            "KXBUNDESLIGATOTAL",
            "KXBUNDESLIGAFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.SERIE_A,
        display_name="Serie A",
        aliases=(
            "serie a",
            "italian serie a",
            "italy serie a",
            "serie a italy",
            "serie a tim",
            "serie a enilive",
            "lega serie a",
            "serie a 2026/27",
            "italian serie a 2026/27",
        ),
        polymarket_gamma_series_id="10203",
        polymarket_gamma_sport="sea",
        # Match-level prefixes only. Short KXSERIEA also matches Serie A Femminile.
        kalshi_series_prefixes=(
            "KXSERIEAGAME",
            "KXSERIEABTTS",
            "KXSERIEATOTAL",
            "KXSERIEAFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CHAMPIONS_LEAGUE,
        display_name="UEFA Champions League",
        aliases=(
            "champions league",
            "uefa champions league",
            "ucl",
            "european cup",
            "champions league 2025/26",
            "champions league 2026/27",
            "uefa champions league 2026/27",
        ),
        polymarket_gamma_series_id="10204",
        polymarket_gamma_sport="ucl",
        # Match-level only. Short KXUCL also matches KXUCLW* women's series.
        kalshi_series_prefixes=(
            "KXUCLGAME",
            "KXUCLBTTS",
            "KXUCLTOTAL",
            "KXUCLFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.EUROPA_LEAGUE,
        display_name="UEFA Europa League",
        aliases=(
            "europa league",
            "uefa europa league",
            "uel",
            "europa league 2025/26",
            "europa league 2026/27",
            "uefa europa league 2026/27",
        ),
        polymarket_gamma_series_id="10209",
        polymarket_gamma_sport="uel",
        kalshi_series_prefixes=(
            "KXUELGAME",
            "KXUELBTTS",
            "KXUELTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CONFERENCE_LEAGUE,
        display_name="UEFA Conference League",
        aliases=(
            "conference league",
            "uefa conference league",
            "uefa europa conference league",
            "europa conference league",
            "uecl",
            "conference league 2025/26",
            "conference league 2026/27",
        ),
        polymarket_gamma_series_id="10437",
        polymarket_gamma_sport="col",
        kalshi_series_prefixes=(
            "KXUECLGAME",
            "KXUECLBTTS",
            "KXUECLTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.UEFA_NATIONS_LEAGUE,
        display_name="UEFA Nations League",
        aliases=(
            "uefa nations league",
            "nations league",
            "unl",
            "uefa nations league 2026/27",
            "uefa nations league 2026 27",
            # Live Matchbook COMPETITION meta-tags 2026-09-22 (soccer id=15):
            # Netherlands vs Germany uses "UEFA Nations League A";
            # Austria vs Israel uses "UEFA Nations League B";
            # Andorra vs Malta uses "UEFA Nations League D".
            # League C was not on that open snapshot and is not registered
            # until an observed COMPETITION tag exists.
            "uefa nations league a",
            "uefa nations league b",
            "uefa nations league d",
        ),
        polymarket_gamma_series_id="11446",
        polymarket_gamma_sport="unl",
        # Complete match-level prefixes only. Short KXUEFANL also matches the
        # season series KXUEFANL and 1H/score/spread/advance neighbors.
        kalshi_series_prefixes=(
            "KXUEFANLGAME",
            "KXUEFANLBTTS",
            "KXUEFANLTOTAL",
            "KXUEFANLFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.SUPER_LIG,
        display_name="Turkish Süper Lig",
        aliases=(
            "super lig",
            "süper lig",
            "superlig",
            "turkish super lig",
            "turkish süper lig",
            "turkey super lig",
            "turkey süper lig",
            "super lig turkey",
            "süper lig 2025/26",
            "süper lig 2026/27",
        ),
        polymarket_gamma_series_id="10292",
        polymarket_gamma_sport="tur",
        kalshi_series_prefixes=(
            "KXSUPERLIGGAME",
            "KXSUPERLIGBTTS",
            "KXSUPERLIGTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.MLS,
        display_name="Major League Soccer",
        aliases=(
            "mls",
            "major league soccer",
            "mls soccer",
            "american mls",
            "usa mls",
            # Live Matchbook meta-tag type=COMPETITION on
            # Inter Miami CF vs San Diego FC (id 34333851245100081, 2026-09-20).
            "us major league soccer",
            "mls 2026",
            "major league soccer 2026",
        ),
        polymarket_gamma_series_id="10189",
        polymarket_gamma_sport="mls",
        # Match-level only. Short KXMLS also matches KXMLSAST* All-Star series.
        kalshi_series_prefixes=(
            "KXMLSGAME",
            "KXMLSBTTS",
            "KXMLSTOTAL",
            "KXMLSFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LEAGUE_ONE,
        display_name="EFL League One",
        aliases=(
            "league one",
            "efl league one",
            "english league one",
            "england league one",
            "sky bet league one",
            "skybet league one",
            "league 1",
            "efl league 1",
            "league one 2025/26",
            "league one 2026/27",
        ),
        polymarket_gamma_series_id="11435",
        polymarket_gamma_sport="el1",
        kalshi_series_prefixes=(
            "KXEFLL1GAME",
            "KXEFLL1BTTS",
            "KXEFLL1TOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LEAGUE_TWO,
        display_name="EFL League Two",
        aliases=(
            "league two",
            "efl league two",
            "english league two",
            "england league two",
            "sky bet league two",
            "skybet league two",
            "league 2",
            "efl league 2",
            "league two 2025/26",
            "league two 2026/27",
        ),
        polymarket_gamma_series_id="11436",
        polymarket_gamma_sport="el2",
        # Kalshi match-level series were not present on public GET /series 2026-09-20.
        kalshi_series_prefixes=(),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.COPA_DEL_REY,
        display_name="Copa del Rey",
        aliases=(
            "copa del rey",
            "the copa del rey",
            "spanish copa del rey",
            "spain copa del rey",
            "copa del rey 2025/26",
            "copa del rey 2026/27",
        ),
        polymarket_gamma_series_id="10316",
        polymarket_gamma_sport="cdr",
        kalshi_series_prefixes=(
            "KXCOPADELREYGAME",
            "KXCOPADELREYBTTS",
            "KXCOPADELREYTOTAL",
            "KXCOPADELREYFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.DFB_POKAL,
        display_name="DFB-Pokal",
        aliases=(
            "dfb pokal",
            "dfb-pokal",
            "german cup",
            "germany dfb pokal",
            "dfb pokal 2025/26",
            "dfb pokal 2026/27",
        ),
        polymarket_gamma_series_id="10317",
        polymarket_gamma_sport="dfb",
        kalshi_series_prefixes=(
            "KXDFBPOKALGAME",
            "KXDFBPOKALBTTS",
            "KXDFBPOKALTOTAL",
            "KXDFBPOKALFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.COPPA_ITALIA,
        display_name="Coppa Italia",
        aliases=(
            "coppa italia",
            "italian cup",
            "italy coppa italia",
            "coppa italia 2025/26",
            "coppa italia 2026/27",
        ),
        polymarket_gamma_series_id="10287",
        polymarket_gamma_sport="itc",
        kalshi_series_prefixes=(
            "KXCOPPAITALIAGAME",
            "KXCOPPAITALIABTTS",
            "KXCOPPAITALIATOTAL",
            "KXCOPPAITALIAFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LIGUE_1,
        display_name="Ligue 1",
        aliases=(
            "ligue 1",
            "ligue1",
            "french ligue 1",
            "france ligue 1",
            "ligue 1 uber eats",
            "ligue 1 mc donalds",
            "ligue 1 2025/26",
            "ligue 1 2026/27",
        ),
        polymarket_gamma_series_id="10195",
        polymarket_gamma_sport="fl1",
        kalshi_series_prefixes=(
            "KXLIGUE1GAME",
            "KXLIGUE1BTTS",
            "KXLIGUE1TOTAL",
            "KXLIGUE1FTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.EREDIVISIE,
        display_name="Eredivisie",
        aliases=(
            "eredivisie",
            "dutch eredivisie",
            "netherlands eredivisie",
            "holland eredivisie",
            "eredivisie 2025/26",
            "eredivisie 2026/27",
        ),
        polymarket_gamma_series_id="10286",
        polymarket_gamma_sport="ere",
        # Match-level only. Short KXEREDIVISIE also matches Eredivisie Vrouwen.
        kalshi_series_prefixes=(
            "KXEREDIVISIEGAME",
            "KXEREDIVISIEBTTS",
            "KXEREDIVISIETOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.PRIMEIRA_LIGA,
        display_name="Primeira Liga",
        aliases=(
            "primeira liga",
            "liga portugal",
            "liga portugal betclic",
            "portuguese primeira liga",
            "portugal primeira liga",
            "primeira liga 2025/26",
            "primeira liga 2026/27",
        ),
        polymarket_gamma_series_id="10330",
        polymarket_gamma_sport="por",
        kalshi_series_prefixes=(
            "KXLIGAPORTUGALGAME",
            "KXLIGAPORTUGALBTTS",
            "KXLIGAPORTUGALTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.SCOTTISH_PREMIERSHIP,
        display_name="Scottish Premiership",
        aliases=(
            "scottish premiership",
            "cinch premiership",
            "william hill premiership",
            "scotland premiership",
            "scottish premiership 2025/26",
            "scottish premiership 2026/27",
        ),
        polymarket_gamma_series_id="10674",
        polymarket_gamma_sport="scop",
        kalshi_series_prefixes=(
            "KXSCOTTISHPREMGAME",
            "KXSCOTTISHPREMBTTS",
            "KXSCOTTISHPREMTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.BELGIAN_PRO_LEAGUE,
        display_name="Belgian Pro League",
        aliases=(
            "belgian pro league",
            "belgium pro league",
            "jupiler pro league",
            "pro league belgium",
            "belgian first division a",
            "belgian pro league 2025/26",
            "belgian pro league 2026/27",
        ),
        polymarket_gamma_series_id="12351",
        polymarket_gamma_sport="bel1",
        kalshi_series_prefixes=(
            "KXBELGIANPLGAME",
            "KXBELGIANPLBTTS",
            "KXBELGIANPLTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LIGA_MX,
        display_name="Liga MX",
        aliases=(
            "liga mx",
            "mexican liga mx",
            "mexico liga mx",
            "liga mx apertura",
            "liga mx clausura",
            "liga mx 2026",
        ),
        polymarket_gamma_series_id="10290",
        polymarket_gamma_sport="mex",
        kalshi_series_prefixes=(
            "KXLIGAMXGAME",
            "KXLIGAMXBTTS",
            "KXLIGAMXTOTAL",
            "KXLIGAMXFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.BRASILEIRAO,
        display_name="Brazilian Série A",
        aliases=(
            "brasileirao",
            "brasileirão",
            "brasileirao serie a",
            "brasileirão série a",
            "brazilian serie a",
            "brazil serie a",
            "campeonato brasileiro serie a",
            "campeonato brasileiro série a",
            "brasileirao 2026",
        ),
        polymarket_gamma_series_id="10359",
        polymarket_gamma_sport="bra",
        # Match-level Serie A only. Short KXBRASILEIRO also matches Serie B/C.
        kalshi_series_prefixes=(
            "KXBRASILEIROGAME",
            "KXBRASILEIROBTTS",
            "KXBRASILEIROTOTAL",
            "KXBRASILEIROFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.ARGENTINA_PRIMERA,
        display_name="Argentine Primera División",
        aliases=(
            "liga profesional",
            "liga profesional de futbol",
            "liga profesional de fútbol",
            "argentine primera",
            "argentina primera",
            "primera division argentina",
            "primera división argentina",
            "liga profesional 2026",
        ),
        polymarket_gamma_series_id="10312",
        polymarket_gamma_sport="arg",
        kalshi_series_prefixes=(
            "KXARGPREMDIVGAME",
            "KXARGPREMDIVBTTS",
            "KXARGPREMDIVTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.COPA_LIBERTADORES,
        display_name="Copa Libertadores",
        aliases=(
            "copa libertadores",
            "conmebol libertadores",
            "libertadores",
            "copa libertadores 2026",
        ),
        polymarket_gamma_series_id="10289",
        polymarket_gamma_sport="lib",
        kalshi_series_prefixes=(
            "KXCONMEBOLLIBGAME",
            "KXCONMEBOLLIBBTTS",
            "KXCONMEBOLLIBTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.SAUDI_PRO_LEAGUE,
        display_name="Saudi Pro League",
        aliases=(
            "saudi pro league",
            "roshn saudi league",
            "saudi professional league",
            "spl saudi",
            "saudi pro league 2025/26",
            "saudi pro league 2026/27",
        ),
        polymarket_gamma_series_id="10361",
        polymarket_gamma_sport="spl",
        kalshi_series_prefixes=(
            "KXSAUDIPLGAME",
            "KXSAUDIPLBTTS",
            "KXSAUDIPLTOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.J1_LEAGUE,
        display_name="J1 League",
        aliases=(
            "j1 league",
            "j-league",
            "j league",
            "japan j1",
            "japanese j1 league",
            "meiji yasuda j1",
            "j1 league 2026",
        ),
        polymarket_gamma_series_id="10360",
        polymarket_gamma_sport="jap",
        # Match-level J1 only. KXJ2LEAGUE* is J2 and is not claimed here.
        kalshi_series_prefixes=(
            "KXJLEAGUEGAME",
            "KXJLEAGUEBTTS",
            "KXJLEAGUETOTAL",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.SOUTH_AFRICAN_PREMIERSHIP,
        display_name="South African Premiership",
        aliases=(
            "south african premiership",
            "south africa premiership",
            "premier soccer league south africa",
            "south african premiership 2025/26",
            "south african premiership 2026/27",
        ),
        polymarket_gamma_series_id="12360",
        polymarket_gamma_sport="saf1",
        # Kalshi match-level series were not present on public GET /series 2026-09-20.
        kalshi_series_prefixes=(),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.NFL,
        display_name="NFL",
        aliases=(
            "nfl",
            "national football league",
            "pro football",
            "american football nfl",
        ),
        polymarket_gamma_series_id="12185",
        polymarket_gamma_sport="nfl",
        kalshi_series_prefixes=("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.NBA,
        display_name="NBA",
        aliases=(
            "nba",
            "national basketball association",
            "pro basketball",
            "pro basketball (m)",
            "nba basketball",
        ),
        polymarket_gamma_series_id="10345",
        polymarket_gamma_sport="nba",
        kalshi_series_prefixes=("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.NCAAB,
        display_name="NCAA Men's Basketball",
        aliases=(
            "ncaab",
            "ncaa men",
            "ncaa men's basketball",
            "ncaa mens basketball",
            "college basketball",
            "cbb",
            "ncaa cbb",
            "ncaa division i men's basketball",
            "ncaamb",
        ),
        polymarket_gamma_series_id="10470",
        polymarket_gamma_sport="cbb",
        kalshi_series_prefixes=("KXNCAAMBGAME", "KXNCAAMBSPREAD", "KXNCAAMBTOTAL"),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.MLB,
        display_name="MLB",
        aliases=(
            "mlb",
            "major league baseball",
            "pro baseball",
        ),
        polymarket_gamma_series_id="3",
        polymarket_gamma_sport="mlb",
        # Stage-1 families only. KXMLBSPREAD and inning/series/futures tickers
        # are observed and deliberately not registered.
        kalshi_series_prefixes=("KXMLBGAME", "KXMLBTOTAL"),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.ATP,
        display_name="ATP",
        aliases=(
            "atp",
            "atp tour",
            "atp hangzhou",
            "hangzhou open",
            "atp chengdu",
            "chengdu open",
        ),
        polymarket_gamma_series_id="10365",
        polymarket_gamma_sport="atp",
        # Exact match series only. KXATPGAME and challenger/doubles series are excluded.
        kalshi_series_prefixes=("KXATPMATCH",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.WTA,
        display_name="WTA",
        aliases=(
            "wta",
            "wta tour",
            "wta singapore",
            "singapore open",
            "wta seoul",
        ),
        polymarket_gamma_series_id="10366",
        polymarket_gamma_sport="wta",
        kalshi_series_prefixes=("KXWTAMATCH",),
    ),
)

_ALIAS_INDEX: dict[str, TargetCompetition] = {}
for _item in TARGET_COMPETITIONS:
    for _alias in (_item.display_name, _item.code.value, *_item.aliases):
        _ALIAS_INDEX[normalize_text(_alias)] = _item

_SERIES_INDEX: dict[str, TargetCompetition] = {
    item.polymarket_gamma_series_id: item
    for item in TARGET_COMPETITIONS
    if item.polymarket_gamma_series_id
}

_NON_FOOTBALL_SPORTS = {
    "cricket",
    "rugby",
    "rugby union",
    "rugby league",
    "tennis",
    "golf",
    "horse racing",
    "horseracing",
    "greyhound",
    "american football",
    "nfl",
    "nba",
    "nba basketball",
    "wnba",
    "nhl",
    "baseball",
    "ice hockey",
    "snooker",
    "darts",
    "boxing",
    "mma",
    "ufc",
}

_FOOTBALL_SPORTS = {"football", "soccer", "association football", "soccer football"}

UNMATCHED_POLYMARKET_COVERAGE = "unmatched / no supported Polymarket coverage"
EVENT_IDENTITY_MISMATCH = "event_identity_mismatch"
SERIES_NOT_QUERIED = "series_not_queried"
UNKNOWN_COMPETITION = "unknown_or_ambiguous_competition"
NON_FOOTBALL_SPORT = "non_football_sport"
REJECTED_NON_NCAAB_BASKETBALL = "rejected_non_ncaab_basketball"
OUT_OF_SCOPE_COMPETITION = "out_of_scope_competition"
NO_VERIFIED_CROSS_VENUE_MAPPING = "No verified cross-venue mapping"
KALSHI_SERIES_NOT_VERIFIED = "Kalshi match-level series not verified"
POLYMARKET_SERIES_NOT_VERIFIED = "Polymarket Gamma series not verified"
MATCHBOOK_ALIASES_NOT_VERIFIED = "Matchbook label aliases not verified"
SEASON_PROPOSITION_NOT_FIXTURE = "season_proposition_not_fixture"

# Operator selector grouping. Canonical codes are the operator model; venue
# tickers stay backend-only.
OPERATOR_COMPETITION_REGISTRY_VERSION = 9
OPERATOR_UNIVERSE_SPORT = "football"
OPERATOR_GROUP_ORDER: tuple[tuple[str, str], ...] = (
    ("uefa", "UEFA"),
    ("england", "England"),
    ("spain", "Spain"),
    ("germany", "Germany"),
    ("italy", "Italy"),
    ("france", "France"),
    ("netherlands", "Netherlands"),
    ("portugal", "Portugal"),
    ("scotland", "Scotland"),
    ("belgium", "Belgium"),
    ("turkey", "Turkey"),
    ("usa_canada", "USA / Canada"),
    ("mexico", "Mexico"),
    ("brazil", "Brazil"),
    ("argentina", "Argentina"),
    ("south_america", "South America"),
    ("saudi_arabia", "Saudi Arabia"),
    ("japan", "Japan"),
    ("south_africa", "South Africa"),
    ("international", "International"),
    ("nfl", "NFL"),
    ("nba", "NBA"),
    ("college_basketball", "College Basketball"),
    ("mlb", "MLB"),
    ("tennis", "Tennis"),
)
OPERATOR_SELECTOR_META: dict[TargetCompetitionCode, tuple[str, str, str]] = {
    TargetCompetitionCode.CHAMPIONS_LEAGUE: ("uefa", "UEFA", "Champions League"),
    TargetCompetitionCode.EUROPA_LEAGUE: ("uefa", "UEFA", "Europa League"),
    TargetCompetitionCode.CONFERENCE_LEAGUE: ("uefa", "UEFA", "Conference League"),
    TargetCompetitionCode.UEFA_NATIONS_LEAGUE: ("uefa", "UEFA", "Nations League"),
    TargetCompetitionCode.PREMIER_LEAGUE: ("england", "England", "Premier League"),
    TargetCompetitionCode.CHAMPIONSHIP: ("england", "England", "Championship"),
    TargetCompetitionCode.LEAGUE_ONE: ("england", "England", "League One"),
    TargetCompetitionCode.LEAGUE_TWO: ("england", "England", "League Two"),
    TargetCompetitionCode.FA_CUP: ("england", "England", "FA Cup"),
    TargetCompetitionCode.CARABAO_CUP: ("england", "England", "Carabao Cup"),
    TargetCompetitionCode.LA_LIGA: ("spain", "Spain", "La Liga"),
    TargetCompetitionCode.COPA_DEL_REY: ("spain", "Spain", "Copa del Rey"),
    TargetCompetitionCode.BUNDESLIGA: ("germany", "Germany", "Bundesliga"),
    TargetCompetitionCode.DFB_POKAL: ("germany", "Germany", "DFB-Pokal"),
    TargetCompetitionCode.SERIE_A: ("italy", "Italy", "Serie A"),
    TargetCompetitionCode.COPPA_ITALIA: ("italy", "Italy", "Coppa Italia"),
    TargetCompetitionCode.LIGUE_1: ("france", "France", "Ligue 1"),
    TargetCompetitionCode.EREDIVISIE: ("netherlands", "Netherlands", "Eredivisie"),
    TargetCompetitionCode.PRIMEIRA_LIGA: ("portugal", "Portugal", "Primeira Liga"),
    TargetCompetitionCode.SCOTTISH_PREMIERSHIP: ("scotland", "Scotland", "Scottish Premiership"),
    TargetCompetitionCode.BELGIAN_PRO_LEAGUE: ("belgium", "Belgium", "Pro League"),
    TargetCompetitionCode.SUPER_LIG: ("turkey", "Turkey", "Süper Lig"),
    TargetCompetitionCode.MLS: ("usa_canada", "USA / Canada", "MLS"),
    TargetCompetitionCode.LIGA_MX: ("mexico", "Mexico", "Liga MX"),
    TargetCompetitionCode.BRASILEIRAO: ("brazil", "Brazil", "Série A"),
    TargetCompetitionCode.ARGENTINA_PRIMERA: ("argentina", "Argentina", "Primera División"),
    TargetCompetitionCode.COPA_LIBERTADORES: ("south_america", "South America", "Libertadores"),
    TargetCompetitionCode.SAUDI_PRO_LEAGUE: ("saudi_arabia", "Saudi Arabia", "Pro League"),
    TargetCompetitionCode.J1_LEAGUE: ("japan", "Japan", "J1 League"),
    TargetCompetitionCode.SOUTH_AFRICAN_PREMIERSHIP: (
        "south_africa",
        "South Africa",
        "Premiership",
    ),
    TargetCompetitionCode.INTERNATIONAL_FRIENDLIES: (
        "international",
        "International",
        "International Friendlies",
    ),
    TargetCompetitionCode.NFL: ("nfl", "NFL", "NFL"),
    TargetCompetitionCode.NBA: ("nba", "NBA", "NBA"),
    TargetCompetitionCode.NCAAB: ("college_basketball", "College Basketball", "NCAA Men"),
    TargetCompetitionCode.MLB: ("mlb", "MLB", "MLB"),
    # Selector labels name the evidence-backed tournaments. Selecting ATP/WTA
    # does not cover the rest of either tour.
    TargetCompetitionCode.ATP: ("tennis", "Tennis", "ATP (Hangzhou, Chengdu)"),
    TargetCompetitionCode.WTA: ("tennis", "Tennis", "WTA (Singapore, Seoul)"),
}
DEFAULT_OPERATOR_COMPETITION_CODES: tuple[TargetCompetitionCode, ...] = (
    TargetCompetitionCode.PREMIER_LEAGUE,
    TargetCompetitionCode.CHAMPIONSHIP,
    TargetCompetitionCode.LA_LIGA,
    TargetCompetitionCode.CARABAO_CUP,
    TargetCompetitionCode.FA_CUP,
    TargetCompetitionCode.INTERNATIONAL_FRIENDLIES,
    TargetCompetitionCode.BUNDESLIGA,
    TargetCompetitionCode.SERIE_A,
)
# Verified match-level Kalshi series from public GET /series. Not guessed.
KALSHI_SERIES_TICKERS_BY_CODE: dict[TargetCompetitionCode, tuple[str, ...]] = {
    TargetCompetitionCode.PREMIER_LEAGUE: (
        "KXEPLGAME",
        "KXEPLBTTS",
        "KXEPLTOTAL",
        "KXEPLFTTS",
    ),
    TargetCompetitionCode.CHAMPIONSHIP: (
        "KXEFLCHAMPIONSHIPGAME",
        "KXEFLCHAMPIONSHIPBTTS",
        "KXEFLCHAMPIONSHIPTOTAL",
    ),
    TargetCompetitionCode.LA_LIGA: (
        "KXLALIGAGAME",
        "KXLALIGABTTS",
        "KXLALIGATOTAL",
        "KXLALIGAFTTS",
    ),
    TargetCompetitionCode.CARABAO_CUP: (
        "KXEFLCUPGAME",
        "KXEFLCUPBTTS",
        "KXEFLCUPTOTAL",
        "KXEFLCUPFTTS",
    ),
    TargetCompetitionCode.FA_CUP: (
        "KXFACUPGAME",
        "KXFACUPBTTS",
        "KXFACUPTOTAL",
        "KXFACUPFTTS",
    ),
    TargetCompetitionCode.INTERNATIONAL_FRIENDLIES: (
        "KXINTLFRIENDLYGAME",
        "KXINTLFRIENDLYBTTS",
        "KXINTLFRIENDLYTOTAL",
    ),
    TargetCompetitionCode.BUNDESLIGA: (
        "KXBUNDESLIGAGAME",
        "KXBUNDESLIGABTTS",
        "KXBUNDESLIGATOTAL",
        "KXBUNDESLIGAFTTS",
    ),
    TargetCompetitionCode.SERIE_A: (
        "KXSERIEAGAME",
        "KXSERIEABTTS",
        "KXSERIEATOTAL",
        "KXSERIEAFTTS",
    ),
    TargetCompetitionCode.CHAMPIONS_LEAGUE: (
        "KXUCLGAME",
        "KXUCLBTTS",
        "KXUCLTOTAL",
        "KXUCLFTTS",
    ),
    TargetCompetitionCode.EUROPA_LEAGUE: (
        "KXUELGAME",
        "KXUELBTTS",
        "KXUELTOTAL",
    ),
    TargetCompetitionCode.CONFERENCE_LEAGUE: (
        "KXUECLGAME",
        "KXUECLBTTS",
        "KXUECLTOTAL",
    ),
    TargetCompetitionCode.UEFA_NATIONS_LEAGUE: (
        "KXUEFANLGAME",
        "KXUEFANLBTTS",
        "KXUEFANLTOTAL",
        "KXUEFANLFTTS",
    ),
    TargetCompetitionCode.SUPER_LIG: (
        "KXSUPERLIGGAME",
        "KXSUPERLIGBTTS",
        "KXSUPERLIGTOTAL",
    ),
    TargetCompetitionCode.MLS: (
        "KXMLSGAME",
        "KXMLSBTTS",
        "KXMLSTOTAL",
        "KXMLSFTTS",
    ),
    TargetCompetitionCode.LEAGUE_ONE: (
        "KXEFLL1GAME",
        "KXEFLL1BTTS",
        "KXEFLL1TOTAL",
    ),
    TargetCompetitionCode.COPA_DEL_REY: (
        "KXCOPADELREYGAME",
        "KXCOPADELREYBTTS",
        "KXCOPADELREYTOTAL",
        "KXCOPADELREYFTTS",
    ),
    TargetCompetitionCode.DFB_POKAL: (
        "KXDFBPOKALGAME",
        "KXDFBPOKALBTTS",
        "KXDFBPOKALTOTAL",
        "KXDFBPOKALFTTS",
    ),
    TargetCompetitionCode.COPPA_ITALIA: (
        "KXCOPPAITALIAGAME",
        "KXCOPPAITALIABTTS",
        "KXCOPPAITALIATOTAL",
        "KXCOPPAITALIAFTTS",
    ),
    TargetCompetitionCode.LIGUE_1: (
        "KXLIGUE1GAME",
        "KXLIGUE1BTTS",
        "KXLIGUE1TOTAL",
        "KXLIGUE1FTTS",
    ),
    TargetCompetitionCode.EREDIVISIE: (
        "KXEREDIVISIEGAME",
        "KXEREDIVISIEBTTS",
        "KXEREDIVISIETOTAL",
    ),
    TargetCompetitionCode.PRIMEIRA_LIGA: (
        "KXLIGAPORTUGALGAME",
        "KXLIGAPORTUGALBTTS",
        "KXLIGAPORTUGALTOTAL",
    ),
    TargetCompetitionCode.SCOTTISH_PREMIERSHIP: (
        "KXSCOTTISHPREMGAME",
        "KXSCOTTISHPREMBTTS",
        "KXSCOTTISHPREMTOTAL",
    ),
    TargetCompetitionCode.BELGIAN_PRO_LEAGUE: (
        "KXBELGIANPLGAME",
        "KXBELGIANPLBTTS",
        "KXBELGIANPLTOTAL",
    ),
    TargetCompetitionCode.LIGA_MX: (
        "KXLIGAMXGAME",
        "KXLIGAMXBTTS",
        "KXLIGAMXTOTAL",
        "KXLIGAMXFTTS",
    ),
    TargetCompetitionCode.BRASILEIRAO: (
        "KXBRASILEIROGAME",
        "KXBRASILEIROBTTS",
        "KXBRASILEIROTOTAL",
        "KXBRASILEIROFTTS",
    ),
    TargetCompetitionCode.ARGENTINA_PRIMERA: (
        "KXARGPREMDIVGAME",
        "KXARGPREMDIVBTTS",
        "KXARGPREMDIVTOTAL",
    ),
    TargetCompetitionCode.COPA_LIBERTADORES: (
        "KXCONMEBOLLIBGAME",
        "KXCONMEBOLLIBBTTS",
        "KXCONMEBOLLIBTOTAL",
    ),
    TargetCompetitionCode.SAUDI_PRO_LEAGUE: (
        "KXSAUDIPLGAME",
        "KXSAUDIPLBTTS",
        "KXSAUDIPLTOTAL",
    ),
    TargetCompetitionCode.J1_LEAGUE: (
        "KXJLEAGUEGAME",
        "KXJLEAGUEBTTS",
        "KXJLEAGUETOTAL",
    ),
    TargetCompetitionCode.NFL: (
        "KXNFLGAME",
        "KXNFLSPREAD",
        "KXNFLTOTAL",
    ),
    TargetCompetitionCode.NBA: (
        "KXNBAGAME",
        "KXNBASPREAD",
        "KXNBATOTAL",
    ),
    TargetCompetitionCode.NCAAB: (
        "KXNCAAMBGAME",
        "KXNCAAMBSPREAD",
        "KXNCAAMBTOTAL",
    ),
    TargetCompetitionCode.MLB: (
        "KXMLBGAME",
        "KXMLBTOTAL",
    ),
    TargetCompetitionCode.ATP: ("KXATPMATCH",),
    TargetCompetitionCode.WTA: ("KXWTAMATCH",),
}


class ScopeDecision(BaseModel):
    allowed: bool
    reason: str | None = None
    competition: TargetCompetition | None = None
    label: str | None = None
    sport: str | None = None


class ScopeFilterResult(BaseModel):
    allowed: list[dict[str, Any]] = Field(default_factory=list)
    skipped: int = 0
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    rejected_labels: list[str] = Field(default_factory=list)
    rejected_events: list[dict[str, Any]] = Field(default_factory=list)
    rejected_events_truncated: bool = False


def _scope_diagnostic_sport(competition: TargetCompetition | None) -> str:
    """Sport token for scope diagnostics. Unknown competitions stay football.

    NFL, basketball, baseball and tennis must not be labelled football once
    the competition is resolved.
    """

    if competition is None:
        return "football"
    code = competition.code
    if code is TargetCompetitionCode.NFL:
        return "american_football"
    if code in {TargetCompetitionCode.NBA, TargetCompetitionCode.NCAAB}:
        return "basketball"
    if code is TargetCompetitionCode.MLB:
        return "baseball"
    if code in {TargetCompetitionCode.ATP, TargetCompetitionCode.WTA}:
        return "tennis"
    return "football"


def resolve_target_competition(label: str | None) -> TargetCompetition | None:
    """Exact alias match only. Unknown and ambiguous labels fail closed."""

    if label is None:
        return None
    normalized = normalize_text(label)
    if not normalized:
        return None
    direct = _ALIAS_INDEX.get(normalized)
    if direct is not None:
        return direct
    stripped = _strip_season_suffix(normalized)
    if stripped != normalized:
        return _ALIAS_INDEX.get(stripped)
    return None


def resolve_target_competition_from_series_id(series_id: str | None) -> TargetCompetition | None:
    if not series_id:
        return None
    return _SERIES_INDEX.get(str(series_id).strip())


_KALSHI_SERIES_FAMILY_TAILS = frozenset({"GAME", "BTTS", "TOTAL", "FTTS", "SPREAD"})


def kalshi_ticker_matches_prefix(ticker: str, prefix: str) -> bool:
    """Match a Kalshi series ticker to a registered prefix without neighbor leakage.

    Soccer family roots such as ``KXEPL`` still match ``KXEPLGAME``. Complete
    NFL series names such as ``KXNFLGAME`` do not match ``KXNFLGAMEFG``.
    """

    if not ticker or not prefix:
        return False
    if ticker == prefix or ticker.startswith(prefix + "-"):
        return True
    head = ticker.split("-", 1)[0]
    if head == prefix:
        return True
    if head.startswith(prefix):
        return head[len(prefix) :] in _KALSHI_SERIES_FAMILY_TAILS
    return False


def resolve_target_competition_from_kalshi_ticker(series_ticker: str | None) -> TargetCompetition | None:
    ticker = str(series_ticker or "").strip().upper()
    if not ticker:
        return None
    from sports_hedge.outrights.universe_scopes import kalshi_ticker_is_season_series

    if kalshi_ticker_is_season_series(ticker):
        return None
    matches: list[tuple[int, TargetCompetition]] = []
    for item in TARGET_COMPETITIONS:
        for prefix in item.kalshi_series_prefixes:
            if kalshi_ticker_matches_prefix(ticker, prefix):
                matches.append((len(prefix), item))
                break
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1]


def polymarket_series_ids_for_targets() -> list[str]:
    """Env/settings default discovery IDs. Operator scope may add more at runtime."""

    return polymarket_series_ids_for_codes(default_operator_competition_code_values())


def default_operator_competition_code_values() -> tuple[str, ...]:
    return tuple(code.value for code in DEFAULT_OPERATOR_COMPETITION_CODES)


def competition_by_code(code: str | TargetCompetitionCode | None) -> TargetCompetition | None:
    if code is None:
        return None
    wanted = code.value if isinstance(code, TargetCompetitionCode) else str(code).strip()
    if not wanted:
        return None
    for item in TARGET_COMPETITIONS:
        if item.code.value == wanted:
            return item
    return None


def competition_has_verified_cross_venue_mapping(item: TargetCompetition) -> bool:
    """Selectable only when Matchbook, Kalshi and Polymarket are all verified."""

    return competition_verification_status(item) == VERIFIED_ALL_3


def matchbook_mapping_verified(item: TargetCompetition) -> bool:
    return bool(item.aliases)


def kalshi_mapping_verified(item: TargetCompetition) -> bool:
    return bool(KALSHI_SERIES_TICKERS_BY_CODE.get(item.code)) and bool(item.kalshi_series_prefixes)


def polymarket_mapping_verified(item: TargetCompetition) -> bool:
    return bool(item.polymarket_gamma_series_id) and bool(item.polymarket_gamma_sport)


def competition_verification_status(item: TargetCompetition) -> str:
    if (
        matchbook_mapping_verified(item)
        and kalshi_mapping_verified(item)
        and polymarket_mapping_verified(item)
    ):
        return VERIFIED_ALL_3
    return PARTIAL_PROVIDER_MAPPING


def competition_unavailable_reason(item: TargetCompetition) -> str | None:
    if competition_verification_status(item) == VERIFIED_ALL_3:
        return None
    missing: list[str] = []
    if not matchbook_mapping_verified(item):
        missing.append(MATCHBOOK_ALIASES_NOT_VERIFIED)
    if not kalshi_mapping_verified(item):
        missing.append(KALSHI_SERIES_NOT_VERIFIED)
    if not polymarket_mapping_verified(item):
        missing.append(POLYMARKET_SERIES_NOT_VERIFIED)
    if len(missing) == 1:
        return missing[0]
    return NO_VERIFIED_CROSS_VENUE_MAPPING


class CompetitionVerificationRow(BaseModel):
    code: str
    display_name: str
    selector_label: str
    group_id: str
    group_label: str
    default_selected: bool
    selectable: bool
    verification_status: str
    matchbook_status: VenueMappingStatus
    matchbook_evidence: str
    kalshi_status: VenueMappingStatus
    kalshi_series_tickers: tuple[str, ...] = Field(default_factory=tuple)
    polymarket_status: VenueMappingStatus
    polymarket_gamma_series_id: str | None = None
    polymarket_gamma_sport: str | None = None
    retrieved_at: str = PROVIDER_MATRIX_RETRIEVED_AT
    unavailable_reason: str | None = None


def operator_verification_matrix() -> list[CompetitionVerificationRow]:
    """Backend registry view. Identifiers stay out of operator UI state."""

    rows: list[CompetitionVerificationRow] = []
    for item in TARGET_COMPETITIONS:
        group_id, group_label, selector_label = OPERATOR_SELECTOR_META[item.code]
        selectable = competition_has_verified_cross_venue_mapping(item)
        rows.append(
            CompetitionVerificationRow(
                code=item.code.value,
                display_name=item.display_name,
                selector_label=selector_label,
                group_id=group_id,
                group_label=group_label,
                default_selected=item.code in DEFAULT_OPERATOR_COMPETITION_CODES,
                selectable=selectable,
                verification_status=competition_verification_status(item),
                matchbook_status=(
                    VenueMappingStatus.VERIFIED
                    if matchbook_mapping_verified(item)
                    else VenueMappingStatus.UNVERIFIED
                ),
                matchbook_evidence=(
                    "Basketball sport-id 4; no NCAA/NCAAB competition tag in the 2026-09-22 census. WNBA/NBA events are rejected_non_ncaab_basketball."
                    if item.code is TargetCompetitionCode.NCAAB
                    else "Label aliases; Matchbook has no competition IDs."
                    if matchbook_mapping_verified(item)
                    else "No verified Matchbook label aliases."
                ),
                kalshi_status=(
                    VenueMappingStatus.VERIFIED
                    if kalshi_mapping_verified(item)
                    else VenueMappingStatus.UNVERIFIED
                ),
                kalshi_series_tickers=KALSHI_SERIES_TICKERS_BY_CODE.get(item.code, ()),
                polymarket_status=(
                    VenueMappingStatus.VERIFIED
                    if polymarket_mapping_verified(item)
                    else VenueMappingStatus.UNVERIFIED
                ),
                polymarket_gamma_series_id=item.polymarket_gamma_series_id,
                polymarket_gamma_sport=item.polymarket_gamma_sport,
                retrieved_at=PROVIDER_MATRIX_RETRIEVED_AT,
                unavailable_reason=competition_unavailable_reason(item),
            )
        )
    return rows


def kalshi_series_tickers_for_codes(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for raw in codes or ():
        item = competition_by_code(raw)
        if item is None:
            continue
        for ticker in KALSHI_SERIES_TICKERS_BY_CODE.get(item.code, ()):
            if ticker not in seen:
                seen.add(ticker)
                tickers.append(ticker)
    return tickers


def polymarket_series_ids_for_codes(codes: list[str] | tuple[str, ...] | None) -> list[str]:
    series_ids: list[str] = []
    seen: set[str] = set()
    for raw in codes or ():
        item = competition_by_code(raw)
        if item is None or not item.polymarket_gamma_series_id:
            continue
        series_id = item.polymarket_gamma_series_id
        if series_id not in seen:
            seen.add(series_id)
            series_ids.append(series_id)
    return series_ids


def normalize_selected_competition_codes(
    codes: list[str] | tuple[str, ...] | None,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Keep selectable canonical codes only. Unknown/disabled codes are dropped."""

    selected: list[str] = []
    seen: set[str] = set()
    for raw in codes or ():
        item = competition_by_code(raw)
        if item is None or not competition_has_verified_cross_venue_mapping(item):
            continue
        if item.code.value in seen:
            continue
        seen.add(item.code.value)
        selected.append(item.code.value)
    if not selected and not allow_empty:
        raise ValueError("universe_scope_empty_selection")
    return tuple(selected)


def resolve_catalogue_competition_code(
    *,
    competition: str | None = None,
    kalshi_series_ticker: str | None = None,
) -> str | None:
    resolved = resolve_target_competition(competition)
    if resolved is not None:
        return resolved.code.value
    ticker_resolved = resolve_target_competition_from_kalshi_ticker(kalshi_series_ticker)
    if ticker_resolved is not None:
        return ticker_resolved.code.value
    return None


def operator_competition_catalog() -> list[dict[str, Any]]:
    """UI catalog keyed by canonical codes. No raw venue tickers."""

    rows: list[dict[str, Any]] = []
    for item in TARGET_COMPETITIONS:
        group_id, group_label, selector_label = OPERATOR_SELECTOR_META[item.code]
        selectable = competition_has_verified_cross_venue_mapping(item)
        rows.append(
            {
                "code": item.code.value,
                "display_name": item.display_name,
                "selector_label": selector_label,
                "group_id": group_id,
                "group_label": group_label,
                "default_selected": item.code in DEFAULT_OPERATOR_COMPETITION_CODES,
                "selectable": selectable,
                "verification_status": competition_verification_status(item),
                "unavailable_reason": competition_unavailable_reason(item),
                "market_scope": "FIXTURE_MATCH",
                "observation_only": False,
                "paper_executable": selectable
                and item.code
                not in {
                    TargetCompetitionCode.MLB,
                    TargetCompetitionCode.ATP,
                    TargetCompetitionCode.WTA,
                },
            }
        )
    return rows


def selected_includes_nfl(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> bool:
    return TargetCompetitionCode.NFL.value in _selected_code_set(selected_codes)


def selected_includes_nba(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> bool:
    return TargetCompetitionCode.NBA.value in _selected_code_set(selected_codes)


def selected_includes_mlb(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> bool:
    return TargetCompetitionCode.MLB.value in _selected_code_set(selected_codes)


def selected_includes_ncaab(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> bool:
    return TargetCompetitionCode.NCAAB.value in _selected_code_set(selected_codes)


def selected_includes_tennis(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> bool:
    codes = _selected_code_set(selected_codes)
    return TargetCompetitionCode.ATP.value in codes or TargetCompetitionCode.WTA.value in codes


_NON_SOCCER_COMPETITION_CODES = frozenset(
    {
        TargetCompetitionCode.NFL.value,
        TargetCompetitionCode.NBA.value,
        TargetCompetitionCode.NCAAB.value,
        TargetCompetitionCode.MLB.value,
        TargetCompetitionCode.ATP.value,
        TargetCompetitionCode.WTA.value,
    }
)


def selected_includes_soccer(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> bool:
    codes = _selected_code_set(selected_codes)
    return any(code not in _NON_SOCCER_COMPETITION_CODES for code in codes)


def _selected_code_set(
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> frozenset[str]:
    if selected_codes is None:
        return frozenset(default_operator_competition_code_values())
    return frozenset(str(code).strip() for code in selected_codes if str(code).strip())


def matchbook_competition_label(payload: dict[str, Any]) -> str | None:
    direct = _first_str(payload, "competition-name", "competition_name", "competition")
    if direct:
        return direct
    for tag in payload.get("meta-tags", payload.get("meta_tags", [])) or []:
        if not isinstance(tag, dict):
            continue
        tag_type = normalize_text(str(tag.get("type", "")))
        name = str(tag.get("name", "")).strip()
        if name and tag_type in {"competition", "league", "tournament"}:
            return name
    return None


def matchbook_sport_label(payload: dict[str, Any]) -> str | None:
    direct = _first_str(payload, "sport-name", "sport_name", "sport")
    if direct:
        return direct
    for tag in payload.get("meta-tags", payload.get("meta_tags", [])) or []:
        if not isinstance(tag, dict):
            continue
        tag_type = normalize_text(str(tag.get("type", "")))
        name = str(tag.get("name", "")).strip()
        if name and tag_type in {"sport"}:
            return name
    return None


def scope_matchbook_event(
    payload: dict[str, Any],
    *,
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
) -> ScopeDecision:
    """Collector-boundary gate. Provider filters must not be the only check."""

    sport = matchbook_sport_label(payload)
    if sport:
        sport_norm = normalize_text(sport)
        selected = _selected_code_set(selected_codes)
        nfl_selected = TargetCompetitionCode.NFL.value in selected
        nba_selected = TargetCompetitionCode.NBA.value in selected
        ncaab_selected = TargetCompetitionCode.NCAAB.value in selected
        mlb_selected = TargetCompetitionCode.MLB.value in selected
        if sport_norm in {"american football", "nfl"}:
            if not nfl_selected:
                return ScopeDecision(
                    allowed=False,
                    reason=NON_FOOTBALL_SPORT,
                    label=matchbook_competition_label(payload),
                    sport=sport,
                )
        elif sport_norm == "tennis":
            if not selected_includes_tennis(selected):
                return ScopeDecision(
                    allowed=False,
                    reason=NON_FOOTBALL_SPORT,
                    label=matchbook_competition_label(payload),
                    sport=sport,
                )
            label = matchbook_competition_label(payload)
            from sports_hedge.tennis.constants import TENNIS_TOUR_ATP
            from sports_hedge.tennis.tournaments import admitted_tournament

            admitted = admitted_tournament(label)
            if admitted is None:
                return ScopeDecision(
                    allowed=False,
                    reason="tennis_tournament_not_admitted",
                    label=label,
                    sport=sport,
                )
            tour, _tournament = admitted
            code = TargetCompetitionCode.ATP if tour == TENNIS_TOUR_ATP else TargetCompetitionCode.WTA
            competition = competition_by_code(code)
            if code.value not in selected:
                return ScopeDecision(
                    allowed=False,
                    reason=OUT_OF_SCOPE_COMPETITION,
                    competition=competition,
                    label=label,
                    sport=sport,
                )
            return ScopeDecision(
                allowed=True,
                competition=competition,
                label=label or (competition.display_name if competition else None),
                sport=sport,
            )
        elif sport_norm in {"basketball", "nba", "nba basketball"}:
            if not nba_selected and not ncaab_selected:
                return ScopeDecision(
                    allowed=False,
                    reason=NON_FOOTBALL_SPORT,
                    label=matchbook_competition_label(payload),
                    sport=sport,
                )
            from sports_hedge.nba.detect import is_nba_payload

            if nba_selected and is_nba_payload(payload):
                pass
            elif ncaab_selected:
                from sports_hedge.ncaab.detect import (
                    matchbook_excluded_basketball_reason,
                    matchbook_has_ncaab_signal,
                )

                excluded = matchbook_excluded_basketball_reason(payload)
                if excluded is not None:
                    return ScopeDecision(
                        allowed=False,
                        reason=REJECTED_NON_NCAAB_BASKETBALL,
                        label=matchbook_competition_label(payload),
                        sport=sport,
                    )
                if matchbook_has_ncaab_signal(payload):
                    ncaab = competition_by_code(TargetCompetitionCode.NCAAB)
                    return ScopeDecision(
                        allowed=True,
                        competition=ncaab,
                        label=matchbook_competition_label(payload)
                        or (ncaab.display_name if ncaab else None),
                        sport=sport,
                    )
                if not nba_selected:
                    return ScopeDecision(
                        allowed=False,
                        reason=REJECTED_NON_NCAAB_BASKETBALL,
                        label=matchbook_competition_label(payload),
                        sport=sport,
                    )
        elif sport_norm in {"baseball", "mlb"}:
            if not mlb_selected:
                return ScopeDecision(
                    allowed=False,
                    reason=NON_FOOTBALL_SPORT,
                    label=matchbook_competition_label(payload),
                    sport=sport,
                )
            from sports_hedge.mlb.constants import MLB_NON_MLB_BASEBALL
            from sports_hedge.mlb.detect import matchbook_is_mlb_competition

            if not matchbook_is_mlb_competition(payload):
                return ScopeDecision(
                    allowed=False,
                    reason=MLB_NON_MLB_BASEBALL,
                    label=matchbook_competition_label(payload),
                    sport=sport,
                )
            from sports_hedge.mlb.constants import MLB_UNSUPPORTED_FAMILY_REASON
            from sports_hedge.mlb.markets import mlb_text_is_rejected_family

            event_name = str(payload.get("name") or payload.get("title") or "")
            if mlb_text_is_rejected_family(event_name):
                return ScopeDecision(
                    allowed=False,
                    reason=MLB_UNSUPPORTED_FAMILY_REASON,
                    label=event_name or matchbook_competition_label(payload),
                    sport=sport,
                )
            mlb = competition_by_code(TargetCompetitionCode.MLB)
            return ScopeDecision(
                allowed=True,
                competition=mlb,
                label=matchbook_competition_label(payload) or (mlb.display_name if mlb else None),
                sport=sport,
            )
        elif sport_norm in _NON_FOOTBALL_SPORTS or sport_norm not in _FOOTBALL_SPORTS:
            return ScopeDecision(
                allowed=False,
                reason=NON_FOOTBALL_SPORT,
                label=matchbook_competition_label(payload),
                sport=sport,
            )

    label = matchbook_competition_label(payload)
    resolved = resolve_target_competition(label)
    if resolved is None:
        return ScopeDecision(
            allowed=False,
            reason=UNKNOWN_COMPETITION,
            label=label,
            sport=sport,
        )
    if resolved.code.value not in _selected_code_set(selected_codes):
        return ScopeDecision(
            allowed=False,
            reason=OUT_OF_SCOPE_COMPETITION,
            competition=resolved,
            label=label,
            sport=sport,
        )
    return ScopeDecision(allowed=True, competition=resolved, label=label, sport=sport)


def scope_polymarket_event(
    payload: dict[str, Any],
    *,
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
) -> ScopeDecision:
    series_target = _polymarket_series_target(payload)
    label = _first_str(payload, "competition", "league", "seriesTitle", "series_title")
    resolved = series_target
    if resolved is None:
        resolved = resolve_target_competition(label)
        if resolved is None:
            series_title = _polymarket_series_title(payload)
            resolved = resolve_target_competition(series_title)
            label = label or series_title
    if resolved is None:
        return ScopeDecision(
            allowed=False,
            reason=UNKNOWN_COMPETITION,
            label=label,
            sport=_scope_diagnostic_sport(resolved),
        )
    if resolved.code.value not in _selected_code_set(selected_codes):
        return ScopeDecision(
            allowed=False,
            reason=OUT_OF_SCOPE_COMPETITION,
            competition=resolved,
            label=label or resolved.display_name,
            sport=_scope_diagnostic_sport(resolved),
        )
    if resolved.code in {TargetCompetitionCode.ATP, TargetCompetitionCode.WTA}:
        from sports_hedge.tennis.normalize import polymarket_stage1_rejection

        rejected = polymarket_stage1_rejection(payload)
        if rejected is not None:
            return ScopeDecision(
                allowed=False,
                reason=rejected,
                competition=resolved,
                label=label or resolved.display_name,
                sport="tennis",
            )
    if resolved.code is TargetCompetitionCode.MLB:
        from sports_hedge.mlb.constants import MLB_NON_MLB_BASEBALL, MLB_UNSUPPORTED_FAMILY_REASON
        from sports_hedge.mlb.markets import mlb_text_is_rejected_family

        sport_obj = payload.get("sport")
        sport_code = ""
        if isinstance(sport_obj, dict):
            sport_code = normalize_text(str(sport_obj.get("sport") or ""))
        if sport_code in {"kbo", "npb", "wbc", "ncaabaseball", "cuba", "mlbb"}:
            return ScopeDecision(
                allowed=False,
                reason=MLB_NON_MLB_BASEBALL,
                competition=resolved,
                label=label or resolved.display_name,
                sport="baseball",
            )
        blob = " ".join(str(payload.get(key) or "") for key in ("slug", "title", "question"))
        normalized_blob = normalize_text(blob)
        if mlb_text_is_rejected_family(blob) or "player props" in normalized_blob:
            return ScopeDecision(
                allowed=False,
                reason=MLB_UNSUPPORTED_FAMILY_REASON,
                competition=resolved,
                label=label or _first_str(payload, "title", "slug") or resolved.display_name,
                sport="baseball",
            )
    if (
        resolved.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE
        and _polymarket_payload_is_season_proposition(payload)
    ):
        return ScopeDecision(
            allowed=False,
            reason=SEASON_PROPOSITION_NOT_FIXTURE,
            competition=resolved,
            label=label or _first_str(payload, "title", "slug") or resolved.display_name,
            sport="football",
        )
    return ScopeDecision(
        allowed=True,
        competition=resolved,
        label=label or resolved.display_name,
        sport=_scope_diagnostic_sport(resolved),
    )


def scope_kalshi_event(
    payload: dict[str, Any],
    *,
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
) -> ScopeDecision:
    ticker = str(payload.get("series_ticker") or payload.get("ticker") or "").strip()
    from sports_hedge.mlb.constants import MLB_NON_MLB_BASEBALL, MLB_UNSUPPORTED_FAMILY_REASON
    from sports_hedge.mlb.detect import rejected_kalshi_mlb_series

    if rejected_kalshi_mlb_series(ticker):
        head = ticker.split("-", 1)[0].upper()
        reason = MLB_UNSUPPORTED_FAMILY_REASON if head.startswith("KXMLB") else MLB_NON_MLB_BASEBALL
        return ScopeDecision(
            allowed=False,
            reason=reason,
            label=ticker,
            sport="baseball",
        )
    series_target = resolve_target_competition_from_kalshi_ticker(ticker)
    if series_target is None:
        nested = payload.get("series")
        if isinstance(nested, dict):
            series_target = resolve_target_competition_from_kalshi_ticker(
                str(nested.get("ticker") or "")
            )
    label = _first_str(payload, "competition", "league", "title")
    resolved = series_target
    if resolved is None:
        resolved = resolve_target_competition(label)
    if resolved is None:
        return ScopeDecision(
            allowed=False,
            reason=UNKNOWN_COMPETITION,
            label=label or ticker or None,
            sport=_scope_diagnostic_sport(resolved),
        )
    if resolved.code.value not in _selected_code_set(selected_codes):
        return ScopeDecision(
            allowed=False,
            reason=OUT_OF_SCOPE_COMPETITION,
            competition=resolved,
            label=label or resolved.display_name,
            sport=_scope_diagnostic_sport(resolved),
        )
    if resolved.code in {TargetCompetitionCode.ATP, TargetCompetitionCode.WTA}:
        meta = payload.get("product_metadata")
        competition_label = None
        if isinstance(meta, dict):
            competition_label = str(meta.get("competition") or "").strip() or None
        from sports_hedge.tennis.tournaments import admitted_tournament

        if admitted_tournament(competition_label) is None:
            return ScopeDecision(
                allowed=False,
                reason="tennis_tournament_not_admitted",
                competition=resolved,
                label=competition_label or label or resolved.display_name,
                sport="tennis",
            )
    if resolved.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE and (
        (ticker and series_target is None)
        or _polymarket_payload_is_season_proposition(payload)
    ):
        # Season series KXUEFANL and unadmitted 1H/spread/score/advance
        # neighbors must not enter the fixture pipeline via competition title.
        return ScopeDecision(
            allowed=False,
            reason=SEASON_PROPOSITION_NOT_FIXTURE,
            competition=resolved,
            label=label or resolved.display_name,
            sport="football",
        )
    return ScopeDecision(
        allowed=True,
        competition=resolved,
        label=label or resolved.display_name,
        sport=_scope_diagnostic_sport(resolved),
    )


_MAX_RETAINED_SCOPE_REJECTIONS = 2000
_SCOPE_LABEL_KEYS = ("title", "name", "question", "event_title", "subtitle")


def _scope_rejection_record(
    payload: dict[str, Any], venue: VenueName, decision: ScopeDecision
) -> dict[str, Any]:
    """Identity fields only. Full provider payloads stay out of the diagnostic."""

    source_id = str(
        payload.get("id") or payload.get("event_ticker") or payload.get("ticker") or ""
    ).strip()
    label = None
    for key in _SCOPE_LABEL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            label = value.strip()[:300]
            break
    if label is None and decision.label:
        label = decision.label.strip()[:300]
    return {
        "venue": venue.value,
        "source_event_id": source_id or None,
        "provider_label": label,
        "provider_label_evidence": "retained" if label else "unavailable",
        "scope_reason": decision.reason or OUT_OF_SCOPE_COMPETITION,
        "competition_label": decision.label,
        "sport": decision.sport,
    }


def filter_in_scope_events(
    payloads: list[dict[str, Any]],
    *,
    venue: VenueName,
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
) -> ScopeFilterResult:
    """Keep selected-competition football only. Out-of-scope is skipped, not an issue."""

    allowed: list[dict[str, Any]] = []
    skipped_by_reason: dict[str, int] = {}
    rejected: list[str] = []
    rejected_events: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    skipped = 0
    truncated = False
    for payload in payloads:
        decision = _scope_for_venue(payload, venue, selected_codes=selected_codes)
        if decision.allowed:
            allowed.append(payload)
            continue
        skipped += 1
        reason = decision.reason or OUT_OF_SCOPE_COMPETITION
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        label = (decision.label or "").strip()
        if label and label not in seen_labels:
            seen_labels.add(label)
            rejected.append(label)
        if len(rejected_events) < _MAX_RETAINED_SCOPE_REJECTIONS:
            rejected_events.append(_scope_rejection_record(payload, venue, decision))
        else:
            truncated = True
    return ScopeFilterResult(
        allowed=allowed,
        skipped=skipped,
        skipped_by_reason=skipped_by_reason,
        rejected_labels=rejected[:50],
        rejected_events=rejected_events,
        rejected_events_truncated=truncated,
    )


def _scope_for_venue(
    payload: dict[str, Any],
    venue: VenueName,
    *,
    selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
) -> ScopeDecision:
    if venue is VenueName.MATCHBOOK:
        return scope_matchbook_event(payload, selected_codes=selected_codes)
    if venue is VenueName.POLYMARKET:
        return scope_polymarket_event(payload, selected_codes=selected_codes)
    if venue is VenueName.KALSHI:
        return scope_kalshi_event(payload, selected_codes=selected_codes)
    return ScopeDecision(allowed=False, reason=UNKNOWN_COMPETITION)


def _polymarket_series_target(payload: dict[str, Any]) -> TargetCompetition | None:
    sport_obj = payload.get("sport")
    if isinstance(sport_obj, dict):
        series = str(sport_obj.get("series") or sport_obj.get("series_id") or "").strip()
        if series:
            resolved = resolve_target_competition_from_series_id(series)
            if resolved is not None:
                return resolved
        sport_code = str(sport_obj.get("sport") or "").strip()
        if sport_code:
            resolved = resolve_target_competition(sport_code)
            if resolved is not None:
                return resolved
    series_id = payload.get("series_id") or payload.get("seriesId")
    if isinstance(series_id, (int, str)):
        resolved = resolve_target_competition_from_series_id(str(series_id))
        if resolved is not None:
            return resolved
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        series_items = [series_items]
    if isinstance(series_items, list):
        for series in series_items:
            if not isinstance(series, dict):
                continue
            resolved = resolve_target_competition_from_series_id(
                str(series.get("id", series.get("series_id", ""))).strip()
            )
            if resolved is not None:
                return resolved
            title = series.get("title") or series.get("name")
            resolved = resolve_target_competition(str(title) if title else None)
            if resolved is not None:
                return resolved
    return None


def _polymarket_series_title(payload: dict[str, Any]) -> str | None:
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        title = series_items.get("title") or series_items.get("name")
        return str(title).strip() if title else None
    if isinstance(series_items, list):
        for series in series_items:
            if not isinstance(series, dict):
                continue
            title = series.get("title") or series.get("name")
            if title:
                return str(title).strip()
    return None


_SEASON_PROPOSITION_TOKENS = ("winner", "champion", "relegat")


def _polymarket_payload_is_season_proposition(payload: dict[str, Any]) -> bool:
    """Reject group/champion/relegation outrights that are not match fixtures.

    Live 2026-09-22 Gamma events such as ``uefa-nations-league-winner-2026-27``
    have no series id. If a label later resolves to Nations League, the title
    still must look like a two-team fixture before it may enter PAPER discovery.
    """

    title = _first_str(payload, "title", "slug", "ticker") or ""
    blob = f" {normalize_text(title)} "
    if " vs " in blob:
        return False
    return any(token in blob for token in _SEASON_PROPOSITION_TOKENS)


def _strip_season_suffix(normalized: str) -> str:
    parts = normalized.split()
    if len(parts) >= 2 and "/" in parts[-1] and parts[-1].replace("/", "").isdigit():
        return " ".join(parts[:-1])
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 4:
        return " ".join(parts[:-1])
    return normalized


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
