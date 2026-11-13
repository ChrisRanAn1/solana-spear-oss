from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"

MODEL_ALIASES = {
    # Anthropic
    "opus5.5":  "claude-opus-5-5",
    "opus5":    "claude-opus-5",
    "opus":     "claude-opus-4-7",
    "opus4.7":  "claude-opus-4-7",
    "sonnet5":  "claude-sonnet-5",
    "sonnet":   "claude-sonnet-5",
    "haiku":    "claude-haiku-4-5-20251001",
    "haiku4.5": "claude-haiku-4-5-20251001",
    "fable":    "claude-fable-5",
    "fable5":   "claude-fable-5",
    # OpenAI GPT-5.6 (flagship / balanced / cheap)
    "sol":      "gpt-5.6-sol",
    "5.6":      "gpt-5.6-sol",
    "gpt5.6":   "gpt-5.6-sol",
    "terra":    "gpt-5.6-terra",
    "5.6-terra":"gpt-5.6-terra",
    "luna":     "gpt-5.6-luna",
    "5.6-luna": "gpt-5.6-luna",
    # OpenAI older / cheaper
    "5.5":      "gpt-5.5",
    "5.4":      "gpt-5.4",
    "5.4-mini": "gpt-5.4-mini",
    "codex":    "gpt-5.3-codex-spark",
    "4o":       "gpt-4o",
    "gpt4o":    "gpt-4o",
    "o3":       "o3",
    "o4-mini":  "o4-mini",
}

MAX_TOKENS_AGENT = 65536
MAX_TOKENS_JUDGE = 128000
MAX_TOKENS_DEDUP = 8192
BACKOFF_ON_RATE_LIMIT_SEC = 30
STALL_LIMIT_SEC = 600
MAX_SOURCE_TOKENS_WARN = 180_000
MAX_SOURCE_TOKENS_HARD = 220_000

HARNESS_ROOT = Path(__file__).parent
PROMPTS_DIR = HARNESS_ROOT / "prompts"
JUDGE_PROMPT = HARNESS_ROOT / "judging.md"
DEDUP_PROMPT = PROMPTS_DIR / "dedup.md"
SHARED_RULES = PROMPTS_DIR / "shared-rules.md"

DESKTOP_DIR = Path.home() / "Desktop"
CONFIG_DIR = Path.home() / ".solana-spear"
CONFIG_FILE = CONFIG_DIR / "config.json"

AGENTS = {
    "Arithmetic":        "Arithmetic.md",
    "AccessControl":     "access-control-agent.md",
    "AccountValidation": "account-validation-agent.md",
    "CPI":               "cpi-cross-program-agent.md",
    "SPL-Token2022":     "spl-and-token2022-agent.md",
    "Economic":          "Economic-agent.md",
    "Invariant":         "invariant-agent.md",
    "MoneyFlow":         "money-flow-agent.md",
}

EXCLUDE_PATH_SEGMENTS = {
    "tests", "test", "benches", "examples", "fuzz",
    "target", ".anchor", "migrations", ".git", "node_modules",
}

EXCLUDE_FILE_REGEXES = [
    r"^tests\.rs$",
    r"^test_.*\.rs$",
    r".*_test\.rs$",
    r".*_tests\.rs$",
    r"^error\.rs$",
    r"^errors\.rs$",
    r"^event\.rs$",
    r"^events\.rs$",
]
