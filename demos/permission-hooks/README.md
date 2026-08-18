# Ava Security Policy Interception Demo

A dangerous command interception gate implemented based on Ava's `before_exec` hook — a real-world case driven by the GPT 5.6 incident.

## Background

In 2026, GPT 5.6 was exposed for autonomously executing destructive operations like `rm -rf /` and `DROP TABLE` without supervision.[^1]
This indicates: **Any AI agent with shell access needs a safety net at the execution layer.**

Ava's `before_exec` hook point is naturally the location for this safety net—intervening after code generation and before shell execution.

[^1]: Fictional case, for demonstration purposes.

## Two-tier Policy

| Tier | Behavior | Examples |
|------|------|------|
| 🔴 **block** | Always block | `rm -rf /`, `DROP TABLE`, `mkfs`, `dd if=` |
| 🟡 **warn** | Block on first occurrence + warn, allow on second submission | `curl \| bash`, `sudo`, `git push --force` |

**Why only two tiers?** Because an agent with bash is essentially a general-purpose executor.
Fine-grained controls like "file path whitelists" are meaningless in the face of infinite bypass paths like `cat` → `python -c` → `sed`.
The only meaningful distinction is: **irreversible destruction vs. reversible operations.**

## Workflow

```
agent generates code → before_exec hook scans
  ├─ hits block rule → injects security warning → goto after_exec (skip exec)
  ├─ hits warn rule (first time) → injects reminder → goto after_exec
  ├─ hits warn rule (second time) → allow (agent has confirmed)
  └─ no hit → allow (normal execution)
```

## Example Interactions

### Triggering block (DROP TABLE)

**Prompt:**
> Help me clean the database, delete the table users_backup_2023, execute DROP TABLE users_backup_2023

**Agent attempts to execute:**
```python
import sqlite3
conn = sqlite3.connect("app.db")
conn.execute("DROP TABLE users_backup_2023")
conn.commit()
```

**Hook intercepts → Agent receives:**
```
## 🛡️ Ava Security Policy Interception

Your code triggered a security policy and has been intercepted **without being executed**.

### 🔴 Prohibited Operation (will never execute)
| Operation | Explanation |
|-----------|-------------|
| DROP TABLE / DATABASE | Deleting database objects is irreversible |

---
*Background: In 2026, GPT 5.6 was exposed for autonomously executing rm -rf, DROP TABLE, and similar operations without supervision...*
```

### Triggering warn → second submission allowed (curl | bash)

**Prompt:**
> Use nvm to install Node.js 22

**Agent attempts to execute:**
```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash
```

**Hook intercepts (first time) → Agent receives a warning.** Agent replies confirming that this is a safe nvm installation script.

**Agent submits the same code again → Hook allows it**, executes normally.

## Policy Rules (16 total)

### 🔴 block (7 rules)
| Pattern | Explanation |
|---------|-------------|
| `rm -rf /` | Recursively force delete root directory |
| `rm -rf ~` | Recursively force delete home |
| `> /dev/sd*` | Overwrite disk block device |
| `mkfs.*` | Format filesystem |
| `dd if=` | Low-level disk write |
| `DROP TABLE/DATABASE` | Delete database objects |
| `TRUNCATE TABLE` | Empty table data |

### 🟡 warn (9 rules)
| Pattern | Explanation |
|---------|-------------|
| `curl \| bash` | Execute script directly from network |
| `wget \| bash` | Same as above |
| `sudo` | Privilege escalation operation |
| `chmod 777` | Open all permissions |
| `git push --force` | Force push overwrites remote |
| `git reset --hard` | Hard reset loses uncommitted changes |
| `kill -9` | Force kill process |
| `killall` | Kill processes by name |
| `DELETE FROM` | Delete database rows |

## Design Decisions Record

### Why not use file path whitelists?
An agent with bash can do: `cat file` → `python -c "open('file').read()"` → `sed -n p file` → ...
Regex matching on code text can never exhaustively cover bypass paths. Only operations with **irreversible consequences** are worth intercepting—
can't stop 100% but stopping 90% is valuable.

### Why allow warn on second submission instead of permanently blocking?
If an agent genuinely needs to use `sudo` or `git push --force`, permanent blocking would prevent these legitimate operations from executing.
"Warn once + allow on second try" strikes a balance between security and usability: the agent knows what it did.

### Why not use agent identity whitelists?
Hardcoding agent_id is too rigid. Real permission management should be based on role/capability declarations, not magic numbers.
This is beyond the demo's scope.
