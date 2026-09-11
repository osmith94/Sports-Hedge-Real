export const opportunities = [
  {
    event: "Newcastle United v Arsenal",
    market: "Match result",
    venues: "Matchbook / Polymarket",
    grossEdge: "2.41%",
    netEdge: "1.86%",
    executable: "£620",
    profit: "£11.53",
    risk: "Low",
    confidence: "99.4%",
  },
  {
    event: "Valencia v Sevilla",
    market: "Over 2.5 goals",
    venues: "Matchbook / Polymarket",
    grossEdge: "1.72%",
    netEdge: "1.13%",
    executable: "£380",
    profit: "£4.29",
    risk: "Moderate",
    confidence: "98.8%",
  },
  {
    event: "Inter v Napoli",
    market: "Both teams to score",
    venues: "Matchbook / Polymarket",
    grossEdge: "1.29%",
    netEdge: "0.71%",
    executable: "£910",
    profit: "£6.46",
    risk: "Low",
    confidence: "99.1%",
  },
];

export const movementPoints = [
  { label: "-90m", probability: 42.0 },
  { label: "-75m", probability: 42.2 },
  { label: "-60m", probability: 42.4 },
  { label: "-45m", probability: 42.8 },
  { label: "Team sheet", probability: 43.1 },
  { label: "+2m", probability: 47.8 },
  { label: "+5m", probability: 49.4 },
  { label: "+15m", probability: 47.6 },
  { label: "+30m", probability: 46.3 },
];

export const reactions = [
  { market: "Match result", firstResponse: "10s", peak: "+6.3pp", retracement: "49%" },
  { market: "Player shots", firstResponse: "24s", peak: "+8.1pp", retracement: "31%" },
  { market: "Total corners", firstResponse: "1m 42s", peak: "+3.9pp", retracement: "58%" },
  { market: "Cards", firstResponse: "2m 18s", peak: "+2.1pp", retracement: "22%" },
];

export const trendCards = [
  {
    title: "Team-sheet shock → partial reversion",
    detail: "Premier League · home favourites · 30 minutes after announcement",
    effect: "41% retrace",
    sample: "N=38",
    stability: "High",
  },
  {
    title: "Corners react after match-result market",
    detail: "Team-sheet events · comparable starting-price bucket",
    effect: "+78s lag",
    sample: "N=26",
    stability: "Medium",
  },
  {
    title: "Red-card price move persists",
    detail: "In-play match result · first 10 minutes after dismissal",
    effect: "82% retained",
    sample: "N=54",
    stability: "High",
  },
  {
    title: "Liquidity accelerates into team sheets",
    detail: "Match result · 120m to 45m before kickoff",
    effect: "+64% depth",
    sample: "N=112",
    stability: "High",
  },
];

export const paperPositions = [
  {
    event: "Newcastle United v Arsenal",
    strategy: "3-way arb",
    capital: "£620",
    lockedProfit: "£11.53",
    return: "1.86%",
    lock: "3h 12m",
    status: "Open",
  },
  {
    event: "Valencia v Sevilla",
    strategy: "2-way arb",
    capital: "£380",
    lockedProfit: "£4.29",
    return: "1.13%",
    lock: "5h 05m",
    status: "Open",
  },
];
