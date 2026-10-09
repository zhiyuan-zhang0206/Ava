# Plugin config images belong to the SDK installation and its consumers

## Context

The plugin declaration decision kept bound config instances and their classes
in two process-global maps. The installer wrote them, but agent views, overlay
validation and SDK reads found them implicitly. A caller could not hold an
independent image; tests cleared and restored both tables around a binding.

## Decision

The SDK installer builds local config bindings and publishes one resolved
image on its existing `Installation`. Each instance owns its class, so a
second class registry is unnecessary. The installer remains the sole writer
of the SDK module's installation slot.

The host and external attachment pass that image into agent config views.
Agent overlays are local to their views. The exec child retains its two boot
stages: core overlay before SDK load, plugin overlay after load. A plugin
overlay builds a replacement image, which the installer publishes.

Gateway and ops read enabled config declarations for validation without
loading an SDK. SDK validation additionally receives its installation's bound
values. Invalid data, schema drift and unknown binding failures retain their
existing explicit error boundaries.

## Consequences

There are no module-level plugin config or config-class tables and no new
global getter, registry slot, ContextVar or reload mechanism. Config-service
fresh-file reads, core frozen/live rules, plugin process-start updates and
provider credential delivery do not change.

This supersedes the bound-config global-map part of
[the declaration decision](2026-10-03-plugins-declare-the-framework-registers.md).
It does not change model-catalog ownership or plugin enable policy.
