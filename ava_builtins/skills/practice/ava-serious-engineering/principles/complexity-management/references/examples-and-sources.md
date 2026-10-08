# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **Tactical Tornado**:Shipping features fast while leaving design wreckage — celebrated short-term, destructive long-term. → alternative: Invest 10–20% of each feature's time in design; refactor as you go instead of patching.
- **Death by a thousand cuts**:Accepting "just this once" workarounds — hardcoded special cases, duplicated constants, methods reaching across layers — because each alone seems harmless. → alternative: Treat every review as a complexity checkpoint; fix regressions on sight.
- **Shallow module proliferation**:Creating many small classes each with a large interface-to-implementation ratio (Classitis). → alternative: Merge related shallow classes into deep modules; a module should hide more than it exposes.
- **Temporal decomposition**:Splitting modules by execution order (`Reader → Parser → Handler`) rather than by knowledge domain. → alternative: Group by the design decision each module encapsulates; one knowledge domain = one module.
- **Fixing a leaked decision at every call site**:A defect caused by one decision duplicated across N sites is "fixed" by patching all N — the fix reproduces the leak, and the next site written from memory reintroduces the defect. → alternative: Route the sites through one owner, fix it there once, and forbid new bypasses mechanically.
- **Premature complexity**:Designing for imagined future requirements before the simple version has proven itself. → alternative: Follow Gall's Law — ship the simplest working system first; add complexity only when real requirements demand it.

## Examples

**Example 1: Deep vs Shallow Module**

❌ Bad (shallow):
```python
class FileReader:
    def __init__(self, path, buffer_size=4096, encoding='utf-8',
                 lock_mode='shared', cache_policy='lru'):
        ...
    def read_bytes(self, count): ...
    def set_buffer_size(self, size): ...
    def get_encoding(self): ...

# Caller must understand buffer_size, encoding, lock_mode, cache_policy
# Interface surface ≈ implementation complexity → shallow
```

✅ Good (deep):
```python
class FileReader:
    def __init__(self, path): ...
    def read(self) -> str: ...

# Caller only needs path and read()
# Implementation handles buffering, encoding, locking, caching internally
# Interface surface ≪ implementation complexity → deep
```

**Example 2: Information Leakage via Temporal Decomposition**

❌ Bad (temporal decomposition — same knowledge leaked across stages):
```python
class RequestReader:
    def parse_headers(self, raw: bytes) -> dict: ...  # knows HTTP format
class RequestParser:
    def validate_method(self, headers: dict) -> str: ...  # knows HTTP format
class RequestHandler:
    def extract_body(self, raw: bytes, headers: dict) -> bytes: ...  # knows HTTP format
# Changing HTTP version handling requires edits in all three classes
```

✅ Good (knowledge-domain decomposition):
```python
class HttpProtocol:
    def parse(self, raw: bytes) -> ParsedRequest: ...
    def serialize(self, request: ParsedRequest) -> bytes: ...
# One module owns all HTTP format knowledge; change it in one place
```

**Example 3: Gall's Law — Start Simple**

❌ Bad (premature complexity):
Building a microservice mesh with event sourcing, CQRS, and sagas for a startup's first user-facing feature — before a single customer exists.

✅ Good (evolve from simple):
Ship a single-process monolith with a simple database. When traffic grows and real bottlenecks emerge, extract services at the seams that actual usage reveals.

## Sources

- Ousterhout, *A Philosophy of Software Design* — complexity definition (C = Σ(cₚ × tₚ)), three symptoms, two causes, deep modules, information hiding, tactical vs strategic programming, pull complexity down. Primary source for this skill.
- Gall, *Systemantics* — Gall's Law: "A complex system that works is invariably found to have evolved from a simple system that worked."
- Ashby, *An Introduction to Cybernetics* (1956) — Law of Requisite Variety (the original source); Weinberg, *An Introduction to General Systems Thinking* — popularized the law in software-adjacent systems thinking.
- Brooks, *The Design of Design* — conceptual integrity as organizational defense against complexity (see `conceptual-integrity`).
