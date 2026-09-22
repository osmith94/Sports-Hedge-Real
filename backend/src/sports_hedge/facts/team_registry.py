"""Competition-keyed senior-club identity registry (Issue #316, MLS/Liga MX #413, census #434).

Fixture identity, HOT scheduling, and historical seeds consume this table.
Unique aliases are explicit and global. Generic city tokens such as Miami or
Leon stay on #415's competition-scoped `generic_aliases` and fail closed
outside that competition. Paris, Sporting, Istanbul, Nacional, and similar
ambiguous tokens are not globalised and are not added as generics here.

Cups reuse the relevant domestic senior-club set. Continental competitions
reuse the union of those domestic tables so the same club keeps one canonical
id. MLS / Liga MX stay on their own competition keys so UCL does not inherit
Miami / Leon / America generics. International friendlies and UEFA Nations
League use senior national teams for identity only — that does not invent
Kalshi market availability.

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
LIGA_MX = "liga_mx"
LEAGUE_ONE = "league_one"
LIGUE_1 = "ligue_1"
EREDIVISIE = "eredivisie"
PRIMEIRA_LIGA = "primeira_liga"
SCOTTISH_PREMIERSHIP = "scottish_premiership"
BELGIAN_PRO_LEAGUE = "belgian_pro_league"
SUPER_LIG = "super_lig"
BRASILEIRAO = "brasileirao"
ARGENTINA_PRIMERA = "argentina_primera"
SAUDI_PRO_LEAGUE = "saudi_pro_league"
J1_LEAGUE = "j1_league"
CHAMPIONS_LEAGUE = "champions_league"
EUROPA_LEAGUE = "europa_league"
CONFERENCE_LEAGUE = "conference_league"
UEFA_NATIONS_LEAGUE = "uefa_nations_league"
COPA_DEL_REY = "copa_del_rey"
DFB_POKAL = "dfb_pokal"
COPPA_ITALIA = "coppa_italia"
COPA_LIBERTADORES = "copa_libertadores"

TEAM_REGISTRY_VERSION = "v1"
TEAM_REGISTRY_ISSUE = 316


class SeniorClub(BaseModel):
    canonical_name: str
    aliases: tuple[str, ...] = Field(default_factory=tuple)
    generic_aliases: tuple[str, ...] = Field(default_factory=tuple)


def _club(canonical: str, *aliases: str, generic: tuple[str, ...] = ()) -> SeniorClub:
    unique = tuple(dict.fromkeys([canonical, *aliases]))
    extra = tuple(item for item in unique if item != canonical)
    generic_unique = tuple(
        dict.fromkeys(item for item in generic if item and item != canonical)
    )
    return SeniorClub(
        canonical_name=canonical,
        aliases=extra,
        generic_aliases=generic_unique,
    )


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
    _club("Inter Miami", "Inter Miami CF", generic=("Miami",)),
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

# 2026 Liga MX senior clubs. Unique legal names stay globally resolvable.
# City-only tokens such as Leon / Queretaro / America are competition-scoped
# generics so they cannot silently rewrite clubs outside Liga MX.
LIGA_MX_CLUBS: tuple[SeniorClub, ...] = (
    _club("Club América", "CF América", "Club America", "CF America", generic=("América", "America")),
    _club("Atlas"),
    _club("Atlético San Luis", "Atletico San Luis", "San Luis"),
    _club("Cruz Azul"),
    _club("Guadalajara", "Chivas", "CD Guadalajara"),
    _club("Juárez", "FC Juárez", "FC Juarez", generic=("Juarez",)),
    _club(
        "Club León",
        "Club Leon",
        "Club León FC",
        "Club Leon FC",
        generic=("León", "Leon"),
    ),
    _club("Mazatlán", "Mazatlan", "Mazatlán FC", "Mazatlan FC"),
    _club("Monterrey", "CF Monterrey"),
    _club("Necaxa"),
    _club("Pachuca", "CF Pachuca"),
    _club("Puebla"),
    _club(
        "Querétaro FC",
        "Queretaro FC",
        generic=("Querétaro", "Queretaro"),
    ),
    _club("Santos Laguna", "Club Santos Laguna"),
    _club("Club Tijuana", "Tijuana", "Tijuana de Caliente"),
    _club("Toluca", "Deportivo Toluca", "Deportivo Toluca FC"),
    _club("Tigres", "Tigres UANL"),
    _club("Pumas UNAM", "Pumas", "UNAM"),
    # Observed on public Kalshi KXLIGAMXGAME 2026-09-20 (Atlante vs Monterrey).
    _club("Atlante", "Atlante FC"),
)

# 2026/27 Süper Lig. Unique aliases are TFF legal names plus Kalshi/Polymarket
# labels captured 2026-09-21. Istanbul/Izmir/Rams remain fail-closed.
SUPER_LIG_CLUBS: tuple[SeniorClub, ...] = (
    _club("Alanyaspor", "Corendon Alanyaspor", "CORENDON Alanyaspor"),
    _club("Amed", "Amed Sportif", "Amed Sportif Faaliyetler", "Amed SFK"),
    _club(
        "Istanbul Basaksehir",
        "Basaksehir",
        "Başakşehir",
        "Istanbul Basaksehir FK",
        "İstanbul Başakşehir FK",
        "Rams Başakşehir FK",
        "Rams Basaksehir FK",
    ),
    _club("Besiktas", "Beşiktaş", "Beşiktaş A.Ş.", "Besiktas Istanbul"),
    _club("Corum", "Çorum FK", "Corum FK", "Arca Çorum FK", "Arca Corum FK"),
    _club("Erzurumspor", "Erzurumspor FK", "Erzurum"),
    _club("Eyupspor", "Eyüpspor"),
    _club("Fenerbahce", "Fenerbahçe", "Fenerbahçe A.Ş.", "Fenerbahce Istanbul"),
    _club("Galatasaray", "Galatasaray A.Ş.", "Galatasaray Istanbul"),
    _club("Gaziantep", "Gaziantep FK", "Gaziantep Futbol Kulübü A.Ş."),
    _club("Genclerbirligi", "Gençlerbirliği", "Genclerbirligi SK", "Gençlerbirliği SK"),
    _club("Goztepe", "Göztepe", "Göztepe A.Ş.", "Goztepe Izmir"),
    _club("Kasimpasa", "Kasımpaşa", "Kasımpaşa A.Ş.", "Kasimpasa Istanbul"),
    _club("Kocaelispor", "Kocaeli"),
    _club("Konyaspor", "Tümosan Konyaspor", "TÜMOSAN Konyaspor"),
    _club("Rizespor", "Caykur Rizespor", "Çaykur Rizespor", "Çaykur Rizespor A.Ş."),
    _club("Samsunspor", "Samsunspor A.Ş."),
    _club("Trabzonspor", "Trabzonspor A.Ş."),
)

# 2026/27 Primeira Liga. Sporting/Nacional/Lisbon/Vitoria stay fail-closed.
PRIMEIRA_LIGA_CLUBS: tuple[SeniorClub, ...] = (
    _club("Academico Viseu", "Académico de Viseu", "Académico de Viseu FC", "Viseu"),
    _club("Alverca", "FC Alverca", "FC Alverca SAD"),
    _club("Arouca", "FC Arouca"),
    _club("Benfica", "SL Benfica", "Sport Lisboa e Benfica"),
    _club("Casa Pia", "Casa Pia AC", "Casa Pia Lisbon"),
    _club("Estoril", "Estoril Praia", "GD Estoril Praia"),
    _club("Estrela Amadora", "CF Estrela da Amadora", "Estrela da Amadora"),
    _club("Famalicao", "Famalicão", "FC Famalicão", "FC Famalicao"),
    _club("Porto", "FC Porto"),
    _club("Gil Vicente", "Gil Vicente FC", "Gil Vicente Barcelos", "Vicente Barcelos"),
    _club("Maritimo", "Marítimo", "CS Marítimo"),
    _club("Moreirense", "Moreirense FC"),
    _club("Nacional Madeira", "CD Nacional", "Nacional da Madeira"),
    _club("Rio Ave", "Rio Ave FC"),
    _club("Santa Clara", "CD Santa Clara", "Santa Clara Azores"),
    _club("Sporting CP", "Sporting Lisbon"),
    _club("Braga", "SC Braga"),
    _club(
        "Guimaraes",
        "Vitória SC",
        "Vitoria SC",
        "Vitoria SC Guimaraes",
        "Vitória de Guimarães",
        "Vitória Guimarães",
    ),
)

# 2026/27 Ligue 1. Paris stays fail-closed (PSG vs Paris FC).
LIGUE_1_CLUBS: tuple[SeniorClub, ...] = (
    _club("Angers", "Angers SCO"),
    _club("Auxerre", "AJ Auxerre"),
    _club("Brest", "Stade Brest", "Stade Brest 29", "Stade Brestois 29"),
    _club("Le Havre", "Le Havre AC"),
    _club("Le Mans", "Le Mans FC"),
    _club("Lens", "RC Lens", "Racing Club De Lens"),
    _club("Lille", "Lille OSC", "LOSC Lille"),
    _club("Lorient", "FC Lorient"),
    _club("Lyon", "Olympique Lyonnais", "Olympique Lyon"),
    _club("Marseille", "Olympique Marseille", "Olympique de Marseille"),
    _club("Monaco", "AS Monaco", "AS Monaco FC"),
    _club("Nice", "OGC Nice"),
    _club("Paris FC"),
    _club("Paris Saint-Germain", "PSG"),
    _club("Rennes", "Stade Rennais", "Stade Rennais FC", "Stade Rennes"),
    _club("Strasbourg", "Strasbourg Alsace", "RC Strasbourg", "RC Strasbourg Alsace"),
    _club("Toulouse", "Toulouse FC"),
    _club("Troyes", "ESTAC Troyes", "ES Troyes AC"),
)

# 2026/27 Eredivisie. Sparta stays fail-closed (Sparta Prague).
EREDIVISIE_CLUBS: tuple[SeniorClub, ...] = (
    _club("ADO Den Haag", "Den Haag"),
    _club("Ajax", "Ajax Amsterdam", "AFC Ajax"),
    _club("AZ Alkmaar", "AZ", "Alkmaar"),
    _club("Cambuur", "SC Cambuur", "SC Cambuur-Leeuwarden"),
    _club("Excelsior", "Excelsior Rotterdam"),
    _club("Feyenoord", "Feyenoord Rotterdam"),
    _club("Fortuna Sittard", "Sittard"),
    _club("Go Ahead Eagles", "GA Eagles"),
    _club("Groningen", "FC Groningen"),
    _club("Heerenveen", "SC Heerenveen"),
    _club("NEC Nijmegen", "NEC", "Nijmegen"),
    _club("PEC Zwolle", "Zwolle"),
    _club("PSV Eindhoven", "PSV"),
    _club("Sparta Rotterdam"),
    _club("Telstar", "SC Telstar", "Telstar 1963"),
    _club("Twente", "FC Twente", "FC Twente Enschede", "Enschede"),
    _club("Utrecht", "FC Utrecht"),
    _club("Willem II", "Willem II Tilburg"),
)

# 2026/27 Scottish Premiership. Dundee and Dundee United stay distinct.
SCOTTISH_PREMIERSHIP_CLUBS: tuple[SeniorClub, ...] = (
    _club("Aberdeen", "Aberdeen FC"),
    _club("Celtic", "Celtic FC"),
    _club("Dundee", "Dundee FC"),
    _club("Dundee United", "Dundee United FC"),
    _club("Falkirk", "Falkirk FC"),
    _club("Heart of Midlothian", "Hearts", "Heart of Midlothian FC"),
    _club("Hibernian", "Hibernian FC", "Hibs"),
    _club("Kilmarnock", "Kilmarnock FC"),
    _club("Motherwell", "Motherwell FC"),
    _club("Rangers", "Rangers FC"),
    _club("St Johnstone", "St. Johnstone", "St Johnstone FC"),
    _club("St Mirren", "St. Mirren", "St Mirren FC"),
)

# 2026/27 Belgian Pro League. Brugge/Bruges/Standard stay fail-closed.
BELGIAN_PRO_LEAGUE_CLUBS: tuple[SeniorClub, ...] = (
    _club("Anderlecht", "RSC Anderlecht"),
    _club("Royal Antwerp", "Royal Antwerp FC", "Antwerp"),
    _club("Beveren"),
    _club("Cercle Brugge"),
    _club("Charleroi", "Royal Charleroi", "Royal Charleroi SC"),
    _club("Club Brugge"),
    _club("Genk", "KRC Genk"),
    _club("Gent", "KAA Gent"),
    _club("Kortrijk"),
    _club("La Louviere", "La Louvière", "RAAL La Louviere"),
    _club("Lommel", "Lommel SK"),
    _club("Mechelen", "Yellow-Red KV Mechelen"),
    _club("OH Leuven", "Leuven", "Oud-Heverlee Leuven"),
    _club("Sint-Truiden", "St. Truidense", "St. Truidense VV"),
    _club("Standard Liege", "Standard Liège"),
    _club("Union Saint-Gilloise", "Union Gilloise", "Union SG"),
    _club("Westerlo", "KVC Westerlo"),
    _club("Zulte Waregem", "SV Zulte Waregem"),
)

# 2026/27 EFL League One. Overlapping Championship canonicals are reused.
LEAGUE_ONE_CLUBS: tuple[SeniorClub, ...] = (
    _club("AFC Wimbledon", "Wimbledon"),
    _club("Barnsley"),
    _club("Blackpool"),
    _club("Bradford City", "Bradford", "Bradford City AFC"),
    _club("Bromley", "Bromley FC"),
    _club("Burton Albion", "Burton"),
    _club("Cambridge United", "Cambridge"),
    _club("Doncaster Rovers", "Doncaster"),
    _club("Huddersfield Town", "Huddersfield"),
    _club("Leicester City", "Leicester"),
    _club("Leyton Orient"),
    _club("Luton Town", "Luton"),
    _club("Mansfield Town", "Mansfield"),
    _club("Milton Keynes Dons", "Milton Keynes", "MK Dons"),
    _club("Notts County", "Notts"),
    _club("Oxford United", "Oxford"),
    _club("Peterborough United", "Peterborough"),
    _club("Plymouth Argyle", "Plymouth"),
    _club("Reading"),
    _club("Sheffield Wednesday", "Sheffield Weds"),
    _club("Stevenage", "Stevenage FC"),
    _club("Stockport County", "Stockport"),
    _club("Wigan Athletic", "Wigan"),
    _club("Wycombe Wanderers", "Wycombe"),
)

# 2026 Brazilian Série A. Bare Vitoria stays fail-closed (Vitória SC).
BRASILEIRAO_CLUBS: tuple[SeniorClub, ...] = (
    _club("Athletico Paranaense", "Paranaense", "CA Paranaense"),
    _club("Atletico Mineiro", "Atlético Mineiro", "Atletico Mineiro MG", "CA Mineiro"),
    _club("Bahia", "EC Bahia", "EC Bahia BA"),
    _club("Botafogo", "Botafogo FR", "Botafogo FR RJ"),
    _club("Chapecoense", "Chapecoense SC"),
    _club("Corinthians", "SC Corinthians", "SC Corinthians SP"),
    _club("Coritiba", "Coritiba FBC"),
    _club("Cruzeiro", "Cruzeiro EC", "Cruzeiro EC MG"),
    _club("Flamengo", "CR Flamengo", "CR Flamengo RJ"),
    _club("Fluminense", "Fluminense FC", "Fluminense FC RJ"),
    _club("Gremio", "Grêmio", "Gremio FB Porto Alegrense RS", "Grêmio FBPA"),
    _club("Internacional", "SC Internacional", "SC Internacional RS"),
    _club("Mirassol", "Mirassol FC", "Mirassol FC SP"),
    _club("Palmeiras", "SE Palmeiras", "SE Palmeiras SP"),
    _club("Red Bull Bragantino", "Bragantino", "Red Bull Bragantino SP"),
    _club("Remo", "Clube do Remo"),
    _club("Santos", "Santos FC", "Santos FC SP"),
    _club("Sao Paulo", "São Paulo", "Sao Paulo FC", "São Paulo FC", "Sao Paulo FC SP"),
    _club("Vasco da Gama", "CR Vasco da Gama", "CR Vasco da Gama RJ"),
    _club("EC Vitoria", "EC Vitória", "EC Vitoria BA", "EC Vitória BA"),
)

# 2026 Argentine Primera. Same-token clubs stay fully qualified.
ARGENTINA_PRIMERA_CLUBS: tuple[SeniorClub, ...] = (
    _club("Aldosivi", "CA Aldosivi"),
    _club("Argentinos Juniors", "AA Argentinos Juniors"),
    _club("Atletico Tucuman", "Atlético Tucumán", "CA Tucumán", "CA Tucuman"),
    _club("Banfield"),
    _club("Barracas Central", "Barracas", "CA Barracas Central"),
    _club("Belgrano", "Belgrano de Cordoba"),
    _club("Boca Juniors"),
    _club("Central Cordoba", "Central Córdoba"),
    _club("Defensa y Justicia", "CSyD Defensa y Justicia"),
    _club("Deportivo Riestra", "Riestra"),
    _club("Estudiantes La Plata", "Estudiantes de La Plata"),
    _club("Estudiantes Rio Cuarto", "Estudiantes RC", "Rio Cuarto"),
    _club("Gimnasia La Plata", "Gimnasia y Esgrima de La Plata"),
    _club("Gimnasia Mendoza", "Gimnasia y Esgrima (M)"),
    _club("Huracan", "Huracán", "CA Huracán"),
    _club("Independiente Avellaneda", "CA Independiente"),
    _club("Independiente Rivadavia", "Rivadavia", "CS Independiente Rivadavia"),
    _club("Instituto", "Instituto AC Córdoba", "Instituto Cordoba"),
    _club("Lanus", "Lanús", "CA Lanús"),
    _club("Newells Old Boys", "Newell's Old Boys", "CA Newell's Old Boys"),
    _club("Platense"),
    _club("Racing Avellaneda", "Racing"),
    _club("River Plate", "CA River Plate"),
    _club("Rosario Central"),
    _club("San Lorenzo", "San Lorenzo de Almagro", "CA San Lorenzo de Almagro"),
    _club("Sarmiento Junin", "Sarmiento"),
    _club("Talleres Cordoba", "Talleres", "CA Talleres"),
    _club("Tigre", "CA Tigre"),
    _club("Union Santa Fe", "CA Unión"),
    _club("Velez Sarsfield", "Vélez Sarsfield"),
)

# 2026/27 Saudi Pro League. AL Suqoor stays UNKNOWN.
SAUDI_PRO_LEAGUE_CLUBS: tuple[SeniorClub, ...] = (
    _club("Abha", "Abha Club"),
    _club("Al Ahli", "Al-Ahli", "Al Ahli Saudi", "Al Ahli Saudi FC"),
    _club("Al Diriyah", "Al-Diriyah", "Al-Diraiyah FC", "Diriyah Club"),
    _club("Al Ettifaq", "Al-Ettifaq", "Al-Ittifaq", "Al-Ittifaq FC", "Al Ettifaq Saudi Club"),
    _club("Al Faisaly", "Al-Faisaly", "Al-Faisaly FC"),
    _club("Al Fateh", "Al-Fateh", "Al Fateh Saudi Club"),
    _club("Al Fayha", "Al-Fayha"),
    _club("Al Hazem", "Al-Hazem"),
    _club("Al Hilal", "Al-Hilal", "Al-Hilal SFC"),
    _club("Al Ittihad", "Al-Ittihad", "Al-Ittihad Club"),
    _club("Al Khaleej", "Al-Khaleej"),
    _club("Al Kholood", "Al-Kholood"),
    _club("Al Nassr", "Al-Nassr", "Al Nassr Club"),
    _club("Al Qadsiah", "Al-Qadsiah", "Al Qadsiah"),
    _club("Al Riyadh", "Al-Riyadh", "Al-Riyadh SC", "Al Riyadh Saudi Club"),
    _club("Al Shabab", "Al-Shabab", "Al-Shabab FC (SA)"),
    _club("Al Taawoun", "Al-Taawoun"),
    _club("Neom", "Neom SC", "NEOM SC"),
)

# 2026 J1. Tokyo/Yokohama/Osaka city tokens stay fail-closed.
J1_LEAGUE_CLUBS: tuple[SeniorClub, ...] = (
    _club("Albirex Niigata", "Albirex"),
    _club("Avispa Fukuoka", "Avispa"),
    _club("Cerezo Osaka", "Cerezo", "Cerezo Ōsaka"),
    _club("Fagiano Okayama", "Fagiano O", "Fagiano Okayama"),
    _club("FC Tokyo", "FC Tōkyō"),
    _club("Gamba Osaka", "Gamba", "Gamba Ōsaka"),
    _club("Kashima Antlers", "Kashima"),
    _club("Kashiwa Reysol", "Kashiwa"),
    _club("Kawasaki Frontale", "Frontale"),
    _club("Kyoto Sanga", "Kyoto Sanga FC", "Kyōto Sanga FC"),
    _club("Machida Zelvia", "Machida Z", "FC Machida Zelvia"),
    _club("Mito Hollyhock", "Mito H", "FC Mito Holly Hock"),
    _club("Nagoya Grampus", "Nagoya"),
    _club("Sanfrecce Hiroshima", "Hiroshima"),
    _club("Shimizu S-Pulse", "Shimizu"),
    _club("Shonan Bellmare", "Shonan"),
    _club("Tokyo Verdy", "Tokyo V"),
    _club("Urawa Red Diamonds", "Urawa"),
    _club("Vissel Kobe", "Kobe", "Vissel Kōbe"),
    _club("Yokohama F Marinos", "Marinos", "Yokohama F. Marinos"),
    _club("Yokohama FC"),
    _club("JEF United Chiba", "United Chiba", "JEF United Ichihara Chiba"),
    _club("V-Varen Nagasaki", "V-Varen"),
)

# Continental reuse of curated domestic tables. MLS / Liga MX stay off this
# union so Champions League does not inherit Miami / Leon / America generics.
_DOMESTIC_CLUBS: tuple[SeniorClub, ...] = (
    *PREMIER_LEAGUE_CLUBS,
    *CHAMPIONSHIP_CLUBS,
    *LEAGUE_ONE_CLUBS,
    *LA_LIGA_CLUBS,
    *BUNDESLIGA_CLUBS,
    *SERIE_A_CLUBS,
    *LIGUE_1_CLUBS,
    *EREDIVISIE_CLUBS,
    *PRIMEIRA_LIGA_CLUBS,
    *SCOTTISH_PREMIERSHIP_CLUBS,
    *BELGIAN_PRO_LEAGUE_CLUBS,
    *SUPER_LIG_CLUBS,
    *BRASILEIRAO_CLUBS,
    *ARGENTINA_PRIMERA_CLUBS,
    *SAUDI_PRO_LEAGUE_CLUBS,
    *J1_LEAGUE_CLUBS,
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
    _club("Republic of Ireland", "Ireland"),
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
    _club("Turkey", "Turkiye", "Türkiye"),
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
    # UEFA Nations League 24 Sep–6 Oct 2026 observed senior men's sides
    # (Kalshi KXUEFANLGAME / Matchbook COMPETITION tags, retrieved 2026-09-22).
    _club("Andorra"),
    _club("Malta"),
    _club("Israel"),
    _club("Kosovo"),
    _club("Liechtenstein"),
    _club("Lithuania"),
    _club("Armenia"),
    _club("Latvia"),
    _club("Georgia"),
    _club("Hungary"),
    _club("Montenegro"),
    _club("Cyprus"),
    _club(
        "Bosnia and Herzegovina",
        "Bosnia-Herzegovina",
        "Bosnia",
    ),
    _club("Romania"),
    _club("Slovenia"),
    _club("Bulgaria"),
    _club("Luxembourg"),
    _club("Faroe Islands"),
    _club("Kazakhstan"),
    _club("Iceland"),
    _club("Estonia"),
    _club("San Marino"),
    _club("Finland"),
    _club("Albania"),
    _club("Belarus"),
    _club("North Macedonia"),
    _club("Slovakia"),
    _club("Moldova"),
    _club("Azerbaijan"),
    _club("Gibraltar"),
)

CLUBS_BY_COMPETITION: dict[str, tuple[SeniorClub, ...]] = {
    PREMIER_LEAGUE: PREMIER_LEAGUE_CLUBS,
    CHAMPIONSHIP: CHAMPIONSHIP_CLUBS,
    LEAGUE_ONE: LEAGUE_ONE_CLUBS,
    LA_LIGA: LA_LIGA_CLUBS,
    BUNDESLIGA: BUNDESLIGA_CLUBS,
    SERIE_A: SERIE_A_CLUBS,
    LIGUE_1: LIGUE_1_CLUBS,
    EREDIVISIE: EREDIVISIE_CLUBS,
    PRIMEIRA_LIGA: PRIMEIRA_LIGA_CLUBS,
    SCOTTISH_PREMIERSHIP: SCOTTISH_PREMIERSHIP_CLUBS,
    BELGIAN_PRO_LEAGUE: BELGIAN_PRO_LEAGUE_CLUBS,
    SUPER_LIG: SUPER_LIG_CLUBS,
    BRASILEIRAO: BRASILEIRAO_CLUBS,
    ARGENTINA_PRIMERA: ARGENTINA_PRIMERA_CLUBS,
    SAUDI_PRO_LEAGUE: SAUDI_PRO_LEAGUE_CLUBS,
    J1_LEAGUE: J1_LEAGUE_CLUBS,
    MLS: MLS_CLUBS,
    LIGA_MX: LIGA_MX_CLUBS,
    # Cups reuse the relevant domestic senior set. Lower-division cup sides
    # stay uncurated rather than inventing a second club universe.
    CARABAO_CUP: PREMIER_LEAGUE_CLUBS + CHAMPIONSHIP_CLUBS,
    FA_CUP: PREMIER_LEAGUE_CLUBS + CHAMPIONSHIP_CLUBS,
    COPA_DEL_REY: LA_LIGA_CLUBS,
    DFB_POKAL: BUNDESLIGA_CLUBS,
    COPPA_ITALIA: SERIE_A_CLUBS,
    CHAMPIONS_LEAGUE: _DOMESTIC_CLUBS,
    EUROPA_LEAGUE: _DOMESTIC_CLUBS,
    CONFERENCE_LEAGUE: _DOMESTIC_CLUBS,
    COPA_LIBERTADORES: BRASILEIRAO_CLUBS + ARGENTINA_PRIMERA_CLUBS,
    INTERNATIONAL_FRIENDLIES: NATIONAL_TEAMS,
    UEFA_NATIONS_LEAGUE: NATIONAL_TEAMS,
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
            merged_aliases = tuple(dict.fromkeys((*existing.aliases, *club.aliases)))
            merged_generic = tuple(
                dict.fromkeys((*existing.generic_aliases, *club.generic_aliases))
            )
            by_key[key] = SeniorClub(
                canonical_name=existing.canonical_name,
                aliases=merged_aliases,
                generic_aliases=merged_generic,
            )
    return tuple(by_key[key] for key in sorted(by_key))


def alias_pairs() -> tuple[tuple[str, str], ...]:
    """Unique aliases including self-aliases. Generic city tokens are excluded."""

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


def generic_aliases_for(competition: str) -> tuple[tuple[str, str], ...]:
    """Competition-scoped generic aliases such as Miami or Leon."""

    clubs = CLUBS_BY_COMPETITION.get(str(competition), ())
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for club in clubs:
        for alias in club.generic_aliases:
            key = (normalize_text(alias), normalize_text(club.canonical_name))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((alias, club.canonical_name))
    return tuple(pairs)


def canonical_team_names() -> frozenset[str]:
    from sports_hedge.normalization.text import normalize_text as _normalize

    return frozenset(_normalize(club.canonical_name) for club in all_senior_clubs())
