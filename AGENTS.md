# Sports Hedge — Agent Instructions

Before implementing or reviewing any Sports Hedge change:

1. Read `docs/CORE_TENETS.md`.
2. Identify every applicable file under `docs/core-tenets/`.
3. Read the detailed feature/architecture specification linked from those tenets.
4. Preserve the Phase 1 paper-only/read-only venue boundary.

Before handing off a PR:

1. Run the relevant tests, lint and/or frontend build.
2. Review the change against every applicable core tenet.
3. In the PR body or final agent comment, list:
   - applicable tenets;
   - satisfied tenets;
   - partial/deferred items;
   - any known conflict;
   - whether data shown is live, historical, modelled or fixture/demo.
4. Do not silently weaken a tenet to complete a task.

If a task conflicts with a core tenet, stop and make the conflict explicit for architect review.

Detailed review guidance: `docs/core-tenets/12_AGENT_REVIEW_CONTRACT.md`.
