"""Frozen schema generations and pre-reset completeness guards.

These inventories are restore boundaries, not executable migration history.
Keep the 2026-08-14 inventory until older backup/PITR restores are ruled out.
"""

from shared.migration_errors import MigrationHistoryGap

_RESET_ANCHOR = "20260923T031516_schema-baseline"

# The 59 timestamped migrations squashed into db/schema.sql at the v0.1.0
# release (2026-08-14 schema reset). Frozen at reset time — do not bump. A DB
# whose applied set contains ANY of these must contain ALL of them before the
# convergence path may delete their tracking rows (see assert_reset_history); a partial set means schema effects that never ran.
_V010_PRE_RESET_SET = frozenset(
    {
        "20260719T223436_root-task-default-parent",
        "20260720T050943_agents-meta-last-active-at",
        "20260720T191255_agents-meta-hibernating-status",
        "20260721T042152_drop-inbound-notify-trigger",
        "20260721T082401_agent-notices-task-id",
        "20260721T082402_agent-tasks-priority",
        "20260721T090000_agents-meta-termination-source",
        "20260722T051500_agent-events-rollup-tables",
        "20260722T072626_agent-events-monthly-partitioning",
        "20260723T023228_add-explorer-preset",
        "20260725T025418_task-reminder-column-rename",
        "20260725T054607_rename-skills-ava-prefix",
        "20260725T060802_pin-haiku-dated-model-id",
        "20260725T074822_rename-skills-ava-prefix-round2",
        "20260728T055350_add-last-wedged-check-at",
        "20260729T041500_cluster-update-lock-note",
        "20260729T093000_termination-source-integrity",
        "20260731T042431_skill-names-dash-canonical",
        "20260731T071400_agent-birth-config",
        "20260731T071500_cluster-defaults",
        "20260731T071600_default-model-deepseek-v4-flash",
        "20260731T084500_seed-presets-drop-skill-index-list",
        "20260731T151000_cluster-last-update",
        "20260801T041104_up-since-at-expand",
        "20260802T202812_inbound-claimed-at",
        "20260803T180647_ops-alert-rules",
        "20260803T181500_ops-metrics-table",
        "20260804T190513_retire-ops-alert-rules",
        "20260804T190839_unified-events-table",
        "20260804T203036_events-readers-neighbors",
        "20260804T214534_ops-alerts",
        "20260805T001003_ops-alerts-source",
        "20260805T083741_kind-category-final",
        "20260807T010148_events-kind-to-event-name",
        "20260807T040600_delivery-alerted-dedup",
        "20260807T054700_pages-serve-dir-reopen",
        "20260807T083219_cluster-ops-idempotency",
        "20260807T183600_skill-names-canonical-three-store",
        "20260807T213500_api-idempotency",
        "20260808T043000_r1-deploy-state-tables",
        "20260808T073335_r1-host-paused-at",
        "20260808T075000_skill-identity-config-refs",
        "20260808T104500_agent-watchers",
        "20260808T184958_select-all-lateral-indexes",
        "20260808T200000_unify-ops-idempotency",
        "20260808T203000_agent-tasks-constraints",
        "20260809T030358_fyi-answerable",
        "20260810T140000_blob-autovacuum-tuning",
        "20260810T224124_events-level-index-includes-critical",
        "20260810T224356_schedule-runs-agent-fk",
        "20260811T050000_contract-sweep-dead-tables",
        "20260811T051000_agent-watchers-composite-pk",
        "20260812T000000_machines-is-staging",
        "20260812T040636_agent-liveness-state",
        "20260812T230738_drop-ops-metrics",
        "20260813T042527_alerts",
        "20260813T231327_events-target-agent-id-index",
        "20260814T092155_llm-usage-cost-snapshot",
        "20260814T182039_machines-pause",
    }
)


# Exact up-file inventory folded on 2026-09-23 from ff598105f. Never extend it.
_PRE_RESET_SET = frozenset(
    {
        "20260814T235959_v010-baseline",
        "20260818T142518_llm-cost-rollup-columns",
        "20260820T175737_extension-registry",
        "20260821T023527_drop-agent-neighbors",
        "20260821T062700_events-frozen-archive-comment",
        "20260821T104519_add-force-terminate-inbound-fence",
        "20260821T141320_watcher-template-version",
        "20260822T082315_client-message-id",
        "20260822T220000_drop-allocated-starting-status",
        "20260824T013311_event-dismissals",
        "20260824T100244_page-session-columns",
        "20260824T112319_pending-known-good",
        "20260824T144451_mcp-computer-name-canonical",
        "20260824T161255_backfill-unpriced-081314",
        "20260824T164501_terminate-keeps-daemon-pages",
        "20260824T165207_agent-watchers-fk",
        "20260824T170157_rollup-day-state",
        "20260824T182647_web-sessions",
        "20260824T201423_mcp-clients",
        "20260825T000000_agent-archive-stats-rollup",
        "20260825T003008_permission-watcher-alert-source",
        "20260825T010000_metrics-turn-dur-hist",
        "20260825T011006_drop-alerts-read-at",
        "20260825T233522_machine-probe-transition-since",
        "20260826T095355_drop-permission-watcher-source-comment",
        "20260826T150000_page-shell-ttl",
        "20260827T021440_root-task-ongoing",
        "20260827T043602_notices-resolved-idx",
        "20260827T073641_task-notify-system-note-inbound",
        "20260827T165000_drop-skill-match-config-keys",
        "20260827T185407_fork-lineage-target-fix",
        "20260828T141614_observability-station-role-check",
        "20260828T191814_heartbeat-pause-log",
        "20260829T030000_drop-events-archive",
        "20260829T043200_heartbeat-pause-log-runner-grant",
        "20260829T083016_machine-units-serve-observability-station",
        "20260829T090700_drop-task-open-status",
        "20260830T051500_drop-hibernating-status",
        "20260830T211450_settle-started-at",
        "20260831T185300_heartbeat-pause-comment-update",
        "20260901T065353_add-last-claim-loop-at",
        "20260901T101039_fold-spawner-on-terminate",
        "20260901T141933_drop-expired-backfill-snapshots",
        "20260901T143242_task-budget-metering",
        "20260901T181810_allow-non-root-ongoing",
        "20260902T025619_watcher-generation",
        "20260902T073802_drop-fold-spawner-triggers",
        "20260902T174014_agent-runtime-incarnation",
        "20260902T190150_durable-lifecycle-intent",
        "20260902T201145_managed-writer-evidence",
        "20260903T020938_incarnation-resources",
        "20260903T044332_default-model-deepseek-v4-flash-vision-exp",
        "20260903T080634_add-last-heartbeat-at",
        "20260903T175722_add-born-spawner",
        "20260904T155441_runtime-admission-runner-lock",
        "20260904T190659_llm-usage-hourly",
        "20260905T073254_agent-impersonation",
        "20260905T121043_failure-feedback",
        "20260905T140829_bound-failure-feedback",
        "20260905T162656_watchdog-dispatch-poison",
        "20260906T034600_bound-work-failed-texts",
        "20260906T050000_wake-suppress",
        "20260906T081715_schedule-fire-log",
        "20260906T125200_heartbeat-nudge-backoff",
        "20260907T152552_corpse-fatal-marker",
        "20260907T190000_notice-expire-at",
        "20260908T042458_impersonation-relay-binding",
        "20260908T063636_impersonation-relay-batch-window",
        "20260908T225900_impersonation-push-ack",
        "20260908T230000_shell-ttl-renewal",
        "20260909T101027_run-timeline-window-default-30m",
        "20260909T171240_agents-meta-preset-name",
        "20260909T232714_impersonation-restore-native-owner",
        "20260910T165723_plugin-stats",
        "20260911T005419_add-resurrect-inbound-fence",
        "20260911T180406_host-deploy-stranded-hold",
        "20260911T192500_stranded-hold-recovery",
        "20260912T100020_default-model-deepseek-v4-flash",
        "20260913T180056_named-impersonation-history",
        "20260915T064420_drop-task-ongoing-status",
        "20260915T103012_impersonation-entries-runner-grant",
        "20260916T054934_permanent-reject-streak",
        "20260916T124832_impersonation-session-id-auth",
        "20260916T164150_lifecycle-pointer-done-guard",
        "20260916T171506_index-live-agent-roster",
        "20260916T172008_observed-agent-metrics",
        "20260916T204617_hierarchy-understanding-nodes",
        "20260916T225140_hierarchy-worker",
        "20260917T045040_default-model-deepseek-flash",
        "20260917T051500_alerts-runner-grant",
        "20260917T195200_add-agents-meta-closed-at",
        "20260918T031422_last-permanent-reject-reason",
        "20260918T113600_hierarchy-tail-seal",
        "20260918T141815_impersonation-relay-batch-window-default-zero",
        "20260919T020500_impersonation-relay-minted",
        "20260919T182700_retire-dead-user-settings-keys",
        "20260920T025622_page-live-port-unique",
        "20260921T061254_event-dismissals-process",
        "20260921T195118_watcher-notify",
        "20260921T211300_completion-notice-digest",
        "20260921T211400_watcher-agent-notify",
    }
)


def assert_reset_history(applied: set[str], required: set[str]) -> None:
    """Refuse partial generations before deleting any applied-set evidence."""
    for generation, history in (
        ("2026-08-14", _V010_PRE_RESET_SET),
        ("2026-09-23", _PRE_RESET_SET),
    ):
        present = applied & history
        missing = history - applied
        crossing = (
            history is _PRE_RESET_SET and _RESET_ANCHOR in required and _RESET_ANCHOR not in applied
        )
        if missing and (present or crossing):
            raise MigrationHistoryGap(
                f"cannot converge across {generation}: DB applied "
                f"{len(present)}/{len(history)} pre-reset migrations "
                f"(missing={sorted(missing)[:5]}{'...' if len(missing) > 5 else ''}). "
                "Upgrade through the release immediately before this reset so "
                "the full history is applied, then upgrade across the reset."
            )
