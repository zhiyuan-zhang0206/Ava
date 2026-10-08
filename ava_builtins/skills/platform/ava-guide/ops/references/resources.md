# Cluster resource diagnosis

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Resource Oversight (the SRE loop)

Every machine's OTel Collector sidecar scrapes the traditional SRE layer into
Prometheus: host CPU / memory / load / disk / filesystem / network everywhere,
plus `postgresql` and `redis` against the cluster's own data plane on a
gateway-capable unit. They live under `job="ava-infra"` with `host` (the OS
hostname / physical identity) and `machine_name` (the Ava roster identity)
labels. The Grafana dashboard `ava-ops-main` (its "Host & data plane" section)
groups by `machine_name` and is the view.

**There are no resource limits in the code, deliberately.** A saturated box
may be a runaway loop or a training job doing exactly what it was asked; which
one it is depends on machine specs and co-tenancy, which the framework cannot
know. So the operator's job is judgment over the data, not enforcement of a
constant:

1. Watch the axes — latency percentiles (LLM, gateway, turn), error and
   warning volume, host utilization, data-plane saturation.
2. When something is out of band, identify the consumer before acting.
3. Then choose: investigate, terminate idle agents to shed load, tell the
   user, or decide the machine is legitimately busy and leave it.

Alert rules (R8-R12: sustained CPU, memory pressure, per-volume disk
watermark, Postgres connection saturation, Redis memory) fire into the same
alerts table and IM pipeline as the application rules. Their thresholds are
deployment facts living in
`deploy/lgtm/config/grafana/provisioning/alerting/rules.yml` — a box whose
normal state trips a rule wants that file edited, never a special case in
framework code.

`ava status` still answers on a cluster with no LGTM backend: each machine row
carries one live CPU / memory / disk reading. That is a current value, not a
history — the history is Prometheus's, and there is exactly one of it.

When disk pressure comes from dead agents' workspaces, the disposal playbook is [workspace-cleanup](../../workspace-cleanup/SKILL.md).
