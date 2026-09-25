import { handleDesktopStatus } from "../../../../lib/desktop-controller";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET(): Response {
  return handleDesktopStatus();
}
