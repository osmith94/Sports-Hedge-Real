# Optional GTA sound alerts (browser-only)

The Sports Hedge dashboard can play four operator-provided, locally stored MP3s for durable scanner milestones. This changes **no** scanner cadence, Price-2 logic, live execution logic, order decisions, capital settings or venue access.

## Cues

| Durable event | Audio cue | Local MP3 basename (prefix) |
| --- | --- | --- |
| `qualifying_detected` (qualifying arb found; not just a near-ARB) | “Ah shit, here we go again” | `gta-here-we-go-again` |
| Price-2 audit `accepted` (does **not** imply a fill) | “Ooooh CJ, my dog” | `gta-san-andreas` |
| `paper_fill_complete` (trade-entered journal event) | Mission passed | `grand-theft-auto-san-andreas-mission-pass` |
| Price-2 audit `rejected` or `incomplete_unavailable` | “All you had to do was follow the damn train” | `big-smoke-train-cj` |

**The original files are local-only** and are deliberately not committed to GitHub. Put your .mp3 files in `C:\Users\oli_s\Sports-Hedge-Real\sounds\` (the repository-root `sounds/` folder shown in the operator screenshot), with the corresponding prefixes. Exact prefix + `.mp3`, or prefix followed by `-...` and `.mp3` are accepted. If a clip isn't the one intended, rename that local file to the matching prefix or update the `SOUND_FILES` map in `frontend/lib/sound-alerts.ts`.

## How to use

1. With Sports Hedge running locally, open `http://127.0.0.1:3000/`.
2. Click **🔇 GTA sounds OFF** in the top bar. It becomes **🔊 GTA sounds ON** and plays the “Here we go again” sample immediately to satisfy browser autoplay restrictions and confirm that the MP3 is available.
3. New qualifying arbs, Price-2 accepted/rejected audits and trade entries now play their corresponding sounds. Click the same button to turn them off and stop any current audio.

Sounds default OFF after a page reload or backend restart. Previous audit records are not replayed after enabling sound. The browser polls existing read-only audit endpoints every 8 seconds **only while sounds are enabled**, with a bounded 2-minute window; no database writes, venue calls or changes to execution. The soundboard deduplicates stable audit IDs, coalesces duplicate cue types in each poll, and queues at most 8 cues to avoid an alert storm. Browser tab/audio policy and polling latency mean that delivery is best effort, not guaranteed real-time. Missing/unavailable files produce a visible audio message and no impact on scanning.

The `paper_fill_complete` journal milestone is not proof that exchange-native orders filled in Real mode. As always, refer to persisted native ACK/reconciliation status for Real order outcomes.

## Debug each clip without switching execution ON

Visit `http://127.0.0.1:3000/api/sounds/arb_detected` for the detection MP3, or replace the trailing cue with `price2_accepted`, `price2_failed`, or `trade_entered`. Missing MP3s return 404; no private path is accepted. The mapping assumes the screenshot's general `gta-san-andreas.mp3` clip contains the CJ greeting, but the audio bytes were not provided to this branch and have not been independently verified.
