"""The CI-run sampler's absolute-state OTLP metric dispositions."""

from __future__ import annotations

# The sampler re-emits all three payloads every day. Keep this exact field set
# separate from the generic OTLP backend's near-capacity module, while the
# backend expands it into _METRIC_DISPOSITION before it validates or exports.
CI_RUN_GAUGE_DISPOSITIONS: dict[tuple[str, str], str] = {
    **{
        ("ci_runs_daily", field): "gauge"
        for field in [
            "runs",
            "instant_skip_runs",
            "watchdog_runs",
            "qa_gate_runs",
            "proof_runs",
            "cancelled_runs",
            "superseded_runs",
            "superseded_zero_runs",
            "failed_runs",
            "retried_failed_runs",
            "self_healed_runs",
            "abandoned_runs",
            "zombie_runs",
            "prs_completed",
            "prs_with_runs",
            "per_pr_duration_median_minutes",
            "per_pr_duration_p90_minutes",
            "per_pr_runs_median",
            "per_pr_runs_p90",
            "per_pr_runs_executed_median",
            "white_run_share",
            "retry_share",
            "noise_run_share",
            "first_pass_pr_share",
        ]
    },
    **{
        ("ci_workflow_window", field): "gauge"
        for field in [
            "runs",
            "failed_runs",
            "self_healed_runs",
            "retried_failed_runs",
            "cancelled_runs",
            "superseded_runs",
            "instant_skip_runs",
            "prs_appeared_on",
            "pr_appearance_share",
            "retry_share",
            "exec_median_seconds",
            "exec_p90_seconds",
        ]
    },
    **{
        ("ci_runs_run", field): "gauge"
        for field in ["window_days", "window_runs", "window_prs", "api_requests"]
    },
}
