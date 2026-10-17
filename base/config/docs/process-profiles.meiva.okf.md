---
type: doc
title: Process configuration profiles
description: Configuration domains follow process consumers and preserve complete administrative reads.
tags: [configuration]
---

# Process configuration profiles

```meiva-contract
id = "process-domains"
kind = "invariant"
statement = "Profile domains follow actual reads in each process kind's import closure, independently of config-panel capability labels. Launchers select gateway, agent, or runner; unknown names fail fast. Excluded domains raise an actionable AttributeError in eager and lite access, and dynamic consumers can use has_domain. Runner support daemons do not consume the agent domain now that shared documents always use Markdown; agent execution belongs to the agent profile."
code = ["base/config/profiles.py:<module>"]
tests = ["cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py::test_profile_domains_match_consumption_matrix", "base/tests/config/test_config_profiles.py::test_runner_profile_domains", "base/tests/config/test_config_profiles.py::test_gateway_profile_excludes_agent_domains", "base/tests/config/test_config_profiles.py::test_unknown_profile_fails_fast", "base/config/tests/test_config_boot_lite.py::test_overlay_unknown_and_out_of_profile_keys_fail_fast"]
```

The constants module can be imported without constructing Settings. The gateway
profile includes its messaging, memory and telemetry daemons; their domains
cannot be derived from the configuration panel's capability labels. Agent hosts
and execution children use the agent profile, while runner support daemons use
the runner profile. The bidirectional consumption guard detects both missing
domains and domains left behind after their last consumer is removed.

Gateway endpoints and agent-published timeline snapshots both consume display
defaults. Agent lifecycle notification and delivery consume daemon settings.
Runner browser MCP consumes sandbox connection timeouts, ops lifecycle consumes
the model setting, and support-daemon event emission consumes observability
settings. The To user read guards (`to_user`) span all three profiles: the ops
listener and the gateway enforce them, and the reading functions sit in the
agent-face closure. These reads explain domains that span more than one process
profile.

An omitted profile argument reads the launcher's environment marker; explicit
`profile=None` constructs the complete model. `PROFILE_UNSET` distinguishes those
cases. Configuration-service reads retain their explicit complete authority for
bootstrap payloads, the configuration panel and overlay snapshots, even when the
process view is restricted. Reflective `get_field` reads still enforce the process
boundary. The profile tests compare all served keys and excluded-domain values;
they do not require unrelated in-profile values to equal the fixture-mutated
singleton. Retired environment keys remain inert so an upgrade does not require
rewriting the unit's environment file before startup.

Restart metadata names the process kind that consumes a setting. Gateway-owned
messaging and memory fields require a gateway restart, which includes gateway
daemons; naming an agent restart or no restart would leave their boot-time reads
unchanged. Registry validation rejects unknown restart labels and process kinds
that lack the field's domain. The profile tests cover those metadata constraints
alongside construction and access behavior.

```meiva-contract
id = "lm-settings-domain"
kind = "invariant"
statement = "`LmSettings` (`base/config/domains/lm.py`) preserves the flat `.env` aliases for language-model settings. `llm_model` defaults to `deepseek-flash` with `capability=\"common\"`, `scope=\"cluster-default\"`, and `per_agent=True` so it serves as both the gateway spawn default and a per-agent overridable setting; `labeler_model` and `hierarchy_model` default to `deepseek-flash` with `capability=\"gateway\"` so their provider keys survive the gateway profile pop; `understand_media_model` defaults to `gemini-3.8-flash`; and `gemini_api_key` carries `capability=\"common\"` for both gateway-side embeddings and agent-side Gemini calls."
code = ["base/config/domains/lm.py:<module>"]
tests = [
  "base/tests/config/test_config_defaults.py::test_llm_model_code_default_is_deepseek_flash",
  "base/tests/config/test_config_defaults.py::test_understand_media_model_code_default_is_gemini_3_8_flash",
]
```
