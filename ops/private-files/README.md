# Private production files

This registry makes private files that are required by production but absent
from the deployed git checkout survivable and auditable. A file belongs here
when all three facts are true before migration:

1. Production needs the file to run.
2. No git tree that survives checkout cleanup tracks it.
3. No other durable copy exists.

The migration first places the exact bytes in an approved durable git source,
then records that source and the expected SHA-256 digest in `manifest.json`.
An entry with `status: archived` is retained as audit history after the file is
no longer a production dependency. The five `ci_autoscale` entries are the
reference: their on-disk copies were verified against `~/Ava-archive`, and the
component is now dead code.

## Add an entry

1. Obtain approval for the migration and copy the file into a durable git
   repository without changing the production copy.
2. Commit the durable copy. Choose a treeish and confirm that Git can read the
   object at the same repository-relative path.
3. Compute SHA-256 from the durable source, then independently compare it with
   the production file. For example:

   ```bash
   git -C ~/Ava-private cat-file blob HEAD:path/to/file | shasum -a 256
   shasum -a 256 "$AVA_HOME/source/path/to/file"
   ```

4. Add one manifest entry with a stable `id`, relative `path`, `git` source,
   lowercase SHA-256 digest, status, and useful audit notes.
5. Run the verifier against the production checkout root.

## Verify

Run manually from the repository:

```bash
.venv/bin/python -m ops.private_files verify --root "$AVA_HOME/source"
```

`--root` is explicit for cron and watchdog use. When omitted, it defaults to
`$AVA_HOME/source` if `AVA_HOME` is set, otherwise to the current directory.
`--manifest` defaults to the co-located `manifest.json`. `--json` emits a JSON
array for machine consumers.

The command emits one result per entry and exits 0 only when every checked file
exists, its digest matches, its git object exists, and the durable source bytes
have the same digest. Missing files, changed bytes, or unavailable sources exit
1. A cron or watchdog can alert on that exit status without granting the
verifier write access.

## Safety boundary

Verification is read-only. It never creates, moves, replaces, or deletes a
checked file or its source. Moving a live private file into a durable source and
deleting dead residue are separate operator-approved changes. Their migration
steps and rollback points belong in the corresponding PR description, not in
this verifier.
