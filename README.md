# solana-spear

A Rust / Anchor audit agent fleet for Solana programs.

`spear` clones a GitHub repo, collects all in-scope `.rs` files, fans them out
to 8 specialist auditor agents in parallel, then runs a judge + dedup pass and
writes a Markdown report to `~/Desktop`. `spear-poc` takes an existing audit
report and tries to reproduce each finding as a runnable proof-of-concept
against the real workspace.

Both tools are REPLs — you paste a GitHub URL (and optionally a scope), and
they do the rest. They can also be driven non-interactively so another agent
(e.g. Claude Code) can call `spear` as a self-contained audit step — see
[Use as a standalone workflow](#use-as-a-standalone-workflow).

> **Status:** the audit fleet (`spear`) is stable and the main product. The PoC
> reproducer (`spear-poc`) is **work in progress** — the environment setup and
> build/heal loop work, but end-to-end finding reproduction is still rough and
> not reliable on every repo. Treat it as experimental.

---

## Layout

```
solana-spear/
├── spear             # launcher: audit REPL (harness.py)
├── spear-poc         # launcher: PoC reproducer REPL (poc/harness.py)
├── harness.py        # audit REPL — parse → clone → fan-out → judge → dedup → write
├── agent_runner.py   # single-agent LLM call with retry / backoff
├── collector.py      # git clone, .rs walker, source concatenation, token estimate
├── llm_client.py     # unified Anthropic + OpenAI streaming client
├── req_parser.py     # NL → { repo_url, scope, commit } parser
├── judge.py          # runs the judge prompt over each agent's output, then merges
├── dedup.py          # semantic merge of judge findings
├── writer.py         # renders the final Markdown report
├── config.py         # models, agents list, prompt paths, limits
├── judging.md        # judge prompt
├── prompts/          # 8 specialist agent prompts + shared-rules + dedup
└── poc/              # spear-poc: env setup, extraction, executor, assertion, debug
    ├── harness.py
    ├── runner.py
    └── prompts/
```

## How it works

```
NL request ─► req_parser ─► clone + collect .rs ─► 8 agents (parallel)
                                                        │
                                                        ▼
         report ◄── writer ◄── dedup ◄── judge (per-agent, merged)
```

1. **Parse** — `req_parser` turns your line ("`audit foo/bar programs/vault`")
   into `{ repo_url, scope, commit }` with one LLM call.
2. **Collect** — `collector` clones the repo (optionally sparse by scope),
   walks every in-scope `.rs` file, and concatenates them into one source blob.
3. **Fan out** — the 8 specialist agents run concurrently against that blob,
   each emitting findings in the shared `| Field | Content |` table format.
4. **Judge** — `judge` runs the `judging.md` false-positive gate over each
   agent's raw findings (one call per agent, bounded concurrency), then merges.
5. **Dedup** — one LLM pass semantically merges duplicate findings across agents.
6. **Write** — `writer` renders `~/Desktop/audit-<repo>-<sha>.md`.

## The agent fleet

`spear` runs these 8 specialists concurrently against the same source blob.
Each has its own prompt in `prompts/`; all share `prompts/shared-rules.md`.

| Agent              | Focus                                                     |
| ------------------ | --------------------------------------------------------- |
| `Arithmetic`       | Over/underflow, precision loss, rounding direction        |
| `AccessControl`    | Signer / authority / has_one / PDA-only entrypoints       |
| `AccountValidation`| Account discriminator, owner, mint, ATA, seed checks      |
| `CPI`              | Cross-program invocation safety, callee assumptions       |
| `SPL-Token2022`    | Extensions, transfer hooks, interest-bearing, fee-on-tx   |
| `Economic`         | Fee/price/incentive design, MEV, oracle assumptions       |
| `Invariant`        | Protocol invariants (total supply, reserves, accounting)  |
| `MoneyFlow`        | Deposits, withdrawals, rewards, refunds, escrow paths     |

Each specialist emits findings in the shared `| Field | Content |` table
format. The judge accepts/rejects each finding under the FP gate in
`judging.md`; the dedup pass then semantically merges duplicates across agents.

## Install

Requires Python 3.10+, `git`, and either an OpenAI or Anthropic API key.

```bash
cd solana-spear
pip install -r requirements.txt
```

Optionally, drop `spear` and `spear-poc` on your `PATH`:

```bash
ln -sfn "$PWD/spear"     /usr/local/bin/spear
ln -sfn "$PWD/spear-poc" /usr/local/bin/spear-poc
```

First run prompts for an API key and saves it (mode `0600`) to
`~/.solana-spear/config.json`.

## Usage — `spear`

```bash
spear                             # opens the REPL
spear --model opus5               # one-off model override
spear --set-model sol             # persist default model
spear --reset-key                 # clear the saved key for current provider
```

Inside the REPL, paste any of:

```
https://github.com/foo/bar
https://github.com/foo/bar/tree/<commit>/programs/vault
audit foo/bar programs/mint, programs/vault
foo/bar only look at programs/vault
```

REPL commands:

```
/help              show help
/model [name]      show / switch model
/clean             remove leftover $TMPDIR/audit-* clones
/reset-key         delete saved API key
/clear             clear screen
/quit  /exit       exit
```

Reports land in `~/Desktop/audit-<repo>-<sha>.md`.

## Customizing the prompts (DIY)

The prompts **are** the product — tune them freely. Nothing but plain Markdown
is loaded at runtime, so edits take effect on the next run with no code change.

- `prompts/shared-rules.md` — prepended to **every** agent as its system
  preamble. This is where the output contract lives (the `| Field | Content |`
  finding format, severity scale, false-positive discipline). Change it once and
  all 8 agents follow.
- `prompts/<agent>.md` — one file per specialist. Edit these to re-focus an
  agent, add protocol-specific checklists, or paste known bug patterns.
- `judging.md` — the judge / false-positive gate. Loosen it for recall, tighten
  it for precision.
- `prompts/dedup.md` — how duplicate findings are merged.

Add, remove, or rename an agent by editing the `AGENTS` dict in `config.py`:

```python
AGENTS = {
    "Arithmetic":        "Arithmetic.md",
    "MyProtocolChecks":  "my-protocol-agent.md",   # ← drop a new prompt in prompts/
    ...
}
```

Keep two things stable so the pipeline keeps parsing output: the
`| Field | Content |` finding table (the judge, dedup, and writer all count/parse
on it) and the `## Audit report: <brand>` header the judge expects. Everything
else — tone, checklists, examples — is yours to rewrite.

## Use as a standalone workflow

Because a run is deterministic (request in → one Markdown file out at a known
path), `spear` can be invoked as a single step by another agent or CI, not just
used interactively.

**One-shot, non-interactive.** Seed the API key once (run `spear` interactively,
or write `~/.solana-spear/config.json` yourself), then pipe a single request
into the embedded harness. It processes the line, hits EOF, and exits:

```bash
# key must already be in ~/.solana-spear/config.json
echo "audit foo/bar programs/vault" \
  | SPEAR_EMBEDDED=1 python3 harness.py --model opus5

# report path is deterministic:
ls ~/Desktop/audit-bar-*.md
```

`~/.solana-spear/config.json` holds the keys and default model:

```json
{
  "default_model": "claude-opus-5-5",
  "anthropic_api_key": "sk-ant-...",
  "openai_api_key": "sk-..."
}
```

**For an agent (e.g. Claude Code).** Hand it a contract like:

> Run `echo "audit <repo> <scope>" | SPEAR_EMBEDDED=1 python3 harness.py`,
> wait for it to exit, then read the newest `~/Desktop/audit-*.md` and summarize
> the findings.

The agent drives the whole audit as one tool call and consumes the Markdown
report as structured output. `SPEAR_EMBEDDED=1` skips the "open a new Terminal
window" behavior of the `spear` launcher and runs in-place.

> Note: `spear` currently audits the **first** target per request; batch multiple
> repos by issuing one invocation each.

## Usage — `spear-poc` (experimental)

```bash
spear-poc
```

Paste (1) the GitHub URL of the audited repo and (2) the local path to the
`spear` Markdown report. `spear-poc` will:

1. Set up the Anchor / Solana toolchain (`env_setup` agent, `poc/env_agent.py`).
2. Clone the repo and figure out the minimal `workspace.members`
   (`build_members` agent).
3. Run `yarn install` and `anchor build`, self-healing common build errors
   (`debug` agent, `poc/debug_agent.py`).
4. For each finding: scope the relevant code (`scope` agent), synthesize an
   attack transaction (`executor` agent), and assert the expected impact
   (`assertion` agent).

Non-interactive entry point:

```bash
python3 -m poc.runner <audit-report.md> <workspace-dir>
```

This part is still being hardened — expect build/repro failures on
less-conventional repos.

## Models

Set with `/model` in the REPL or `--set-model` on the CLI. Aliases in
`config.py`:

```
opus5 / opus5.5       → claude-opus-5-5   (default)
opus / opus4.7        → claude-opus-4-7
sonnet / sonnet5      → claude-sonnet-5
haiku / haiku4.5      → claude-haiku-4-5
fable / fable5        → claude-fable-5
sol / 5.6 / gpt5.6    → gpt-5.6-sol
terra / luna          → gpt-5.6-terra / gpt-5.6-luna
5.5 / 5.4 / 5.4-mini  → gpt-5.5 / gpt-5.4 / gpt-5.4-mini
codex                 → gpt-5.3-codex-spark
```

Default: `claude-opus-5-5`. The provider (Anthropic vs OpenAI) is inferred from
the model name; the matching key from `config.json` is used automatically.

## Limits & tuning

Defined in `config.py`:

| Constant                | Default    | Meaning                                 |
| ----------------------- | ---------- | --------------------------------------- |
| `MAX_TOKENS_AGENT`      | 65 536     | Per-agent output cap                    |
| `MAX_TOKENS_JUDGE`      | 128 000    | Judge output cap                        |
| `MAX_TOKENS_DEDUP`      | 8 192      | Per dedup LLM call                      |
| `STALL_LIMIT_SEC`       | 600        | Idle-kill an agent with no token growth |
| `MAX_SOURCE_TOKENS_WARN`| 180 000    | Warn when source is large               |
| `MAX_SOURCE_TOKENS_HARD`| 220 000    | Refuse to run above this                |

File filtering:

- `EXCLUDE_PATH_SEGMENTS` — `tests`, `benches`, `examples`, `fuzz`, `target`,
  `.anchor`, `migrations`, `.git`, `node_modules`
- `EXCLUDE_FILE_REGEXES` — `tests.rs`, `test_*.rs`, `*_test(s).rs`,
  `error(s).rs`, `event(s).rs`

## Files written

| Path                                      | Written by |
| ----------------------------------------- | ---------- |
| `~/.solana-spear/config.json`             | first run — API keys, default model |
| `~/.solana-spear/workspaces/<repo>/`      | `spear-poc` — cached workspaces |
| `$TMPDIR/audit-*`                         | `spear` — per-run clone (auto-cleaned; `/clean` for leftovers) |
| `~/Desktop/audit-<repo>-<sha>.md`         | `spear` — final report |

## License

MIT — see [LICENSE](LICENSE).
