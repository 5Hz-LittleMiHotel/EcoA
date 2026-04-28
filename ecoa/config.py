import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class ModelProfile:
    name: str
    client: Anthropic
    model: str


def _env(name: str, fallback: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return fallback
    return value


def _make_client(api_key: str | None, base_url: str | None) -> Anthropic:
    kwargs = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return Anthropic(**kwargs)


def _build_model_profile(prefix: str, default_model: str | None) -> ModelProfile:
    model = _env(f"{prefix}_MODEL_ID", default_model)
    if not model:
        raise RuntimeError(
            f"Missing model id for {prefix.lower()} profile. "
            f"Set {prefix}_MODEL_ID or MODEL_ID in .env."
        )
    api_key = _env(f"{prefix}_ANTHROPIC_API_KEY", _env("ANTHROPIC_API_KEY"))
    base_url = _env(f"{prefix}_ANTHROPIC_BASE_URL", _env("ANTHROPIC_BASE_URL"))
    return ModelProfile(
        name=prefix.lower(),
        client=_make_client(api_key, base_url),
        model=model,
    )


DEFAULT_MODEL = _env("MODEL_ID")
STRONG_PROFILE = _build_model_profile("STRONG", DEFAULT_MODEL)
WEAK_PROFILE = _build_model_profile("WEAK", DEFAULT_MODEL)
MODEL_PROFILES = {
    "strong": STRONG_PROFILE,
    "planner": STRONG_PROFILE,
    "reflection": STRONG_PROFILE,
    "weak": WEAK_PROFILE,
    "react": WEAK_PROFILE,
    "executor": WEAK_PROFILE,
}


def get_model_profile(profile: str | ModelProfile | None = None) -> ModelProfile:
    if isinstance(profile, ModelProfile):
        return profile
    key = (profile or "weak").lower()
    if key not in MODEL_PROFILES:
        valid = ", ".join(sorted(MODEL_PROFILES))
        raise ValueError(f"Unknown model profile '{profile}'. Valid: {valid}")
    return MODEL_PROFILES[key]


# Backward-compatible aliases for existing modules. New orchestration code should
# prefer explicit ModelProfile objects.
client = WEAK_PROFILE.client
MODEL = WEAK_PROFILE.model

WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TASKS_DIR = WORKDIR / ".tasks"
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

THRESHOLD = 50000
KEEP_RECENT = 3
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def detect_repo_root(cwd: Path) -> Path | None:
    """Return git repo root if cwd is inside a repo, else None."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        if r.returncode != 0:
            return None
        root = Path(r.stdout.strip())
        return root if root.exists() else None
    except Exception:
        return None


REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR
