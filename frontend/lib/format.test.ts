import { describe, expect, it } from "vitest";

import { kickoffLocalLabel, kickoffRelativeLabel, relativeTime } from "./format";

const KICKOFF_UTC = "2026-09-21T15:00:00.000Z";
const KICKOFF_LOCAL_FORMAT: Intl.DateTimeFormatOptions = {
  weekday: "short",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short",
};

describe("kickoffLocalLabel hydration stability", () => {
  it("keeps the initial SSR and first client render on the canonical ISO", () => {
    expect(kickoffLocalLabel(KICKOFF_UTC)).toBe(KICKOFF_UTC);
    expect(kickoffLocalLabel(KICKOFF_UTC, null)).toBe(KICKOFF_UTC);
    expect(kickoffLocalLabel(KICKOFF_UTC, Number.NaN)).toBe(KICKOFF_UTC);
    expect(kickoffLocalLabel(KICKOFF_UTC, undefined)).toBe(KICKOFF_UTC);
  });

  it("matches relativeTime / kickoffRelativeLabel before hydration so React sees identical text", () => {
    expect(relativeTime(KICKOFF_UTC)).toBe(KICKOFF_UTC);
    expect(kickoffRelativeLabel(KICKOFF_UTC)).toBe(KICKOFF_UTC);
    expect(kickoffLocalLabel(KICKOFF_UTC)).toBe(relativeTime(KICKOFF_UTC));
  });

  it("does not use runtime-dependent Intl during the hydration render", () => {
    const date = new Date(KICKOFF_UTC);
    const enGb = new Intl.DateTimeFormat("en-GB", KICKOFF_LOCAL_FORMAT).format(date);
    const enUs = new Intl.DateTimeFormat("en-US", KICKOFF_LOCAL_FORMAT).format(date);
    expect(enGb).not.toBe(enUs);
    expect(kickoffLocalLabel(KICKOFF_UTC)).not.toBe(enGb);
    expect(kickoffLocalLabel(KICKOFF_UTC)).not.toBe(enUs);
    expect(kickoffLocalLabel(KICKOFF_UTC)).toBe(KICKOFF_UTC);
  });
});

describe("kickoffLocalLabel hydrated local display", () => {
  it("formats a useful operator-local kickoff after hydration", () => {
    const now = Date.parse(KICKOFF_UTC);
    const label = kickoffLocalLabel(KICKOFF_UTC, now);
    const expected = new Intl.DateTimeFormat(undefined, KICKOFF_LOCAL_FORMAT).format(
      new Date(KICKOFF_UTC),
    );
    expect(label).toBe(expected);
    expect(label).not.toBe(KICKOFF_UTC);
    expect(label).toMatch(/\d/);
    expect(label.length).toBeGreaterThan(8);
  });

  it("does not change kickoff semantics or invent a different instant", () => {
    const now = Date.parse(KICKOFF_UTC);
    expect(now).toBe(Date.parse("2026-09-21T15:00:00.000Z"));
    expect(kickoffLocalLabel(KICKOFF_UTC)).toBe(KICKOFF_UTC);
    expect(kickoffLocalLabel(null)).toBe("—");
    expect(kickoffLocalLabel(undefined)).toBe("—");
    expect(kickoffLocalLabel("")).toBe("—");
    expect(kickoffLocalLabel("not-a-kickoff", now)).toBe("not-a-kickoff");
    expect(kickoffLocalLabel(KICKOFF_UTC, now)).toBe(
      new Intl.DateTimeFormat(undefined, KICKOFF_LOCAL_FORMAT).format(new Date(now)),
    );
  });
});
