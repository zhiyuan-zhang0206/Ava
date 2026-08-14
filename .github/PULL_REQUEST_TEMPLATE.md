<!-- See .agents/skills/write-a-pr-description/SKILL.md. A PR description is not a summary + a
list of changed files — give the reviewer the intermediate state needed to catch
design errors without reading the whole diff. Delete these comments as you fill
each section. -->

## Checklist
<!-- Check the boxes that apply to this PR. -->

- [ ] Documentation — updates subsystem passports or other docs
- [ ] Breaking change — alters a public API or on-wire contract
- [ ] New plugin / skill — adds or modifies a plugin or skill
- [ ] Database migration — includes a migration under migrations/
- [ ] Dependency change — adds, removes, or bumps a dependency

## What & why

## File-tree diff
<!-- A tree following the repo structure; mark each entry (A/M/D/R) + a short
note. Put a ★ on critical paths: new/removed entry points, cross-process /
cross-network calls. -->

```
```

## Runtime behavior changed
<!-- The control flow / invariants a reader cannot infer from the tree. As needed. -->

## NOT tested
<!-- Explicit boundaries. "happy path works" is not "everything works". -->
