from __future__ import annotations

from sports_hedge.normalization.text import AliasRegistry


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
    }
    for alias, canonical in pairs.items():
        aliases.add(alias, canonical)
        aliases.add(canonical, canonical)
    return aliases


football_alias_registry = _registry()


def resolve_team_name(value: str) -> str:
    return football_alias_registry.resolve(value)
