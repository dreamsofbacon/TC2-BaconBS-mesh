import os
import subprocess
from pathlib import Path


APP_VERSION = "0.1.6"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def get_git_commit_short() -> str:
    env_commit = str(os.getenv("BBS_GIT_COMMIT", "")).strip()
    if env_commit:
        return env_commit[:12]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        commit = str(result.stdout or "").strip()
        if result.returncode == 0 and commit:
            return commit
    except Exception:
        pass
    return ""


def get_display_version() -> str:
    override = str(os.getenv("BBS_VERSION_DISPLAY", "")).strip()
    if override:
        return override

    commit = get_git_commit_short()
    if commit:
        return f"v{APP_VERSION} ({commit})"
    return f"v{APP_VERSION}"