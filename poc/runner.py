"""PoC Runner — main CLI.

Usage:
    python3 -m poc.runner <audit-report.md> <workspace-dir>

For each finding in the audit report:
  1. Assertion Agent generates verdict spec (once).
  2. Executor Agent generates .test.ts, up to 5 attempts.
  3. Each attempt: write file → `yarn ts-mocha <file>` → parse stdout for POC_VALID tag.
  4. Verdict: VALID (1 pass) or WORTH_TO_CHECK (5 fails).

Writes augmented report next to input as `<name>-with-poc.md`.
"""
import argparse
import asyncio
import datetime as _dt
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

# Make sibling `collector` / `llm_client` importable when running as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector import collect_rs_files, concat_source  # type: ignore
from llm_client import LLMClient  # type: ignore
from poc.assertion_agent import make_verdict_spec
from poc.executor_agent import generate_test, ModelRefusalError
from poc.extractor import extract_findings, render_finding_for_prompt


CONFIG_FILE = Path.home() / ".solana-spear/config.json"
LOG_DIR = Path.home() / ".solana-spear" / "logs"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUIRED_TOOLS = ["anchor", "avm", "solana", "node", "yarn", "rustc", "cargo"]
MAX_ATTEMPTS = 10


_LOGGER = logging.getLogger("poc")
_LOGGER.setLevel(logging.DEBUG)  # FileHandler decides the actual filtering


def _setup_run_log(workspace: Path, override: Optional[Path] = None) -> Optional[Path]:
    """Open a log file for this run. Returns the file path, or None (when explicitly disabled).

    override:
      - None → default ~/.solana-spear/logs/<workspace-name>-<utc-ts>.log
      - Path("/dev/null") → disable the log (returns None, no handler added)
      - Path(other) → use this path
    """
    if override is not None and str(override) in ("/dev/null", "-"):
        return None
    if override is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.utcnow().strftime("%Y%m%d-%H%M%SZ")
        log_path = LOG_DIR / f"{workspace.name}-{ts}.log"
    else:
        log_path = override.expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # First clear any existing FileHandler on this Logger (avoid accumulation across multiple runs)
    for h in list(_LOGGER.handlers):
        if isinstance(h, logging.FileHandler):
            _LOGGER.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    fh = logging.FileHandler(str(log_path), encoding="utf-8", mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    ))
    _LOGGER.addHandler(fh)
    _LOGGER.info("=" * 72)
    _LOGGER.info(f"run start: workspace={workspace}")
    _LOGGER.info("=" * 72)
    return log_path


def _teardown_run_log() -> None:
    for h in list(_LOGGER.handlers):
        if isinstance(h, logging.FileHandler):
            _LOGGER.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


# on_step(step_id, status, detail)
#   step_id: "preflight" | "provision" | "rust_ver" | "solana_ver" | "platform_tools"
#          | "anchor_ver" | "build" | "extract" | "detect" | "collect" | "client"
#   status : "start" | "tick" | "ok" | "fail"
#   detail : human-readable string
StepCallback = Callable[[str, str, str], None]


def _log_step(step: str, status: str, detail: str) -> None:
    """Write on_step events to the log. Standalone function, used for tee in the wrappers."""
    level = logging.INFO if status in ("ok", "start") else (
        logging.ERROR if status == "fail" else logging.DEBUG
    )
    _LOGGER.log(level, f"step {step} {status}: {detail}")


@dataclass
class PocContext:
    report_path: Path
    workspace: Path
    findings: list[dict]
    workspace_info: dict
    source_blob: str
    client: LLMClient
    build_roots: list[Path]
    source_root: Path


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _make_client(cfg: dict) -> LLMClient:
    """Instantiate the provider-agnostic LLM adapter."""
    # LLMClient handles both openai_api_key and anthropic_api_key config fields
    # and picks provider by model name prefix. If neither key is present, prompt.
    if not (cfg.get("openai_api_key") or cfg.get("api_key") or cfg.get("anthropic_api_key")):
        key = getpass.getpass("[poc] API key: ").strip()
        # store into whatever field matches the model's provider
        from llm_client import infer_provider  # local import to avoid cycle at module load
        model = cfg.get("default_model") or "gpt-4o"
        field = "anthropic_api_key" if infer_provider(model) == "anthropic" else "openai_api_key"
        cfg[field] = key
    return LLMClient(cfg)


def _preflight() -> list[str]:
    """Return list of missing tools."""
    return [t for t in REQUIRED_TOOLS if not shutil.which(t)]


def _tool_versions() -> dict[str, str]:
    """Best-effort collect one-line versions for the tools we care about."""
    out: dict[str, str] = {}
    for t in ["anchor", "avm", "solana", "node", "yarn"]:
        p = shutil.which(t)
        if not p:
            continue
        try:
            r = subprocess.run([t, "--version"], capture_output=True, text=True, timeout=5)
            line = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
            out[t] = line
        except Exception:
            continue
    return out


def _detect_js_versions(workspace: Path) -> dict[str, str]:
    """Detect the versions of a few packages in the workspace that often crash the executor, for use in the prompt.

    Reads the `version` field directly from node_modules/<pkg>/package.json. The point is to show
    the executor: the test you're about to write will import which version of these packages, so pick the matching API signatures.

    The most critical is @coral-xyz/anchor — 0.29 uses `new Program(idl, programId, provider)`,
    a 3-arg signature, while 0.30+ changed it to `new Program(idl, provider)` (programId is read from idl.address).
    Get it wrong and bn.js assert hard-crashes, not even catchable.
    """
    out: dict[str, str] = {}
    interesting = [
        "@coral-xyz/anchor",
        "@solana/web3.js",
        "@solana/spl-token",
        "solana-bankrun",
        "anchor-bankrun",
        "typescript",
    ]
    for pkg in interesting:
        pkg_json = workspace / "node_modules" / pkg / "package.json"
        if not pkg_json.is_file():
            continue
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        v = data.get("version")
        if isinstance(v, str) and v:
            out[pkg] = v
    return out


def _detect_programs(workspace: Path) -> dict[str, str]:
    """Read Anchor.toml or scan programs/*/src/lib.rs for program name → declare_id.

    Best-effort. Returns {name: program_id_string}.
    """
    out: dict[str, str] = {}
    anchor_toml = workspace / "Anchor.toml"
    if anchor_toml.exists():
        try:
            text = anchor_toml.read_text(encoding="utf-8")
            # Match `name = "id"` lines inside `[programs.*]` sections
            in_programs = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("[programs."):
                    in_programs = True
                    continue
                if s.startswith("[") and in_programs:
                    in_programs = False
                    continue
                if in_programs:
                    m = re.match(r"^([a-zA-Z0-9_\-]+)\s*=\s*\"([1-9A-HJ-NP-Za-km-z]{32,44})\"", s)
                    if m:
                        out[m.group(1)] = m.group(2)
        except Exception:
            pass
    # Fallback: scan for declare_id!
    if not out:
        for lib in workspace.rglob("programs/*/src/lib.rs"):
            try:
                content = lib.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'declare_id!\("([1-9A-HJ-NP-Za-km-z]{32,44})"\)', content)
                if m:
                    name = lib.parent.parent.name
                    out[name] = m.group(1)
            except Exception:
                continue
    return out


def _rewrite_cargo_workspace_members(cargo_toml_text: str, member_paths: list[str]) -> str:
    """Narrow the members = [...] list in the [workspace] section of the root Cargo.toml to the given path list.

    member_paths: workspace-relative paths, e.g. ["programs/futarchy", "programs/damm_v2_cpi"].
    Usually contains audit_targets + the transitive closure of their path dependencies (computed by the build_members agent).
    Preserves the exclude section and other workspace fields. Uses line-level regex surgery, doesn't parse the whole TOML.
    """
    lines = cargo_toml_text.splitlines(keepends=True)
    new_members_block = (
        "members = [\n"
        + "".join(f'    "{p}",\n' for p in member_paths)
        + "]\n"
    )

    # Find the [workspace] section
    out: list[str] = []
    i = 0
    in_workspace = False
    replaced = False
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if stripped == "[workspace]":
            in_workspace = True
            out.append(ln)
            i += 1
            continue
        if in_workspace and stripped.startswith("[") and stripped.endswith("]"):
            in_workspace = False
        if in_workspace and stripped.startswith("members"):
            # Support three forms: members = [], members = ["a", "b"], members = [\n ... \n]
            if "[" in ln and "]" in ln:
                out.append(new_members_block)
                replaced = True
                i += 1
                continue
            # Multi-line array, keep eating until ]
            j = i + 1
            while j < len(lines) and "]" not in lines[j]:
                j += 1
            out.append(new_members_block)
            replaced = True
            i = j + 1
            continue
        out.append(ln)
        i += 1

    if not replaced:
        # No members found in the workspace section — add one
        # Insert it right below the [workspace] line
        final: list[str] = []
        inserted = False
        for ln in out:
            final.append(ln)
            if not inserted and ln.strip() == "[workspace]":
                final.append(new_members_block)
                inserted = True
        return "".join(final)
    return "".join(out)


def _rewrite_anchor_localnet(anchor_toml_text: str, targets: list[str]) -> str:
    """Delete non-target program lines from the [programs.localnet] section of Anchor.toml.

    Leaves [programs.<other cluster>] sections untouched (mainnet/devnet etc.), only touches localnet.
    """
    targets_set = set(targets)
    lines = anchor_toml_text.splitlines(keepends=True)
    out: list[str] = []
    in_localnet = False
    for ln in lines:
        stripped = ln.strip()
        if stripped == "[programs.localnet]":
            in_localnet = True
            out.append(ln)
            continue
        if in_localnet and stripped.startswith("[") and stripped.endswith("]"):
            in_localnet = False
            out.append(ln)
            continue
        if in_localnet and stripped and not stripped.startswith("#"):
            m = _re.match(r"^([A-Za-z_][\w-]*)\s*=", stripped)
            if m and m.group(1) not in targets_set:
                # This program is not a target, delete this line
                continue
        out.append(ln)
    return "".join(out)


def _restore_pristine_config(workspace: Path) -> None:
    """Before building, restore root Cargo.toml + Anchor.toml to committed state,
    to avoid the narrowed version left by the last run interfering with this run's scope decision."""
    if not (workspace / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "-C", str(workspace), "checkout", "HEAD", "--", "Cargo.toml", "Anchor.toml"],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _apply_scope_narrowing(
    workspace: Path,
    audit_targets: list[str],
    build_members: list[str] | None = None,
) -> None:
    """Narrow root Cargo.toml + Anchor.toml by target program list.

    audit_targets: names of programs the user confirmed to audit/deploy, written into Anchor.toml [programs.localnet]
    build_members: minimal compile closure (workspace-relative directory paths), written into Cargo.toml [workspace.members].
                   Must contain each of audit_targets (as `programs/<name>`) + their path dependencies.
                   If None, falls back to only audit_targets (old behavior, build may fail on path dependencies).

    Idempotent: restore pristine first, then rewrite. No post-build restore — the workspace is in the cache dir,
    the next run will pristine + narrow again.
    """
    if not audit_targets:
        return
    if build_members is None:
        build_members = [f"programs/{t}" for t in audit_targets]

    _restore_pristine_config(workspace)

    cargo_p = workspace / "Cargo.toml"
    if cargo_p.exists():
        original = cargo_p.read_text(encoding="utf-8")
        narrowed = _rewrite_cargo_workspace_members(original, build_members)
        if narrowed != original:
            cargo_p.write_text(narrowed, encoding="utf-8")

    anchor_p = workspace / "Anchor.toml"
    if anchor_p.exists():
        original = anchor_p.read_text(encoding="utf-8")
        narrowed = _rewrite_anchor_localnet(original, audit_targets)
        if narrowed != original:
            anchor_p.write_text(narrowed, encoding="utf-8")


def _anchor_build(
    workspace: Path,
    on_line: Optional[Callable[[str], None]] = None,
    programs: Optional[list[str]] = None,
) -> tuple[bool, str, float]:
    """Run `anchor build` in workspace, streaming output. Return (ok, tail, seconds).

    programs=None → `anchor build` (build all programs in the workspace).
    programs=[...] → run `anchor build -p <name>` in sequence, abort on the first failure.

    Uses the absolute path from _anchor_bin() rather than the anchor on PATH — this way, even if the user's shell
    has anchor pointing elsewhere (e.g. another version installed by cargo), the build still uses the version-aligned one.
    """
    import collections
    targets: list[list[str]] = [["-p", n] for n in programs] if programs else [[]]
    total_dt = 0.0
    all_tail: list[str] = []

    for extra in targets:
        marker = f" -p {extra[1]}" if extra else ""
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [_anchor_bin(), "build", *extra],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        tail: "collections.deque[str]" = collections.deque(maxlen=200)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                tail.append(line)
                if on_line:
                    on_line(line)
            proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            dt = time.monotonic() - t0
            total_dt += dt
            all_tail.append(
                f"---- anchor build{marker} <TIMEOUT after {dt:.0f}s> ----\n" + "\n".join(tail)
            )
            return False, "\n\n".join(all_tail)[-2000:] + "\n\n<TIMEOUT>", total_dt

        dt = time.monotonic() - t0
        total_dt += dt
        all_tail.append(
            f"---- anchor build{marker} (rc={proc.returncode}, {dt:.0f}s) ----\n" + "\n".join(tail)
        )
        if proc.returncode != 0:
            return False, "\n\n".join(all_tail)[-2000:], total_dt

    return True, "\n\n".join(all_tail)[-2000:], total_dt


def _run_test(
    workspace: Path,
    test_file: Path,
    timeout_sec: int = 900,
    on_line: Optional[Callable[[str, float], None]] = None,
) -> tuple[int, str]:
    """Run ts-mocha on a single test file, streaming stdout. Return (exit_code, combined_output).

    `on_line(line, elapsed_seconds)` fires for each stdout/stderr line as it comes in.
    Default 15 min — bankrun setup + long attack chains can take a while, especially
    if the model generates many warpToSlot steps.
    """
    import collections
    cmd = [
        "yarn", "run", "ts-mocha",
        "-p", "tsconfig.json",
        "-t", str(timeout_sec * 1000),
        str(test_file.relative_to(workspace)),
    ]
    t0 = time.monotonic()
    # Environment variables:
    # - NODE_OPTIONS: Node 22.7+ defaults to built-in strip-types (ESM loader), which breaks CJS named imports. Turn it off and let ts-node take over
    # - SBF_OUT_DIR / BPF_OUT_DIR: search path where bankrun/solana-program-test looks for .so. By default it doesn't look at target/deploy/
    deploy_dir = workspace / "target" / "deploy"
    env = {
        **os.environ,
        "NODE_OPTIONS": "--no-experimental-strip-types",
        "SBF_OUT_DIR": str(deploy_dir),
        "BPF_OUT_DIR": str(deploy_dir),  # old variable name
    }
    proc = subprocess.Popen(
        cmd, cwd=str(workspace),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    # Save everything to return at the end; also stream line-by-line through on_line
    all_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            all_lines.append(line)
            if on_line:
                on_line(line, time.monotonic() - t0)
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return 124, "TIMEOUT after {}s\n{}".format(timeout_sec, "\n".join(all_lines))
    return proc.returncode, "\n".join(all_lines)


def _parse_verdict(output: str) -> str:
    """Classify test output into: VALID, INEFFECTIVE, SETUP_ERROR, TIMEOUT, UNKNOWN."""
    if output.startswith("TIMEOUT"):
        return "TIMEOUT"
    if "POC_VALID:" in output:
        return "VALID"
    if "POC_INEFFECTIVE:" in output:
        return "INEFFECTIVE"
    if "POC_SETUP_ERROR" in output:
        return "SETUP_ERROR"
    return "UNKNOWN"


# Compile precheck: run tsc --noEmit before ts-mocha, to separate compile errors from runtime errors.
# ts-mocha takes ~several minutes per run (bankrun startup + deploy), so discovering compile errors that late is wasteful.
# tsc --noEmit only does type checking, results in seconds; on failure let the executor fix it in the inner loop before entering the main flow.
COMPILE_FIX_MAX = 5


def _typecheck_ts(
    workspace: Path,
    test_file: Path,
    timeout_sec: int = 120,
) -> tuple[bool, str]:
    """Run tsc --noEmit to type-check the single test_file. Returns (ok, diagnostic).

    Approach: write a temporary tsconfig that extends the project's own tsconfig.json but narrows `include`
    to only this test_file, to avoid a slow whole-project typecheck. For the tsc bin, prefer
    node_modules/.bin/tsc, falling back to `yarn run tsc`.

    Key overrides:
      - skipLibCheck=true — the project tsconfig often doesn't set this, so tsc reports
        library type errors from `node_modules/@solana/**/*.d.ts` too (the libs carry newer
        TS syntax while the local tsc version is old). The executor can't fix those, it would just loop forever.
        We only care about the compile correctness of our own test file, so skip library files.
      - noEmit=true — a safety net, to prevent the extended project tsconfig from emitting dist.

    Also post-filters: if all remaining errors point to node_modules/, treat as ok
    (fallback for an old tsc where skipLibCheck didn't take effect). A failure in our test will always report a tests/... path.

    The paths tsc prints are workspace-relative. We feed them back to the executor verbatim.
    When the workspace has no tsconfig, treat it as "skip typecheck" and return (True, "").
    """
    project_tsconfig = workspace / "tsconfig.json"
    if not project_tsconfig.exists():
        return True, "(no tsconfig.json in workspace, skipping typecheck)"

    rel_test = test_file.relative_to(workspace)
    tmp_tsconfig = workspace / f".poc-typecheck-{test_file.stem}.json"
    tmp_tsconfig.write_text(
        json.dumps({
            "extends": "./tsconfig.json",
            "compilerOptions": {
                "skipLibCheck": True,
                "noEmit": True,
            },
            "include": [str(rel_test)],
        }, indent=2),
        encoding="utf-8",
    )

    tsc_local = workspace / "node_modules" / ".bin" / "tsc"
    if tsc_local.exists():
        cmd = [str(tsc_local), "--noEmit", "--skipLibCheck", "-p", tmp_tsconfig.name]
    else:
        # No local tsc, fall back to yarn — relies on yarn being able to find typescript from the workspace root
        cmd = ["yarn", "run", "-s", "tsc", "--noEmit", "--skipLibCheck", "-p", tmp_tsconfig.name]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env={**os.environ, "NODE_OPTIONS": "--no-experimental-strip-types"},
        )
    except subprocess.TimeoutExpired:
        _safe_bak(tmp_tsconfig)
        return False, f"<TSC TIMEOUT after {timeout_sec}s>"
    except FileNotFoundError as e:
        _safe_bak(tmp_tsconfig)
        return True, f"(tsc not runnable: {e}, skipping typecheck)"

    _safe_bak(tmp_tsconfig)

    # tsc prints diagnostics mainly to stdout, stderr only on a real crash
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode == 0:
        return True, output.strip() or "(tsc: 0 errors)"

    # Post-filter: keep only errors related to the test file (or our own files in the workspace).
    # tsc error line format: "<path>(<line>,<col>): error TSxxxx: ...". Errors from node_modules
    # library files are meaningless to the executor — it can't fix lib type definitions. skipLibCheck should
    # already have filtered these out, but an old tsc version or a tsconfig typeRoots config might bypass it, so this is a fallback.
    err_lines = output.strip().splitlines()
    own_errors = [ln for ln in err_lines if not ln.lstrip().startswith("node_modules/")]
    # Decide whether it's "an error genuinely in our own code": at least one line matches a test/src path + "error TS"
    has_own_error = any(("error TS" in ln) for ln in own_errors)
    if not has_own_error:
        # All are library type errors from node_modules/ (or empty) — treat as passing
        return True, output.strip() or f"(tsc exit {proc.returncode}, but only lib errors — treated ok)"

    # Has our own errors — feed back only the lines related to our own files, so lib noise doesn't distract
    filtered = "\n".join(own_errors).strip()
    return False, filtered or output.strip() or f"tsc exit {proc.returncode} (no output)"


def _safe_bak(p: Path) -> None:
    """Move the temporary tsconfig away once done (mv to .bak), don't rm — keep it for debugging."""
    if not p.exists():
        return
    try:
        p.rename(p.with_suffix(p.suffix + ".bak"))
    except OSError:
        pass


def _short(s: str, n: int = 80) -> str:
    return (s or "").replace("\n", " ")[:n]


def _error_summary(output: str, max_lines: int = 20) -> str:
    """Extract a compact tail of the most useful error info from mocha/ts output."""
    if not output:
        return "(no output)"
    lines = output.rstrip().splitlines()
    # Real error keywords ("at " is not included, because it appears on every line of a stack trace, which would misalign the window)
    error_kws = (
        "Error:", "AssertionError", "AnchorError", "TypeError",
        "ReferenceError", "SyntaxError", "SendTransactionError",
        "POC_SETUP_ERROR", "POC_INEFFECTIVE", "Uncaught",
        "Cannot find module", "failing", "custom program error",
    )
    # Start displaying from the first real error line — this way you see "Error: xxx" rather than just the stack tail
    first = next(
        (i for i, ln in enumerate(lines) if any(kw in ln for kw in error_kws)),
        None,
    )
    if first is not None:
        return "\n".join(lines[first : first + max_lines])
    return "\n".join(lines[-max_lines:])


async def _run_one_finding(
    client: LLMClient,
    finding: dict,
    source_blob: str,
    workspace: Path,
    workspace_info: dict,
) -> dict:
    """Return verdict dict: {status, spec, attempts: [...]}"""
    num = finding.get("number", "?")
    print(f"\n[poc] === Finding #{num} ===")
    print(f"[poc]   desc: {_short(finding.get('description', ''), 100)}")

    finding_prose = render_finding_for_prompt(finding)

    print(f"[poc] step 1/2: assertion agent…")
    try:
        spec = await make_verdict_spec(client, finding_prose, source_blob=source_blob)
    except Exception as e:
        print(f"[poc] ✗  assertion agent failed: {e}")
        return {"status": "ASSERTION_FAILED", "error": str(e), "attempts": []}
    print(f"[poc]   observations: {[o['name'] for o in spec['observations']]}")
    print(f"[poc]   predicate:    {spec['success_predicate']['condition_ts']}")

    attempts: list[dict] = []
    prev_error: str | None = None
    prev_test_ts: str | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[poc] step 2/2: executor attempt {attempt}/{MAX_ATTEMPTS}…", flush=True)
        t0 = time.monotonic()

        def _on_progress(chars: int):
            # cheap live counter (tokens ≈ chars/4)
            print(f"\r[poc]   generating… {chars//4} tok      ", end="", flush=True)

        try:
            test_ts = await generate_test(
                client, finding_prose, source_blob, spec, workspace_info,
                prev_error=prev_error, prev_test_ts=prev_test_ts, on_progress=_on_progress,
            )
        except ModelRefusalError as e:
            print(f"\n[poc] ✗  executor refused: {e}")
            print(f"[poc]   retrying with the same input is doomed to fail, skipping remaining attempts")
            attempts.append({"attempt": attempt, "verdict": "REFUSED", "error": str(e)})
            return {"status": "REFUSED", "spec": spec, "attempts": attempts}
        except Exception as e:
            print(f"\n[poc] ✗  executor agent errored: {e}")
            attempts.append({"attempt": attempt, "verdict": "EXECUTOR_ERROR", "error": str(e)})
            continue
        gen_dt = time.monotonic() - t0
        print(f"\n[poc]   generated {len(test_ts)} chars in {gen_dt:.0f}s")

        test_dir = workspace / "tests"
        test_dir.mkdir(exist_ok=True)
        test_path = test_dir / f"poc_f{num}_a{attempt}.test.ts"
        test_path.write_text(test_ts, encoding="utf-8")
        print(f"[poc]   wrote {test_path.relative_to(workspace)}")

        # ---- Compile precheck (compile-fix inner loop) ----
        # ts-mocha takes several minutes per run, so discovering compile errors that late is wasteful. First do a quick
        # tsc --noEmit type check; on failure let the executor fix it repeatedly, not counting against MAX_ATTEMPTS. Only
        # enter the main flow once compilation passes.
        compile_ok = False
        prev_compile_error: str | None = None
        for cf_i in range(1, COMPILE_FIX_MAX + 1):
            tc_t0 = time.monotonic()
            compile_ok, tsc_out = _typecheck_ts(workspace, test_path)
            tc_dt = time.monotonic() - tc_t0
            if compile_ok:
                if cf_i > 1:
                    print(f"[poc]   ✓ compilation passed (fixed after {cf_i-1} compile-fix rounds, {tc_dt:.1f}s)")
                else:
                    print(f"[poc]   ✓ compilation passed ({tc_dt:.1f}s)")
                break

            print(f"[poc]   ✗ tsc failed (compile-fix {cf_i}/{COMPILE_FIX_MAX}, {tc_dt:.1f}s)")
            # Print only the first 10 diagnostic lines to keep the screen clean; the full error is passed to the executor
            for line in tsc_out.splitlines()[:10]:
                print(f"[poc]     │ {line[:140]}")

            if cf_i >= COMPILE_FIX_MAX:
                break

            prev_compile_error = tsc_out
            try:
                test_ts = await generate_test(
                    client, finding_prose, source_blob, spec, workspace_info,
                    prev_error=prev_error, prev_test_ts=test_ts,
                    prev_compile_error=prev_compile_error,
                    on_progress=_on_progress,
                )
            except ModelRefusalError as e:
                print(f"\n[poc] ✗  executor refused during compile-fix: {e}")
                break
            except Exception as e:
                print(f"\n[poc] ✗  executor errored during compile-fix: {e}, continuing with current version")
                break
            print(f"\n[poc]   compile-fix regenerated {len(test_ts)} chars")
            test_path.write_text(test_ts, encoding="utf-8")

        if not compile_ok:
            # The compile-fix inner loop is exhausted and still doesn't pass — record one attempt failure, feed the tsc output
            # as prev_error into the next main attempt, so the executor rethinks the entire test.
            print(f"[poc]   ⏸ still not compiling after {COMPILE_FIX_MAX} compile-fix rounds, treating this attempt as failed")
            attempts.append({
                "attempt": attempt,
                "verdict": "COMPILE_FAILED",
                "exit_code": None,
                "test_file": str(test_path.relative_to(workspace)),
                "log_tail": (prev_compile_error or "")[-3000:],
            })
            prev_error = f"[TSC COMPILE ERROR — the previous version failed tsc --noEmit]\n{prev_compile_error or ''}"
            prev_test_ts = test_ts
            continue

        run_t0 = time.monotonic()

        # Print key lines as it runs — the user doesn't have to wait 5 minutes idle
        def _test_on_line(line: str, elapsed: float) -> None:
            s = line.lstrip()
            # Print all of our own tags
            if s.startswith("POC_"):
                print(f"[poc]   [{elapsed:5.1f}s] {s[:140]}")
            # ts-mocha compile errors, AnchorError, etc.
            elif any(kw in s for kw in ("Error:", "AnchorError", "TypeError", "ReferenceError",
                                          "SyntaxError", "Cannot find module", "custom program error")):
                print(f"[poc]   [{elapsed:5.1f}s] {s[:140]}")
            # mocha summary line
            elif "passing" in s or "failing" in s:
                print(f"[poc]   [{elapsed:5.1f}s] {s[:140]}")

        exit_code, output = _run_test(workspace, test_path, on_line=_test_on_line)
        run_dt = time.monotonic() - run_t0
        verdict = _parse_verdict(output)
        icon = {"VALID": "✓", "INEFFECTIVE": "○", "SETUP_ERROR": "✗", "TIMEOUT": "⏰"}.get(verdict, "?")
        print(f"[poc]   ran in {run_dt:.0f}s → exit {exit_code}, verdict {icon} {verdict}")

        attempts.append({
            "attempt": attempt,
            "verdict": verdict,
            "exit_code": exit_code,
            "test_file": str(test_path.relative_to(workspace)),
            "log_tail": output[-3000:],
        })

        if verdict == "VALID":
            print(f"[poc] ✓  Finding #{num}: VALID (attempt {attempt})")
            return {"status": "VALID", "spec": spec, "attempts": attempts}

        # Print the error summary immediately on failure, so the user doesn't have to wait for all 5 to finish
        summary = _error_summary(output)
        if summary:
            print(f"[poc]   ─── error summary (attempt {attempt}) ───")
            for line in summary.splitlines():
                print(f"[poc]   │ {line}")
            print(f"[poc]   ────────────────────────")

        prev_error = output
        prev_test_ts = test_ts

    # All 5 failed
    last = attempts[-1] if attempts else {}
    print(f"\n[poc] ⏸  Finding #{num}: WORTH_TO_CHECK ({MAX_ATTEMPTS} attempts failed)")
    print(f"[poc]   verdicts: {[a['verdict'] for a in attempts]}")
    print(f"[poc]   last tail at: {last.get('test_file', '?')}")
    print(f"[poc]   the full log will be written into the final report md")
    return {"status": "WORTH_TO_CHECK", "spec": spec, "attempts": attempts}


def _write_augmented_report(report_path: Path, verdicts: dict) -> Path:
    """Append PoC results section to a copy of the audit report."""
    original = report_path.read_text(encoding="utf-8")
    out_path = report_path.with_name(report_path.stem + "-with-poc.md")

    lines = ["\n---\n\n## PoC verification results\n"]
    for num, v in sorted(verdicts.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
        status = v["status"]
        icon = {"VALID": "✅", "WORTH_TO_CHECK": "⏸", "ASSERTION_FAILED": "❓"}.get(status, "?")
        if status == "REFUSED":
            icon = "🚫"
        lines.append(f"### Finding #{num} — {icon} {status}\n")
        if "spec" in v:
            lines.append("Verdict spec:\n")
            lines.append("```json")
            lines.append(json.dumps(v["spec"], indent=2, ensure_ascii=False))
            lines.append("```\n")
        for a in v.get("attempts", []):
            lines.append(f"- Attempt {a['attempt']}: **{a['verdict']}** (exit {a.get('exit_code', '?')}, `{a.get('test_file', '')}`)")
        if status == "VALID":
            good = [a for a in v["attempts"] if a["verdict"] == "VALID"]
            if good:
                lines.append("\n**Confirming log tail:**\n")
                lines.append("```")
                lines.append(good[0]["log_tail"][-1500:])
                lines.append("```")
        elif status == "WORTH_TO_CHECK":
            # In the failure case, give the tail of the last attempt, convenient for a human to look at
            with_log = [a for a in v.get("attempts", []) if a.get("log_tail")]
            if with_log:
                last = with_log[-1]
                lines.append(f"\n**Last attempt (attempt {last['attempt']}, {last['verdict']}) log tail:**\n")
                lines.append("```")
                lines.append(last["log_tail"][-2000:])
                lines.append("```")
        lines.append("")

    out_path.write_text(original + "\n".join(lines), encoding="utf-8")
    return out_path


def _emit(cb: Optional[StepCallback], step: str, status: str, detail: str) -> None:
    _log_step(step, status, detail)
    if cb is not None:
        cb(step, status, detail)


def check_tools(on_step: Optional[StepCallback] = None) -> list[str]:
    """Preflight tool check. Emits 'preflight' step.

    Returns the list of missing tools (empty if all present). Does NOT raise —
    the caller decides whether to invoke `_ensure_tools` (env_agent install loop)
    or hard-fail.
    """
    _emit(on_step, "preflight", "start", "checking anchor/solana/node/yarn/rustc/cargo…")
    # First splice known install directories into PATH — e.g. if the user installed rustup on a previous run of this tool,
    # this freshly opened shell that hasn't sourced ~/.cargo/env can still find it via the harness.
    _refresh_install_path()
    missing = _preflight()
    if missing:
        _emit(on_step, "preflight", "fail", f"missing: {', '.join(missing)}")
        return missing
    versions = _tool_versions()
    version_str = " · ".join(v for v in versions.values() if v) or "all present"
    _emit(on_step, "preflight", "ok", version_str)
    return []


import re as _re


# ---- Provision (rust / solana / anchor) target detection ----

# Anchor version → known compatible solana + rustc versions (used only when the project doesn't declare them explicitly).
# Picks a "well-known stable" version each time, rather than the most aggressive.
_ANCHOR_TO_SOLANA_FALLBACK = {
    "0.26": "1.14.29", "0.27": "1.14.29", "0.28": "1.16.27",
    "0.29": "1.17.34", "0.30": "1.18.26", "0.31": "2.0.21",
    "0.32": "2.1.11",
}
_ANCHOR_TO_RUST_FALLBACK = {
    "0.26": "1.62.0",  "0.27": "1.62.0",  "0.28": "1.65.0",
    "0.29": "1.72.1",  "0.30": "1.75.0",  "0.31": "1.79.0",
    "0.32": "1.82.0",
}


# ---- Anchor version auto-switching ----

_ANCHOR_TOOLCHAIN_RE = _re.compile(
    r"\[toolchain\][^\[]*?anchor_version\s*=\s*\"([^\"]+)\"", _re.DOTALL,
)
# `[toolchain] solana_version = "..."` — supported by anchor 0.29+
_ANCHOR_SOLANA_RE = _re.compile(
    r"\[toolchain\][^\[]*?solana_version\s*=\s*\"([^\"]+)\"", _re.DOTALL,
)
# Dependency line: `anchor-lang = "x.y.z"`  or  `anchor-lang = { version = "x.y.z", ... }`
_ANCHOR_LANG_LITERAL_RE = _re.compile(r'anchor-lang\s*=\s*"([^"]+)"')
_ANCHOR_LANG_TABLE_RE = _re.compile(r'anchor-lang\s*=\s*\{[^}]*?version\s*=\s*"([^"]+)"')
# rust-toolchain.toml: `[toolchain] channel = "..."`
_RUST_TOOLCHAIN_CHANNEL_RE = _re.compile(
    r"\[toolchain\][^\[]*?channel\s*=\s*\"([^\"]+)\"", _re.DOTALL,
)


def _parse_anchor_version_from_toml(p: Path) -> str | None:
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    m = _ANCHOR_TOOLCHAIN_RE.search(text)
    return m.group(1) if m else None


def _parse_anchor_lang_from_cargo(p: Path) -> str | None:
    """Return normalized X.Y.Z from anchor-lang dep, or None. Handles:
      anchor-lang = "0.29.0"
      anchor-lang = "^0.29.0"      → 0.29.0
      anchor-lang = "0.29"          → 0.29.0
      anchor-lang = { version = "0.29.0", features = [...] }
    """
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    m = _ANCHOR_LANG_LITERAL_RE.search(text) or _ANCHOR_LANG_TABLE_RE.search(text)
    if not m:
        return None
    return _normalize_semver(m.group(1))


def _normalize_semver(raw: str) -> str | None:
    """`^0.29.0` → `0.29.0`, `0.29` → `0.29.0`, `>=0.29,<0.30` → `0.29.0` (first X.Y.Z found)."""
    if not raw:
        return None
    m = _re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if not m:
        return None
    major, minor, patch = m.group(1), m.group(2), m.group(3) or "0"
    return f"{major}.{minor}.{patch}"


def _workspace_excluded_paths(source_root: Path) -> set[Path]:
    """Read root Cargo.toml [workspace] exclude/members, return absolute paths to skip in fallback scan.

    Best-effort — just used by the case 3 fallback to avoid digging out the version of an excluded package like jup-sdk.
    """
    root_cargo = source_root / "Cargo.toml"
    if not root_cargo.exists():
        return set()
    try:
        text = root_cargo.read_text(encoding="utf-8")
    except Exception:
        return set()
    m = _re.search(r"\[workspace\]([\s\S]*?)(?:\n\[|$)", text)
    if not m:
        return set()
    section = m.group(1)
    excl = _re.search(r"exclude\s*=\s*\[([^\]]*)\]", section)
    if not excl:
        return set()
    paths: set[Path] = set()
    for item in _re.findall(r'"([^"]+)"', excl.group(1)):
        paths.add((source_root / item).resolve())
    return paths


def _detect_required_anchor_version(
    source_root: Path,
    build_roots: list[Path],
) -> tuple[str | None, dict[Path, str]]:
    """Detect the anchor version each build_root needs, by priority.

    Priority (computed independently per build_root):
      1. build_root/Anchor.toml [toolchain] anchor_version
      2. source_root/Anchor.toml [toolchain] anchor_version  (monorepo top level)
      3. anchor-lang dependency version in build_root/programs/*/Cargo.toml

    Returns (unified_version, per_root): if all build_roots detect the same version,
    unified_version is that version; if inconsistent, returns None and per_root contains each one's version
    (the caller decides whether to error out or fall back).
    """
    per_root: dict[Path, str] = {}
    root_toml_ver = _parse_anchor_version_from_toml(source_root / "Anchor.toml")
    excluded = _workspace_excluded_paths(source_root)
    for br in build_roots:
        v = _parse_anchor_version_from_toml(br / "Anchor.toml")
        if not v:
            v = root_toml_ver
        if not v:
            # Case 1: br is a monorepo/anchor workspace root, has programs/*/Cargo.toml
            programs_dir = br / "programs"
            if programs_dir.is_dir():
                for cargo in programs_dir.rglob("Cargo.toml"):
                    if cargo.resolve() in excluded or any(p in excluded for p in cargo.resolve().parents):
                        continue
                    v = _parse_anchor_lang_from_cargo(cargo)
                    if v:
                        break
        if not v:
            # Case 2: br itself is a program directory, read br/Cargo.toml directly
            v = _parse_anchor_lang_from_cargo(br / "Cargo.toml")
        if not v:
            # Case 3: fallback — scan for any anchor-lang dependency under source_root (skip workspace exclude)
            for cargo in source_root.rglob("Cargo.toml"):
                rel = cargo.relative_to(source_root)
                if any(seg in ("node_modules", "target", ".anchor") for seg in rel.parts):
                    continue
                if cargo.resolve() in excluded or any(p in excluded for p in cargo.resolve().parents):
                    continue
                v = _parse_anchor_lang_from_cargo(cargo)
                if v:
                    break
        if v:
            per_root[br] = v

    if not per_root:
        return None, {}
    versions = set(per_root.values())
    if len(versions) == 1:
        return versions.pop(), per_root
    return None, per_root


# ---- solana / rust target detection (used by provisioner) ----

def _parse_solana_version_from_toml(p: Path) -> str | None:
    """Extract solana_version from the Anchor.toml [toolchain] section. Supported by anchor 0.29+."""
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    m = _ANCHOR_SOLANA_RE.search(text)
    return _normalize_semver(m.group(1)) if m else None


def _parse_rust_channel(search_dirs: list[Path]) -> str | None:
    """Look for rust-toolchain.toml / rust-toolchain in order, return the channel string.

    Supports:
      - rust-toolchain.toml: `[toolchain] channel = "1.79.0"` (modern format)
      - rust-toolchain     : single-line version string `1.79.0` (old format)
    Non-semver channels (stable/nightly/beta) always return None — don't install nightly.
    """
    for d in search_dirs:
        toml_path = d / "rust-toolchain.toml"
        if toml_path.exists():
            try:
                text = toml_path.read_text(encoding="utf-8")
            except Exception:
                continue
            m = _RUST_TOOLCHAIN_CHANNEL_RE.search(text)
            if m:
                v = _normalize_semver(m.group(1))
                if v:
                    return v
        legacy = d / "rust-toolchain"
        if legacy.exists():
            try:
                text = legacy.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            v = _normalize_semver(text)
            if v:
                return v
    return None


def _anchor_major_minor(v: str) -> str | None:
    """`0.31.1` → `"0.31"`. Used for looking up the fallback table."""
    if not v:
        return None
    m = _re.match(r"^(\d+\.\d+)", v)
    return m.group(1) if m else None


def _detect_project_targets(
    build_roots: list[Path],
    source_root: Path,
) -> dict[str, str | None]:
    """Read project metadata, return target toolchain versions.

    Returns a dict with three keys: anchor / solana / rust. Each value is a semver string or None.
    None means "the project doesn't declare it and it can't be inferred"; the provisioner skips that step.

    Priority (high to low):
      solana:
        1. Any build_root's Anchor.toml [toolchain].solana_version
        2. source_root's Anchor.toml [toolchain].solana_version
        3. Look up _ANCHOR_TO_SOLANA_FALLBACK by anchor version
      rust:
        1. Any build_root's rust-toolchain(.toml)
        2. source_root's rust-toolchain(.toml)
        3. Look up _ANCHOR_TO_RUST_FALLBACK by anchor version
      anchor: reuses the _detect_required_anchor_version logic
    """
    anchor_ver, _per_root = _detect_required_anchor_version(source_root, build_roots)

    # solana: build_roots first, then source_root
    solana_ver: str | None = None
    for br in build_roots:
        solana_ver = _parse_solana_version_from_toml(br / "Anchor.toml")
        if solana_ver:
            break
    if not solana_ver:
        solana_ver = _parse_solana_version_from_toml(source_root / "Anchor.toml")
    if not solana_ver and anchor_ver:
        mm = _anchor_major_minor(anchor_ver)
        if mm:
            solana_ver = _ANCHOR_TO_SOLANA_FALLBACK.get(mm)

    # rust: build_roots + source_root
    rust_ver = _parse_rust_channel(list(build_roots) + [source_root])
    if not rust_ver and anchor_ver:
        mm = _anchor_major_minor(anchor_ver)
        if mm:
            rust_ver = _ANCHOR_TO_RUST_FALLBACK.get(mm)

    return {"anchor": anchor_ver, "solana": solana_ver, "rust": rust_ver}


_ANCHOR_VER_LINE_RE = _re.compile(r"(\d+\.\d+\.\d+)")


def _avm_installed_versions() -> set[str]:
    """List the anchor versions avm has actually installed — look directly at ~/.avm/bin/anchor-<ver> files.

    More reliable than grepping `avm list` output: avm list lists all available versions (whether installed or not),
    only those marked `(installed, current)` are truly installed.
    """
    avm_bin = Path.home() / ".avm" / "bin"
    if not avm_bin.exists():
        return set()
    versions: set[str] = set()
    for f in avm_bin.iterdir():
        name = f.name
        if not name.startswith("anchor-"):
            continue
        v = name[len("anchor-"):]
        if _re.match(r"^\d+\.\d+\.\d+$", v):
            versions.add(v)
    return versions


def _anchor_bin_for(version: str) -> Path:
    """Path to the anchor-<version> binary installed by avm."""
    return Path.home() / ".avm" / "bin" / f"anchor-{version}"


def _avm_bin() -> Path | None:
    """Path to the avm binary itself (not the anchor shim)."""
    p = Path.home() / ".avm" / "bin" / "avm"
    if p.exists():
        return p
    # fallback: avm on PATH
    which = shutil.which("avm")
    return Path(which) if which else None


# Global state: once _ensure_anchor_version succeeds, remember which anchor binary to use.
# Subsequent subprocesses like _anchor_build call this absolute path directly, bypassing the cargo version & avm shim on PATH.
_ACTIVE_ANCHOR_BIN: Path | None = None


def _anchor_bin() -> str:
    """Which anchor binary to use currently. Uses the one on PATH when not pinned."""
    global _ACTIVE_ANCHOR_BIN
    if _ACTIVE_ANCHOR_BIN is not None and _ACTIVE_ANCHOR_BIN.exists():
        return str(_ACTIVE_ANCHOR_BIN)
    return "anchor"


def _current_anchor_version() -> str | None:
    """The anchor version that will be used to build. If _ACTIVE_ANCHOR_BIN is set, use it; otherwise ask the one on PATH."""
    bin_ = _anchor_bin()
    try:
        r = subprocess.run(
            [bin_, "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    m = _ANCHOR_VER_LINE_RE.search(r.stdout + r.stderr)
    return m.group(1) if m else None


def _anchor_shadowing_hint() -> str | None:
    """For logging only: the path `anchor` points to in the user's shell, for debugging."""
    p = shutil.which("anchor")
    if not p:
        return None
    return f"the `anchor` in your shell is {p} — this tool's build isn't affected by it (uses the absolute path of the avm-installed one), just an FYI"


async def _ensure_tools(
    missing: list[str],
    client: LLMClient,
    on_step: Optional[StepCallback] = None,
    max_iters: int = 3,
) -> None:
    """LLM loop to install missing base tools (anchor/avm/solana/node/yarn/rustc/cargo).

    Each round: gather_env_context → propose_env_fix → _apply_env_plan → _refresh_install_path
    → rerun _preflight. Empty missing = success. Exhausted or give_up → raise.

    Difference from _ensure_anchor_version: this path doesn't run retry_cmd (the install actions are all in commands),
    success/failure is judged by the _preflight result.
    """
    from poc.env_agent import gather_env_context, propose_env_fix

    _emit(on_step, "preflight", "tick", f"env_agent taking over (missing: {', '.join(missing)})")

    prev_diagnoses: list[str] = []
    last_op = f"preflight: missing {', '.join(missing)}"
    last_out = "(no failure output — this is a preflight-driven install, not a failed command)"
    current_missing = list(missing)

    for it in range(1, max_iters + 1):
        _emit(on_step, "preflight", "tick", f"↳ env_agent {it}/{max_iters}: analyzing what's missing…")
        ctx = gather_env_context(last_op, last_out, prev_diagnoses, missing_tools=current_missing)
        try:
            plan = await propose_env_fix(client, ctx)
        except Exception as e:
            _LOGGER.error(f"env_agent (tools) iter {it} exception: {e!r}")
            _emit(on_step, "preflight", "fail", f"env_agent errored: {e}")
            raise RuntimeError(f"env_agent call failed: {e}") from e
        _LOGGER.info(f"env_agent (tools) iter {it} plan: {json.dumps(plan, ensure_ascii=False)[:2000]}")
        prev_diagnoses.append(plan["diagnosis"][:200])
        _emit(on_step, "preflight", "tick", f"↳ env_agent {it}: {plan['diagnosis'][:120]}")

        if plan.get("give_up"):
            _emit(on_step, "preflight", "fail", f"env_agent give up: {plan['diagnosis'][:200]}")
            raise RuntimeError(f"env_agent gave up fixing missing tools: {plan['diagnosis']}")

        def _tick_line(line: str, _it=it):
            s = line.lstrip()
            if (
                s.startswith("error")
                or s.startswith("Compiling")
                or s.startswith("Downloading")
                or s.startswith("Updating")
                or s.startswith("info:")
                or "installing" in s.lower()
                or "failed" in s.lower()
            ):
                _emit(on_step, "preflight", "tick", f"↳ env_agent {_it}: {s[:100]}")

        ok, apply_msgs = _apply_env_plan(plan, on_line=_tick_line)
        for m in apply_msgs:
            _emit(on_step, "preflight", "tick", f"↳ env_agent {it}: {m}")

        # Regardless of whether apply succeeded, refresh PATH once and recheck preflight — some commands may have only partly succeeded
        added = _refresh_install_path()
        if added:
            _emit(on_step, "preflight", "tick", f"↳ env_agent {it}: PATH += {', '.join(added)}")

        current_missing = _preflight()
        if not current_missing:
            _emit(on_step, "preflight", "ok", "all tools present (via env_agent)")
            return

        if not ok:
            last_out = (
                f"[env_agent apply failed]\n{chr(10).join(apply_msgs)}\n"
                f"\n[preflight still missing]: {', '.join(current_missing)}"
            )
        else:
            last_out = (
                f"[apply ok, but preflight still missing]: {', '.join(current_missing)}\n"
                f"[apply log]:\n{chr(10).join(apply_msgs)[-1500:]}"
            )
        last_op = f"preflight: missing {', '.join(current_missing)}"

    _emit(on_step, "preflight", "fail", f"still missing after {max_iters} rounds: {', '.join(current_missing)}")
    raise RuntimeError(
        f"env_agent failed to install all tools in {max_iters} rounds. Still missing: {', '.join(current_missing)}\n"
        f"  last diagnosis: {prev_diagnoses[-1] if prev_diagnoses else '(none)'}"
    )


async def _ensure_anchor_version(
    required: str,
    on_step: Optional[StepCallback] = None,
    client: Optional[LLMClient] = None,
    max_env_agent_iters: int = 3,
) -> None:
    """Ensure an avm-installed anchor-<required> exists, record it to _ACTIVE_ANCHOR_BIN for subsequent subprocesses to use directly.

    Strategy:
      1. Already installed → use directly (record _ACTIVE_ANCHOR_BIN)
      2. Not installed → call avm to install, then same as above on success
      3. Install failed and a client was provided → enter env_agent loop (up to max_env_agent_iters rounds),
         each round the LLM proposes env vars / commands / cargo config edits, apply then retry
      4. Loop exhausted or agent give_up → raise, upper layer aborts the whole run

    Avoids two pitfalls:
      - PATH shadowing: use the ~/.avm/bin/anchor-<ver> absolute path directly, not PATH
      - avm shim hang: don't go through the ~/.avm/bin/anchor shim, run ~/.avm/bin/anchor-<ver> directly
    """
    global _ACTIVE_ANCHOR_BIN

    _emit(on_step, "anchor_ver", "start", f"requiring {required}…")

    installed = _avm_installed_versions()
    target_bin = _anchor_bin_for(required)

    if required in installed and target_bin.exists():
        _ACTIVE_ANCHOR_BIN = target_bin
        v = _current_anchor_version()
        if v == required:
            _emit(on_step, "anchor_ver", "ok", f"avm already has {required} · {target_bin}")
            return
        _emit(on_step, "anchor_ver", "fail", f"{target_bin} reports {v}, not {required}")
        raise RuntimeError(f"{target_bin} exists but --version reports {v}, may be broken. Try `avm uninstall {required} && avm install {required}`")

    avm = _avm_bin()
    if avm is None:
        _emit(on_step, "anchor_ver", "fail", "avm not present, can't install anchor")
        raise RuntimeError("anchor {} required but avm is not installed. Install with: `cargo install --git https://github.com/coral-xyz/anchor avm --locked`".format(required))

    # First directly run avm install
    install_cmd = f"{avm} install {required}"
    rc, out = await _try_avm_install(required, avm, on_step)
    if rc == 0 and target_bin.exists():
        _finalize_anchor_pin(required, target_bin, on_step)
        return

    # Failed → go to env_agent (needs client)
    if client is None:
        _emit(on_step, "anchor_ver", "fail", f"avm install {required} exit {rc}, no LLM client available for env_agent")
        raise RuntimeError(
            f"avm install {required} failed:\n  {_diagnose_avm_install_failure(out)}\n"
            f"trailing output:\n{out[-800:]}"
        )

    from poc.env_agent import gather_env_context, propose_env_fix

    prev_diagnoses: list[str] = []
    last_out = out
    last_op = f"avm install {required}"
    for it in range(1, max_env_agent_iters + 1):
        _emit(on_step, "anchor_ver", "tick", f"↳ env_agent {it}/{max_env_agent_iters}: analyzing failure…")
        ctx = gather_env_context(last_op, last_out, prev_diagnoses)
        try:
            plan = await propose_env_fix(client, ctx)
        except Exception as e:
            _LOGGER.error(f"env_agent (anchor) iter {it} exception: {e!r}")
            _emit(on_step, "anchor_ver", "fail", f"env_agent errored: {e}")
            raise RuntimeError(f"env_agent call failed: {e}") from e
        _LOGGER.info(f"env_agent (anchor) iter {it} plan: {json.dumps(plan, ensure_ascii=False)[:2000]}")
        prev_diagnoses.append(plan["diagnosis"][:200])
        _emit(on_step, "anchor_ver", "tick", f"↳ env_agent {it}: {plan['diagnosis'][:120]}")

        if plan.get("give_up"):
            _emit(on_step, "anchor_ver", "fail", f"env_agent give up: {plan['diagnosis'][:200]}")
            raise RuntimeError(f"env_agent gave up fixing: {plan['diagnosis']}")

        # apply env/commands/edits; if any step is invalid or a subcommand fails, feed the msg back into the next round
        def _tick_line(line: str, _it=it):
            s = line.lstrip()
            if s.startswith("error") or s.startswith("Compiling") or s.startswith("Downloading") or s.startswith("Updating") or "failed" in s.lower():
                _emit(on_step, "anchor_ver", "tick", f"↳ env_agent {_it}: {s[:100]}")

        ok, apply_msgs = _apply_env_plan(plan, on_line=_tick_line)
        for m in apply_msgs:
            _emit(on_step, "anchor_ver", "tick", f"↳ env_agent {it}: {m}")
        if not ok:
            last_out = f"[env_agent apply failed]\n{chr(10).join(apply_msgs)}\n\noriginal failure:\n{last_out[-1500:]}"
            continue

        # retry
        retry_cmd = plan.get("retry_cmd", last_op).strip()
        _emit(on_step, "anchor_ver", "tick", f"↳ env_agent {it}: retry `{retry_cmd}`…")

        # retry_cmd also goes through the whitelist — the agent may try `avm install` or `rustup toolchain install`
        if not _env_cmd_allowed(retry_cmd):
            _emit(on_step, "anchor_ver", "fail", f"retry_cmd not in whitelist: {retry_cmd}")
            raise RuntimeError(f"env_agent's retry_cmd rejected: {retry_cmd}")

        rc, out = _run_env_cmd(retry_cmd, on_line=_tick_line)
        last_op = retry_cmd
        last_out = out
        if rc == 0 and target_bin.exists():
            _finalize_anchor_pin(required, target_bin, on_step)
            return

    # Loop exhausted
    _emit(on_step, "anchor_ver", "fail", f"still failing after {max_env_agent_iters} env_agent rounds")
    raise RuntimeError(
        f"env_agent failed to fix avm install {required} in {max_env_agent_iters} rounds:\n"
        f"  last diagnosis: {prev_diagnoses[-1] if prev_diagnoses else '(none)'}\n"
        f"  last failure output:\n{last_out[-800:]}"
    )


async def _try_avm_install(
    required: str,
    avm: Path,
    on_step: Optional[StepCallback],
) -> tuple[int, str]:
    """Run avm install <ver> once, streaming key lines to on_step, return (rc, output)."""
    _emit(on_step, "anchor_ver", "tick", f"avm install {required}… (the first time may take 10+ minutes)")
    n_compiled = [0]

    def _forward(line: str) -> None:
        s = line.lstrip()
        if s.startswith("Compiling "):
            n_compiled[0] += 1
            parts = s.split()
            what = " ".join(parts[1:3]) if len(parts) >= 3 else parts[-1]
            _emit(on_step, "anchor_ver", "tick", f"avm install: Compiling {what} (#{n_compiled[0]})")
        elif s.startswith("Downloading ") or s.startswith("Updating "):
            _emit(on_step, "anchor_ver", "tick", f"avm install: {s[:60]}")
        elif s.startswith("error") or "failed" in s.lower():
            _emit(on_step, "anchor_ver", "tick", f"avm install: {s[:100]}")

    return _run_env_cmd(f"{avm} install {required}", on_line=_forward)


def _finalize_anchor_pin(
    required: str,
    target_bin: Path,
    on_step: Optional[StepCallback],
) -> None:
    """After avm install / env_agent succeeds: pin _ACTIVE_ANCHOR_BIN and verify the version matches."""
    global _ACTIVE_ANCHOR_BIN
    if not target_bin.exists():
        _emit(on_step, "anchor_ver", "fail", f"install reported success but {target_bin} wasn't created")
        raise RuntimeError(f"{target_bin} not found after install")
    _ACTIVE_ANCHOR_BIN = target_bin
    v = _current_anchor_version()
    if v != required:
        _emit(on_step, "anchor_ver", "fail", f"after install {target_bin} reports {v}, not {required}")
        raise RuntimeError(f"{target_bin} --version returned {v}, expected {required}")
    _emit(on_step, "anchor_ver", "ok", f"installed and selected {required} · {target_bin}")


def _diagnose_avm_install_failure(output: str) -> str:
    """Extract common root causes from avm install failure output, give one actionable hint."""
    o = output.lower()
    if "static.rust-lang.org" in o and ("tls" in o or "handshake" in o or "connect" in o):
        return (
            "Can't reach static.rust-lang.org (rustup CDN is blocked). "
            "Fix: switch network (phone hotspot); or set a rust mirror and rerun this tool:\n"
            "    export RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup\n"
            "    export RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup"
        )
    if "rustc" in o and ("1.79" in o or "1.7" in o):
        return "avm needs an old rustc to build this anchor version, and rustup can't download it (usually static.rust-lang.org is unreachable)."
    if "could not download" in o or "connection refused" in o or "timed out" in o:
        return "Download failed, possibly a network/proxy issue. Check the https_proxy / http_proxy environment variables."
    if "permission denied" in o or "read-only" in o:
        return "Permission issue with the ~/.avm or ~/.cargo directory. `ls -ld ~/.avm ~/.cargo`."
    if "disk" in o or "no space" in o:
        return "Not enough disk space."
    return "See the raw output below."


# ==== Provisioner: rust / solana / platform-tools alignment ====
# These are all pinned commands called directly by the harness (not via the LLM), so they don't go through the whitelist;
# URLs / version numbers are read from project metadata, sanitized by `_normalize_semver`.

_ACTIVE_SOLANA_BIN: Path | None = None


def _solana_bin() -> str:
    """Which solana binary to use currently. Uses the absolute path if pinned, otherwise PATH."""
    global _ACTIVE_SOLANA_BIN
    if _ACTIVE_SOLANA_BIN is not None and _ACTIVE_SOLANA_BIN.exists():
        return str(_ACTIVE_SOLANA_BIN)
    return "solana"


def _current_solana_version() -> str | None:
    try:
        r = subprocess.run(
            [_solana_bin(), "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    m = _ANCHOR_VER_LINE_RE.search(r.stdout + r.stderr)
    return m.group(1) if m else None


def _rustup_installed_toolchains() -> set[str]:
    """The X.Y.Z versions shown as installed in `rustup toolchain list` (host suffix stripped)."""
    if not shutil.which("rustup"):
        return set()
    try:
        r = subprocess.run(
            ["rustup", "toolchain", "list"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()
    out: set[str] = set()
    for line in (r.stdout + r.stderr).splitlines():
        m = _re.match(r"^(\d+\.\d+\.\d+)", line.strip())
        if m:
            out.add(m.group(1))
    return out


async def _ensure_rust_toolchain(
    target: str,
    on_step: Optional[StepCallback] = None,
) -> None:
    """Ensure rustup has the target version of rustc + rust-src component. Doesn't switch default.

    The rust-toolchain.toml in the workspace is auto-selected (if the project declares one), or the user's existing
    default is kept. Doesn't touch the rustup default, to avoid affecting the user's other projects.
    """
    _emit(on_step, "rust_ver", "start", f"requiring rustc {target}…")
    if not shutil.which("rustup"):
        _emit(on_step, "rust_ver", "fail", "rustup not installed")
        raise RuntimeError("rustup not installed, but the project requires rustc {}".format(target))

    installed = _rustup_installed_toolchains()

    def _tick(line: str) -> None:
        s = line.lstrip()
        if s.startswith("info:") or s.startswith("error") or "installing" in s.lower():
            _emit(on_step, "rust_ver", "tick", s[:100])

    if target not in installed:
        _emit(on_step, "rust_ver", "tick", f"rustup toolchain install {target} (may take 3-8 minutes)…")
        rc, out = _run_env_cmd(
            f"rustup toolchain install {target} --profile minimal",
            on_line=_tick,
        )
        if rc != 0:
            _emit(on_step, "rust_ver", "fail", f"rustup install {target} exit {rc}")
            raise RuntimeError(f"rustup toolchain install {target} failed:\n{out[-800:]}")

    # rust-src is needed for SBF builds (cargo-build-sbf --sbf-out-dir depends on it)
    _emit(on_step, "rust_ver", "tick", f"rustup component add rust-src --toolchain {target}…")
    rc, _out = _run_env_cmd(
        f"rustup component add rust-src --toolchain {target}",
        on_line=_tick,
    )
    if rc != 0:
        _emit(on_step, "rust_ver", "tick", "adding rust-src failed (may not be needed, continuing)")

    _emit(on_step, "rust_ver", "ok", f"rustc {target} installed · rust-src ✓")


def _platform_tools_symlink_path(solana_ver: str) -> Path:
    return (
        Path.home() / ".local" / "share" / "solana" / "install" / "releases"
        / solana_ver / "solana-release" / "bin" / "platform-tools-sdk"
        / "sbf" / "dependencies" / "platform-tools"
    )


def _find_healthy_platform_tools_cache() -> Path | None:
    """Scan ~/.cache/solana/v*/platform-tools/ for a complete directory (containing rust/lib and non-empty).

    Returns the one with the newest mtime, or None. Used for symlink broken-link self-repair.
    """
    cache_root = Path.home() / ".cache" / "solana"
    if not cache_root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for vdir in cache_root.glob("v*/platform-tools"):
        if not vdir.is_dir():
            continue
        rust_lib = vdir / "rust" / "lib"
        if not rust_lib.is_dir():
            continue
        try:
            has_content = any(rust_lib.iterdir())
        except OSError:
            continue
        if not has_content:
            continue
        try:
            mtime = vdir.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, vdir))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _platform_tools_healthy(solana_ver: str) -> bool:
    """symlink exists & target exists & target/rust/lib is a directory and non-empty."""
    link = _platform_tools_symlink_path(solana_ver)
    if not link.exists():
        return False
    rust_lib = link / "rust" / "lib"
    if not rust_lib.is_dir():
        return False
    try:
        return any(rust_lib.iterdir())
    except OSError:
        return False


def _read_platform_tools_version(solana_ver: str) -> Optional[str]:
    """Read the platform-tools version number from the installed solana-release (both install-init / direct tarball download carry it)."""
    release_dir = (
        Path.home() / ".local" / "share" / "solana" / "install"
        / "releases" / solana_ver / "solana-release"
    )
    candidates = [
        release_dir / "bin" / "platform-tools-sdk" / "sbf" / "scripts" / "platform-tools-version.txt",
        release_dir / "bin" / "sdk" / "sbf" / "scripts" / "platform-tools-version.txt",
        release_dir / "bin" / "sdk" / "bpf" / "scripts" / "platform-tools-version.txt",
    ]
    for f in candidates:
        try:
            if f.is_file():
                v = f.read_text().strip()
                if v:
                    return v.lstrip("v")
        except OSError:
            continue
    return None


def _install_platform_tools_from_tarball(
    solana_ver: str,
    on_step: Optional[StepCallback],
) -> bool:
    """3rd-layer fallback: directly download the platform-tools tarball to ~/.cache/solana/v<pt_ver>/platform-tools/.

    Returns True on success (the caller rechecks healthy). Returns False on failure to let the caller raise.
    """
    pt_ver = _read_platform_tools_version(solana_ver)
    if not pt_ver:
        _emit(on_step, "platform_tools", "tick", "can't read platform-tools version number (the <release>/bin/sdk structure may have changed), skipping direct-download fallback")
        return False

    try:
        triple = _detect_solana_target_triple()
    except RuntimeError as e:
        _emit(on_step, "platform_tools", "tick", f"platform detection failed: {e}")
        return False

    cache_ver_dir = Path.home() / ".cache" / "solana" / f"v{pt_ver}"
    tools_dir = cache_ver_dir / "platform-tools"
    tarball = cache_ver_dir / f"platform-tools-{triple}.tar.bz2"

    if not (tarball.is_file() and tarball.stat().st_size > 1_000_000):
        gh_path = f"anza-xyz/platform-tools/releases/download/v{pt_ver}/platform-tools-{triple}.tar.bz2"
        urls = _github_release_urls(gh_path)
        legacy = f"solana-labs/platform-tools/releases/download/v{pt_ver}/platform-tools-{triple}.tar.bz2"
        urls.extend(_github_release_urls(legacy))
        _emit(on_step, "platform_tools", "tick", f"direct-downloading platform-tools v{pt_ver} ({triple})…")
        try:
            _download_with_mirrors(
                urls, tarball, on_step, "platform_tools",
                f"platform-tools v{pt_ver}",
                per_url_no_output_timeout=30,
            )
        except RuntimeError as e:
            _emit(on_step, "platform_tools", "tick", f"direct download failed: {str(e)[:200]}")
            return False

    # Back up the existing directory (no rm), extract the new one
    if tools_dir.exists() or tools_dir.is_symlink():
        ts = int(time.time())
        rc, out = _run_env_cmd(f'mv "{tools_dir}" "{tools_dir}.bak.{ts}"')
        if rc != 0:
            _emit(on_step, "platform_tools", "tick", f"backing up old platform-tools failed rc={rc}, aborting")
            return False
    tools_dir.mkdir(parents=True, exist_ok=True)
    _emit(on_step, "platform_tools", "tick", f"extracting to {tools_dir}")
    rc, out = _run_env_cmd(
        f'tar -xjf "{tarball}" -C "{tools_dir}"',
        no_output_timeout=120,
        wall_timeout=600,
    )
    if rc != 0:
        _emit(on_step, "platform_tools", "tick", f"tar extraction rc={rc}: {out[-200:]}")
        return False

    # symlink to the location the release expects
    link = _platform_tools_symlink_path(solana_ver)
    link.parent.mkdir(parents=True, exist_ok=True)
    rc, out = _run_env_cmd(f'ln -sfn "{tools_dir}" "{link}"')
    if rc != 0:
        _emit(on_step, "platform_tools", "tick", f"ln -sfn rc={rc}: {out[-200:]}")
        return False

    return _platform_tools_healthy(solana_ver)


def _verify_and_repair_platform_tools(
    solana_ver: str,
    on_step: Optional[StepCallback] = None,
) -> None:
    """Verify platform-tools is complete, self-repair a broken link, prewarm with --force-tools-install if missing.

    A raise means even all the above failed, and the whole provision should abort.
    """
    _emit(on_step, "platform_tools", "start", f"checking platform-tools for solana {solana_ver}…")

    if _platform_tools_healthy(solana_ver):
        _emit(on_step, "platform_tools", "ok", "already complete")
        return

    link = _platform_tools_symlink_path(solana_ver)
    parent = link.parent

    # 1. symlink broken → scan cache for a complete directory, fix with ln -sfn
    healthy_cache = _find_healthy_platform_tools_cache()
    if healthy_cache is not None:
        _emit(on_step, "platform_tools", "tick", f"symlink broken, repairing to {healthy_cache}…")
        parent.mkdir(parents=True, exist_ok=True)
        # Use shell ln -sfn rather than Python os.symlink, to keep behavior consistent with debug_agent's self-repair
        home = str(Path.home())
        cache_rel = "~" + str(healthy_cache).removeprefix(home)
        link_rel = "~" + str(link).removeprefix(home)
        rc, out = _run_env_cmd(f"ln -sfn {cache_rel} {link_rel}")
        if rc == 0 and _platform_tools_healthy(solana_ver):
            _emit(on_step, "platform_tools", "ok", f"symlink redirected to {healthy_cache.name}")
            return
        _emit(on_step, "platform_tools", "tick", f"ln -sfn repair didn't pass verification (rc={rc}), continuing fallback")

    # 2. cache is no help either → cargo-build-sbf --force-tools-install (pulls from CDN)
    if shutil.which("cargo-build-sbf"):
        _emit(on_step, "platform_tools", "tick", "cargo-build-sbf --force-tools-install (a few minutes, a few hundred MB)…")

        def _tick(line: str) -> None:
            s = line.lstrip()
            if s.startswith("info:") or s.startswith("Downloading") or "extracting" in s.lower() or s.startswith("error"):
                _emit(on_step, "platform_tools", "tick", s[:100])

        rc, out = _run_env_cmd(
            "cargo-build-sbf --force-tools-install",
            on_line=_tick,
            no_output_timeout=60,
        )
        if _platform_tools_healthy(solana_ver):
            _emit(on_step, "platform_tools", "ok", "platform-tools reinstalled")
            return
        _emit(on_step, "platform_tools", "tick", f"still incomplete after --force-tools-install (rc={rc}), trying direct tarball download")

    # 3. --force-tools-install also depends on GitHub. Manually download the platform-tools tarball, via mirror.
    if _install_platform_tools_from_tarball(solana_ver, on_step):
        _emit(on_step, "platform_tools", "ok", "platform-tools reinstalled via direct mirror download")
        return

    _emit(on_step, "platform_tools", "fail", "platform-tools can't be recovered automatically")
    raise RuntimeError(
        f"platform-tools repair failed: symlink={link}\n"
        f"  No complete platform-tools found in the cache directory, and neither `--force-tools-install` nor direct mirror download fixed it.\n"
        f"  For network issues, edit ~/.solana-spear/config.json and add:\n"
        f'    "github_mirror": "https://ghfast.top/"\n'
        f'    "https_proxy":  "http://127.0.0.1:7890"\n'
        f"  Or manually reinstall solana {solana_ver} with a different stable version."
    )


# ---- Download / platform helpers (poc-standalone, don't touch root config.py) ----

# Over mainland-China networks, pulling large files from github.com/releases is often broken/extremely slow. A small built-in set of mirrors serves as fallback,
# and the user can also add "github_mirror" in ~/.solana-spear/config.json to override and sort it to the front.
_GITHUB_MIRRORS_DEFAULT = [
    "",                          # direct — go direct when it's fastest and reachable
    "https://ghfast.top/",
    "https://gh.llkk.cc/",
    "https://ghproxy.com/",
]

_PROXY_APPLIED_ONCE = False


def _poc_config_field(key: str) -> Optional[str]:
    """Read a poc-side added field from ~/.solana-spear/config.json. The root config.py doesn't recognize these
    fields, but it's dict-based, so adding fields is harmless (it gets the default via .get for unknown keys)."""
    v = _load_config().get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _apply_poc_proxy_from_config() -> None:
    """Inject https_proxy / all_proxy from config.json into the session env once, so subsequent
    subprocesses use the user-configured proxy. Idempotent."""
    global _PROXY_APPLIED_ONCE
    if _PROXY_APPLIED_ONCE:
        return
    _PROXY_APPLIED_ONCE = True
    for key in ("https_proxy", "http_proxy", "all_proxy", "no_proxy"):
        v = _poc_config_field(key)
        if not v:
            continue
        # Set both lowercase and uppercase, for compatibility with curl/rustup/pip/etc
        _SESSION_ENV_OVERRIDES[key] = v
        _SESSION_ENV_OVERRIDES[key.upper()] = v
        os.environ[key] = v
        os.environ[key.upper()] = v


def _detect_solana_target_triple() -> str:
    import platform as _plat
    system = _plat.system().lower()
    machine = _plat.machine().lower()
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        return "x86_64-apple-darwin"
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
        return "x86_64-unknown-linux-gnu"
    raise RuntimeError(f"Unrecognized platform {system}/{machine}, can't infer solana target triple")


def _github_release_urls(gh_path: str) -> list[str]:
    """gh_path is the part after github.com/, e.g. 'anza-xyz/agave/releases/download/v1.17.34/solana-release-<triple>.tar.bz2'.

    Returns a list of candidate URLs sorted by [user_mirror] + _GITHUB_MIRRORS_DEFAULT, deduplicated and order-preserving.
    """
    gh_path = gh_path.lstrip("/")
    user_mirror = _poc_config_field("github_mirror")
    prefixes: list[str] = []
    if user_mirror:
        # Normalize, one trailing /
        um = user_mirror if user_mirror.endswith("/") else user_mirror + "/"
        prefixes.append(um)
    for p in _GITHUB_MIRRORS_DEFAULT:
        if p not in prefixes:
            prefixes.append(p)

    urls: list[str] = []
    for p in prefixes:
        if p == "":
            urls.append(f"https://github.com/{gh_path}")
        else:
            # General format for proxy mirrors: <prefix>https://github.com/<path>
            urls.append(f"{p}https://github.com/{gh_path}")
    return urls


def _http_head_size(url: str, timeout: int = 4) -> Optional[int]:
    """best-effort HEAD (follows 30x redirects) to get Content-Length. Returns None on failure/no header."""
    try:
        r = subprocess.run(
            ["curl", "-sIL", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 2,
            env=_current_env(),
        )
    except Exception:
        return None
    total: Optional[int] = None
    for line in (r.stdout or "").splitlines():
        # HTTP headers are case-insensitive, and follow to the last Content-Length (after redirects)
        if line.lower().startswith("content-length:"):
            try:
                total = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
    return total if (total and total > 0) else None


def _curl_download_with_progress(
    url: str,
    part: Path,
    on_step: Optional[StepCallback],
    step_id: str,
    label: str,
    total_bytes: Optional[int],
    no_progress_timeout: int,
    wall_timeout: int = 900,
) -> tuple[int, str]:
    """curl silently downloads to part, a watcher thread stats the file size every second + emits a percentage tick.

    - If the file size doesn't change for no_progress_timeout seconds → terminate curl, return 124.
    - Returns (rc, err_tail). rc==0 means curl exited normally and the file exists.
    """
    import threading, time as _time

    try:
        proc = subprocess.Popen(
            ["curl", "-sS", "-fL", "-o", str(part), url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env=_current_env(),
        )
    except FileNotFoundError as e:
        return 127, f"curl not found: {e}"

    stop = threading.Event()
    last_size = 0
    last_change_ts = _time.monotonic()
    kill_reason: list[str] = []

    def _watcher() -> None:
        nonlocal last_size, last_change_ts
        start = _time.monotonic()
        last_emit_ts = 0.0
        while not stop.is_set():
            try:
                size = part.stat().st_size if part.exists() else 0
            except OSError:
                size = 0
            now = _time.monotonic()
            if size != last_size:
                last_size = size
                last_change_ts = now
            elapsed = max(0.1, now - start)
            if now - last_emit_ts >= 1.0:
                last_emit_ts = now
                mb = size / 1_048_576
                speed_kbs = size / elapsed / 1024
                if total_bytes:
                    pct = min(100, int(size * 100 / total_bytes))
                    total_mb = total_bytes / 1_048_576
                    _emit(on_step, step_id, "tick",
                          f"{label} {pct}% ({mb:.1f}/{total_mb:.1f} MB @ {speed_kbs:.0f} KB/s)")
                else:
                    _emit(on_step, step_id, "tick",
                          f"{label} {mb:.1f} MB @ {speed_kbs:.0f} KB/s")
            if no_progress_timeout and (now - last_change_ts) > no_progress_timeout:
                kill_reason.append(f"<NO_PROGRESS {no_progress_timeout}s>")
                try:
                    proc.terminate()
                except Exception:
                    pass
                return
            if wall_timeout and elapsed > wall_timeout:
                kill_reason.append(f"<TIMEOUT {wall_timeout}s>")
                try:
                    proc.terminate()
                except Exception:
                    pass
                return
            _time.sleep(0.5)

    t = threading.Thread(target=_watcher, daemon=True)
    t.start()

    # curl uses -sS, so it prints nothing in the normal case; on failure it prints an error to stderr (merged into stdout). Drain it to avoid a pipe stall.
    stdout_tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        stdout_tail.append(line.rstrip())
        if len(stdout_tail) > 40:
            stdout_tail = stdout_tail[-40:]

    rc = proc.wait()
    stop.set()
    t.join(timeout=2)

    if kill_reason:
        return 124, kill_reason[0]
    if rc != 0:
        return rc, "\n".join(stdout_tail) if stdout_tail else f"curl exit {rc}"
    return 0, ""


def _download_with_mirrors(
    urls: list[str],
    dest: Path,
    on_step: Optional[StepCallback],
    step_id: str,
    label: str,
    per_url_no_output_timeout: int = 30,
) -> None:
    """Try each URL in turn, switch to the next on failure. On success mv .part → dest. Raise if all fail.

    - curl downloads silently, a Python watcher reports the percentage every second (from the total obtained via HEAD).
    - If the file size doesn't change for `per_url_no_output_timeout` seconds → treat that source as stalled, switch to the next.
    - Failed .part files are not rm'd, they're mv'd to .part.bak.<ts> for inspection (feedback_no_rm_rf).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    errors: list[str] = []

    for i, url in enumerate(urls, 1):
        # HEAD to get total (best-effort, within 6s), doubling as a liveness check for the URL.
        total_bytes = _http_head_size(url, timeout=6)
        tsize = f"{total_bytes / 1_048_576:.1f} MB" if total_bytes else "?"
        _emit(on_step, step_id, "tick",
              f"[{i}/{len(urls)}] {label} ({tsize}) ← {url[:70]}")

        # Clear .part before each attempt (back up rather than rm)
        if part.exists():
            ts = int(time.time())
            try:
                part.rename(part.with_name(part.name + f".bak.{ts}"))
            except OSError:
                pass

        rc, err_tail = _curl_download_with_progress(
            url, part, on_step, step_id, label, total_bytes,
            no_progress_timeout=per_url_no_output_timeout,
            wall_timeout=900,
        )
        if rc == 0 and part.is_file() and part.stat().st_size > 1_000_000:
            part.rename(dest)
            size_mb = dest.stat().st_size / 1_048_576
            _emit(on_step, step_id, "tick", f"download complete {label} ({size_mb:.1f} MB)")
            return
        errors.append(f"{url}: rc={rc} {err_tail[-200:] if err_tail else ''}")
        _emit(on_step, step_id, "tick", f"source {i} failed (rc={rc}), trying next…")

    hint = (
        "All download sources failed. Edit ~/.solana-spear/config.json and add:\n"
        '  "github_mirror": "https://ghfast.top/"   # or swap in one you can reach\n'
        '  "https_proxy":  "http://127.0.0.1:7890"  # or your local proxy'
    )
    raise RuntimeError(f"{label} download failed:\n" + "\n".join(errors[-3:]) + "\n\n" + hint)


async def _ensure_solana_version(
    target: str,
    on_step: Optional[StepCallback] = None,
) -> None:
    """Ensure the solana CLI is the target version, and platform-tools is complete.

    Key changes (vs the old version):
      - No longer `curl | sh` to let the install script pull GitHub internally. Download the tarball ourselves + ln -sfn.
      - Fast path A: current solana --version is already target.
      - Fast path B: <releases>/<target>/solana-release/bin/solana already exists (installed before but not activated) → just switch the symlink.
      - Main path: download solana-release-<triple>.tar.bz2 from github.com/anza-xyz/agave/releases, with mirror fallback.
    """
    global _ACTIVE_SOLANA_BIN

    _emit(on_step, "solana_ver", "start", f"requiring solana {target}…")
    _apply_poc_proxy_from_config()

    install_root = Path.home() / ".local" / "share" / "solana" / "install"
    releases_root = install_root / "releases"
    release_dir = releases_root / target / "solana-release"
    solana_bin = release_dir / "bin" / "solana"
    active_link = install_root / "active_release"

    def _activate_and_verify() -> None:
        _refresh_install_path()
        ab = install_root / "active_release" / "bin" / "solana"
        if ab.exists():
            _ACTIVE_SOLANA_BIN = ab

    # Fast path A
    if _current_solana_version() == target:
        _emit(on_step, "solana_ver", "tick", f"already at {target}")
        _activate_and_verify()
        _verify_and_repair_platform_tools(target, on_step)
        _emit(on_step, "solana_ver", "ok", f"{target} · platform-tools ✓")
        return

    # Fast path B: installed before (active_release may point to a different version) → just change the symlink
    if solana_bin.is_file():
        _emit(on_step, "solana_ver", "tick", f"reusing installed {target}, switching active_release")
        rc, out = _run_env_cmd(f'ln -sfn "{release_dir}" "{active_link}"')
        if rc != 0:
            _emit(on_step, "solana_ver", "fail", f"ln -sfn active_release exit {rc}")
            raise RuntimeError(f"switching active_release failed:\n{out[-800:]}")
    else:
        # Main path: download tarball directly, bypassing install-init
        _emit(on_step, "solana_ver", "tick", f"current {_current_solana_version()}, installing {target}…")
        try:
            triple = _detect_solana_target_triple()
        except RuntimeError as e:
            _emit(on_step, "solana_ver", "fail", str(e))
            raise
        gh_path = f"anza-xyz/agave/releases/download/v{target}/solana-release-{triple}.tar.bz2"
        urls = _github_release_urls(gh_path)
        # Versions before v1.17.x have their tarball in solana-labs/solana. Append a fallback set at the end.
        legacy_path = f"solana-labs/solana/releases/download/v{target}/solana-release-{triple}.tar.bz2"
        urls.extend(_github_release_urls(legacy_path))

        cache = Path.home() / ".cache" / "solana-spear" / "tarballs"
        tarball = cache / f"solana-release-{target}-{triple}.tar.bz2"
        if not (tarball.is_file() and tarball.stat().st_size > 1_000_000):
            _download_with_mirrors(
                urls, tarball, on_step, "solana_ver",
                f"solana-release {target}",
                per_url_no_output_timeout=30,
            )
        else:
            _emit(on_step, "solana_ver", "tick", f"reusing cached tarball {tarball.name}")

        # Back up the old release directory (feedback_no_rm_rf), don't rm
        if release_dir.exists() or release_dir.is_symlink():
            ts = int(time.time())
            bak = release_dir.with_name(f"solana-release.bak.{ts}")
            _emit(on_step, "solana_ver", "tick", f"backing up old release → {bak.name}")
            rc, out = _run_env_cmd(f'mv "{release_dir}" "{bak}"')
            if rc != 0:
                raise RuntimeError(f"backing up old release failed:\n{out[-400:]}")

        release_dir.parent.mkdir(parents=True, exist_ok=True)
        _emit(on_step, "solana_ver", "tick", f"extracting tarball → {release_dir.parent}")
        rc, out = _run_env_cmd(
            f'tar -xjf "{tarball}" -C "{release_dir.parent}"',
            no_output_timeout=120,
            wall_timeout=600,
        )
        if rc != 0 or not solana_bin.is_file():
            _emit(on_step, "solana_ver", "fail", f"tar extraction failed rc={rc}")
            raise RuntimeError(
                f"tar extraction failed (rc={rc}):\n{out[-600:]}\n"
                f"the tarball may be corrupt, delete {tarball} and retry."
            )

        rc, out = _run_env_cmd(f'ln -sfn "{release_dir}" "{active_link}"')
        if rc != 0:
            _emit(on_step, "solana_ver", "fail", f"ln -sfn active_release exit {rc}")
            raise RuntimeError(f"switching active_release failed:\n{out[-400:]}")

    _activate_and_verify()

    got = _current_solana_version()
    if got != target:
        _emit(on_step, "solana_ver", "fail", f"after install solana --version reports {got}, not {target}")
        raise RuntimeError(
            f"after installing solana, --version returned {got}, expected {target}. "
            f"Check that {active_link} points correctly."
        )

    _verify_and_repair_platform_tools(target, on_step)
    _emit(on_step, "solana_ver", "ok", f"{target} · platform-tools ✓")


async def _provision_toolchain(
    build_roots: list[Path],
    source_root: Path,
    client: LLMClient,
    on_step: Optional[StepCallback] = None,
) -> None:
    """Proactively read project metadata and align rust / solana / anchor / platform-tools.

    Each failing step raises, letting the prepare_verification upper layer abort. When the project doesn't declare a toolchain version,
    the corresponding step is skipped rather than raising.
    """
    targets = _detect_project_targets(build_roots, source_root)
    _emit(
        on_step, "provision", "start",
        f"target anchor={targets.get('anchor') or '?'} · "
        f"solana={targets.get('solana') or '?'} · "
        f"rust={targets.get('rust') or '?'}",
    )

    rust = targets.get("rust")
    if rust:
        await _ensure_rust_toolchain(rust, on_step)

    solana = targets.get("solana")
    if solana:
        await _ensure_solana_version(solana, on_step)

    anchor = targets.get("anchor")
    if anchor:
        await _ensure_anchor_version(anchor, on_step, client=client)

    _emit(on_step, "provision", "ok", "toolchain aligned to project requirements")


def _link_deploy_artifacts(workspace: Path, build_roots: list[Path]) -> tuple[int, int]:
    """Symlink each build_root's target/deploy/*.so and target/idl/*.json into the workspace's corresponding directories.

    In the Cargo workspace case, build_root/target is the same as workspace/target (same copy), so the symlink is a no-op.
    In a non-workspace layout, each build_root has its own target, so a real symlink aggregation is needed.

    Returns (n_so_linked, n_idl_linked). Existing targets (same-name .so/.json) are not overwritten.
    """
    dst_deploy = workspace / "target" / "deploy"
    dst_idl = workspace / "target" / "idl"
    dst_deploy.mkdir(parents=True, exist_ok=True)
    dst_idl.mkdir(parents=True, exist_ok=True)

    n_so = 0
    n_idl = 0
    for br in build_roots:
        if br.resolve() == workspace.resolve():
            continue
        src_deploy = br / "target" / "deploy"
        if src_deploy.exists():
            # If the Cargo workspace shares target, then br/target is actually workspace/target
            if src_deploy.resolve() == dst_deploy.resolve():
                continue
            for so in src_deploy.glob("*.so"):
                link = dst_deploy / so.name
                if link.exists() or link.is_symlink():
                    continue
                try:
                    link.symlink_to(so.resolve())
                    n_so += 1
                except OSError:
                    # Failure such as cross-device → copy fallback
                    shutil.copy2(so, link)
                    n_so += 1
        src_idl = br / "target" / "idl"
        if src_idl.exists() and src_idl.resolve() != dst_idl.resolve():
            for j in src_idl.glob("*.json"):
                link = dst_idl / j.name
                if link.exists() or link.is_symlink():
                    continue
                try:
                    link.symlink_to(j.resolve())
                    n_idl += 1
                except OSError:
                    shutil.copy2(j, link)
                    n_idl += 1
    return n_so, n_idl


# ---- Whitelist for the debug agent ----

_CMD_ALLOWLIST = [
    # --- Anchor CLI version ---
    _re.compile(r"^avm install \d+\.\d+\.\d+$"),
    _re.compile(r"^avm use \d+\.\d+\.\d+$"),
    _re.compile(r"^avm uninstall \d+\.\d+\.\d+$"),
    # --- Git restore to pristine state (the heaviest strategic measure, for whack-a-mole scenarios) ---
    # Only allow checkout Cargo.lock — this is the only file the debug agent needs to restore.
    # Don't allow checkout Cargo.toml / Anchor.toml, because these two files were already
    # changed by scope narrowing at the start of this run (keeping only the target program); a checkout would restore them to pristine
    # state, which expands the scope back and causes whack-a-mole. Keep narrowing's priority.
    _re.compile(r"^git checkout HEAD -- Cargo\.lock$"),
    _re.compile(r"^git stash push -u -m 'debug-agent-[\w-]{1,32}'$"),
    # --- Cargo / Rust (clean up via cargo's own commands, no rm -rf) ---
    _re.compile(r"^cargo update(?:\s+-p\s+[\w-]+(?:[@:]\S+)?(?:\s+--precise\s+\S+)?)*$"),
    _re.compile(r"^cargo clean$"),
    _re.compile(r"^cargo fetch$"),
    _re.compile(r"^cargo generate-lockfile$"),  # equivalent to the old `rm Cargo.lock && cargo update`
    _re.compile(r"^rustup update(?:\s+stable)?$"),
    _re.compile(r"^rustup toolchain install \d+\.\d+\.\d+(?:-\S+)?$"),
    _re.compile(r"^rustup default \d+\.\d+\.\d+(?:-\S+)?$"),
    _re.compile(r"^rustup component add rust-src(?:\s+--toolchain\s+\S+)?$"),
    # --- Node / Yarn ---
    _re.compile(r"^yarn install(?:\s+--force)?$"),
    # --- Workspace cache: back up with `mv X X.bak` rather than rm (reversible, user-auditable) ---
    # All workspace-relative, not allowed to touch any path outside the workspace.
    _re.compile(r"^mv Cargo\.lock Cargo\.lock\.bak$"),
    _re.compile(r"^mv \.anchor \.anchor\.bak$"),
    _re.compile(r"^mv node_modules node_modules\.bak$"),
    _re.compile(r"^mv target target\.bak$"),
    _re.compile(r"^mv target/idl target/idl\.bak$"),
    _re.compile(r"^mv target/deploy target/deploy\.bak$"),
    # --- Old anchor + new solana compatibility: symlink (doesn't delete anything, only adds a symlink) ---
    _re.compile(
        r"^ln -sf? "
        r"~/\.local/share/solana/install/active_release/bin/cargo-build-sbf "
        r"~/\.local/share/solana/install/active_release/bin/cargo-build-bpf$"
    ),
    # --- platform-tools symlink broken-link repair (release directory points to a nonexistent cache vX.Y) ---
    # Symptom: the solana installer installed vNew platform-tools but the symlink in release still points to vOld,
    # or vice versa. Overwrite the symlink with -sfn (macOS ln requires -n for a directory symlink, otherwise it stuffs the target
    # into the old target directory instead of replacing it). Both paths are pinned: only allow cache/vX.Y/platform-tools
    # pointing to releases/X.Y.Z/solana-release/.../platform-tools.
    _re.compile(
        r"^ln -sfn? "
        r"~/\.cache/solana/v[\d.]+/platform-tools "
        r"~/\.local/share/solana/install/releases/\d+\.\d+\.\d+/solana-release/bin/"
        r"platform-tools-sdk/sbf/dependencies/platform-tools$"
    ),
    # --- Solana platform-tools recovery: use cargo-build-sbf's built-in forced re-download ---
    _re.compile(r"^cargo-build-sbf --force-tools-install$"),
    # --- Solana CLI reinstall (official URL, pinned version or stable) ---
    # The installer script **overwrites** the release directory of the current version, or installs to a new version directory and switches active_release,
    # no need to rm -rf the old one first.
    _re.compile(
        r'^sh -c "\$\(curl -sSfL https://release\.anza\.xyz/'
        r'(?:stable|v?\d+\.\d+\.\d+)/install\)"$'
    ),
    _re.compile(
        r'^sh -c "\$\(curl -sSfL https://release\.solana\.com/'
        r'(?:stable|v?\d+\.\d+\.\d+)/install\)"$'
    ),
]

_EDIT_ALLOWLIST = [
    _re.compile(r"^Anchor\.toml$"),
    _re.compile(r"^Cargo\.toml$"),
    _re.compile(r"^Cargo\.lock$"),
    _re.compile(r"^programs/[\w-]+/Cargo\.toml$"),
    _re.compile(r"^programs/[\w-]+/[\w-]+/Cargo\.toml$"),
]


# ---- Whitelist for env_agent (separate from debug_agent; env_agent is allowed to operate on global rust/cargo config) ----

_ENV_CMD_ALLOWLIST = [
    _re.compile(r"^rustup toolchain install \d+\.\d+\.\d+(?:-\S+)?$"),
    _re.compile(r"^rustup default \d+\.\d+\.\d+(?:-\S+)?$"),
    _re.compile(r"^avm install \d+\.\d+\.\d+$"),
    _re.compile(r"^avm uninstall \d+\.\d+\.\d+$"),
    _re.compile(r"^avm use \d+\.\d+\.\d+$"),
    _re.compile(r"^cargo update(?:\s+-p\s+[\w-]+(?:[@:]\S+)?(?:\s+--precise\s+\S+)?)*$"),
    _re.compile(r"^cargo clean$"),
    # Base-tool installers — only official URLs / official repos / known package names. The LLM can't replace the URL.
    _re.compile(
        r"^curl --proto '=https' --tlsv1\.2 -sSf https://sh\.rustup\.rs \| sh -s -- -y"
        r"(?:\s+--default-toolchain\s+\S+)?(?:\s+--profile\s+\S+)?$"
    ),
    _re.compile(
        r'^sh -c "\$\(curl -sSfL https://release\.(?:anza\.xyz|solana\.com)/stable/install\)"$'
    ),
    _re.compile(
        r"^cargo install --git https://github\.com/coral-xyz/anchor avm --locked(?:\s+--force)?$"
    ),
    _re.compile(r"^brew install (?:node|node@\d+|yarn)(?:\s+(?:node|node@\d+|yarn)){0,2}$"),
    _re.compile(r"^npm install -g yarn$"),
]

_ENV_VAR_ALLOWLIST = {
    "RUSTUP_DIST_SERVER", "RUSTUP_UPDATE_ROOT",
    "CARGO_HTTP_PROXY", "CARGO_HTTPS_PROXY", "CARGO_HTTP_CHECK_REVOKE",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    "SOLANA_DOWNLOAD_ROOT",  # the new flow uses direct tarball download, kept for the env_agent debug path
}

# Only allow modifying the user-level cargo config. ~/.rustup/settings.toml / shell rc are off-limits.
_ENV_EDIT_PATHS = {
    str((Path.home() / ".cargo" / "config.toml").resolve()),
    str((Path.home() / ".cargo" / "config").resolve()),  # old cargo
}


def _env_cmd_allowed(cmd: str) -> bool:
    return any(p.match(cmd.strip()) for p in _ENV_CMD_ALLOWLIST)


def _env_var_allowed(key: str) -> bool:
    return key in _ENV_VAR_ALLOWLIST


def _env_edit_path_allowed(path_str: str) -> Path | None:
    """Parse the file path the user passed (supports ~, absolute paths), only return a Path if it falls in the whitelist, else None."""
    p = Path(path_str).expanduser()
    # Allow the agent to pass just a relative ~/.cargo/config.toml (even if file is written in relative form)
    resolved = p.resolve()
    if str(resolved) in _ENV_EDIT_PATHS:
        return resolved
    return None


# Session-level env override — env vars the env_agent decided on, saved to the process env to take effect for subsequent subprocesses
_SESSION_ENV_OVERRIDES: dict[str, str] = {}


# After the LLM installs tools, the binaries usually land in these known directories. The harness splices them into PATH itself,
# the LLM can't touch PATH (it's not in _ENV_VAR_ALLOWLIST).
_INSTALL_PATH_DIRS = [
    "~/.cargo/bin",                                              # rustup, cargo, rustc, avm binary itself
    "~/.avm/bin",                                                # anchor shim
    "~/.local/share/solana/install/active_release/bin",          # solana CLI (Anza / Solana Labs installer)
    "/opt/homebrew/bin",                                         # brew on Apple Silicon
    "/usr/local/bin",                                            # brew on Intel macOS
    "~/.local/bin",                                              # pip --user, some npm globals
]


def _refresh_install_path() -> list[str]:
    """Prepend the existing directories in _INSTALL_PATH_DIRS to os.environ["PATH"] (deduplicated).

    Returns the list of directories added this time, for tick display.
    """
    current = os.environ.get("PATH", "").split(os.pathsep)
    current_set = set(current)
    added: list[str] = []
    prepend: list[str] = []
    for raw in _INSTALL_PATH_DIRS:
        p = str(Path(raw).expanduser())
        if not Path(p).is_dir():
            continue
        if p in current_set:
            continue
        prepend.append(p)
        current_set.add(p)
        added.append(p)
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + current)
    return added


def _current_env() -> dict[str, str]:
    """The full env the current subprocess should use: os.environ + overrides accumulated by the agent in this session."""
    e = dict(os.environ)
    e.update(_SESSION_ENV_OVERRIDES)
    return e


def _apply_env_plan(
    plan: dict,
    on_line: Optional[Callable[[str], None]] = None,
) -> tuple[bool, list[str]]:
    """Apply the fix plan returned by the agent: env_vars → session env; commands → whitelisted run; edits → cargo config.

    Returns (all_ok, messages) — a success/failure summary of each step, for tick display.
    """
    msgs: list[str] = []

    # 1. env vars
    for k, v in (plan.get("env_vars") or {}).items():
        if not _env_var_allowed(k):
            msgs.append(f"env var rejected (not in whitelist): {k}")
            continue
        _SESSION_ENV_OVERRIDES[k] = v
        # Also write into the current process env so subprocess.Popen inherits it by default
        os.environ[k] = v
        msgs.append(f"env: {k}={v[:60]}")

    # 2. commands
    for cmd in (plan.get("commands") or []):
        if not _env_cmd_allowed(cmd):
            msgs.append(f"command rejected (not in whitelist): {cmd}")
            return False, msgs
        msgs.append(f"running `{cmd}` …")
        rc, out = _run_env_cmd(cmd, on_line=on_line)
        if rc != 0:
            msgs.append(f"`{cmd}` exit {rc}")
            return False, msgs
        msgs.append(f"`{cmd}` ✓")

    # 3. edits (cargo config and the like)
    for edit in (plan.get("edits") or []):
        target = _env_edit_path_allowed(edit.get("file", ""))
        if target is None:
            msgs.append(f"edit path rejected: {edit.get('file')}")
            return False, msgs
        ok, msg = _apply_config_edit(target, edit)
        msgs.append(msg)
        if not ok:
            return False, msgs

    return True, msgs


def _run_env_cmd(
    cmd: str,
    on_line: Optional[Callable[[str], None]] = None,
    *,
    wall_timeout: int = 1800,
    no_output_timeout: Optional[int] = None,
) -> tuple[int, str]:
    """Run an env_agent-allowed command, with accumulated env override, streaming output.

    - wall_timeout: hard upper limit in seconds (total runtime)
    - no_output_timeout: kill if the subprocess stdout is silent for > N seconds (fail early when a download stalls, letting the upper layer switch mirrors)
    """
    import collections, queue as _queue, threading, time
    # `avm use X.Y.Z` in env_agent can also trigger an interactive install prompt, so prepend install.
    m = _re.match(r"^avm use (\d+\.\d+\.\d+)$", cmd.strip())
    if m:
        cmd = f"avm install {m.group(1)} && avm use {m.group(1)}"
    try:
        proc = subprocess.Popen(
            cmd, shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env=_current_env(),
        )
    except FileNotFoundError as e:
        return 127, f"binary not found: {e}"
    tail: "collections.deque[str]" = collections.deque(maxlen=400)
    assert proc.stdout is not None

    q: "_queue.Queue[Optional[str]]" = _queue.Queue()

    def _reader() -> None:
        try:
            for ln in proc.stdout:  # type: ignore[union-attr]
                q.put(ln)
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    start_ts = time.monotonic()
    last_ts = start_ts
    kill_reason: Optional[str] = None
    while True:
        try:
            line = q.get(timeout=1.0)
        except _queue.Empty:
            now = time.monotonic()
            if no_output_timeout is not None and (now - last_ts) > no_output_timeout:
                kill_reason = f"<NO_OUTPUT_TIMEOUT {no_output_timeout}s>"
                break
            if (now - start_ts) > wall_timeout:
                kill_reason = "<TIMEOUT>"
                break
            continue
        if line is None:  # EOF
            break
        line = line.rstrip()
        tail.append(line)
        last_ts = time.monotonic()
        if on_line:
            on_line(line)

    if kill_reason is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:
            pass
        return 124, "\n".join(tail) + "\n\n" + kill_reason

    remaining = max(1, wall_timeout - int(time.monotonic() - start_ts))
    try:
        proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return 124, "\n".join(tail) + "\n\n<TIMEOUT>"
    return proc.returncode, "\n".join(tail)


def _apply_config_edit(target: Path, edit: dict) -> tuple[bool, str]:
    """Append/replace for cargo config and the like. old="" means append to the end of the file, otherwise exact replacement (must occur exactly once)."""
    old = edit.get("old", "")
    new = edit.get("new", "")
    if not isinstance(new, str):
        return False, f"{target.name}: new is not a string"

    target.parent.mkdir(parents=True, exist_ok=True)

    if old == "":
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        sep = "" if not existing or existing.endswith("\n") else "\n"
        target.write_text(existing + sep + new + ("\n" if not new.endswith("\n") else ""), encoding="utf-8")
        return True, f"{target}: appended {len(new)} chars"

    if not target.exists():
        return False, f"{target}: file doesn't exist but you gave a non-empty `old`"
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return False, f"{target}: `old` not found"
    if count > 1:
        return False, f"{target}: `old` occurs {count} times, not unique"
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True, f"{target}: 1 replacement"


def _cmd_allowed(cmd: str) -> bool:
    return any(p.match(cmd.strip()) for p in _CMD_ALLOWLIST)


def _edit_path_allowed(path: str) -> bool:
    return any(p.match(path) for p in _EDIT_ALLOWLIST)


def _run_debug_cmd(cmd: str, workspace: Path, on_line: Optional[Callable[[str], None]] = None) -> tuple[int, str]:
    """Run a whitelisted shell command. Return (returncode, combined_output_tail)."""
    import collections
    # `avm use X.Y.Z` on an uninstalled version prompts interactively for install confirmation
    # and stdin=DEVNULL below would give it EOF -> immediate fail. Rewrite to install-then-use.
    # `avm install X.Y.Z` is idempotent (no-op if already installed).
    m = _re.match(r"^avm use (\d+\.\d+\.\d+)$", cmd.strip())
    if m:
        cmd = f"avm install {m.group(1)} && avm use {m.group(1)}"
    proc = subprocess.Popen(
        cmd, shell=True, cwd=str(workspace),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    tail: "collections.deque[str]" = collections.deque(maxlen=200)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        if on_line:
            on_line(line)
    try:
        proc.wait(timeout=1800)  # avm install may take over ten minutes
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return 124, "\n".join(tail) + "\n\n<TIMEOUT>"
    return proc.returncode, "\n".join(tail)


def _apply_edit(workspace: Path, edit: dict) -> tuple[bool, str]:
    """Return (ok, msg). Requires `old` to occur exactly once."""
    p = workspace / edit["file"]
    if not p.exists():
        return False, f"file doesn't exist: {edit['file']}"
    text = p.read_text(encoding="utf-8")
    count = text.count(edit["old"])
    if count == 0:
        return False, f"{edit['file']}: `old` not found"
    if count > 1:
        return False, f"{edit['file']}: `old` occurs {count} times, not unique"
    p.write_text(text.replace(edit["old"], edit["new"], 1), encoding="utf-8")
    return True, f"{edit['file']}: 1 replacement"


def _slugify(s: str, max_len: int = 40) -> str:
    """audit-report-slug: strip non-alnum, lowercase, keep dashes/underscores."""
    s = re.sub(r"[^\w\s-]", "", s.strip().lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:max_len].rstrip("-") or "poc"


def _save_valid_pocs(report_path: Path, workspace: Path, verdicts: dict) -> Path | None:
    """For each VALID verdict, copy its winning .test.ts to <audit-md-dir>/<audit-stem>-poc/.

    Each saved file gets a header comment block with metadata (spec, attempt, timestamp).
    Returns the output dir path, or None if no VALID findings.
    """
    valid = [(num, v) for num, v in verdicts.items() if v.get("status") == "VALID"]
    if not valid:
        return None

    out_dir = report_path.with_name(report_path.stem + "-poc")
    out_dir.mkdir(exist_ok=True)

    for num, v in valid:
        # Find the successful attempt
        winning = next((a for a in v.get("attempts", []) if a.get("verdict") == "VALID"), None)
        if not winning:
            continue
        src_path = workspace / winning["test_file"]
        if not src_path.exists():
            print(f"[poc]   ⚠  the test file for finding #{num} is missing: {src_path}")
            continue

        body = src_path.read_text(encoding="utf-8")
        spec = v.get("spec") or {}
        header = _build_poc_header(num, spec, winning, workspace)
        dst_path = out_dir / f"finding-{num}.test.ts"
        dst_path.write_text(header + "\n" + body, encoding="utf-8")

    return out_dir


def _build_poc_header(num, spec: dict, winning_attempt: dict, workspace: Path) -> str:
    """Build the /* PoC ... */ comment block prepended to saved test files."""
    obs = spec.get("observations", [])
    pred = spec.get("success_predicate", {})
    lines = [
        "/**",
        f" * PoC for Finding #{num}",
        f" * Workspace: {workspace.name}",
        f" * Won on attempt {winning_attempt.get('attempt')}/{MAX_ATTEMPTS} (exit {winning_attempt.get('exit_code')})",
        " *",
        " * Verdict spec:",
    ]
    if spec.get("reasoning"):
        lines.append(f" *   reasoning: {spec['reasoning'][:200]}")
    if obs:
        lines.append(" *   observations:")
        for o in obs:
            if isinstance(o, dict):
                lines.append(f" *     - {o.get('name')} ({o.get('type')}): {(o.get('description') or '')[:80]}")
    if pred.get("condition_ts"):
        lines.append(f" *   predicate: {pred['condition_ts']}")
    if pred.get("success_log_template"):
        lines.append(f" *   on VALID:  {pred['success_log_template'][:120]}")
    lines.append(" */")
    return "\n".join(lines)


async def _anchor_build_with_debug(
    workspace: Path,
    client: LLMClient,
    on_step: Optional[StepCallback],
    max_iters: int = 15,
    programs: Optional[list[str]] = None,
) -> tuple[bool, str, float]:
    """Build with automatic debug loop. Returns (ok, tail, total_seconds).

    programs=None → build the whole workspace; list → build only the specified programs (`anchor build -p NAME` loop).
    """
    from poc.debug_agent import gather_debug_context, propose_build_fix

    n_compiled = [0]

    def _make_build_ticker(prefix: str = ""):
        def _on_line(line: str) -> None:
            s = line.lstrip()
            if s.startswith("Compiling ") or s.startswith("Downloading ") or s.startswith("Updating "):
                parts = s.split()
                if len(parts) >= 2:
                    verb = parts[0]
                    what = " ".join(parts[1:3]) if len(parts) >= 3 else parts[1]
                    if verb == "Compiling":
                        n_compiled[0] += 1
                    _emit(on_step, "build", "tick", f"{prefix}{verb} {what} (#{n_compiled[0]})")
            elif s.startswith("Finished ") or s.startswith("Compilation "):
                _emit(on_step, "build", "tick", f"{prefix}{s[:80]}")
            elif s.startswith("error") or "panicked" in s:
                _emit(on_step, "build", "tick", f"{prefix}{s[:80]}")
        return _on_line

    total_dt = 0.0
    scope = f" (build only: {', '.join(programs)})" if programs else ""
    _emit(on_step, "build", "start", f"anchor build{scope} (the first time may take 3-15 minutes)…")
    ok, tail, dt = _anchor_build(workspace, on_line=_make_build_ticker(), programs=programs)
    total_dt += dt
    if ok:
        return True, tail, total_dt

    prev_diagnoses: list[str] = []  # kept so downstream can read it, but no longer appended to
    attempted_commands: set[str] = set()  # dedup: don't rerun a command already run, forcing the agent to try something else
    attempted_edits: set[tuple[str, str, str]] = set()  # (file, old, new), also deduplicated
    session_history: list[dict] = []  # cross-round (round, family, actions, outcome), passed to the LLM to see patterns
    for it in range(1, max_iters + 1):
        _emit(on_step, "build", "tick", f"↳ debug {it}/{max_iters}: analyzing build failure…")
        ctx = gather_debug_context(workspace, tail, history=session_history)
        _LOGGER.debug(f"debug iter {it} build_tail:\n{tail[-3000:]}")
        current_family = ctx.get("error_family", "other")
        try:
            plan = await propose_build_fix(client, ctx)
        except Exception as e:
            _LOGGER.error(f"debug agent iter {it} exception: {e!r}")
            _emit(on_step, "build", "fail", f"debug agent errored: {e}")
            return False, tail, total_dt
        _LOGGER.info(f"debug iter {it} plan: {json.dumps(plan, ensure_ascii=False)[:2000]}")

        _emit(on_step, "build", "tick", f"↳ debug {it}: {plan['diagnosis'][:120]}")

        if plan.get("give_up"):
            _emit(on_step, "build", "fail", f"debug agent give up: {plan['diagnosis'][:200]}")
            return False, tail, total_dt

        # Validate + execute commands
        ran_any = False
        any_cmd_failed = False  # a cmd failed → skip build retry, keep the cmd stderr for the next round
        dedup_notes: list[str] = []  # collect skipped commands, put back into tail next round to make the agent try something else
        actions_summary: list[str] = []  # what this round actually did, for session_history

        def _record_history(outcome: str) -> None:
            session_history.append({
                "round": it,
                "family": current_family,
                "actions": "; ".join(actions_summary) if actions_summary else f"(no-op: {outcome})",
                "outcome": outcome,
            })

        for cmd in plan.get("commands", []):
            if not _cmd_allowed(cmd):
                _emit(on_step, "build", "fail", f"rejected unsafe command: {cmd}")
                _record_history(f"cmd_rejected: {cmd[:60]}")
                return False, tail, total_dt
            cmd_key = cmd.strip()
            if cmd_key in attempted_commands:
                # Same command already run (regardless of last rc) → reject, make the agent try something else
                _emit(on_step, "build", "tick", f"↳ debug {it}: skip duplicate command `{cmd_key}` (try something else)")
                dedup_notes.append(f"[cmd already attempted this session, skipped] {cmd_key}")
                actions_summary.append(f"skip-dup `{cmd_key[:50]}`")
                continue
            attempted_commands.add(cmd_key)
            ran_any = True
            _emit(on_step, "build", "tick", f"↳ debug {it}: running `{cmd}`…")
            rc, cout = _run_debug_cmd(cmd, workspace)
            if rc != 0:
                # cmd failed → put stderr into tail, and mark this round as not retrying build
                # (previously it would immediately retry build and overwrite tail, so next round the LLM couldn't see why the cmd failed)
                tail = f"[cmd `{cmd}` exit {rc}]\n{cout[-2500:]}\n\n{tail[-1500:]}"
                _emit(on_step, "build", "tick", f"↳ debug {it}: `{cmd}` failed (exit {rc}) — keeping stderr, skipping build retry")
                any_cmd_failed = True
                actions_summary.append(f"`{cmd_key[:50]}` FAIL(exit {rc})")
                continue
            _emit(on_step, "build", "tick", f"↳ debug {it}: `{cmd}` ✓")
            actions_summary.append(f"`{cmd_key[:50]}` ok")
            # After `avm use X.Y.Z` succeeds, _ACTIVE_ANCHOR_BIN must also be switched over —
            # otherwise the pinned anchor absolute path in [7/11] won't follow, and the agent's switch was wasted.
            m = _re.match(r"^avm use (\d+\.\d+\.\d+)$", cmd.strip())
            if m:
                new_bin = _anchor_bin_for(m.group(1))
                if new_bin.exists():
                    global _ACTIVE_ANCHOR_BIN
                    _ACTIVE_ANCHOR_BIN = new_bin
                    _emit(on_step, "build", "tick", f"↳ debug {it}: active anchor → {m.group(1)}")

        # Validate + apply edits
        for edit in plan.get("edits", []):
            if not _edit_path_allowed(edit.get("file", "")):
                _emit(on_step, "build", "fail", f"rejected edit path: {edit.get('file')}")
                _record_history(f"edit_rejected: {edit.get('file')}")
                return False, tail, total_dt
            edit_key = (edit.get("file", ""), edit.get("old", ""), edit.get("new", ""))
            if edit_key in attempted_edits:
                _emit(on_step, "build", "tick", f"↳ debug {it}: skip duplicate edit ({edit_key[0]})")
                dedup_notes.append(f"[edit already attempted this session, skipped] {edit_key[0]}")
                actions_summary.append(f"skip-dup edit {edit_key[0]}")
                continue
            attempted_edits.add(edit_key)
            ran_any = True
            eok, emsg = _apply_edit(workspace, edit)
            _emit(on_step, "build", "tick", f"↳ debug {it}: edit {emsg}")
            if not eok:
                tail = f"[edit failed: {emsg}]\n\n{tail[-3000:]}"
                actions_summary.append(f"edit {edit_key[0]} FAIL: {emsg[:60]}")
            else:
                actions_summary.append(f"edit {edit_key[0]} ok")

        # If all commands/edits this round were deduplicated away, or a cmd failed → don't retry build, make the agent try something else
        if dedup_notes:
            tail = "\n".join(dedup_notes) + "\n\n" + tail[-3000:]
        if not ran_any:
            _emit(on_step, "build", "tick", f"↳ debug {it}: all duplicates, skipping build retry, making the agent try something else")
            _record_history("all_dedup_skipped")
            continue
        if any_cmd_failed:
            _emit(on_step, "build", "tick", f"↳ debug {it}: a cmd failed, skipping build retry, letting the agent see stderr and try something else")
            _record_history("cmd_failed_no_build_retry")
            continue

        # retry build
        n_compiled[0] = 0
        _emit(on_step, "build", "tick", f"↳ debug {it}: retry anchor build…")
        ok, tail, dt = _anchor_build(
            workspace, on_line=_make_build_ticker(f"[retry {it}] "), programs=programs,
        )
        total_dt += dt
        if ok:
            _emit(on_step, "build", "tick", f"↳ debug {it}: build ✓ ({dt:.0f}s)")
            _record_history("build_ok")
            return True, tail, total_dt
        _record_history("build_still_failed")

    _emit(on_step, "build", "fail", f"still failing after {max_iters} debug rounds")
    _LOGGER.error(f"anchor build final failure tail (workspace={workspace}):\n{tail[-4000:]}")
    return False, tail, total_dt


async def _build_all_roots(
    build_roots: list[Path],
    client: LLMClient,
    on_step: Optional[StepCallback],
    programs: Optional[list[str]] = None,
) -> float:
    """Run anchor build for each build_root (each with its own debug loop). Raise on any failure.

    Externally emits only one 'build' step; with multiple roots, uses an [i/N] prefix in the tick to distinguish them.
    programs=None → build all; list → build only the specified programs within each build_root.
    """
    total_dt = 0.0
    n = len(build_roots)
    for i, br in enumerate(build_roots, 1):
        prefix = f"[{i}/{n}] " if n > 1 else ""

        # The inner _anchor_build_with_debug emits its own build start/tick/ok/fail — intercept and rewrite
        def _inner(sid: str, status: str, detail: str, _p=prefix, _br=br):
            if sid != "build":
                _emit(on_step, sid, status, detail)
                return
            if status == "start":
                # Let the first root's start pass through (the user can see anchor build started),
                # convert later roots to tick, to avoid the StepPrinter counter jumping around
                if _p == "" or i == 1:
                    _emit(on_step, "build", "start" if i == 1 else "tick", f"{_p}{_br.name}: {detail}")
                else:
                    _emit(on_step, "build", "tick", f"{_p}{_br.name}: {detail}")
            elif status == "tick":
                _emit(on_step, "build", "tick", f"{_p}{detail}")
            elif status == "ok":
                _emit(on_step, "build", "tick", f"↳ {_p}{_br.name} ✓ {detail}")
            elif status == "fail":
                _emit(on_step, "build", "fail", f"{_p}{_br.name}: {detail}")

        ok, tail, dt = await _anchor_build_with_debug(br, client, _inner, programs=programs)
        total_dt += dt
        if not ok:
            raise RuntimeError(
                f"anchor build failed @ {br.name} (after debug retries). tail:\n{tail[-1200:]}"
            )
    return total_dt


async def prepare_verification(
    report_path: Path,
    workspace: Path,
    *,
    build_roots: list[Path] | None = None,
    source_root: Path | None = None,
    skip_build: bool = False,
    skip_preflight: bool = False,
    preflight_missing: list[str] | None = None,
    log_file: Optional[Path] = None,
    on_step: Optional[StepCallback] = None,
    programs: list[str] | None = None,
    urls_and_subpaths: list[tuple[str, str]] | None = None,
    scope_confirm: Optional[Callable[[dict], list[str] | None]] = None,
) -> PocContext:
    """Run all pre-LLM setup: tool check, client, provision (rust/solana/anchor),
    build (with debug retry), extract, detect, collect.

    build_roots: list of directories containing Anchor.toml. Default [workspace]. With multiple roots, build in sequence then
                 use _link_deploy_artifacts to aggregate .so/.json into the workspace.
    source_root: the root for collecting .rs, default workspace. In the multi-root case, usually pass clone_root,
                 so source_blob can encompass all relevant build_roots.

    Emits progress via on_step(step_id, status, detail) — status is
    'start' | 'tick' | 'ok' | 'fail'.

    log_file: specifies the log path. None = default ~/.solana-spear/logs/<workspace>-<utc>.log.
              Pass Path("/dev/null") or Path("-") to disable logging. The log contains each step,
              build tail, LLM plan JSON, and subprocess output.

    Set skip_preflight=True when caller already ran check_tools() first AND
    has confirmed there are no missing tools; otherwise this function runs preflight + env_agent itself before proceeding.

    preflight_missing: the caller already ran check_tools and got the missing list, forwarded through this
    parameter. If passed, skips the internal check_tools call (to avoid the preflight step appearing twice in the UI),
    but still uses this list to trigger _ensure_tools.
    """
    if not report_path.exists():
        raise RuntimeError(f"report not found: {report_path}")
    if not workspace.exists() or not workspace.is_dir():
        raise RuntimeError(f"workspace not found or not a dir: {workspace}")

    # Env var escape hatch: lets us limit the programs even when entering from the root harness.py (not controlled by this poc).
    # Usage: POC_ONLY_PROGRAMS=conditional_vault,futarchy python3 ...
    if programs is None:
        env_val = os.environ.get("POC_ONLY_PROGRAMS", "").strip()
        if env_val:
            programs = [p.strip() for p in _re.split(r"[,\s]+", env_val) if p.strip()]

    # Open the log file. A new one each time prepare_verification is entered (cross-run handler accumulation is already cleaned in setup).
    log_path = _setup_run_log(workspace, log_file)
    if log_path is not None:
        _emit(on_step, "log", "ok", f"log: {log_path}")

    if build_roots is None:
        build_roots = [workspace]
    if source_root is None:
        source_root = workspace

    _LOGGER.info(
        f"report={report_path} build_roots={[str(b) for b in build_roots]} "
        f"source_root={source_root} skip_build={skip_build} skip_preflight={skip_preflight}"
    )

    missing_tools: list[str] = []
    if preflight_missing is not None:
        # The caller already ran preflight, use the list they gave directly
        missing_tools = list(preflight_missing)
    elif not skip_preflight:
        missing_tools = check_tools(on_step)

    # --- client (needed before build, because the debug loop needs the LLM; ensure_tools does too) ---
    _emit(on_step, "client", "start", "loading config…")
    cfg = _load_config()
    client = _make_client(cfg)
    from poc.llm_setup import poc_model, poc_base_url
    p_model = poc_model()
    p_base = poc_base_url() or (cfg.get("base_url") or "").strip()
    where = f"{p_model} via {p_base}" if p_base else p_model
    _emit(on_step, "client", "ok", where)

    # --- Scope agent: LLM recommends target programs from URL + audit md + workspace, user confirms ---
    # Only triggered when there are urls_and_subpaths and the user hasn't manually specified programs. The result overrides programs.
    build_members: list[str] | None = None
    if programs is None and urls_and_subpaths and scope_confirm is not None:
        from poc.scope_agent import propose_scope, propose_build_members
        _emit(on_step, "scope", "start", "LLM recommending audit scope…")
        try:
            proposal = await propose_scope(client, workspace, urls_and_subpaths, report_path)
        except Exception as e:
            _emit(on_step, "scope", "fail", f"scope agent errored: {e}")
            raise RuntimeError(f"scope agent failed: {e}") from e
        _emit(on_step, "scope", "tick",
              f"recommended: [{', '.join(proposal.get('targets', []))}] · skipped {len(proposal.get('skipped', []))}")
        # Hand off to the upper layer for interactive confirmation (callback provided by harness), returns the final target list or None (abort)
        confirmed = scope_confirm(proposal)
        if confirmed is None:
            _emit(on_step, "scope", "fail", "user aborted at the scope confirmation stage")
            raise RuntimeError("aborted by user at scope confirmation")
        programs = confirmed
        _emit(on_step, "scope", "ok", f"audit target: [{', '.join(programs)}]")

        # Phase 2: chase the path dependency closure, compute the minimal compile workspace.members
        _emit(on_step, "scope", "start", "chasing path dependency closure (build members)…")
        try:
            bm_result = await propose_build_members(client, workspace, programs)
        except Exception as e:
            _emit(on_step, "scope", "fail", f"build_members agent errored: {e}")
            raise RuntimeError(f"build_members agent failed: {e}") from e
        build_members = bm_result.get("build_members") or [f"programs/{t}" for t in programs]
        # Show the extras (path dependencies) that were added, for the user to see
        target_paths = {f"programs/{t}" for t in programs}
        extras = sorted(set(build_members) - target_paths)
        if extras:
            _emit(on_step, "scope", "ok",
                  f"build includes path dependencies: [{', '.join(extras)}]  ({bm_result.get('rationale', '')[:120]})")
        else:
            _emit(on_step, "scope", "ok", "no extra path dependencies")

    # --- Scope narrowing: narrow root Cargo.toml + Anchor.toml by audit_targets + build_members ---
    # Idempotent, each run restores pristine first then narrows, to avoid residue from the previous run.
    if programs and (workspace / "Cargo.toml").exists():
        _emit(on_step, "scope", "tick",
              f"narrowing → [programs.localnet]={len(programs)} · [workspace.members]={len(build_members) if build_members else len(programs)}")
        _apply_scope_narrowing(workspace, programs, build_members)

    # --- Missing tools? Let env_agent install them (needs client) ---
    if missing_tools:
        await _ensure_tools(missing_tools, client, on_step)  # a raise propagates up

    # --- Provision: proactively align rust / solana / anchor / platform-tools ---
    # First check whether the anchor version conflicts between build_roots; abort directly on conflict (can't install two versions at once)
    unified, per_root = _detect_required_anchor_version(source_root, build_roots)
    if per_root and unified is None:
        conflict = ", ".join(f"{br.name}={v}" for br, v in per_root.items())
        _emit(on_step, "anchor_ver", "fail", f"version conflict: {conflict}")
        raise RuntimeError(f"multiple build_roots require different anchor versions: {conflict}")

    await _provision_toolchain(build_roots, source_root, client, on_step)

    # --- anchor build (with debug retry) — run once per build_root ---
    if skip_build:
        _emit(on_step, "build", "ok", "skipped (--skip-build)")
    else:
        total_dt = await _build_all_roots(build_roots, client, on_step, programs=programs)

        # aggregate artifacts
        n_so, n_idl = _link_deploy_artifacts(workspace, build_roots)
        idl_dir = workspace / "target" / "idl"
        n_idls_final = len(list(idl_dir.glob("*.json"))) if idl_dir.exists() else 0
        summary_bits = [f"{total_dt:.0f}s", f"{len(build_roots)} program(s)", f"{n_idls_final} IDL"]
        if n_so or n_idl:
            summary_bits.append(f"symlinked {n_so} .so + {n_idl} .json")
        _emit(on_step, "build", "ok", ", ".join(summary_bits))

    # --- extract findings ---
    _emit(on_step, "extract", "start", "parsing audit md…")
    findings = extract_findings(report_path)
    if not findings:
        _emit(on_step, "extract", "fail", "no findings in report")
        raise RuntimeError("no findings extracted from report")
    _emit(on_step, "extract", "ok", f"{len(findings)} finding(s)")

    # --- detect programs by a full scan from source_root (including under all build_roots) ---
    _emit(on_step, "detect", "start", "reading Anchor.toml…")
    programs: dict[str, str] = {}
    for br in build_roots:
        programs.update(_detect_programs(br))
    if not programs:
        # Fallback: scan again from source_root
        programs = _detect_programs(source_root)
    if programs:
        preview = " · ".join(f"{n} → {pid[:6]}…" for n, pid in list(programs.items())[:3])
        if len(programs) > 3:
            preview += f" · (+{len(programs)-3})"
        _emit(on_step, "detect", "ok", preview)
    else:
        _emit(on_step, "detect", "ok", "(none — Anchor.toml missing?)")

    # --- collect source: only gather .rs under build_root, to avoid pulling in unrelated code from the monorepo ---
    _emit(on_step, "collect", "start", "scanning .rs files…")
    scope: list[str] = []
    for br in build_roots:
        try:
            rel = br.relative_to(source_root)
        except ValueError:
            # build_root is not under source_root (shouldn't happen, fallback)
            rel = Path(br.name)
        scope.append(str(rel) if str(rel) != "." else "")
    rs_files, _ = collect_rs_files(source_root, [s for s in scope if s])
    if not rs_files and not any(scope):
        # The source_root == build_root case (scope entirely empty), use the no-scope call
        rs_files, _ = collect_rs_files(source_root, [])
    source_blob = _concat_source_with_headers(source_root, rs_files, build_roots)
    _emit(on_step, "collect", "ok", f"{len(rs_files)} files, {len(source_blob)//1024} KB")

    return PocContext(
        report_path=report_path,
        workspace=workspace,
        findings=findings,
        workspace_info={
            "root": str(workspace),
            "programs": programs,
            "build_roots": [str(br) for br in build_roots],
            "source_root": str(source_root),
            "js_versions": _detect_js_versions(workspace),
        },
        source_blob=source_blob,
        client=client,
        build_roots=build_roots,
        source_root=source_root,
    )


def _concat_source_with_headers(
    source_root: Path,
    files: list[Path],
    build_roots: list[Path],
) -> str:
    """Concatenate like concat_source, but additionally note each file's owning build_root before it.

    With a single build_root that == source_root, falls back to the original format without the redundant header.
    """
    if len(build_roots) == 1 and build_roots[0].resolve() == source_root.resolve():
        # old behavior
        return concat_source(source_root, files)

    def _which_root(f: Path) -> Path | None:
        for br in build_roots:
            try:
                f.relative_to(br)
                return br
            except ValueError:
                continue
        return None

    parts: list[str] = []
    for f in files:
        rel_src = f.relative_to(source_root)
        br = _which_root(f)
        br_rel = str(br.relative_to(source_root)) if br else "?"
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            content = f"<< read error: {e} >>"
        header = (
            "// " + "=" * 60 + "\n"
            f"// === build_root: {br_rel or '.'}/\n"
            f"// === file:       {rel_src}\n"
            "// " + "=" * 60
        )
        parts.append(f"{header}\n{content}\n")
    return "\n".join(parts)


async def verify_findings(
    ctx: PocContext,
    only: list[int] | None = None,
) -> Path:
    """Run LLM loop over ctx.findings; write augmented report. Returns its path."""
    findings = ctx.findings
    if only:
        wanted = {str(n) for n in only}
        findings = [f for f in findings if str(f.get("number")) in wanted]
        if not findings:
            raise RuntimeError(f"no findings match --only {only}")

    verdicts: dict[str, dict] = {}
    for f in findings:
        num = str(f.get("number", "?"))
        try:
            verdicts[num] = await _run_one_finding(
                ctx.client, f, ctx.source_blob, ctx.workspace, ctx.workspace_info
            )
        except KeyboardInterrupt:
            print(f"\n[poc] interrupted at finding #{num}")
            break
        except Exception as e:
            print(f"[poc] ✗  finding #{num} unexpected error: {type(e).__name__}: {e}")
            verdicts[num] = {"status": "RUNNER_ERROR", "error": str(e), "attempts": []}

    print("\n[poc] === summary ===")
    for num, v in verdicts.items():
        icon = {"VALID": "✅", "WORTH_TO_CHECK": "⏸", "REFUSED": "🚫"}.get(v["status"], "?")
        print(f"  {icon} #{num}: {v['status']}")

    # Save the successful tests of VALID PoCs to a separate directory, for convenient delivery
    poc_dir = _save_valid_pocs(ctx.report_path, ctx.workspace, verdicts)
    if poc_dir is not None:
        n_valid = sum(1 for v in verdicts.values() if v.get("status") == "VALID")
        print(f"[poc] valid PoCs: {n_valid} saved to {poc_dir}/")

    out_path = _write_augmented_report(ctx.report_path, verdicts)
    print(f"[poc] augmented report: {out_path}")
    return out_path


async def run_verification(
    report_path: Path,
    workspace: Path,
    only: list[int] | None = None,
    skip_build: bool = False,
    log_file: Optional[Path] = None,
    programs: list[str] | None = None,
) -> Path:
    """CLI wrapper: prepare_verification + verify_findings, plain-print progress."""
    print(f"[poc] report:    {report_path}")
    print(f"[poc] workspace: {workspace}")

    def _cli_step(step: str, status: str, detail: str) -> None:
        label = {
            "preflight":      "toolchain",
            "provision":      "provision",
            "rust_ver":       "rustc",
            "solana_ver":     "solana",
            "platform_tools": "platform-tools",
            "anchor_ver":     "anchor version",
            "build":          "anchor build",
            "extract":        "extract findings",
            "detect":         "detect programs",
            "collect":        "collect source",
            "client":         "connect client",
            "log":            "log",
        }.get(step, step)
        if status == "start":
            print(f"[poc] {label}… {detail}")
        elif status == "ok":
            print(f"[poc] {label}: {detail}")
        elif status == "fail":
            print(f"[poc] {label} FAILED: {detail}")

    try:
        ctx = await prepare_verification(
            report_path, workspace, skip_build=skip_build, log_file=log_file,
            on_step=_cli_step, programs=programs,
        )
        return await verify_findings(ctx, only=only)
    finally:
        _teardown_run_log()


async def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="Path to audit-*.md")
    ap.add_argument("workspace", help="Path to cloned Anchor project (where audit ran)")
    ap.add_argument("--only", type=int, nargs="*", help="Only run these finding numbers")
    ap.add_argument("--skip-build", action="store_true", help="Skip `anchor build` (assume already built)")
    ap.add_argument("--programs", "-p", type=str, nargs="+", default=None, metavar="NAME",
                    help="Build only the specified programs (the names in Anchor.toml [programs]), "
                         "separated by spaces. e.g. --programs conditional_vault futarchy. "
                         "Default: build all programs in the workspace.")
    ap.add_argument("--log-file", type=str, default=None,
                    help="Path for the per-run log (default ~/.solana-spear/logs/<workspace>-<ts>.log)")
    ap.add_argument("--no-log", action="store_true", help="Disable per-run log file")
    args = ap.parse_args()

    report_path = Path(args.report).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()

    log_file: Optional[Path] = None
    if args.no_log:
        log_file = Path("/dev/null")
    elif args.log_file:
        log_file = Path(args.log_file).expanduser()

    try:
        await run_verification(
            report_path, workspace, args.only, args.skip_build,
            log_file=log_file, programs=args.programs,
        )
    except RuntimeError as e:
        print(f"[poc] fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())
