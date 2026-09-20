"""Competition-keyed senior-club identity registry (Issue #316, MLS #413).

Fixture identity, HOT scheduling, and historical seeds consume this table.
Aliases are explicit. Unknown remainders stay unchanged (fail-closed FC strip
only when the remainder is already a curated canonical).

Cups reuse the English senior-club set. International friendlies use senior
national teams for identity only — that does not invent Kalshi market
availability. MLS aliases are provider-observed senior-club forms only.

Data class: maintained identity registry. Not live quotes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.normalization.text import normalize_text

# String keys match TargetCompetitionCode values. facts/ must not import application.
PREMIER_LEAGUE = "premier_league"
CHAMPIONSHIP = "championship"
LA_LIGA = "la_liga"
CARABAO_CUP = "carabao_cup"
FA_CUP = "fa_cup"
INTERNATIONAL_FRIENDLIES = "international_friendlies"
BUNDESLIGA = "bundesliga"
SERIE_A = "serie_a"
MLS = "mls"

TEAM_REGISTRY_VERSION = "v1"
TEAM_REGISTRY_ISSUE = 316


class SeniorClub(BaseModel):
    canonical_name: str
    aliases: tuple[str, ...] = Field(default_factory=tuple)


def _club(canonical: str, *aliases: str) -> SeniorClub:
    unique = tuple(dict.fromkeys([canonical, *aliases]))
    extra = tuple(item for item in unique if item != canonical)
    return SeniorClub(canonical_name=canonical, aliases=extra)


PREMIER_LEAGUE_CLUBS: tuple[SeniorClub, ...] = (
    _club("Arsenal"),
    _club("Aston Villa"),
    _club("Bournemouth", "AFC Bournemouth"),
    _club("Brentford"),
    _club("Brighton and Hove Albion", "Brighton", "Brighton & Hove Albion"),
    _club("Burnley"),
    _club("Chelsea"),
    _club("Crystal Palace"),
    _club("Everton"),
    _club("Fulham"),
    _club("Leeds United", "Leeds"),
    _club("Liverpool"),
    _club("Manchester City", "Man City"),
    _club("Manchester United", "Man United", "Man Utd"),
    _club("Newcastle United", "Newcastle"),
    _club("Nottingham Forest", "Nott'm Forest", "Nottm Forest"),
    _club("Sunderland"),
    _club("Tottenham Hotspur", "Tottenham", "Spurs"),
    _club("West Ham United", "West Ham"),
    _club("Wolverhampton Wanderers", "Wolves"),
)

CHAMPIONSHIP_CLUBS: tuple[SeniorClub, ...] = (
    _club("Birmingham City", "Birmingham"),
    _club("Blackburn Rovers", "Blackburn"),
    _club("Bristol City"),
    _club("Charlton Athletic", "Charlton"),
    _club("Coventry City", "Coventry"),
    _club("Derby County", "Derby"),
    _club("Hull City", "Hull"),
    _club("Ipswich Town", "Ipswich"),
    _club("Leicester City", "Leicester"),
    _club("Middlesbrough"),
    _club("Millwall"),
    _club("Norwich City", "Norwich"),
    _club("Oxford United", "Oxford"),
    _club("Plymouth Argyle", "Plymouth"),
    _club("Portsmouth"),
    _club("Preston North End", "Preston"),
    _club("Queens Park Rangers", "QPR"),
    _club("Sheffield United", "Sheffield Utd"),
    _club("Sheffield Wednesday", "Sheffield Weds"),
    _club("Southampton"),
    _club("Stoke City", "Stoke"),
    _club("Swansea City", "Swansea"),
    _club("Watford"),
    _club("West Bromwich Albion", "West Brom"),
    _club("Wrexham"),
    # Football-Data England labels used by historical catalog
    _club("Barnsley"),
    _club("Blackpool"),
    _club("Cardiff City", "Cardiff"),
    _club("Huddersfield Town", "Huddersfield"),
    _club("Luton Town", "Luton"),
    _club("Peterborough United", "Peterboro", "Peterborough"),
    _club("Reading"),
    _club("Rotherham United", "Rotherham"),
    _club("Wigan Athletic", "Wigan"),
)

LA_LIGA_CLUBS: tuple[SeniorClub, ...] = (
    _club("Athletic Club", "Ath Bilbao", "Athletic Bilbao"),
    _club("Atletico Madrid", "Ath Madrid", "Atletico Madrid", "Atlético Madrid"),
    _club("Barcelona", "FC Barcelona", "Barca"),
    _club("Celta Vigo", "Celta"),
    _club("Deportivo Alaves", "Alaves", "Deportivo Alavés"),
    _club("Elche", "Elche CF"),
    _club("Espanyol", "Espanol", "Espanyol Barcelona", "RCD Espanyol",
          "RCD Espanyol Barcelona", "RCD Espanyol de Barcelona"),
    _club("Getafe"),
    _club("Girona"),
    _club("Levante"),
    _club("Mallorca"),
    _club("Osasuna"),
    _club("Rayo Vallecano", "Vallecano"),
    _club("Real Betis", "Betis"),
    _club("Real Madrid", "Real Madrid CF"),
    _club("Real Oviedo", "Oviedo"),
    _club("Real Sociedad", "Sociedad"),
    _club("Sevilla"),
    _club("Valencia"),
    _club("Villarreal", "Villarreal CF"),
    _club("Leganes"),
    _club("Las Palmas"),
    _club("Real Valladolid", "Valladolid"),
    _club("Malaga", "Malaga CF", "Málaga", "Málaga CF"),
)

BUNDESLIGA_CLUBS: tuple[SeniorClub, ...] = (
    _club(
        "Bayern Munich",
        "FC Bayern München",
        "FC Bayern Munchen",
        "Bayern München",
        "Bayern Munchen",
        "FC Bayern Munich",
        "FC Bayern",
    ),
    _club("Bayer Leverkusen", "Bayer 04 Leverkusen", "Leverkusen"),
    _club("Borussia Dortmund", "Dortmund", "BVB"),
    _club("RB Leipzig", "Leipzig"),
    _club("VfB Stuttgart", "Stuttgart"),
    _club("Eintracht Frankfurt", "Frankfurt", "SGE"),
    _club("SC Freiburg", "Freiburg"),
    _club("Werder Bremen", "Bremen"),
    _club("VfL Wolfsburg", "Wolfsburg"),
    _club(
        "Borussia Monchengladbach",
        "Borussia Mönchengladbach",
        "Gladbach",
        "Borussia M'gladbach",
    ),
    _club(
        "Union Berlin",
        "1. FC Union Berlin",
        "1 FC Union Berlin",
        "FC Union Berlin",
    ),
    _club("Mainz 05", "Mainz", "1. FSV Mainz 05", "FSV Mainz 05"),
    _club("Augsburg", "FC Augsburg"),
    _club("Hoffenheim", "TSG Hoffenheim"),
    _club("Heidenheim", "1. FC Heidenheim", "1 FC Heidenheim"),
    _club("St Pauli", "FC St. Pauli", "St. Pauli", "FC St Pauli"),
    _club("Hamburger SV", "Hamburg", "HSV"),
    _club("FC Koln", "Koln", "Köln", "1. FC Köln", "1. FC Koln", "FC Cologne"),
)

SERIE_A_CLUBS: tuple[SeniorClub, ...] = (
    _club("Inter", "Inter Milan", "FC Internazionale", "Internazionale", "FC Inter"),
    _club("AC Milan", "Milan"),
    _club("Juventus", "Juventus Turin"),
    _club("Napoli", "SSC Napoli"),
    _club("Atalanta", "Atalanta BC", "Atalanta B.C."),
    _club("Roma", "AS Roma"),
    _club("Lazio", "SS Lazio"),
    _club("Fiorentina"),
    _club("Bologna"),
    _club("Torino"),
    _club("Udinese"),
    _club("Genoa"),
    _club("Cagliari"),
    _club("Parma", "Parma Calcio"),
    _club("Lecce"),
    _club("Como", "Como 1907"),
    _club("Sassuolo", "Sassuolo Calcio", "US Sassuolo", "US Sassuolo Calcio"),
    _club("Pisa"),
    _club("Cremonese"),
    _club("Hellas Verona", "Verona"),
    _club("Monza", "AC Monza"),
    # Kalshi Serie A GAME/FTTS sibling titles use the legal name. Capture/replay
    # and owner-live both listed this fixture as KXSERIEAGAME.
    _club("Frosinone", "Frosinone Calcio"),
)

# 2026 MLS senior clubs. Aliases are live provider-observed forms from
# Matchbook / Kalshi / Polymarket read-only metadata (2026-09-20), not
# guessed shorthands. "Miami" is Inter Miami only because Kalshi GAME/BTTS/
# TOTAL titles used Miami while FTTS/Matchbook/Polymarket used Inter Miami CF
# on the same 26SEP20MIASD fixture. Do not treat USL Miami FC as this club.
MLS_CLUBS: tuple[SeniorClub, ...] = (
    _club("Atlanta United", "Atlanta", "Atlanta United FC"),
    _club("Austin", "Austin FC"),
    _club("Montreal", "CF Montreal", "CF Montréal"),
    _club("Charlotte", "Charlotte FC"),
    _club("Chicago Fire", "Chicago Fire FC"),
    _club("Colorado Rapids", "Colorado", "Colorado Rapids SC"),
    _club("Columbus Crew", "Columbus"),
    _club("DC United", "D.C. United", "D.C. United SC"),
    _club("Cincinnati", "FC Cincinnati"),
    _club("Dallas", "FC Dallas"),
    _club("Houston Dynamo", "Houston"),
    _club("Inter Miami", "Inter Miami CF", "Miami"),
    _club(
        "LA Galaxy",
        "Los Angeles Galaxy",
        "Los Angeles G",
    ),
    _club(
        "Los Angeles FC",
        "LAFC",
        "Los Angeles F",
    ),
    _club("Minnesota United", "Minnesota", "Minnesota United FC"),
    _club("Nashville", "Nashville SC"),
    _club("New England Revolution", "New England"),
    _club("New York City", "New York City FC", "NYCFC"),
    _club("New York Red Bulls", "New York RB"),
    _club("Orlando City", "Orlando", "Orlando City SC"),
    _club("Philadelphia Union", "Philadelphia"),
    _club("Portland Timbers", "Portland"),
    _club("Real Salt Lake", "Salt Lake"),
    _club("San Diego", "San Diego FC"),
    _club("San Jose Earthquakes", "San Jose"),
    _club("Seattle Sounders", "Seattle", "Seattle Sounders FC"),
    _club("Sporting Kansas City", "Kansas City"),
    _club("St. Louis City", "Saint Louis", "St. Louis City SC"),
    _club("Toronto", "Toronto FC"),
    _club("Vancouver Whitecaps", "Vancouver", "Vancouver Whitecaps FC"),
)

NATIONAL_TEAMS: tuple[SeniorClub, ...] = (
    _club("England"),
    _club("France"),
    _club("Germany"),
    _club("Spain"),
    _club("Italy"),
    _club("Portugal"),
    _club("Netherlands", "Holland"),
    _club("Belgium"),
    _club("Scotland"),
    _club("Wales"),
    _club("Republic of Ireland"),
    _club("Northern Ireland"),
    _club("Brazil"),
    _club("Argentina"),
    _club("United States", "USA", "USMNT"),
    _club("Mexico"),
    _club("Japan"),
    _club("South Korea", "Korea Republic"),
    _club("Australia"),
    _club("Sweden"),
    _club("Denmark"),
    _club("Norway"),
    _club("Switzerland"),
    _club("Austria"),
    _club("Poland"),
    _club("Croatia"),
    _club("Serbia"),
    _club("Ukraine"),
    _club("Turkey"),
    _club("Greece"),
    _club("Czech Republic", "Czechia"),
    _club("Morocco"),
    _club("Egypt"),
    _club("Nigeria"),
    _club("Senegal"),
    _club("Ghana"),
    _club("Colombia"),
    _club("Uruguay"),
    _club("Canada"),
)

CLUBS_BY_COMPETITION: dict[str, tuple[SeniorClub, ...]] = {
    PREMIER_LEAGUE: PREMIER_LEAGUE_CLUBS,
    CHAMPIONSHIP: CHAMPIONSHIP_CLUBS,
    LA_LIGA: LA_LIGA_CLUBS,
    BUNDESLIGA: BUNDESLIGA_CLUBS,
    SERIE_A: SERIE_A_CLUBS,
    MLS: MLS_CLUBS,
    # Cups reuse English senior clubs. Do not invent a separate lower-league universe.
    CARABAO_CUP: PREMIER_LEAGUE_CLUBS + CHAMPIONSHIP_CLUBS,
    FA_CUP: PREMIER_LEAGUE_CLUBS + CHAMPIONSHIP_CLUBS,
    INTERNATIONAL_FRIENDLIES: NATIONAL_TEAMS,
}


def clubs_for(competition: str) -> tuple[SeniorClub, ...]:
    return CLUBS_BY_COMPETITION[str(competition)]


def all_senior_clubs() -> tuple[SeniorClub, ...]:
    """Deduplicated canonical clubs across targeted competitions."""

    by_key: dict[str, SeniorClub] = {}
    for clubs in CLUBS_BY_COMPETITION.values():
        for club in clubs:
            key = normalize_text(club.canonical_name)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = club
                continue
            merged = tuple(dict.fromkeys((*existing.aliases, *club.aliases)))
            by_key[key] = SeniorClub(canonical_name=existing.canonical_name, aliases=merged)
    return tuple(by_key[key] for key in sorted(by_key))


def alias_pairs() -> tuple[tuple[str, str], ...]:
    """Every explicit alias including self-aliases, keyed to canonical names."""

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for club in all_senior_clubs():
        for alias in (club.canonical_name, *club.aliases):
            item = (alias, club.canonical_name)
            key = (normalize_text(alias), normalize_text(club.canonical_name))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(item)
    return tuple(pairs)


def canonical_team_names() -> frozenset[str]:
    from sports_hedge.normalization.text import normalize_text as _normalize

    return frozenset(_normalize(club.canonical_name) for club in all_senior_clubs())
