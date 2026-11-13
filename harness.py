import asyncio
import getpass
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_runner import run_agent
from collector import (
    clone_repo,
    collect_rs_files,
    concat_source,
    estimate_tokens,
    repo_name_from_url,
)
from config import (
    AGENTS,
    CONFIG_DIR,
    CONFIG_FILE,
    DEDUP_PROMPT,
    DEFAULT_MODEL,
    JUDGE_PROMPT,
    MAX_SOURCE_TOKENS_HARD,
    MAX_SOURCE_TOKENS_WARN,
    MAX_TOKENS_DEDUP,
    MODEL_ALIASES,
    PROMPTS_DIR,
    SHARED_RULES,
    STALL_LIMIT_SEC,
)
from dedup import run_dedup
from judge import run_judge
from llm_client import LLMClient, infer_provider
from req_parser import parse_request
from writer import write_raw_fallback, write_report


BANNER = r"""
   ╭──────────────────────────────────────────────────╮
   │   🗡️   solana-spear  ·  Rust audit agent fleet   │
   ╰──────────────────────────────────────────────────╯
"""

HELP = """
Commands:
  /help              Show help
  /model [name]      View/switch model (sol / terra / luna / opus5 / sonnet …)
  /clean             Clean up leftover $TMPDIR/audit-* directories
  /reset-key         Delete the saved API key
  /clear             Clear the screen
  /quit  /exit       Exit

Natural-language input examples:
  audit https://github.com/foo/bar programs/mint, programs/vault
  https://github.com/foo/bar only programs/vault
  foo/bar whole repo
"""


def _read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data))
    CONFIG_FILE.chmod(0o600)


def _key_field_for(model: str) -> str:
    """Which config field holds the API key for this model's provider."""
    return "anthropic_api_key" if infer_provider(model) == "anthropic" else "openai_api_key"


def _load_or_prompt_key(reset: bool = False) -> None:
    """Ensure the config has an API key for the currently-selected model.

    Config layout: `openai_api_key` and `anthropic_api_key` can both exist;
    the one matching the current model gets loaded by LLMClient. --reset-key
    only clears the key for the CURRENT provider.
    """
    data = _read_config()
    model = data.get("default_model") or DEFAULT_MODEL
    field = _key_field_for(model)
    provider = "Anthropic" if field == "anthropic_api_key" else "OpenAI"
    if reset:
        data.pop(field, None)
        _write_config(data)
        print(f"[spear] Deleted {provider} API key.")
    key = data.get(field)
    if key:
        return
    key = getpass.getpass(f"[spear] Enter {provider} API key (hidden): ").strip()
    if not key:
        print("[spear] No key provided, exiting.")
        sys.exit(1)
    data[field] = key
    _write_config(data)
    print(f"[spear] key saved to {CONFIG_FILE}")


def _resolve_model(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    return MODEL_ALIASES.get(text.lower(), text)


def _current_model() -> str:
    return _read_config().get("default_model", DEFAULT_MODEL)


def _set_model(name: str) -> str:
    resolved = _resolve_model(name)
    data = _read_config()
    data["default_model"] = resolved
    _write_config(data)
    return resolved


def _ask(msg: str) -> str:
    print(msg, end="", flush=True)
    return input().strip()


def _make_client() -> LLMClient:
    """Instantiate LLMClient from the current config."""
    return LLMClient(_read_config())


def _fmt_tokens(t: int) -> str:
    if t >= 1000:
        return f"{t/1000:.1f}k"
    return str(t)


def _tmp_audit_dirs() -> list[Path]:
    """All temporary directories left behind by clone_repo."""
    return sorted(Path(tempfile.gettempdir()).glob("audit-*"))


def _dir_sizes(dirs: list[Path]) -> dict[Path, int]:
    """Batch-fetch each directory's byte count in a single du call; fall back to 0 on failure."""
    if not dirs:
        return {}
    try:
        proc = subprocess.run(
            ["du", "-sk", *[str(d) for d in dirs]],
            capture_output=True, text=True, timeout=30,
        )
        out: dict[Path, int] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    out[Path(parts[1])] = int(parts[0]) * 1024
                except ValueError:
                    pass
        return out
    except Exception:
        return {}


def _clean_tmp() -> None:
    dirs = _tmp_audit_dirs()
    if not dirs:
        print("[spear] No audit-* temporary directories to clean up.")
        return
    sizes = _dir_sizes(dirs)
    total = sum(sizes.get(d, 0) for d in dirs)
    total_mb = total / 1024 / 1024
    print(f"[spear] Found {len(dirs)} audit-* directories, ~{total_mb:.1f} MB total:")
    for d in dirs[:10]:
        mb = sizes.get(d, 0) / 1024 / 1024
        print(f"    {d.name:<28}  {mb:6.1f} MB")
    if len(dirs) > 10:
        print(f"    ... and {len(dirs) - 10} more")
    try:
        ans = input("[spear] Delete all? (y/N): ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        print("[spear] Cancelled.")
        return
    ok = err = 0
    for d in dirs:
        try:
            shutil.rmtree(d)
            ok += 1
        except Exception as e:
            err += 1
            print(f"    ✗ {d.name}: {e}")
    print(f"[spear] Deleted {ok} directories ({err} failed), freed ~{total_mb:.1f} MB")


def _cleanup_repo_root(repo_root: Path) -> None:
    """Remove this run's clone directory after the audit finishes, to keep $TMPDIR from piling up."""
    try:
        shutil.rmtree(repo_root)
        print(f"[spear] Cleaned up temporary directory {repo_root}")
    except Exception as e:
        print(f"[spear] Failed to clean up temporary directory {repo_root}: {e}")


def _drain_stdin() -> None:
    """Flush the stdin input buffer.

    During an audit the REPL is blocked on a subprocess/LLM call. If the user
    pasted multiple lines (e.g. two newline-separated URLs), the first line is
    consumed and the rest sit in the kernel tty buffer. As soon as the audit
    returns, the next `input()` immediately swallows the buffered lines as user
    input, making it look like "spear sent the link again by itself".
    """
    if not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        # Non-unix / termios unsupported: best-effort, ignore
        pass


def _enable_bracketed_paste() -> None:
    """Make the terminal bundle pasted content into one chunk so pasting no longer auto-submits.

    Without this, pasting `URL\\n` triggers an immediate input() return; with it
    enabled, readline treats the whole paste (including newlines) as a single
    editable block, and only submits when the user actually presses Enter.
    Only GNU readline 8+ recognizes this binding; libedit / old readline
    silently ignore it.
    """
    try:
        import readline
        readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        pass


async def _run_all_agents(client: LLMClient, source_blob: str, model: str):
    import time
    shared = SHARED_RULES.read_text(encoding="utf-8")
    names = list(AGENTS.keys())
    max_name = max(len(n) for n in names)
    total = len(names)
    t0 = time.monotonic()
    print(f"[spear] Running {total} agents concurrently (model: {model}):", flush=True)

    progress: dict[str, dict] = {
        n: {
            "state": "queued", "start": None, "end": None,
            "size": 0, "err": None, "tokens": 0,
            "last_tok_change": None,
        }
        for n in names
    }

    dyn_state = {"lines": 0}
    use_ansi = sys.stdout.isatty()

    def _idle_since(name: str) -> float:
        p = progress[name]
        ref = p["last_tok_change"] or p["start"]
        if ref is None:
            return 0.0
        return time.monotonic() - ref

    def _compose_dynamic() -> list[str]:
        running = [n for n in names if progress[n]["state"] == "running"]
        if not running:
            return []
        elapsed = time.monotonic() - t0
        done_n = sum(1 for n in names if progress[n]["state"] == "done")
        fail_n = sum(1 for n in names if progress[n]["state"] == "failed")
        header = (
            f"─── live · elapsed {elapsed:.0f}s · "
            f"running {len(running)} · done {done_n} · failed {fail_n} · "
            f"idle-kill @{STALL_LIMIT_SEC}s ───"
        )
        lines = [header]
        for n in running:
            p = progress[n]
            dt = time.monotonic() - p["start"]
            tok = _fmt_tokens(p["tokens"])
            idle = _idle_since(n)
            warn = " ⚠️" if idle >= STALL_LIMIT_SEC * 0.6 else ""
            lines.append(
                f"  🔄 {n:<{max_name}}  {dt:5.0f}s  {tok:>6} tok  "
                f"idle {idle:5.0f}s{warn}"
            )
        return lines

    def _erase_dynamic():
        if not use_ansi or dyn_state["lines"] == 0:
            return
        sys.stdout.write(f"\033[{dyn_state['lines']}A\033[J")
        sys.stdout.flush()
        dyn_state["lines"] = 0

    def _paint_dynamic():
        if not use_ansi:
            return
        _erase_dynamic()
        lines = _compose_dynamic()
        if not lines:
            return
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        dyn_state["lines"] = len(lines)

    def _permanent(msg: str):
        _erase_dynamic()
        print(msg, flush=True)
        _paint_dynamic()

    async def _wrapped(name: str, fname: str):
        agent_prompt = (PROMPTS_DIR / fname).read_text(encoding="utf-8")
        system = shared + "\n\n---\n\n" + agent_prompt
        user = "The following is the source code to audit; output findings per the shared rules.\n\n" + source_blob
        progress[name]["state"] = "running"
        progress[name]["start"] = time.monotonic()
        progress[name]["last_tok_change"] = time.monotonic()
        elapsed = time.monotonic() - t0
        _permanent(f"[+{elapsed:5.0f}s] 🔄 {name:<{max_name}} started")

        def _on_progress(tokens: int):
            if tokens > progress[name]["tokens"]:
                progress[name]["tokens"] = tokens
                progress[name]["last_tok_change"] = time.monotonic()

        try:
            _, out, err = await run_agent(
                client, name, system, user, model, on_progress=_on_progress
            )
        except asyncio.CancelledError:
            progress[name]["end"] = time.monotonic()
            progress[name]["state"] = "failed"
            progress[name]["err"] = f"stalled >{STALL_LIMIT_SEC}s"
            dt = progress[name]["end"] - progress[name]["start"]
            elapsed = time.monotonic() - t0
            tok = _fmt_tokens(progress[name]["tokens"])
            _permanent(
                f"[+{elapsed:5.0f}s] ✗  {name:<{max_name}} "
                f"killed after stalling {dt:5.0f}s ({tok} tok, no growth {STALL_LIMIT_SEC}s+)"
            )
            return name, None, progress[name]["err"]

        progress[name]["end"] = time.monotonic()
        dt = progress[name]["end"] - progress[name]["start"]
        elapsed = time.monotonic() - t0
        tok = _fmt_tokens(progress[name]["tokens"])
        if out is not None:
            progress[name]["state"] = "done"
            progress[name]["size"] = len(out)
            sz = len(out)
            size_str = f"{sz//1024}KB" if sz >= 1024 else f"{sz}B"
            _permanent(
                f"[+{elapsed:5.0f}s] ✓  {name:<{max_name}} done {dt:5.0f}s "
                f"({size_str}, {tok} tok)"
            )
        else:
            progress[name]["state"] = "failed"
            progress[name]["err"] = err
            _permanent(
                f"[+{elapsed:5.0f}s] ✗  {name:<{max_name}} failed {dt:5.0f}s ({err})"
            )
        return name, out, err

    async def _refresher():
        try:
            while True:
                await asyncio.sleep(1)
                _paint_dynamic()
        except asyncio.CancelledError:
            return

    task_by_name: dict[str, asyncio.Task] = {}

    async def _stall_watcher():
        try:
            while True:
                await asyncio.sleep(5)
                for n in names:
                    if progress[n]["state"] != "running":
                        continue
                    if _idle_since(n) >= STALL_LIMIT_SEC:
                        task = task_by_name.get(n)
                        if task and not task.done():
                            task.cancel()
        except asyncio.CancelledError:
            return

    for n, f in AGENTS.items():
        task_by_name[n] = asyncio.create_task(_wrapped(n, f))
    refresher = asyncio.create_task(_refresher())
    watcher = asyncio.create_task(_stall_watcher())
    try:
        results = await asyncio.gather(*task_by_name.values())
    finally:
        watcher.cancel()
        refresher.cancel()
        for t in (watcher, refresher):
            try:
                await t
            except asyncio.CancelledError:
                pass
        _erase_dynamic()

    outputs: dict[str, str] = {}
    status: dict[str, str] = {}
    for name, out, err in results:
        if out is not None:
            outputs[name] = out
            status[name] = "ok"
        else:
            status[name] = f"failed: {err}"

    elapsed = time.monotonic() - t0
    ok_n = sum(1 for s in status.values() if s == "ok")
    total_tok = sum(progress[n]["tokens"] for n in names)
    print(
        f"[spear] agent stage complete: {ok_n}/{total} succeeded, "
        f"took {elapsed:.1f}s, total output ≈ {_fmt_tokens(total_tok)} tok",
        flush=True,
    )
    return outputs, status


async def _run_audit(
    client: LLMClient, repo_url: str, scope: list[str], commit: str | None, model: str
) -> None:
    if not shutil.which("git"):
        print("[spear] git is required; install it and retry.")
        return
    pin = f" @ {commit}" if commit else " @ HEAD"
    scope_msg = f"  scope={scope}" if scope else "  (whole repo)"
    print(f"[spear] Fetching… {repo_url}{pin}{scope_msg}")
    try:
        repo_root, sha = clone_repo(repo_url, commit, scope=scope)
    except Exception as e:
        print(f"[spear] clone failed: {e}")
        return
    print(f"[spear] commit {sha} → {repo_root}")

    try:
        files, excluded = collect_rs_files(repo_root, scope)
        if not files:
            print("[spear] No .rs files found (is the scope correct?)")
            return
        source_blob = concat_source(repo_root, files)
        tokens = estimate_tokens(source_blob)
        kb = len(source_blob) // 1024
        print(f"[spear] Collected {len(files)} .rs files (excluded {excluded})")
        print(f"[spear] source size {kb}KB / ~{tokens//1000}k tokens")
        if tokens > MAX_SOURCE_TOKENS_HARD:
            print(f"[spear] Exceeds hard limit of {MAX_SOURCE_TOKENS_HARD//1000}k tokens, please narrow the scope.")
            return
        if tokens > MAX_SOURCE_TOKENS_WARN:
            print(f"[spear] ⚠️  source is large (~{tokens//1000}k tokens), continuing.")

        outputs, status = await _run_all_agents(client, source_blob, model)

        ok_count = sum(1 for s in status.values() if s == "ok")
        if ok_count == 0:
            print("[spear] All agents failed, skipping judge.")
            return
        skipped = [n for n, s in status.items() if s != "ok"]
        tail = f", skipped: {', '.join(skipped)}" if skipped else ""
        print(f"[spear] {ok_count}/{len(status)} done{tail}. Starting judge…")
        agent_findings = sum(out.count("| Field | Content |") for out in outputs.values())
        print(f"[spear] agent stage produced {agent_findings} findings")

        import time as _t
        judge_prompt = JUDGE_PROMPT.read_text(encoding="utf-8")
        t0 = _t.monotonic()

        # judge is non-streaming (see judge.py) — streaming judge collides with
        # Codex Pro's per-account single-flight limit across terminals. Poll the
        # task every 10s so the user gets a heartbeat while it runs.
        judge_task = asyncio.create_task(
            run_judge(client, judge_prompt, outputs, model)
        )
        while not judge_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(judge_task), timeout=10)
            except asyncio.TimeoutError:
                print(f"    ⏳ judge running… ({_t.monotonic()-t0:.0f}s)")

        judge_out, judge_tokens, judge_err = judge_task.result()
        tok_str = _fmt_tokens(judge_tokens) if judge_tokens else "?"
        print(
            f"[spear] judge finished, took {_t.monotonic()-t0:.1f}s "
            f"(output ≈ {tok_str} tok)"
        )
        if judge_out:
            judge_findings = judge_out.count("| Field | Content |")
            print(f"[spear] judge output {judge_findings} findings")

        repo_name = repo_name_from_url(repo_url)
        if judge_out is None:
            path = write_raw_fallback(
                repo_name, scope, sha, len(files), excluded, status, outputs, judge_err or "unknown"
            )
            print(f"[spear] judge failed: {judge_err}")
            print(f"[spear] Wrote raw output: {path}")
            return

        # dedup stage: parse the judge output back into a Finding list,
        # do a semantic dedup/merge in a single LLM call; on failure, fall back
        # to using the judge output as-is.
        dedup_prompt = DEDUP_PROMPT.read_text(encoding="utf-8")
        t1 = _t.monotonic()
        dedup_out, dedup_err, dedup_stats = await run_dedup(
            client, judge_out, dedup_prompt, total_agents=ok_count, max_tokens=MAX_TOKENS_DEDUP,
            source_blob=source_blob,
        )
        dt = _t.monotonic() - t1
        if dedup_out is not None:
            print(
                f"[spear] dedup complete, took {dt:.1f}s "
                f"(parsed {dedup_stats['parsed']}, "
                f"{dedup_stats['llm_calls']} LLM calls, {dedup_stats['merged_out']} after merge)"
            )
            final_out = dedup_out
        else:
            print(f"[spear] dedup failed: {dedup_err}, using judge output as-is")
            final_out = judge_out

        # project_name comes from the dedup LLM (falls back to `## Audit report: X`
        # in the judge prose if empty). Used to hoist the project's top-level name
        # into the top-level `# Audit Report:` header, avoiding it appearing after
        # `## Findings`.
        project_name = dedup_stats.get("project_name", "")

        path = write_report(
            repo_name, scope, sha, len(files), excluded, status, outputs, final_out,
            project_name=project_name,
        )
        print(f"[spear] Report written to: {path}")
    finally:
        _cleanup_repo_root(repo_root)


async def _handle_line(client: LLMClient, line: str, state: dict) -> None:
    line = line.strip()
    if not line:
        return
    if line in ("/quit", "/exit", ":q"):
        state["quit"] = True
        return
    if line == "/help":
        print(HELP)
        return
    if line == "/clear":
        print("\033[2J\033[H", end="")
        return
    if line == "/reset-key":
        _load_or_prompt_key(reset=True)
        return
    if line == "/clean":
        _clean_tmp()
        return
    if line.startswith("/model"):
        parts = line.split(maxsplit=1)
        if len(parts) == 1:
            print(f"[spear] Current model: {_current_model()}")
            print("[spear] Common aliases: sol / terra / luna / 5.4 / 5.4-mini / opus5 / sonnet / haiku / fable")
            print("[spear] Or enter the full name directly (gpt-5.6-sol, claude-opus-5, ...)")
        else:
            new = _set_model(parts[1])
            print(f"[spear] Switched to {new}")
        return
    if line.startswith("/"):
        print(f"[spear] Unknown command: {line}   /help lists available commands")
        return

    print("[spear] Parsing request…")
    try:
        targets, err = await parse_request(client, line)
    except Exception as e:
        status = getattr(e, "status_code", None)
        body = getattr(e, "body", None) or getattr(e, "response", None)
        print(f"[spear] parser error: {type(e).__name__}: {e}")
        if status is not None:
            print(f"[spear]   HTTP {status}")
        if body is not None:
            print(f"[spear]   body: {str(body)[:400]}")
        return
    if not targets:
        reason = f": {err}" if err else ". Please describe it again."
        print(f"[spear] Could not identify an audit target{reason}")
        return

    print("[spear] Parsed result:")
    for i, t in enumerate(targets, 1):
        commit_str = f" @ {t['commit']}" if t.get("commit") else " @ HEAD"
        scope_str = f"  ({', '.join(t['scope'])})" if t.get("scope") else "  (whole repo)"
        print(f"  {i}. {t['repo_url']}{commit_str}{scope_str}")

    if len(targets) > 1:
        print(f"[spear] ⚠️  Detected {len(targets)} targets; multi-repo merged audit + confirmation loop is not implemented yet, running the first one.")
    t0 = targets[0]
    await _run_audit(
        client, t0["repo_url"], t0.get("scope") or [], t0.get("commit"), _current_model()
    )


async def _repl() -> None:
    _enable_bracketed_paste()
    client = _make_client()
    print(BANNER)
    print(f"[spear] Model: {_current_model()}    type /help for commands, /quit to exit")
    state = {"quit": False}
    while not state["quit"]:
        _drain_stdin()
        try:
            line = input("\nspear ❯ ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\n[spear] (Ctrl+C, press again or type /quit to exit)")
            continue
        try:
            await _handle_line(client, line, state)
        except KeyboardInterrupt:
            print("\n[spear] Interrupted the current task.")
    print("[spear] bye 👋")


def _arg_value(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main() -> None:
    reset = "--reset-key" in sys.argv
    set_default = _arg_value("--set-model")
    cli_model = _arg_value("--model")

    if set_default:
        resolved = _set_model(set_default)
        print(f"[spear] Saved default model: {resolved}")
    if cli_model:
        resolved = _set_model(cli_model)
        print(f"[spear] Using model for this session: {resolved}")

    _load_or_prompt_key(reset=reset)
    try:
        asyncio.run(_repl())
    except KeyboardInterrupt:
        print("\n[spear] bye 👋")


if __name__ == "__main__":
    main()
