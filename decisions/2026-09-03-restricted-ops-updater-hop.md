# Reuse the updater for the first bounded immutable ops transition

The old ops process cannot acknowledge its own absence. The new normal ops
daemon also cannot safely start before the additive schema exists. A restricted
observation entry in the same service and endpoint supplies the existing
updater's post-stop readback without a second daemon, callback port or registry.

The first positive slice replaces an already verified, restartable restricted A
with restricted B. The existing updater mutex/handoff owns local execution;
the existing deployment operation owns transition authority. Compensating inputs
extend the existing handoff record rather than introducing another journal.
Readback remains bootstrap-only and never publishes the normal current release.

Saved source/argv is not a bootable rollback image. Therefore normal/source A is
refused before stop until its separate LKG and first-orchestrator bridge is
proved. This refusal bounds the slice; it does not remove that mandatory work.
CI uses distinct prepared images of the same code revision and explicitly does
not claim old/new schema or application-version compatibility.
