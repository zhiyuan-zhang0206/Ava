# Structured comments with colocated documentation

## Decision and scope

We chose a closed source-reference syntax and typed records in existing
component-local OKF documents. Python and JavaScript/TypeScript are the first
parser boundary. Changed supported source files migrate in full; every typed
reference is checked across the repository. Untouched legacy prose remains
visible migration work, without a stored baseline or per-file waiver.

The motivating failure was high-volume concurrent agent contributions retaining
or adding plausible prose that duplicated code or contradicted behavior. A
skill alone could not enforce a durable source/document boundary. The first
migration covered gateway memory-search constraints, Ava Code host hooks,
browser content security policy and SDK compaction help.

## Prior art and simplest sufficient mechanism

The repository already had colocated OKF owners, AST-based document anchors,
static SDK export provenance, a tracked-file universe and a TypeScript compiler.
The implementation reused these boundaries rather than introducing a document
service, runtime imports or a new package dependency. A source pointer identifies
a closed contract; tests and exact definitions invalidate dangling claims.

API docstrings remain a generated projection because SDK help consumes them.
Their document is the authoring owner. Suppression comments and ordinary
internal docstrings are not alternate prose containers.

## Alternatives and trade-offs

- Advisory comment-reduction guidance alone could not prevent forgotten or
  conflicting instructions during concurrent generation.
- A total immediate deletion would destroy caller help and useful rationale,
  and make an unreviewable repository-wide change. Whole-file migration on
  changed files introduces a real gate while leaving the full inventory explicit.
- Free-form tags or allowlists would retain the original prose and create a
  growing exemption surface. Closed records and exact pointers were chosen.
- A CI model reviewer and signed review reports were rejected as unnecessary
  infrastructure. Independent semantic review is a contributor requirement;
  deterministic CI does not pretend to establish semantic truth.

A surviving symbol and test name do not prove that a statement remains true.
Reviewers must inspect relevant code and assertions. The cost is that changing a
legacy file also requires reconciling its prose; full-tree strict enforcement
remains a later migration cutover, not a claim made by this contribution.

Current instructions live in
[structured code comments](../../../conventions/engineering/structured-code-comments.md).
