# Reserve full test suites for CI

The user prohibited local full-suite test runs. Local verification is limited
to bounded tests for changed behavior and its direct consumers, plus static
checks. Shared-layer changes still require full-suite verification in CI;
they no longer justify a local full-backend run.

This supersedes the local full-suite requirement in the run-local-tests skill
and clarifies the consumer-coverage rule in defensive-patterns. Existing
historical incident narratives remain unchanged: their broad-coverage lesson
is satisfied in CI. Full-suite frontend hooks must not silently override this
policy; use a named hook skip and report the targeted tests run instead.
