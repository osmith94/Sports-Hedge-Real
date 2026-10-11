import { readFile, readdir } from "node:fs/promises";
import path from "node:path";

import { SOUND_FILES, type SoundCue } from "../../../../lib/sound-alerts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Operator-owned, local MP3s live in the repo-root `sounds/` directory.
 * No binary game audio is checked into GitHub. Only the four fixed cues can
 * be requested: never accept an arbitrary filename or filesystem path.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ cue: string }> },
): Promise<Response> {
  const { cue } = await params;
  if (!Object.prototype.hasOwnProperty.call(SOUND_FILES, cue)) {
    return new Response("Unknown sound", { status: 404 });
  }

  // Next.js is launched with frontend/ as cwd; don't rely on the caller's shell.
  const soundsDir = path.resolve(process.cwd(), "..", "sounds");
  const prefix = SOUND_FILES[cue as SoundCue].toLowerCase();
  try {
    const files = await readdir(soundsDir, { withFileTypes: true });
    const matched = files
      .filter((entry) => {
        if (!entry.isFile() || !entry.name.toLowerCase().endsWith(".mp3")) return false;
        const base = entry.name.slice(0, -4).toLowerCase();
        return base === prefix || base.startsWith(`${prefix}-`);
      })
      .sort((a, b) => a.name.length - b.name.length);
    if (!matched[0]) {
      return new Response("MP3 not found in sounds/", { status: 404 });
    }
    const bytes = await readFile(path.join(soundsDir, matched[0].name));
    return new Response(new Uint8Array(bytes), {
      headers: {
        "Content-Type": "audio/mpeg",
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    return new Response("Sound unavailable", { status: 404 });
  }
}
