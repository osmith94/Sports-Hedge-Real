import type { VenueDegradationIncident } from "./api";

export function venueDegradationFilename(venue: string, capturedAt: string): string {
  const stamp = String(capturedAt || "unknown")
    .replace(/[:.]/g, "-")
    .replace(/[^\dTZ-]/gi, "");
  return `sports-hedge-${venue}-degradation-${stamp}.json`;
}

export type JsonDownloadBridge = {
  createObjectURL?: (blob: Blob) => string;
  revokeObjectURL?: (url: string) => void;
  click?: (anchor: { href: string; download: string }) => void;
};

export function downloadVenueWhyIncident(
  incident: VenueDegradationIncident,
  bridge: JsonDownloadBridge = {},
): { filename: string; href: string; json: string } {
  const filename = venueDegradationFilename(incident.affected_venue, incident.captured_at);
  const json = `${JSON.stringify(incident, null, 2)}\n`;
  const blob = new Blob([json], { type: "application/json" });
  const createObjectURL =
    bridge.createObjectURL ??
    (typeof URL !== "undefined" && typeof URL.createObjectURL === "function"
      ? (value: Blob) => URL.createObjectURL(value)
      : undefined);
  const href = createObjectURL
    ? createObjectURL(blob)
    : `data:application/json;charset=utf-8,${encodeURIComponent(json)}`;
  const click =
    bridge.click ??
    ((anchor) => {
      if (typeof document === "undefined") return;
      const node = document.createElement("a");
      node.href = anchor.href;
      node.download = anchor.download;
      node.rel = "noopener";
      document.body.appendChild(node);
      node.click();
      node.remove();
    });
  click({ href, download: filename });
  const revoke =
    bridge.revokeObjectURL ??
    (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function"
      ? (value: string) => URL.revokeObjectURL(value)
      : undefined);
  if (revoke && href.startsWith("blob:")) revoke(href);
  return { filename, href, json };
}
