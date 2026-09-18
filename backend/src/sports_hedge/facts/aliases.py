from __future__ import annotations

from sports_hedge.normalization.text import AliasRegistry

# Conservative affixes only. Never drop United/City/Athletic-style identity terms.
SAFE_TEAM_AFFIX_TOKENS = frozenset({"fc", "cf", "afc", "sc"})


def _registry() -> AliasRegistry:
    aliases = AliasRegistry()
    pairs = {
        # Premier League / football-data.co.uk
        "Man United": "Manchester United",
        "Man Utd": "Manchester United",
        "Man City": "Manchester City",
        "Newcastle": "Newcastle United",
        "Nott'm Forest": "Nottingham Forest",
        "Nottm Forest": "Nottingham Forest",
        "Wolves": "Wolverhampton Wanderers",
        "Spurs": "Tottenham Hotspur",
        "Tottenham": "Tottenham Hotspur",
        "West Ham": "West Ham United",
        "Brighton": "Brighton and Hove Albion",
        "Brighton & Hove Albion": "Brighton and Hove Albion",
        "Brighton and Hove Albion": "Brighton and Hove Albion",
        "Leicester": "Leicester City",
        "Leeds": "Leeds United",
        "Ipswich": "Ipswich Town",
        "Sheffield Utd": "Sheffield United",
        "Sheffield Weds": "Sheffield Wednesday",
        "West Brom": "West Bromwich Albion",
        "QPR": "Queens Park Rangers",
        "Birmingham": "Birmingham City",
        "Blackburn": "Blackburn Rovers",
        "Charlton": "Charlton Athletic",
        "Derby": "Derby County",
        "Hull": "Hull City",
        "Norwich": "Norwich City",
        "Oxford": "Oxford United",
        "Plymouth": "Plymouth Argyle",
        "Stoke": "Stoke City",
        "Swansea": "Swansea City",
        "Cardiff": "Cardiff City",
        "Middlesbrough": "Middlesbrough",
        "Coventry": "Coventry City",
        "Bristol City": "Bristol City",
        "Preston": "Preston North End",
        "Huddersfield": "Huddersfield Town",
        # La Liga / football-data.co.uk
        "Ath Madrid": "Atletico Madrid",
        "Ath Bilbao": "Athletic Club",
        "Athletic Bilbao": "Athletic Club",
        "Athletic Club": "Athletic Club",
        "Barcelona": "Barcelona",
        "Espanol": "Espanyol",
        "Sociedad": "Real Sociedad",
        "Betis": "Real Betis",
        "Celta": "Celta Vigo",
        "Vallecano": "Rayo Vallecano",
        "Alaves": "Deportivo Alaves",
        "Mallorca": "Mallorca",
        "Villarreal": "Villarreal",
        "Villarreal CF": "Villarreal",
        "Sevilla": "Sevilla",
        "Valencia": "Valencia",
        "Osasuna": "Osasuna",
        "Getafe": "Getafe",
        "Girona": "Girona",
        "Leganes": "Leganes",
        "Las Palmas": "Las Palmas",
        "Valladolid": "Real Valladolid",
        "Oviedo": "Real Oviedo",
        "Elche": "Elche",
        "Elche CF": "Elche",
        "Levante": "Levante",
        "Malaga": "Malaga",
        "Malaga CF": "Malaga",
        "Málaga": "Malaga",
        "Málaga CF": "Malaga",
        # Bundesliga / observed Matchbook vs Kalshi provider variants
        "Bayern Munich": "Bayern Munich",
        "FC Bayern München": "Bayern Munich",
        "FC Bayern Munchen": "Bayern Munich",
        "Bayern München": "Bayern Munich",
        "Bayern Munchen": "Bayern Munich",
        "FC Bayern Munich": "Bayern Munich",
        "FC Bayern": "Bayern Munich",
        "Union Berlin": "Union Berlin",
        "1. FC Union Berlin": "Union Berlin",
        "1 FC Union Berlin": "Union Berlin",
        "FC Union Berlin": "Union Berlin",
        # Serie A / observed Matchbook vs Kalshi club-name variants (issue 277).
        # AC/Calcio are club-specific aliases, not a global affix strip.
        "Monza": "Monza",
        "AC Monza": "Monza",
        "Sassuolo": "Sassuolo",
        "Sassuolo Calcio": "Sassuolo",
        "US Sassuolo": "Sassuolo",
        "US Sassuolo Calcio": "Sassuolo",
        "AC Milan": "AC Milan",
        "Milan": "AC Milan",
        # La Liga / observed Kalshi geographic-suffix variants (issue 277).
        # Barcelona remains a distinct senior club; never a global city strip.
        "Espanyol Barcelona": "Espanyol",
        "RCD Espanyol": "Espanyol",
        "RCD Espanyol Barcelona": "Espanyol",
        "RCD Espanyol de Barcelona": "Espanyol",
        "FC Barcelona": "Barcelona",
    }
    for alias, canonical in pairs.items():
        aliases.add(alias, canonical)
        aliases.add(canonical, canonical)
    return aliases


football_alias_registry = _registry()
_CANONICAL_TEAM_NAMES = frozenset(football_alias_registry.aliases.values())


def _canonical_remainder_after_safe_affixes(normalized: str) -> str | None:
    """Rewrite only when the remainder is already a curated canonical club.

    This is not a global FC/CF strip. Unknown remainder stays unchanged so
    senior vs youth/women/reserves and same-city clubs remain fail-closed.
    """

    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    if tokens[-1] in SAFE_TEAM_AFFIX_TOKENS:
        remainder = " ".join(tokens[:-1])
        if remainder in _CANONICAL_TEAM_NAMES:
            return remainder
    if tokens[0] in SAFE_TEAM_AFFIX_TOKENS:
        remainder = " ".join(tokens[1:])
        if remainder in _CANONICAL_TEAM_NAMES:
            return remainder
    if tokens[0].isdigit() and len(tokens) >= 3 and tokens[1] in SAFE_TEAM_AFFIX_TOKENS:
        remainder = " ".join(tokens[2:])
        if remainder in _CANONICAL_TEAM_NAMES:
            return remainder
    return None


def curated_team_names_conflict(left: str, right: str) -> bool:
    """True when both names are curated canonicals and they are different clubs."""

    if left == right:
        return False
    return left in _CANONICAL_TEAM_NAMES and right in _CANONICAL_TEAM_NAMES


def resolve_team_name(value: str) -> str:
    resolved = football_alias_registry.resolve(value)
    if resolved in _CANONICAL_TEAM_NAMES:
        return resolved
    stripped = _canonical_remainder_after_safe_affixes(resolved)
    return stripped if stripped is not None else resolved
