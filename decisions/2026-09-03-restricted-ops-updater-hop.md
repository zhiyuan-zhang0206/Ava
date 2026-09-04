# Reuse the updater for the first bounded immutable ops transition

The old ops process cannot acknowledge its own absence. The new normal ops
daemon also cannot safely start before the additive schema exists. A restricted
observation entry in the same service and endpoint supplies the existing
updater's post-stop readback without a second daemon, callback port or registry.

The first positive slice replaces an already verified, restartable restricted A
with restricted B. The existing updater mutex/handoff owns local execution;
the existing deployment operation owns transition authority. Compensating inputs
live in a separate versioned, bounded recovery envelope tied to the exact handoff
generation. This separation preserves malformed recovery evidence while allowing
ordinary malformed spawn markers with no compensation record to be repaired.
Readback remains bootstrap-only and never publishes the normal current release.

The candidate updater may replace the predecessor handoff only by an atomic CAS
over the prepared generation, PID, and birth time after positive owner-death
evidence. Every recovery recollects inventory: immutable unit, service, and
launcher facts remain exact, while only the verified single A/B observer process
may turn over. A fork without a new exact session record is treated as ambiguous,
and journal growth is capped rather than becoming an unbounded local input.

Saved source/argv is not a bootable rollback image. Therefore normal/source A is
refused before stop until its separate LKG and first-orchestrator bridge is
proved. This refusal bounds the slice; it does not remove that mandatory work.
CI uses distinct prepared images of the same code revision and explicitly does
not claim old/new schema or application-version compatibility.
