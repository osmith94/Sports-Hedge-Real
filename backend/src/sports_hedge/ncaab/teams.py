"""Strict current NCAA Division I men's basketball registry.

Built from the 2026-09-22 ESPN public roster census (362 programs).
Aliases that map to more than one school are recorded as ambiguous
and never resolve. Learned aliases cannot bypass those conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sports_hedge.normalization.text import AliasRegistry, normalize_text


@dataclass(frozen=True, slots=True)
class NcaabProgram:
    abbreviation: str
    canonical: str
    location: str
    mascot: str
    espn_id: str
    aliases: tuple[str, ...]


NCAAB_PROGRAMS: tuple[NcaabProgram, ...] = (
    NcaabProgram("ACU", "Abilene Christian Wildcats", "Abilene Christian", "Wildcats", "2000", ()),
    NcaabProgram("AF", "Air Force Falcons", "Air Force", "Falcons", "2005", ()),
    NcaabProgram("AKR", "Akron Zips", "Akron", "Zips", "2006", ()),
    NcaabProgram("AAMU", "Alabama A&M Bulldogs", "Alabama A&M", "Bulldogs", "2010", ()),
    NcaabProgram("ALA", "Alabama Crimson Tide", "Alabama", "Crimson Tide", "333", ()),
    NcaabProgram("ALST", "Alabama State Hornets", "Alabama State", "Hornets", "2011", ()),
    NcaabProgram("ALCN", "Alcorn State Braves", "Alcorn State", "Braves", "2016", ()),
    NcaabProgram("AMER", "American University Eagles", "American University", "Eagles", "44", ()),
    NcaabProgram("APP", "App State Mountaineers", "App State", "Mountaineers", "2026", ()),
    NcaabProgram("ASU", "Arizona State Sun Devils", "Arizona State", "Sun Devils", "9", ()),
    NcaabProgram("ARIZ", "Arizona Wildcats", "Arizona", "Wildcats", "12", ()),
    NcaabProgram("ARK", "Arkansas Razorbacks", "Arkansas", "Razorbacks", "8", ()),
    NcaabProgram("ARST", "Arkansas State Red Wolves", "Arkansas State", "Red Wolves", "2032", ()),
    NcaabProgram("UAPB", "Arkansas-Pine Bluff Golden Lions", "Arkansas-Pine Bluff", "Golden Lions", "2029", ()),
    NcaabProgram("ARMY", "Army Black Knights", "Army", "Black Knights", "349", ()),
    NcaabProgram("AUB", "Auburn Tigers", "Auburn", "Tigers", "2", ()),
    NcaabProgram("APSU", "Austin Peay Governors", "Austin Peay", "Governors", "2046", ()),
    NcaabProgram("BYU", "BYU Cougars", "BYU", "Cougars", "252", ()),
    NcaabProgram("BALL", "Ball State Cardinals", "Ball State", "Cardinals", "2050", ()),
    NcaabProgram("BAY", "Baylor Bears", "Baylor", "Bears", "239", ()),
    NcaabProgram("BELL", "Bellarmine Knights", "Bellarmine", "Knights", "91", ()),
    NcaabProgram("BEL", "Belmont Bruins", "Belmont", "Bruins", "2057", ()),
    NcaabProgram("BCU", "Bethune-Cookman Wildcats", "Bethune-Cookman", "Wildcats", "2065", ()),
    NcaabProgram("BING", "Binghamton Bearcats", "Binghamton", "Bearcats", "2066", ()),
    NcaabProgram("BOIS", "Boise State Broncos", "Boise State", "Broncos", "68", ()),
    NcaabProgram("BC", "Boston College Eagles", "Boston College", "Eagles", "103", ()),
    NcaabProgram("BU", "Boston University Terriers", "Boston University", "Terriers", "104", ()),
    NcaabProgram("BGSU", "Bowling Green Falcons", "Bowling Green", "Falcons", "189", ()),
    NcaabProgram("BRAD", "Bradley Braves", "Bradley", "Braves", "71", ()),
    NcaabProgram("BRWN", "Brown Bears", "Brown", "Bears", "225", ()),
    NcaabProgram("BRY", "Bryant Bulldogs", "Bryant", "Bulldogs", "2803", ()),
    NcaabProgram("BUCK", "Bucknell Bison", "Bucknell", "Bison", "2083", ()),
    NcaabProgram("BUF", "Buffalo Bulls", "Buffalo", "Bulls", "2084", ()),
    NcaabProgram("BTLR", "Butler Bulldogs", "Butler", "Bulldogs", "2086", ()),
    NcaabProgram("CP", "Cal Poly Mustangs", "Cal Poly", "Mustangs", "13", ()),
    NcaabProgram("CSUB", "Cal State Bakersfield Roadrunners", "Cal State Bakersfield", "Roadrunners", "2934", ()),
    NcaabProgram("CSUF", "Cal State Fullerton Titans", "Cal State Fullerton", "Titans", "2239", ()),
    NcaabProgram("CSUN", "Cal State Northridge Matadors", "Cal State Northridge", "Matadors", "2463", ()),
    NcaabProgram("CBU", "California Baptist Lancers", "California Baptist", "Lancers", "2856", ()),
    NcaabProgram("CAL", "California Golden Bears", "California", "Golden Bears", "25", ()),
    NcaabProgram("CAM", "Campbell Fighting Camels", "Campbell", "Fighting Camels", "2097", ()),
    NcaabProgram("CAN", "Canisius Golden Griffins", "Canisius", "Golden Griffins", "2099", ()),
    NcaabProgram("CARK", "Central Arkansas Bears", "Central Arkansas", "Bears", "2110", ()),
    NcaabProgram("CCSU", "Central Connecticut Blue Devils", "Central Connecticut", "Blue Devils", "2115", ()),
    NcaabProgram("CMU", "Central Michigan Chippewas", "Central Michigan", "Chippewas", "2117", ()),
    NcaabProgram("COFC", "Charleston Cougars", "Charleston", "Cougars", "232", ()),
    NcaabProgram("CHSO", "Charleston Southern Buccaneers", "Charleston Southern", "Buccaneers", "2127", ()),
    NcaabProgram("CLT", "Charlotte 49ers", "Charlotte", "49ers", "2429", ()),
    NcaabProgram("UTC", "Chattanooga Mocs", "Chattanooga", "Mocs", "236", ()),
    NcaabProgram("CHST", "Chicago State Cougars", "Chicago State", "Cougars", "2130", ()),
    NcaabProgram("CIN", "Cincinnati Bearcats", "Cincinnati", "Bearcats", "2132", ()),
    NcaabProgram("CLEM", "Clemson Tigers", "Clemson", "Tigers", "228", ()),
    NcaabProgram("CLE", "Cleveland State Vikings", "Cleveland State", "Vikings", "325", ()),
    NcaabProgram("CCU", "Coastal Carolina Chanticleers", "Coastal Carolina", "Chanticleers", "324", ()),
    NcaabProgram("COLG", "Colgate Raiders", "Colgate", "Raiders", "2142", ()),
    NcaabProgram("COLO", "Colorado Buffaloes", "Colorado", "Buffaloes", "38", ()),
    NcaabProgram("CSU", "Colorado State Rams", "Colorado State", "Rams", "36", ()),
    NcaabProgram("COLU", "Columbia Lions", "Columbia", "Lions", "171", ()),
    NcaabProgram("COPP", "Coppin State Eagles", "Coppin State", "Eagles", "2154", ()),
    NcaabProgram("COR", "Cornell Big Red", "Cornell", "Big Red", "172", ()),
    NcaabProgram("CREI", "Creighton Bluejays", "Creighton", "Bluejays", "156", ()),
    NcaabProgram("DART", "Dartmouth Big Green", "Dartmouth", "Big Green", "159", ()),
    NcaabProgram("DAV", "Davidson Wildcats", "Davidson", "Wildcats", "2166", ()),
    NcaabProgram("DAY", "Dayton Flyers", "Dayton", "Flyers", "2168", ()),
    NcaabProgram("DEP", "DePaul Blue Demons", "DePaul", "Blue Demons", "305", ()),
    NcaabProgram("DEL", "Delaware Blue Hens", "Delaware", "Blue Hens", "48", ()),
    NcaabProgram("DSU", "Delaware State Hornets", "Delaware State", "Hornets", "2169", ()),
    NcaabProgram("DEN", "Denver Pioneers", "Denver", "Pioneers", "2172", ()),
    NcaabProgram("DETM", "Detroit Mercy Titans", "Detroit Mercy", "Titans", "2174", ()),
    NcaabProgram("DRKE", "Drake Bulldogs", "Drake", "Bulldogs", "2181", ()),
    NcaabProgram("DREX", "Drexel Dragons", "Drexel", "Dragons", "2182", ()),
    NcaabProgram("DUKE", "Duke Blue Devils", "Duke", "Blue Devils", "150", ()),
    NcaabProgram("DUQ", "Duquesne Dukes", "Duquesne", "Dukes", "2184", ()),
    NcaabProgram("ECU", "East Carolina Pirates", "East Carolina", "Pirates", "151", ()),
    NcaabProgram("ETSU", "East Tennessee State Buccaneers", "East Tennessee State", "Buccaneers", "2193", ()),
    NcaabProgram("ETAM", "East Texas A&M Lions", "East Texas A&M", "Lions", "2837", ()),
    NcaabProgram("EIU", "Eastern Illinois Panthers", "Eastern Illinois", "Panthers", "2197", ()),
    NcaabProgram("EKU", "Eastern Kentucky Colonels", "Eastern Kentucky", "Colonels", "2198", ()),
    NcaabProgram("EMU", "Eastern Michigan Eagles", "Eastern Michigan", "Eagles", "2199", ()),
    NcaabProgram("EWU", "Eastern Washington Eagles", "Eastern Washington", "Eagles", "331", ()),
    NcaabProgram("ELON", "Elon Phoenix", "Elon", "Phoenix", "2210", ()),
    NcaabProgram("EVAN", "Evansville Purple Aces", "Evansville", "Purple Aces", "339", ()),
    NcaabProgram("FAIR", "Fairfield Stags", "Fairfield", "Stags", "2217", ()),
    NcaabProgram("FDU", "Fairleigh Dickinson Knights", "Fairleigh Dickinson", "Knights", "161", ()),
    NcaabProgram("FAMU", "Florida A&M Rattlers", "Florida A&M", "Rattlers", "50", ()),
    NcaabProgram("FAU", "Florida Atlantic Owls", "Florida Atlantic", "Owls", "2226", ()),
    NcaabProgram("FLA", "Florida Gators", "Florida", "Gators", "57", ()),
    NcaabProgram("FGCU", "Florida Gulf Coast Eagles", "Florida Gulf Coast", "Eagles", "526", ()),
    NcaabProgram("FIU", "Florida International Panthers", "Florida International", "Panthers", "2229", ()),
    NcaabProgram("FSU", "Florida State Seminoles", "Florida State", "Seminoles", "52", ()),
    NcaabProgram("FOR", "Fordham Rams", "Fordham", "Rams", "2230", ()),
    NcaabProgram("FRES", "Fresno State Bulldogs", "Fresno State", "Bulldogs", "278", ()),
    NcaabProgram("FUR", "Furman Paladins", "Furman", "Paladins", "231", ()),
    NcaabProgram("GWEB", "Gardner-Webb Runnin' Bulldogs", "Gardner-Webb", "Runnin' Bulldogs", "2241", ()),
    NcaabProgram("GMU", "George Mason Patriots", "George Mason", "Patriots", "2244", ()),
    NcaabProgram("GW", "George Washington Revolutionaries", "George Washington", "Revolutionaries", "45", ()),
    NcaabProgram("GTWN", "Georgetown Hoyas", "Georgetown", "Hoyas", "46", ()),
    NcaabProgram("UGA", "Georgia Bulldogs", "Georgia", "Bulldogs", "61", ()),
    NcaabProgram("GASO", "Georgia Southern Eagles", "Georgia Southern", "Eagles", "290", ()),
    NcaabProgram("GAST", "Georgia State Panthers", "Georgia State", "Panthers", "2247", ()),
    NcaabProgram("GT", "Georgia Tech Yellow Jackets", "Georgia Tech", "Yellow Jackets", "59", ()),
    NcaabProgram("GONZ", "Gonzaga Bulldogs", "Gonzaga", "Bulldogs", "2250", ('Zags',)),
    NcaabProgram("GRAM", "Grambling Tigers", "Grambling", "Tigers", "2755", ()),
    NcaabProgram("GCU", "Grand Canyon Lopes", "Grand Canyon", "Lopes", "2253", ()),
    NcaabProgram("GB", "Green Bay Phoenix", "Green Bay", "Phoenix", "2739", ()),
    NcaabProgram("HAMP", "Hampton Pirates", "Hampton", "Pirates", "2261", ()),
    NcaabProgram("HARV", "Harvard Crimson", "Harvard", "Crimson", "108", ()),
    NcaabProgram("HAW", "Hawai'i Rainbow Warriors", "Hawai'i", "Rainbow Warriors", "62", ()),
    NcaabProgram("HPU", "High Point Panthers", "High Point", "Panthers", "2272", ()),
    NcaabProgram("HOF", "Hofstra Pride", "Hofstra", "Pride", "2275", ()),
    NcaabProgram("HC", "Holy Cross Crusaders", "Holy Cross", "Crusaders", "107", ()),
    NcaabProgram("HCU", "Houston Christian Huskies", "Houston Christian", "Huskies", "2277", ()),
    NcaabProgram("HOU", "Houston Cougars", "Houston", "Cougars", "248", ()),
    NcaabProgram("HOW", "Howard Bison", "Howard", "Bison", "47", ()),
    NcaabProgram("IUIN", "IU Indianapolis Jaguars", "IU Indianapolis", "Jaguars", "85", ()),
    NcaabProgram("IDST", "Idaho State Bengals", "Idaho State", "Bengals", "304", ()),
    NcaabProgram("IDHO", "Idaho Vandals", "Idaho", "Vandals", "70", ()),
    NcaabProgram("ILL", "Illinois Fighting Illini", "Illinois", "Fighting Illini", "356", ()),
    NcaabProgram("ILST", "Illinois State Redbirds", "Illinois State", "Redbirds", "2287", ()),
    NcaabProgram("UIW", "Incarnate Word Cardinals", "Incarnate Word", "Cardinals", "2916", ()),
    NcaabProgram("IU", "Indiana Hoosiers", "Indiana", "Hoosiers", "84", ()),
    NcaabProgram("INST", "Indiana State Sycamores", "Indiana State", "Sycamores", "282", ()),
    NcaabProgram("IONA", "Iona Gaels", "Iona", "Gaels", "314", ()),
    NcaabProgram("IOWA", "Iowa Hawkeyes", "Iowa", "Hawkeyes", "2294", ()),
    NcaabProgram("ISU", "Iowa State Cyclones", "Iowa State", "Cyclones", "66", ()),
    NcaabProgram("JKST", "Jackson State Tigers", "Jackson State", "Tigers", "2296", ()),
    NcaabProgram("JAX", "Jacksonville Dolphins", "Jacksonville", "Dolphins", "294", ()),
    NcaabProgram("JXST", "Jacksonville State Gamecocks", "Jacksonville State", "Gamecocks", "55", ()),
    NcaabProgram("JMU", "James Madison Dukes", "James Madison", "Dukes", "256", ()),
    NcaabProgram("KC", "Kansas City Roos", "Kansas City", "Roos", "140", ()),
    NcaabProgram("KU", "Kansas Jayhawks", "Kansas", "Jayhawks", "2305", ()),
    NcaabProgram("KSU", "Kansas State Wildcats", "Kansas State", "Wildcats", "2306", ()),
    NcaabProgram("KENN", "Kennesaw State Owls", "Kennesaw State", "Owls", "338", ()),
    NcaabProgram("KENT", "Kent State Golden Flashes", "Kent State", "Golden Flashes", "2309", ()),
    NcaabProgram("UK", "Kentucky Wildcats", "Kentucky", "Wildcats", "96", ()),
    NcaabProgram("NOLA", "LSU New Orleans Privateers", "LSU New Orleans", "Privateers", "2443", ()),
    NcaabProgram("LSU", "LSU Tigers", "LSU", "Tigers", "99", ()),
    NcaabProgram("LAS", "La Salle Explorers", "La Salle", "Explorers", "2325", ()),
    NcaabProgram("LAF", "Lafayette Leopards", "Lafayette", "Leopards", "322", ()),
    NcaabProgram("LAM", "Lamar Cardinals", "Lamar", "Cardinals", "2320", ()),
    NcaabProgram("LEM", "Le Moyne Dolphins", "Le Moyne", "Dolphins", "2330", ()),
    NcaabProgram("LEH", "Lehigh Mountain Hawks", "Lehigh", "Mountain Hawks", "2329", ()),
    NcaabProgram("LIB", "Liberty Flames", "Liberty", "Flames", "2335", ()),
    NcaabProgram("LIP", "Lipscomb Bisons", "Lipscomb", "Bisons", "288", ()),
    NcaabProgram("LR", "Little Rock Trojans", "Little Rock", "Trojans", "2031", ()),
    NcaabProgram("LBSU", "Long Beach State Beach", "Long Beach State", "Beach", "299", ()),
    NcaabProgram("LIU", "Long Island University Sharks", "Long Island University", "Sharks", "112358", ()),
    NcaabProgram("LONG", "Longwood Lancers", "Longwood", "Lancers", "2344", ()),
    NcaabProgram("UL", "Louisiana Ragin' Cajuns", "Louisiana", "Ragin' Cajuns", "309", ()),
    NcaabProgram("LT", "Louisiana Tech Bulldogs", "Louisiana Tech", "Bulldogs", "2348", ()),
    NcaabProgram("LOU", "Louisville Cardinals", "Louisville", "Cardinals", "97", ()),
    NcaabProgram("LUC", "Loyola Chicago Ramblers", "Loyola Chicago", "Ramblers", "2350", ('Loyola of Chicago',)),
    NcaabProgram("L-MD", "Loyola Maryland Greyhounds", "Loyola Maryland", "Greyhounds", "2352", ('Loyola (MD)',)),
    NcaabProgram("LMU", "Loyola Marymount Lions", "Loyola Marymount", "Lions", "2351", ()),
    NcaabProgram("ME", "Maine Black Bears", "Maine", "Black Bears", "311", ()),
    NcaabProgram("MAN", "Manhattan Jaspers", "Manhattan", "Jaspers", "2363", ()),
    NcaabProgram("MRST", "Marist Red Foxes", "Marist", "Red Foxes", "2368", ()),
    NcaabProgram("MARQ", "Marquette Golden Eagles", "Marquette", "Golden Eagles", "269", ()),
    NcaabProgram("MRSH", "Marshall Thundering Herd", "Marshall", "Thundering Herd", "276", ()),
    NcaabProgram("UMES", "Maryland Eastern Shore Hawks", "Maryland Eastern Shore", "Hawks", "2379", ()),
    NcaabProgram("MD", "Maryland Terrapins", "Maryland", "Terrapins", "120", ()),
    NcaabProgram("MASS", "Massachusetts Minutemen", "Massachusetts", "Minutemen", "113", ()),
    NcaabProgram("MCN", "McNeese Cowboys", "McNeese", "Cowboys", "2377", ()),
    NcaabProgram("MEM", "Memphis Tigers", "Memphis", "Tigers", "235", ()),
    NcaabProgram("MER", "Mercer Bears", "Mercer", "Bears", "2382", ()),
    NcaabProgram("MERC", "Mercyhurst Lakers", "Mercyhurst", "Lakers", "2385", ()),
    NcaabProgram("MRMK", "Merrimack Warriors", "Merrimack", "Warriors", "2771", ()),
    NcaabProgram("M-OH", "Miami (OH) RedHawks", "Miami (OH)", "RedHawks", "193", ('Miami Ohio', 'Miami of Ohio', 'Red Hawks')),
    NcaabProgram("MIA", "Miami Hurricanes", "Miami", "Hurricanes", "2390", ('Miami (FL)', 'Miami FL', 'Miami Florida', 'The Hurricanes')),
    NcaabProgram("MSU", "Michigan State Spartans", "Michigan State", "Spartans", "127", ()),
    NcaabProgram("MICH", "Michigan Wolverines", "Michigan", "Wolverines", "130", ()),
    NcaabProgram("MTSU", "Middle Tennessee Blue Raiders", "Middle Tennessee", "Blue Raiders", "2393", ()),
    NcaabProgram("MILW", "Milwaukee Panthers", "Milwaukee", "Panthers", "270", ()),
    NcaabProgram("MINN", "Minnesota Golden Gophers", "Minnesota", "Golden Gophers", "135", ()),
    NcaabProgram("MSST", "Mississippi State Bulldogs", "Mississippi State", "Bulldogs", "344", ()),
    NcaabProgram("MVSU", "Mississippi Valley State Delta Devils", "Mississippi Valley State", "Delta Devils", "2400", ()),
    NcaabProgram("MOST", "Missouri State Bears", "Missouri State", "Bears", "2623", ()),
    NcaabProgram("MIZ", "Missouri Tigers", "Missouri", "Tigers", "142", ()),
    NcaabProgram("MONM", "Monmouth Hawks", "Monmouth", "Hawks", "2405", ()),
    NcaabProgram("MONT", "Montana Grizzlies", "Montana", "Grizzlies", "149", ()),
    NcaabProgram("MTST", "Montana State Bobcats", "Montana State", "Bobcats", "147", ()),
    NcaabProgram("MORE", "Morehead State Eagles", "Morehead State", "Eagles", "2413", ()),
    NcaabProgram("MORG", "Morgan State Bears", "Morgan State", "Bears", "2415", ("morgst",)),
    NcaabProgram("MSM", "Mount St. Mary's Mountaineers", "Mount St. Mary's", "Mountaineers", "116", ()),
    NcaabProgram("MUR", "Murray State Racers", "Murray State", "Racers", "93", ()),
    NcaabProgram("NCSU", "NC State Wolfpack", "NC State", "Wolfpack", "152", ()),
    NcaabProgram("NJIT", "NJIT Highlanders", "NJIT", "Highlanders", "2885", ()),
    NcaabProgram("NAVY", "Navy Midshipmen", "Navy", "Midshipmen", "2426", ()),
    NcaabProgram("NEB", "Nebraska Cornhuskers", "Nebraska", "Cornhuskers", "158", ()),
    NcaabProgram("NEV", "Nevada Wolf Pack", "Nevada", "Wolf Pack", "2440", ()),
    NcaabProgram("UNH", "New Hampshire Wildcats", "New Hampshire", "Wildcats", "160", ()),
    NcaabProgram("NHVN", "New Haven Chargers", "New Haven", "Chargers", "2441", ()),
    NcaabProgram("UNM", "New Mexico Lobos", "New Mexico", "Lobos", "167", ()),
    NcaabProgram("NMSU", "New Mexico State Aggies", "New Mexico State", "Aggies", "166", ()),
    NcaabProgram("NIA", "Niagara Purple Eagles", "Niagara", "Purple Eagles", "315", ()),
    NcaabProgram("NICH", "Nicholls Colonels", "Nicholls", "Colonels", "2447", ()),
    NcaabProgram("NORF", "Norfolk State Spartans", "Norfolk State", "Spartans", "2450", ()),
    NcaabProgram("UNA", "North Alabama Lions", "North Alabama", "Lions", "2453", ()),
    NcaabProgram("NCAT", "North Carolina A&T Aggies", "North Carolina A&T", "Aggies", "2448", ()),
    NcaabProgram("NCCU", "North Carolina Central Eagles", "North Carolina Central", "Eagles", "2428", ()),
    NcaabProgram("UNC", "North Carolina Tar Heels", "North Carolina", "Tar Heels", "153", ('Carolina',)),
    NcaabProgram("UND", "North Dakota Fighting Hawks", "North Dakota", "Fighting Hawks", "155", ()),
    NcaabProgram("NDSU", "North Dakota State Bison", "North Dakota State", "Bison", "2449", ()),
    NcaabProgram("UNF", "North Florida Ospreys", "North Florida", "Ospreys", "2454", ()),
    NcaabProgram("UNT", "North Texas Mean Green", "North Texas", "Mean Green", "249", ()),
    NcaabProgram("NE", "Northeastern Huskies", "Northeastern", "Huskies", "111", ()),
    NcaabProgram("NAU", "Northern Arizona Lumberjacks", "Northern Arizona", "Lumberjacks", "2464", ()),
    NcaabProgram("UNCO", "Northern Colorado Bears", "Northern Colorado", "Bears", "2458", ()),
    NcaabProgram("NIU", "Northern Illinois Huskies", "Northern Illinois", "Huskies", "2459", ()),
    NcaabProgram("UNI", "Northern Iowa Panthers", "Northern Iowa", "Panthers", "2460", ()),
    NcaabProgram("NKU", "Northern Kentucky Norse", "Northern Kentucky", "Norse", "94", ()),
    NcaabProgram("NWST", "Northwestern State Demons", "Northwestern State", "Demons", "2466", ()),
    NcaabProgram("NU", "Northwestern Wildcats", "Northwestern", "Wildcats", "77", ()),
    NcaabProgram("ND", "Notre Dame Fighting Irish", "Notre Dame", "Fighting Irish", "87", ()),
    NcaabProgram("OAK", "Oakland Golden Grizzlies", "Oakland", "Golden Grizzlies", "2473", ()),
    NcaabProgram("OHIO", "Ohio Bobcats", "Ohio", "Bobcats", "195", ()),
    NcaabProgram("OSU", "Ohio State Buckeyes", "Ohio State", "Buckeyes", "194", ()),
    NcaabProgram("OU", "Oklahoma Sooners", "Oklahoma", "Sooners", "201", ()),
    NcaabProgram("OKST", "Oklahoma State Cowboys", "Oklahoma State", "Cowboys", "197", ()),
    NcaabProgram("ODU", "Old Dominion Monarchs", "Old Dominion", "Monarchs", "295", ()),
    NcaabProgram("MISS", "Ole Miss Rebels", "Ole Miss", "Rebels", "145", ()),
    NcaabProgram("OMA", "Omaha Mavericks", "Omaha", "Mavericks", "2437", ()),
    NcaabProgram("ORU", "Oral Roberts Golden Eagles", "Oral Roberts", "Golden Eagles", "198", ()),
    NcaabProgram("ORE", "Oregon Ducks", "Oregon", "Ducks", "2483", ()),
    NcaabProgram("ORST", "Oregon State Beavers", "Oregon State", "Beavers", "204", ()),
    NcaabProgram("PAC", "Pacific Tigers", "Pacific", "Tigers", "279", ()),
    NcaabProgram("PSU", "Penn State Nittany Lions", "Penn State", "Nittany Lions", "213", ()),
    NcaabProgram("PENN", "Pennsylvania Quakers", "Pennsylvania", "Quakers", "219", ()),
    NcaabProgram("PEPP", "Pepperdine Waves", "Pepperdine", "Waves", "2492", ()),
    NcaabProgram("PITT", "Pittsburgh Panthers", "Pittsburgh", "Panthers", "221", ()),
    NcaabProgram("PORT", "Portland Pilots", "Portland", "Pilots", "2501", ()),
    NcaabProgram("PRST", "Portland State Vikings", "Portland State", "Vikings", "2502", ()),
    NcaabProgram("PV", "Prairie View A&M Panthers", "Prairie View A&M", "Panthers", "2504", ()),
    NcaabProgram("PRES", "Presbyterian Blue Hose", "Presbyterian", "Blue Hose", "2506", ()),
    NcaabProgram("PRIN", "Princeton Tigers", "Princeton", "Tigers", "163", ()),
    NcaabProgram("PROV", "Providence Friars", "Providence", "Friars", "2507", ()),
    NcaabProgram("PUR", "Purdue Boilermakers", "Purdue", "Boilermakers", "2509", ()),
    NcaabProgram("PFW", "Purdue Fort Wayne Mastodons", "Purdue Fort Wayne", "Mastodons", "2870", ()),
    NcaabProgram("QUIN", "Quinnipiac Bobcats", "Quinnipiac", "Bobcats", "2514", ()),
    NcaabProgram("RAD", "Radford Highlanders", "Radford", "Highlanders", "2515", ()),
    NcaabProgram("URI", "Rhode Island Rams", "Rhode Island", "Rams", "227", ()),
    NcaabProgram("RICE", "Rice Owls", "Rice", "Owls", "242", ()),
    NcaabProgram("RICH", "Richmond Spiders", "Richmond", "Spiders", "257", ()),
    NcaabProgram("RID", "Rider Broncs", "Rider", "Broncs", "2520", ()),
    NcaabProgram("RMU", "Robert Morris Colonials", "Robert Morris", "Colonials", "2523", ()),
    NcaabProgram("RUTG", "Rutgers Scarlet Knights", "Rutgers", "Scarlet Knights", "164", ()),
    NcaabProgram("SELA", "SE Louisiana Lions", "SE Louisiana", "Lions", "2545", ()),
    NcaabProgram("SIUE", "SIU Edwardsville Cougars", "SIU Edwardsville", "Cougars", "2565", ()),
    NcaabProgram("SMU", "SMU Mustangs", "SMU", "Mustangs", "2567", ()),
    NcaabProgram("SAC", "Sacramento State Hornets", "Sacramento State", "Hornets", "16", ()),
    NcaabProgram("SHU", "Sacred Heart Pioneers", "Sacred Heart", "Pioneers", "2529", ()),
    NcaabProgram("JOES", "Saint Joseph's Hawks", "Saint Joseph's", "Hawks", "2603", ("St. Joseph's", "St Joseph's", 'Saint Josephs')),
    NcaabProgram("SLU", "Saint Louis Billikens", "Saint Louis", "Billikens", "139", ()),
    NcaabProgram("SMC", "Saint Mary's Gaels", "Saint Mary's", "Gaels", "2608", ("St. Mary's", 'Saint Marys', 'St Marys')),
    NcaabProgram("SPU", "Saint Peter's Peacocks", "Saint Peter's", "Peacocks", "2612", ()),
    NcaabProgram("SHSU", "Sam Houston Bearkats", "Sam Houston", "Bearkats", "2534", ()),
    NcaabProgram("SAM", "Samford Bulldogs", "Samford", "Bulldogs", "2535", ()),
    NcaabProgram("SDSU", "San Diego State Aztecs", "San Diego State", "Aztecs", "21", ()),
    NcaabProgram("USD", "San Diego Toreros", "San Diego", "Toreros", "301", ()),
    NcaabProgram("SF", "San Francisco Dons", "San Francisco", "Dons", "2539", ()),
    NcaabProgram("SJSU", "San José State Spartans", "San José State", "Spartans", "23", ()),
    NcaabProgram("SCU", "Santa Clara Broncos", "Santa Clara", "Broncos", "2541", ()),
    NcaabProgram("SEA", "Seattle U Redhawks", "Seattle U", "Redhawks", "2547", ()),
    NcaabProgram("HALL", "Seton Hall Pirates", "Seton Hall", "Pirates", "2550", ()),
    NcaabProgram("SIE", "Siena Saints", "Siena", "Saints", "2561", ()),
    NcaabProgram("USA", "South Alabama Jaguars", "South Alabama", "Jaguars", "6", ()),
    NcaabProgram("SC", "South Carolina Gamecocks", "South Carolina", "Gamecocks", "2579", ()),
    NcaabProgram("SCST", "South Carolina State Bulldogs", "South Carolina State", "Bulldogs", "2569", ()),
    NcaabProgram("UPST", "South Carolina Upstate Spartans", "South Carolina Upstate", "Spartans", "2908", ()),
    NcaabProgram("SDAK", "South Dakota Coyotes", "South Dakota", "Coyotes", "233", ()),
    NcaabProgram("SDST", "South Dakota State Jackrabbits", "South Dakota State", "Jackrabbits", "2571", ()),
    NcaabProgram("USF", "South Florida Bulls", "South Florida", "Bulls", "58", ()),
    NcaabProgram("SEMO", "Southeast Missouri State Redhawks", "Southeast Missouri State", "Redhawks", "2546", ()),
    NcaabProgram("SIU", "Southern Illinois Salukis", "Southern Illinois", "Salukis", "79", ()),
    NcaabProgram("SOU", "Southern Jaguars", "Southern", "Jaguars", "2582", ()),
    NcaabProgram("USM", "Southern Miss Golden Eagles", "Southern Miss", "Golden Eagles", "2572", ()),
    NcaabProgram("SUU", "Southern Utah Thunderbirds", "Southern Utah", "Thunderbirds", "253", ()),
    NcaabProgram("SBU", "St. Bonaventure Bonnies", "St. Bonaventure", "Bonnies", "179", ()),
    NcaabProgram("SJU", "St. John's Red Storm", "St. John's", "Red Storm", "2599", ("Saint John's", 'St Johns', 'St. Johns')),
    NcaabProgram("STMN", "St. Thomas Tommies", "St. Thomas", "Tommies", "2900", ()),
    NcaabProgram("STAN", "Stanford Cardinal", "Stanford", "Cardinal", "24", ()),
    NcaabProgram("SFA", "Stephen F. Austin Lumberjacks", "Stephen F. Austin", "Lumberjacks", "2617", ()),
    NcaabProgram("STET", "Stetson Hatters", "Stetson", "Hatters", "56", ()),
    NcaabProgram("STO", "Stonehill Skyhawks", "Stonehill", "Skyhawks", "284", ()),
    NcaabProgram("STBK", "Stony Brook Seawolves", "Stony Brook", "Seawolves", "2619", ()),
    NcaabProgram("SYR", "Syracuse Orange", "Syracuse", "Orange", "183", ()),
    NcaabProgram("TCU", "TCU Horned Frogs", "TCU", "Horned Frogs", "2628", ()),
    NcaabProgram("TAR", "Tarleton State Texans", "Tarleton State", "Texans", "2627", ()),
    NcaabProgram("TEM", "Temple Owls", "Temple", "Owls", "218", ()),
    NcaabProgram("TNST", "Tennessee State Tigers", "Tennessee State", "Tigers", "2634", ()),
    NcaabProgram("TNTC", "Tennessee Tech Golden Eagles", "Tennessee Tech", "Golden Eagles", "2635", ()),
    NcaabProgram("TENN", "Tennessee Volunteers", "Tennessee", "Volunteers", "2633", ('Vols',)),
    NcaabProgram("TA&M", "Texas A&M Aggies", "Texas A&M", "Aggies", "245", ()),
    NcaabProgram("AMCC", "Texas A&M-Corpus Christi Islanders", "Texas A&M-Corpus Christi", "Islanders", "357", ()),
    NcaabProgram("TEX", "Texas Longhorns", "Texas", "Longhorns", "251", ()),
    NcaabProgram("TXSO", "Texas Southern Tigers", "Texas Southern", "Tigers", "2640", ()),
    NcaabProgram("TXST", "Texas State Bobcats", "Texas State", "Bobcats", "326", ()),
    NcaabProgram("TTU", "Texas Tech Red Raiders", "Texas Tech", "Red Raiders", "2641", ()),
    NcaabProgram("CIT", "The Citadel Bulldogs", "The Citadel", "Bulldogs", "2643", ()),
    NcaabProgram("TOL", "Toledo Rockets", "Toledo", "Rockets", "2649", ()),
    NcaabProgram("TOW", "Towson Tigers", "Towson", "Tigers", "119", ()),
    NcaabProgram("TROY", "Troy Trojans", "Troy", "Trojans", "2653", ()),
    NcaabProgram("TULN", "Tulane Green Wave", "Tulane", "Green Wave", "2655", ()),
    NcaabProgram("TLSA", "Tulsa Golden Hurricane", "Tulsa", "Golden Hurricane", "202", ()),
    NcaabProgram("UAB", "UAB Blazers", "UAB", "Blazers", "5", ()),
    NcaabProgram("UALB", "UAlbany Great Danes", "UAlbany", "Great Danes", "399", ()),
    NcaabProgram("UCD", "UC Davis Aggies", "UC Davis", "Aggies", "302", ()),
    NcaabProgram("UCI", "UC Irvine Anteaters", "UC Irvine", "Anteaters", "300", ()),
    NcaabProgram("UCR", "UC Riverside Highlanders", "UC Riverside", "Highlanders", "27", ()),
    NcaabProgram("UCSD", "UC San Diego Tritons", "UC San Diego", "Tritons", "28", ()),
    NcaabProgram("UCSB", "UC Santa Barbara Gauchos", "UC Santa Barbara", "Gauchos", "2540", ()),
    NcaabProgram("UCF", "UCF Knights", "UCF", "Knights", "2116", ()),
    NcaabProgram("UCLA", "UCLA Bruins", "UCLA", "Bruins", "26", ()),
    NcaabProgram("CONN", "UConn Huskies", "UConn", "Huskies", "41", ('Connecticut', 'Connecticut Huskies')),
    NcaabProgram("UIC", "UIC Flames", "UIC", "Flames", "82", ()),
    NcaabProgram("ULM", "UL Monroe Warhawks", "UL Monroe", "Warhawks", "2433", ()),
    NcaabProgram("UMBC", "UMBC Retrievers", "UMBC", "Retrievers", "2378", ()),
    NcaabProgram("UML", "UMass Lowell River Hawks", "UMass Lowell", "River Hawks", "2349", ()),
    NcaabProgram("UNCA", "UNC Asheville Bulldogs", "UNC Asheville", "Bulldogs", "2427", ()),
    NcaabProgram("UNCG", "UNC Greensboro Spartans", "UNC Greensboro", "Spartans", "2430", ()),
    NcaabProgram("UNCW", "UNC Wilmington Seahawks", "UNC Wilmington", "Seahawks", "350", ()),
    NcaabProgram("UNLV", "UNLV Rebels", "UNLV", "Rebels", "2439", ()),
    NcaabProgram("USC", "USC Trojans", "USC", "Trojans", "30", ('Southern California', 'Southern Cal')),
    NcaabProgram("UTA", "UT Arlington Mavericks", "UT Arlington", "Mavericks", "250", ()),
    NcaabProgram("UTM", "UT Martin Skyhawks", "UT Martin", "Skyhawks", "2630", ()),
    NcaabProgram("RGV", "UT Rio Grande Valley Vaqueros", "UT Rio Grande Valley", "Vaqueros", "292", ()),
    NcaabProgram("UTEP", "UTEP Miners", "UTEP", "Miners", "2638", ()),
    NcaabProgram("UTSA", "UTSA Roadrunners", "UTSA", "Roadrunners", "2636", ()),
    NcaabProgram("USU", "Utah State Aggies", "Utah State", "Aggies", "328", ()),
    NcaabProgram("UTU", "Utah Tech Trailblazers", "Utah Tech", "Trailblazers", "3101", ()),
    NcaabProgram("UTAH", "Utah Utes", "Utah", "Utes", "254", ()),
    NcaabProgram("UVU", "Utah Valley Wolverines", "Utah Valley", "Wolverines", "3084", ()),
    NcaabProgram("VCU", "VCU Rams", "VCU", "Rams", "2670", ()),
    NcaabProgram("VMI", "VMI Keydets", "VMI", "Keydets", "2678", ()),
    NcaabProgram("VAL", "Valparaiso Beacons", "Valparaiso", "Beacons", "2674", ()),
    NcaabProgram("VAN", "Vanderbilt Commodores", "Vanderbilt", "Commodores", "238", ()),
    NcaabProgram("UVM", "Vermont Catamounts", "Vermont", "Catamounts", "261", ()),
    NcaabProgram("VILL", "Villanova Wildcats", "Villanova", "Wildcats", "222", ()),
    NcaabProgram("UVA", "Virginia Cavaliers", "Virginia", "Cavaliers", "258", ()),
    NcaabProgram("VT", "Virginia Tech Hokies", "Virginia Tech", "Hokies", "259", ()),
    NcaabProgram("WAG", "Wagner Seahawks", "Wagner", "Seahawks", "2681", ()),
    NcaabProgram("WAKE", "Wake Forest Demon Deacons", "Wake Forest", "Demon Deacons", "154", ()),
    NcaabProgram("WASH", "Washington Huskies", "Washington", "Huskies", "264", ()),
    NcaabProgram("WSU", "Washington State Cougars", "Washington State", "Cougars", "265", ()),
    NcaabProgram("WEB", "Weber State Wildcats", "Weber State", "Wildcats", "2692", ()),
    NcaabProgram("UWF", "West Florida Argonauts", "West Florida", "Argonauts", "2697", ()),
    NcaabProgram("WGA", "West Georgia Wolves", "West Georgia", "Wolves", "2698", ()),
    NcaabProgram("WVU", "West Virginia Mountaineers", "West Virginia", "Mountaineers", "277", ()),
    NcaabProgram("WCU", "Western Carolina Catamounts", "Western Carolina", "Catamounts", "2717", ()),
    NcaabProgram("WIU", "Western Illinois Leathernecks", "Western Illinois", "Leathernecks", "2710", ()),
    NcaabProgram("WKU", "Western Kentucky Hilltoppers", "Western Kentucky", "Hilltoppers", "98", ()),
    NcaabProgram("WMU", "Western Michigan Broncos", "Western Michigan", "Broncos", "2711", ()),
    NcaabProgram("WICH", "Wichita State Shockers", "Wichita State", "Shockers", "2724", ()),
    NcaabProgram("W&M", "William & Mary Tribe", "William & Mary", "Tribe", "2729", ()),
    NcaabProgram("WIN", "Winthrop Eagles", "Winthrop", "Eagles", "2737", ()),
    NcaabProgram("WIS", "Wisconsin Badgers", "Wisconsin", "Badgers", "275", ()),
    NcaabProgram("WOF", "Wofford Terriers", "Wofford", "Terriers", "2747", ()),
    NcaabProgram("WRST", "Wright State Raiders", "Wright State", "Raiders", "2750", ()),
    NcaabProgram("WYO", "Wyoming Cowboys", "Wyoming", "Cowboys", "2751", ()),
    NcaabProgram("XAV", "Xavier Musketeers", "Xavier", "Musketeers", "2752", ()),
    NcaabProgram("YALE", "Yale Bulldogs", "Yale", "Bulldogs", "43", ()),
    NcaabProgram("YSU", "Youngstown State Penguins", "Youngstown State", "Penguins", "2754", ()),
)

# Generic labels that must never choose among similarly named schools.
_HARD_AMBIGUOUS = frozenset(
    {
        "miami",
        "state",
        "tech",
        "a m",
        "st",
        "saint",
        "loyola",
        "carolina",
        "ut",
        "southern",
        "university",
        "college",
        "sc",
    }
)

# Women's / other basketball that must not resolve as D1 men.
_REJECT_LABELS = frozenset(
    {
        "wnba",
        "nba",
        "g league",
        "nba g league",
        "summer league",
        "nba summer league",
        "ncaaw",
        "ncaa women",
        "womens college basketball",
        "women's college basketball",
        "cwbb",
        "d2",
        "d3",
        "division ii",
        "division iii",
        "naia",
        "junior college",
        "juco",
        "high school",
    }
)


def _auto_aliases(program: NcaabProgram) -> tuple[str, ...]:
    values = [
        program.canonical,
        program.abbreviation,
        program.location,
        program.mascot,
        *program.aliases,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return tuple(out)


def _build_registry() -> tuple[AliasRegistry, dict[str, NcaabProgram], frozenset[str], frozenset[str]]:
    registry = AliasRegistry()
    by_canonical: dict[str, NcaabProgram] = {}
    canonicals: set[str] = set()
    claimed: dict[str, str] = {}
    collisions: set[str] = set()
    for program in NCAAB_PROGRAMS:
        canonical_norm = normalize_text(program.canonical)
        if canonical_norm in canonicals:
            raise ValueError(f"duplicate NCAAB canonical: {program.canonical}")
        canonicals.add(canonical_norm)
        by_canonical[canonical_norm] = program
        registry.add(program.canonical, program.canonical)
        claimed[canonical_norm] = canonical_norm
        for alias in _auto_aliases(program):
            key = normalize_text(alias)
            if not key or key in _HARD_AMBIGUOUS:
                collisions.add(key)
                continue
            owner = claimed.get(key)
            if owner is None:
                claimed[key] = canonical_norm
                registry.add(alias, program.canonical)
            elif owner != canonical_norm:
                collisions.add(key)
    # Strip collided aliases so resolve() cannot pick a winner.
    for key in collisions:
        registry.aliases.pop(key, None)
    return registry, by_canonical, frozenset(canonicals), frozenset(k for k in collisions if k)


ncaab_alias_registry, _BY_CANONICAL, _CANONICAL_NAMES, _COLLIDED_ALIASES = _build_registry()

AMBIGUOUS_NCAAB_LABELS = frozenset(_HARD_AMBIGUOUS | _COLLIDED_ALIASES)


@dataclass(frozen=True, slots=True)
class NcaabTeamResolution:
    canonical: str | None
    abbreviation: str | None
    ambiguous: bool
    rejected: bool
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.canonical) and not self.ambiguous and not self.rejected


def resolve_ncaab_team(value: str | None) -> NcaabTeamResolution:
    """Resolve a provider label onto one current D1 men's program, or fail closed."""

    normalized = normalize_text(str(value or ""))
    if not normalized:
        return NcaabTeamResolution(None, None, False, True, "empty_ncaab_team_label")
    if normalized in _REJECT_LABELS:
        return NcaabTeamResolution(None, None, False, True, "excluded_basketball_population")
    if normalized in AMBIGUOUS_NCAAB_LABELS:
        return NcaabTeamResolution(
            None,
            None,
            True,
            False,
            "ambiguous_ncaab_school_name",
        )
    resolved = ncaab_alias_registry.resolve(value or "")
    if resolved in _CANONICAL_NAMES:
        program = _BY_CANONICAL[resolved]
        return NcaabTeamResolution(program.canonical, program.abbreviation, False, False)
    return NcaabTeamResolution(None, None, False, False, "unknown_ncaab_team")


def is_canonical_ncaab_team(name: str | None) -> bool:
    return normalize_text(str(name or "")) in _CANONICAL_NAMES


def ncaab_teams_conflict(left: str, right: str) -> bool:
    if left == right:
        return False
    return is_canonical_ncaab_team(left) and is_canonical_ncaab_team(right)


def require_resolved_ncaab_team(value: str | None) -> str:
    resolved = resolve_ncaab_team(value)
    if not resolved.ok or resolved.canonical is None:
        raise ValueError(resolved.reason or "unresolved_ncaab_team")
    return resolved.canonical


def ncaab_program_count() -> int:
    return len(NCAAB_PROGRAMS)


def ncaab_short_name(team: str) -> str:
    resolved = resolve_ncaab_team(team)
    if resolved.ok and resolved.canonical:
        program = _BY_CANONICAL[normalize_text(resolved.canonical)]
        return program.location or program.canonical
    return team


NCAAB_ABBREVIATIONS: tuple[str, ...] = tuple(program.abbreviation for program in NCAAB_PROGRAMS)
