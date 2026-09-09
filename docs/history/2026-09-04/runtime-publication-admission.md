# Runtime publication admission

Runtime admission must be decided from the publication facts that the process
actually loaded, not from an installed revision or a mutable environment flag.
Hosted admission therefore resolves the loaded image once per host process and
revalidates its immutable binding for each admission, then asks the existing
publication layer for a decision while deployment and registry facts are locked.

A pending publication is a maintenance posture, not a launch failure. Hosted
admission leaves the agent queued and consumes no inbound work, and a committed
publication refuses a historical NULL resource row whose closure cannot be
inferred. Incomplete historical v2 publication without activation hash or
challenge is not new-mode permission.

The original #1567 scope also covered the native process runtime: process
admission before metadata ownership, spawn-stamped bounded first births, and
controller-side resume of an exact birth. That path retired upstream (#1924
removed the process runtime) and is not shipped here; the hosted runtime is the
surviving admission surface. The exec-owner installed-entry proof was likewise
withdrawn from this PR (its cold-offline job never passed on this rebased stack)
and is tracked separately.

The alternative of checking only the currently installed checkout was rejected:
an already running service can execute a different image from the files visible
on disk. Treating a pending publication as legacy protocol zero was also
rejected because it would silently authorize old cleanup and new ownership while
the writer set is intentionally changing.

The current contract is recorded in
`shared/incarnation-resources.ava.okf.md`; publication selection and activation
remain owned by their separate runtime-publication documents.

Update: the first native end-to-end proof showed that `SELECT ... FOR UPDATE`
also requires table UPDATE privilege. Granting that privilege to `ava_runner`
would have crossed the deployment authority boundary. Runtime admission now
takes only the row lock through a fixed security-definer function while keeping
deployment-state reads under the runner's existing SELECT grant.
