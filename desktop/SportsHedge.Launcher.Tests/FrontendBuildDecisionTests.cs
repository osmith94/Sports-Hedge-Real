using SportsHedge.Launcher.Core.Build;
using Xunit;

namespace SportsHedge.Launcher.Tests;

public sealed class FrontendBuildDecisionTests
{
    private const string Sha = "abc1230000000000000000000000000000000000";

    private static FrontendBuildMarker Marker(string sha = Sha, string buildId = "b1", bool dirty = false) => new()
    {
        GitSha = sha,
        NextBuildId = buildId,
        BuildTimestamp = "2026-09-25T00:00:00Z",
        FrontendDirty = dirty,
        Builder = "test",
    };

    [Fact]
    public void Matching_sha_and_build_id_skips_rebuild()
    {
        var decision = FrontendBuildDecision.Evaluate(Sha, false, Marker(), "b1");
        Assert.False(decision.RebuildRequired);
        Assert.Equal(BuildDecisionReason.Match, decision.Reason);
    }

    [Fact]
    public void Sha_comparison_ignores_case()
    {
        Assert.False(FrontendBuildDecision.Evaluate(Sha.ToUpperInvariant(), false, Marker(), "b1").RebuildRequired);
    }

    [Fact]
    public void Sha_mismatch_requires_rebuild_and_reports_both_shas()
    {
        var decision = FrontendBuildDecision.Evaluate("def4560000000000000000000000000000000000", false, Marker(), "b1");
        Assert.True(decision.RebuildRequired);
        Assert.Equal(BuildDecisionReason.ShaMismatch, decision.Reason);
        Assert.Contains("Current checkout SHA: def456", decision.Describe());
        Assert.Contains($"Frontend built for SHA: {Sha}", decision.Describe());
    }

    [Fact]
    public void Missing_marker_requires_rebuild()
    {
        var decision = FrontendBuildDecision.Evaluate(Sha, false, null, "b1");
        Assert.True(decision.RebuildRequired);
        Assert.Equal(BuildDecisionReason.MissingMarker, decision.Reason);
    }

    [Fact]
    public void Missing_next_build_requires_rebuild_even_if_marker_matches()
    {
        Assert.Equal(BuildDecisionReason.MissingNextBuild, FrontendBuildDecision.Evaluate(Sha, false, Marker(), null).Reason);
        Assert.Equal(BuildDecisionReason.MissingNextBuild, FrontendBuildDecision.Evaluate(Sha, false, Marker(), " ").Reason);
    }

    [Fact]
    public void A_different_next_build_than_recorded_requires_rebuild()
    {
        // e.g. a manual `npm run build` replaced .next after the marker was written.
        var decision = FrontendBuildDecision.Evaluate(Sha, false, Marker(buildId: "b1"), "b2");
        Assert.True(decision.RebuildRequired);
        Assert.Equal(BuildDecisionReason.NextBuildIdMismatch, decision.Reason);
    }

    [Fact]
    public void Invalid_marker_requires_rebuild()
    {
        Assert.Equal(BuildDecisionReason.InvalidMarker, FrontendBuildDecision.Evaluate(Sha, false, Marker() with { Schema = "other" }, "b1").Reason);
        Assert.Equal(BuildDecisionReason.InvalidMarker, FrontendBuildDecision.Evaluate(Sha, false, Marker(sha: ""), "b1").Reason);
        Assert.Equal(BuildDecisionReason.InvalidMarker, FrontendBuildDecision.Evaluate(Sha, false, Marker(buildId: ""), "b1").Reason);
    }

    [Fact]
    public void Uncommitted_frontend_changes_require_rebuild()
    {
        Assert.Equal(BuildDecisionReason.DirtyNow, FrontendBuildDecision.Evaluate(Sha, true, Marker(), "b1").Reason);
        Assert.Equal(BuildDecisionReason.DirtyAtBuild, FrontendBuildDecision.Evaluate(Sha, false, Marker(dirty: true), "b1").Reason);
    }

    [Fact]
    public void Marker_store_round_trips_and_treats_corruption_as_missing()
    {
        var layout = TestLayouts.Create();
        TestLayouts.WriteBuild(layout, Sha, buildId: "xyz");

        var read = FrontendBuildMarkerStore.Read(layout.FrontendBuildMarkerPath);
        Assert.NotNull(read);
        Assert.Equal(Sha, read!.GitSha);
        Assert.Equal("xyz", read.NextBuildId);
        Assert.Contains("\"git_sha\"", File.ReadAllText(layout.FrontendBuildMarkerPath));
        Assert.Contains("\"build_timestamp\"", File.ReadAllText(layout.FrontendBuildMarkerPath));
        Assert.Equal("xyz", FrontendBuildMarkerStore.ReadNextBuildId(layout.NextBuildIdPath));

        File.WriteAllText(layout.FrontendBuildMarkerPath, "{not json");
        Assert.Null(FrontendBuildMarkerStore.Read(layout.FrontendBuildMarkerPath));
        FrontendBuildMarkerStore.Delete(layout.FrontendBuildMarkerPath);
        Assert.Null(FrontendBuildMarkerStore.Read(layout.FrontendBuildMarkerPath));
    }

    [Fact]
    public void Marker_lives_inside_next_output_so_next_dev_or_refresh_wipes_invalidate_it()
    {
        var layout = TestLayouts.Create();
        Assert.StartsWith(layout.NextDir, layout.FrontendBuildMarkerPath, StringComparison.Ordinal);
    }

    [Fact]
    public void Build_script_writes_the_same_marker_schema()
    {
        var script = File.ReadAllText(Path.Combine(TestPaths.RepoRoot, "scripts", "windows", "Build-SportsHedge-App.ps1"));
        Assert.Contains(FrontendBuildMarker.CurrentSchema, script);
        foreach (var field in new[] { "git_sha", "git_branch", "build_timestamp", "next_build_id", "frontend_dirty", "builder" })
        {
            Assert.Contains(field, script);
        }
        Assert.Contains("sports-hedge-build.json", script);
    }
}
