# Build Members Agent — compute the minimal compilable workspace.members from audit_targets

The user has confirmed the target programs to audit (`audit_targets`); now you must compute the **minimal set of workspace.members required to compile these targets**.

## Why this step is needed

Anchor projects commonly use a monorepo structure:
```
programs/
├── futarchy/            (target 1, references damm_v2_cpi via path)
├── conditional_vault/   (target 2)
├── damm_v2_cpi/         (futarchy depends on it, but the user did not select it for audit)
├── bid_wall/            (unrelated to the targets)
└── ...
```

If `workspace.members` contains only the targets, Cargo cannot resolve `path = "../damm_v2_cpi"` and will fail. So you must **recursively find every sibling crate transitively referenced by the targets** and put them all into members. But **do not add them to `[programs.localnet]`** —— they are only referenced as libraries by the targets; they produce no .so and need no keypair.

## Your algorithm

1. `closure = set(audit_targets)`  (each target lives in `programs/<name>/`)
2. For each crate in the closure:
   - `probe: cat_file(programs/<name>/Cargo.toml)`
   - Find lines of the form `name = { ..., path = "../XXX", ... }` in the `[dependencies]` section
   - Extract the sibling directory name that the `path` value points to
3. Add sibling crates to the closure. If one has not been probed before, keep recursing with cat_file
4. Until the closure is stable (a full pass adds no new members)
5. **Return `build_members`** = each crate in the closure converted to a workspace-relative directory path (`programs/futarchy`)

## Probe usage

Each round you may emit only one `agent_step`, either:
- `action: "probe"` to keep reading a Cargo.toml — mainly `cat_file`, occasionally `list_dir` (confirm a directory exists) or `grep` (search across files within the workspace)
- `action: "commit"` to submit the final `build_members`

**Probe at least every Cargo.toml in audit_targets**; do not commit on a guess. Up to 15 probe rounds.

Available probes:
- `cat_file(path)` — read a workspace-relative file
- `list_dir(path)` — list a directory (confirm whether `programs/xxx/` exists)
- `grep(pattern, path)` — use sparingly, only when you need to confirm across files where a given crate name appears

## Edge cases

- `path = "../damm_v2_cpi"` (relative to this crate's directory) → resolve it to a workspace-relative path and add it to the closure. Derive it with the `programs/<current>/..` logic —— e.g. starting from `programs/futarchy`, `../damm_v2_cpi` = `programs/damm_v2_cpi`
- `path = "../../shared/foo"` → expands outside the workspace? Check whether `shared/foo/Cargo.toml` exists; if it does, add it
- Directory does not exist → **still put it into build_members** (Cargo will produce a meaningful error for the user to investigate, rather than us silently dropping it)
- Circular dependency (A→B→A) → the closure set deduplicates naturally and will not loop forever
- `path` deps that are git or crates.io versions (no `path` field) → ignore them entirely; those are external dependencies that Cargo fetches on its own

## Output schema (at commit time)

```json
{
  "build_members": [
    "programs/futarchy",
    "programs/conditional_vault",
    "programs/damm_v2_cpi"
  ],
  "rationale": "futarchy depends on damm_v2_cpi via { path = \"../damm_v2_cpi\" }; conditional_vault has no path dependencies"
}
```

- `build_members` must **contain every audit_target** (each target appears as `programs/<name>`)
- Use workspace-relative paths (with the `programs/` prefix), not `./` and not absolute paths
- `rationale` explains in one or two sentences which crates were added beyond audit_targets, and why

## Common mistakes (avoid these)

- ❌ Committing after cat_file on just one target —— you must look at the Cargo.toml of all audit_targets and recursively expand every newly discovered crate
- ❌ Putting crates outside audit_targets into audit_targets —— you only output build_members; you do not modify audit_targets
- ❌ Adding external crates (git deps, crates.io deps) into build_members —— only add path-type sibling crates
- ❌ Using absolute paths —— always use workspace-relative paths
