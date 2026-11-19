# Shared-workspace rules (read this at the start of every task)

You are working alongside other agents in a shared workspace. You do not know what
they have done or what they are doing right now. The `srcp-capabilities` MCP server
is the team's shared memory, task board and safe file store. **Using it is in your
own interest** — it stops you redoing work, colliding with a peer, or losing an edit.

Configure the server (any MCP-capable runtime — Claude Code, Hermes, Codex):

```json
{
  "mcpServers": {
    "srcp": {
      "command": "python3",
      "args": ["-m", "srcp.service_server"],
      "env": {
        "PYTHONPATH": "/home/ryan/project/srcp/src",
        "SRCP_DB": "/home/ryan/project/srcp/results/team.sqlite3"
      }
    }
  }
}
```

## Before you start

1. **`memory_recall(namespace, query)`** — check what earlier agents learned about
   this task. This is the single highest-value call: a two-second lookup can save
   you an hour of repeated investigation.
2. **`plan_status(board)`** — see which tasks are already claimed or done.

## When you pick up work

3. **`plan_claim(board, task_id, principal)`** — reserve a task before starting it.
   If it returns `ALREADY_CLAIMED`, do not do that task; pick another. This is what
   prevents two agents from producing conflicting work on the same item.
4. Work on your task. When finished:
   - **`plan_complete(board, task_id, principal, result)`** — record your outcome.
   - **`memory_commit(namespace, entry, principal, tags)`** — write down anything
     non-obvious you learned (a failed approach, a gotcha, a decision rationale).
     Future agents will thank you, and so will your own next session.

## When you edit a shared file

5. Prefer **`file_edit(resource_id, principal, op)`** with a declarative op over
   hand-editing a shared file:
   - `{"kind": "json_set", "key": "status", "value": "done"}` — set one field.
   - `{"kind": "json_merge", "value": {...}}` — merge several fields.
   - `{"kind": "append", "content": "..."}` — add a line (logs, notes).
   - `{"kind": "replace", "content": "..."}` — whole-file rewrite (only when you
     own the file; a concurrent rewrite surfaces a real conflict instead of
     silently clobbering).

   It handles lease, version checking and retry internally. Concurrent edits to
   *different* fields both survive; two edits to the *same* field are surfaced as
   a conflict, never silently resolved by "last write wins".

6. **`file_read(resource_id, principal)`** — read the authoritative shared copy
   with its version. If `stale_risk` is set, another agent holds a write lease;
   re-read before acting on the content.

6b. **Real files (no shadow copy)**: if the server runs with `--root`, bind a real
   file once with **`file_bind(resource_id, path, principal)`**. Then `file_edit`
   writes to that actual file atomically and `file_read` returns its real bytes.
   If you (or a human) edited the file directly with normal tools, the next read/edit
   returns `AUTHORITY_CONFLICT` — your direct change is preserved, never overwritten.
   Resolve it by having an owner call **`file_adopt(resource_id, principal)`**, which
   records the external change in the audit trail; then normal edits resume.

## What NOT to do

- For bound files, editing directly on disk is *detected* (you get
  `AUTHORITY_CONFLICT` and must ask an owner to adopt). Prefer `file_edit` so your
  change is versioned and visible to the team without an adopt step.
- For unbound resources, do not write the backing file directly — SRCP's copy is
  authoritative and a direct write is invisible to the team.
- Do not skip `plan_claim`. Claiming is cheap; duplicate work is expensive.
- Do not leave a task claimed-but-never-completed if you abandon it — call
  `plan_complete` with a note, or let the claim expire (`hold_ms`).

## Why follow this

Every rule above saves *you* time: fewer repeated investigations, no duplicated
work, no silently lost edits. The protocol underneath (leases, fencing, CAS,
conflict records, audit) runs automatically — you never see it. You are not
"complying with a protocol"; you are using a shared brain that makes your job easier.
