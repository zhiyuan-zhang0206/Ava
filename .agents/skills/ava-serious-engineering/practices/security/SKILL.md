---
name: security
description: "Use when designing, reviewing, or changing anything that touches a trust boundary — authentication, proxies, input handling, secrets, dependencies, deployment configuration. Covers the minimum security discipline every engineer owes a system, not the security-engineer's manual."
---

# Security

## One-Sentence Core
> Security is a property of the design, not a phase: make trust boundaries explicit and few, keep secrets out of code and logs, validate everything that crosses a boundary, and patch early — the cheapest fix is the one made at design time, and the most expensive is the one found in production.

## Core Principles

- **Make trust boundaries explicit and few**:For every component, write down what inputs are trusted, from whom, and what crossing the boundary may do; the attack surface is the sum of the crossings. — **Why**:Thomas & Hunt (Tip 72, Minimize Attack Surface): systems that mix untrusted input and powerful actions are attack hotspots, and "the more code that touches a piece of data, the more exposure that data has to breakage." A boundary nobody has named cannot be defended. — **How**:In design and review, name each boundary (client→gate, gate→app, app→DB, process→network) and its contract; keep crossings few; make each crossing translate errors and validate input explicitly — never with a catch-all that conflates error classes (the gate's `except Exception` collapsed a defined 404 into a 502 and kept a deleted icon alive in browser caches for weeks).

- **Secrets never live in code, commits, or logs**:Keys, tokens, passwords, and credential-bearing URLs enter through environment or a secret store, are redacted in logs by default, and are scrubbed from anything exported. — **Why**:Cluster ruling (2026-08-02): cleartext secrets in the DB are acceptable because reading the DB already proves possession of the cluster secret — the store adds no new disclosure surface. Logs and external services, by contrast, are reachable without the secret, so they must never contain it; sanitizing on export is the export action's responsibility. — **How**:Inject secrets via env/secret store; redact anything credential-shaped in logs; scan commits for accidental secrets; when a rotation happens (redis requirepass, 2026-07-14), record it and coordinate every consumer — a rotated secret some config still holds is split-brain.

- **Validate at the boundary; deny by default**:Everything crossing a trust boundary is untrusted until validated against a schema, and access decisions fail closed. — **Why**:OWASP Top 10's injection and insecure-design categories (A03, A04) are the standing evidence that unvalidated boundary input is the attack class that never goes away; validation is cheapest exactly at the boundary, where the data shape is known before the payload reaches an interpreter. — **How**:Schema-validate every request at the HTTP boundary; parameterize queries; reject unknown fields rather than ignoring them; use whitelists for anything enumerable; default-deny in ACLs and firewall rules.

- **Least privilege, everywhere, by construction**:Every process, role, credential, and network binding carries the minimum capability its job requires — so a compromise is contained by the boundary it happens in. — **Why**:Blast radius scales with privilege; the Ava data plane already embodies the pattern: the Postgres role is NOSUPERUSER owning only its own database, pg/redis bind loopback plus the reachable host (never all interfaces), and a split deployment needs reachability *and* the cluster secret. — **How**:Per-component credentials; scoped API tokens; at review, verify no role or credential can do more than its caller needs; keep network bindings as narrow as the deployment allows.

- **Supply-chain hygiene is security hygiene**:Every dependency is code that runs inside your trust boundary; lock versions, review new dependencies before adding them, and track CVEs for what you already run. — **Why**:OWASP A06 (Vulnerable and Outdated Components) and A08 (Software and Data Integrity Failures) codify what the ecosystem keeps rediscovering: unvetted or floating dependencies are an unbounded attack surface that arrives with no review. `principles/dependency-management` says every dependency is a future change amplified; with an adversary in mind it is also a future compromise amplified. — **How**:Commit lock files; a new dependency gets a review of what it does, who maintains it, and its own dependency tree before merge; track CVEs for direct dependencies; update deliberately — with the test suite as gate — not reactively or never.

- **Security is a design input, not a wrap-up**:Threat-model at design time, when the fix is a paragraph; patch early, because "later" is too late. — **Why**:Tip 73 (Patch Early): report and fix security issues promptly. The corollary is that the cheapest place to find a boundary mistake is before it ships — the gate favicon bug and the login-loopback regression were both one-line design decisions (a catch-all; a hardcoded loopback URL) that each became a production incident. — **How**:In every design review ask: what is the trust boundary, what crosses it, and what happens when a caller is malicious? Security review is a standing item in the review checklist (`practices/review`), not a phase after "done".

## Checklist
- [ ] **MUST** Is the trust boundary of this component written down — what inputs are trusted, from whom, and what each crossing may do?
- [ ] **MUST** Does every boundary crossing translate error classes explicitly — no catch-all `except Exception` that turns a defined 4xx into a 502 or a silent success?
- [ ] **MUST** Does any code, committed config, or log line contain a secret (key, token, password, credential-bearing URL)?
- [ ] **MUST** Are secrets injected via environment/secret store and redacted from logs by default?
- [ ] **MUST** Is every input at an external boundary validated against a schema (types, ranges, whitelist) before use, with unknown input rejected?
- [ ] **MUST** Does every component hold the minimum privilege — scoped credentials, least-capability roles, network bindings as narrow as the deployment allows?
- [ ] **MUST** Are dependency versions locked, and has every new or changed dependency been reviewed before merge?
- [ ] **MUST** Has a threat-model pass been done in design ("what can a malicious caller do at each boundary?")?
- [ ] **SHOULD** Are CVEs tracked for direct dependencies, and are updates applied deliberately with the test suite as gate?
- [ ] **SHOULD** Does every credential have a documented rotation path (who rotates, how, and how consumers learn before they break)?

## Anti-Patterns
- **The catch-all boundary**:`except Exception: return 502` in a proxy — a defined 404 becomes "app down", browsers cache stale content forever, and the user sees a deleted icon "resurrect" (gate favicon incident) → alternative: pass through defined status codes and bodies; degrade to 502 only on transport failure.
- **The secret in the log line**:debug-logging a token "temporarily" or committing `.env` "just this once" — logs and repos are reachable without the secret, unlike the DB → alternative: env injection + default redaction + commit scanning; treat every leaked secret as already public and rotate.
- **The credential aimed at the wrong endpoint**:hardcoding `127.0.0.1:{port}` as the login target — remote clients POST the cluster secret to their own loopback and the request never arrives; worse, the fix was silently reverted by a rebase and shipped in main and prod (gate login regression) → alternative: derive endpoints from machine configuration, and pin the security property with a regression test that fails if the fix disappears.
- **The unexpired cache**:deleting an asset but leaving browsers/proxies holding it — removal is not complete until caches are handled → alternative: explicit cache-control/expiry on removed assets; verify deletion end-to-end (direct curl to the path, not the cached tab).
- **The floating dependency**:`dep>=x` or unpinned installs — supply chain arrives unreviewed and unversioned → alternative: lock files committed, new dependencies reviewed, CVE tracking.
- **Security as a wrap-up phase**:"we'll harden after launch" — retrofit security is skipped or rushed precisely when the system is biggest → alternative: threat model at design; security as a standing review checklist item.

## Examples(bad → good)

**Example 1 — the boundary must not lie about error classes (gate proxy, 2026-08)**

❌ Bad (catch-all collapses defined responses into "app down"):
```python
def proxy_app(path: str) -> Response:
    try:
        with urllib.request.urlopen(app_url + path) as resp:
            return Response(resp.status, resp.read())
    except Exception:
        return Response(502, "app updating")   # a defined 404 becomes "app down"
```
A deleted icon returns 404 → the proxy answers 502 text/html → browsers never invalidate the favicon cache → the old icon "resurrects".

✅ Good (error classes translated explicitly):
```python
def proxy_app(path: str) -> Response:
    try:
        with urllib.request.urlopen(app_url + path) as resp:
            return Response(resp.status, resp.read())
    except urllib.error.HTTPError as e:      # the app answered: pass its verdict through
        return Response(e.code, e.read())
    except OSError:                          # transport failure: the app is truly down
        return Response(502, "app updating")
```

**Example 2 — secrets: where they may and may not live**

❌ Bad (secret in code, aimed at a hardcoded endpoint):
```python
# login form injected by the gate
LOGIN_URL = "http://127.0.0.1:8000/api/auth/login"   # remote clients POST the
fetch(LOGIN_URL, {body: {password: SECRET}})          # cluster secret to their own loopback
```
A remote browser sends the secret to its own machine; the gateway log shows zero hits. The fix was once silently reverted by a rebase and shipped in main and prod.

✅ Good (secret injected, endpoint derived, property pinned by a test):
```python
# base URL derived from machine config, secret from the environment
gateway_base = shared.machine.reachable_host()        # AVA_MACHINE_HOST > machine_host > localhost
fetch(f"{gateway_base}/api/auth/login", {body: {password: os.environ["CLUSTER_SECRET"]}})
# regression test: assert the login form targets reachable_host(), not 127.0.0.1 — fails if reverted
```

**Example 3 — validate at the boundary (deny by default)**

❌ Bad (unvalidated input reaches an interpreter):
```python
@app.route("/search")
def search(request):
    q = request.args["q"]
    rows = db.execute(f"SELECT * FROM docs WHERE title LIKE '%{q}%'")  # injection
    return rows
```

✅ Good (schema-validated, parameterized, rejected when unknown):
```python
@app.route("/search")
def search(request):
    q = request.args["q"]
    if not isinstance(q, str) or len(q) > 200:        # validate shape at the boundary
        return Response(400, "invalid query")
    rows = db.execute("SELECT * FROM docs WHERE title LIKE ?", (f"%{q}%",))  # parameterized
    return rows
```

## Relationships
- `principles/error-handling` — the trust boundary is where the failure model is decided: a defined 4xx is a contract, a transport failure is a different class, and crash-early vs graceful degradation splits exactly at the boundary. The gate 404→502 is both an error-handling and a security defect; fixing one framing alone misses the other.
- `principles/dependency-management` — supply-chain hygiene is dependency management with an adversary in mind: every dependency is code running inside your trust boundary.
- `practices/concurrency` — connection lifecycles are trust-boundary crossings that can die silently (redis-py dead-transport crash #2613); and connection URLs carry credentials, so connection configuration is secrets management.
- `ai-era/verification-discipline` — security properties need verification like any other property: the login-loopback fix was silently reverted, and only a regression test that fails on revert pins it. AI-generated code gets the same adversarial review — see also `practices/review`.
- `ai-era/judgment-and-trust` — "deciding whether to trust code" is literally a security decision; the gatekeeper role is the enforcement of the trust boundary at every merge.
- `references/03-pragmatic-programmer.md §8.4` — Tips 72–73 (Minimize Attack Surface; Patch Early); Tip 8 (Good-Enough Software) for "how secure is good enough" as a requirements discussion, not an engineer's private call.
- OWASP Top 10 (2021) — A01 Broken Access Control, A03 Injection, A04 Insecure Design, A06 Vulnerable and Outdated Components: the canonical catalog behind the validation, least-privilege, and supply-chain principles.

## Sources
- Thomas & Hunt, *The Pragmatic Programmer* (20th anniv. ed.), Tips 72–73 (Minimize Attack Surface / Patch Early), Tip 8 (Good-Enough Software) — `references/03-pragmatic-programmer.md §8.4`
- Ava incident records (memory pool): `ava/bugs/gate-404-to-502-favicon-resurrection.md` (catch-all swallowed 4xx→502, fixed in PR #1294); `ava/bugs/gate-login-loopback-regression-2026-08-03.md` (security fix silently reverted by a rebase, replayed in PR #1261); `ava/design/user-ruling-db-secrets-cleartext-20260802.md` (user ruling: cleartext at rest acceptable; never in logs or external services); `infra/security/redis-password-rotated.md` (requirepass rotation synced with consumers)
- Addy Osmani, *agent-skills*: `security-and-hardening` — production-grade security review skill (see `research/agent-skills-ecosystem.md`)
- OWASP Top 10 (2021) — <https://owasp.org/www-project-top-ten/>
