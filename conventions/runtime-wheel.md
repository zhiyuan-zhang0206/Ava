# Runtime wheel packaging gate

The application wheel includes the SDK, CLI, agent kernel, shared modules,
service daemons, gateway, operations code and built-in plugins. Development
still supports editable installations. Production activation is unchanged by
this packaging gate: a successful wheel build is not a deployed release.

The `Runtime wheel` CI workflow installs locked dependencies and the built wheel
into a fresh virtualenv outside the checkout. It verifies required service and
exec-child members, rejects editable metadata, imports selected runtime modules
with networking forbidden, and invokes CLI help from an unrelated directory
using an isolated interpreter. A negative control deletes `agent/exec_child.py`
from a copy of the wheel and must fail the same member gate for that exact
missing member.

This is not a fleet readiness test. It does not start services, migrate data,
validate native dependencies on every platform, or prove the runtime resource
closure of frontend/external-plugin state. The wheel carries the baseline and
paired SQL migrations, commands, schedule definitions and native observability
configuration at the relative paths existing consumers use. Isolated imports
also exercise the hosted-agent daemon and gateway without entering their service
lifespans. Shipping configuration does not enable a service or change storage
provider selection. Side-by-side activation must add
those gates before changing production launch paths. A merge does not establish
runtime health; only an explicitly authorized deployment and observed service
and agent verification can do that.
