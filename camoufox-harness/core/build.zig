const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const driver_mod = b.createModule(.{
        .root_source_file = b.path("driver.zig"),
        .target = target,
        .optimize = optimize,
    });

    const exe = b.addExecutable(.{
        .name = "core",
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/main.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    exe.root_module.addImport("driver", driver_mod);
    b.installArtifact(exe);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    if (b.args) |args| run_cmd.addArgs(args);
    const run_step = b.step("run", "Run the transport smoke driver (needs a Camoufox binary arg)");
    run_step.dependOn(&run_cmd.step);

    // Faz 7: perf benchmark binary (round-trip latency per domain, RSS,
    // cold start, N-context degradation). Usage: perf <camoufox-bin>.
    const perf_exe = b.addExecutable(.{
        .name = "perf",
        .root_module = b.createModule(.{
            .root_source_file = b.path("tests/perf/main.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    perf_exe.root_module.addImport("driver", driver_mod);
    b.installArtifact(perf_exe);

    const run_perf = b.addRunArtifact(perf_exe);
    run_perf.step.dependOn(b.getInstallStep());
    if (b.args) |args| run_perf.addArgs(args);
    const perf_step = b.step("perf", "Run the Camoufox perf benchmark (needs a Camoufox binary arg)");
    perf_step.dependOn(&run_perf.step);

    const tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("tests.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    tests.root_module.addImport("driver", driver_mod);
    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);
}
