import { handleDesktopExit } from "../../../../lib/desktop-controller";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  return handleDesktopExit(request);
}
