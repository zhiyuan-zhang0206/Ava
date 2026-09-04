# Runtime publication admission

Runtime admission must be decided from the publication facts that the process
actually loaded, not from an installed revision or a mutable environment flag.
Process admission therefore resolves the loaded image before entering the
database transaction, while the hosted agent service shares one boot-time
resolution and revalidates its immutable binding for each admission.

A pending publication is a maintenance posture, not a launch failure. Process
birth exits before taking ownership, hosted admission leaves the agent queued,
and neither path consumes inbound work. The existing launch deadline and attempt
budget remain fixed across that pause. A managed first birth records those bounds
in the incarnation resource evidence so a later controller can resume only the
exact birth and refuse an ambiguous prior native launch.

The alternative of checking only the currently installed checkout was rejected:
an already running service can execute a different image from the files visible
on disk. Treating a pending publication as legacy protocol zero was also rejected
because it would silently authorize old cleanup and new ownership while the
writer set is intentionally changing.

The current contract is recorded in
`shared/incarnation-resources.ava.okf.md`; publication selection and activation
remain owned by their separate runtime-publication documents.

Update: the first native end-to-end proof showed that `SELECT ... FOR UPDATE`
also requires table UPDATE privilege. Granting that privilege to `ava_runner`
would have crossed the deployment authority boundary. Runtime admission now
takes only the row lock through a fixed security-definer function while keeping
deployment-state reads under the runner's existing SELECT grant.
