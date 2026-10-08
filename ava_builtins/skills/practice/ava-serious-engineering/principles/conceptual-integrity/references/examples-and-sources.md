# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-Patterns

- **Design by committee**:Every stakeholder gets a say in the design; the result is a bloated compromise that satisfies everyone and pleases no one. → alternative: One authorized designer owns the concept; stakeholders provide constraints and desiderata, not design decisions.
- **Silent concept drift**:The code evolves away from the Design Concept without the concept being updated — or the concept changes without the code being refactored. Both directions create a gap that widens with every change. → alternative: When a module doesn't fit, either reject the module or update the concept document first, then refactor.
- **Concept collision**:Two parts of the system use the same concept (e.g., "User") with different shapes, behaviors, or lifecycles — and nobody has declared them separate Bounded Contexts. → alternative: Either unify the concept (if they mean the same thing) or split them into separate contexts with explicit translation (if they don't).
- **API surface anarchy**:Different endpoints use different pagination, different error formats, different auth patterns — the system has no single owner of the interface. → alternative: One person owns the API surface design; every endpoint conforms to the same conventions or has a documented, justified exception.

## Examples

**Example 1: Committee Design vs Single Vision**

❌ Bad (committee design):
The team designs a user management system. Product wants role-based access. Engineering wants attribute-based access. Security wants mandatory access control. The compromise: all three, with a configuration flag to switch. The result is a system where nobody — including users — can predict how permissions resolve.

✅ Good (single-vision design):
The System Architect evaluates all three models against the core use case. The product is an internal tool with simple hierarchies → role-based access is chosen. The other models are documented as future considerations, not implemented. Every permission check in the system works the same way.

**Example 2: Concept Collision in API**

❌ Bad (same concept, different shapes):
```
POST /api/users          # body: { "name": "Alice" }
POST /api/admin/users    # body: { "fullName": "Alice" }
GET  /api/users/123      # response: { "name": "Alice", "id": 123 }
GET  /api/admin/users/123 # response: { "userName": "Alice", "userId": 123 }
```
Two endpoints for the same concept "User" with inconsistent field names, URL patterns, and response shapes. The user must learn two mental models.

✅ Good (consistent API surface):
```
POST /api/users          # body: { "name": "Alice" }
GET  /api/users/123      # response: { "id": 123, "name": "Alice" }
# Admin operations use the same User model; admin-specific fields go in a separate AdminContext
```

**Example 3: Concept Drift**

❌ Bad (silent drift):
The Design Concept says "every entity has a single owner." Six months later, a feature adds shared ownership for Documents — but the concept document is never updated. New team members read the concept, implement single-owner for Folders, and the system now has two contradictory ownership models with no explicit choice.

✅ Good (concept-first evolution):
When shared ownership is needed, the architect updates the Design Concept: "Entities have one or more owners; Documents may be shared; Folders remain single-owner for simplicity." The concept change is reviewed, agreed, and then the code follows.

## Sources

- Brooks, *The Design of Design* — conceptual integrity, Design Concept, authorized chief designer, collaboration models (surgical team, chief programmer team), process vs greatness. Primary source for this skill.
- Brooks, *The Mythical Man-Month* — the original statement of conceptual integrity and the surgical team model.
- Ousterhout, *A Philosophy of Software Design* — deep modules, consistency, obviousness (the code-level expression of conceptual integrity). See `../../../references/01-philosophy-of-software-design.md` §3.1, §5.3.
- Thomas & Hunt, *The Pragmatic Programmer* — ETC (Easier To Change) as the test of whether a design supports conceptual evolution. See `../../../references/03-pragmatic-programmer.md` Tip 14.
