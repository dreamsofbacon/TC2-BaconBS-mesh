import logging
import os
import subprocess
from pathlib import Path


APP_VERSION = "0.1.6"

# Why the commit could not be resolved, for the Diagnostics page. The commit
# is the only thing that distinguishes one build from the next -- APP_VERSION
# has not changed in a long time -- so an install that cannot resolve it
# shows the same version string forever, with nothing saying why.
_resolution_note = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


# git rev-parse --short yields 7 characters. The file-read path below is
# trimmed to match so an install does not appear to change version merely
# because of HOW its commit was resolved.
_SHORT_LEN = 7


def _commit_from_git_dir() -> str:
    """Read the commit straight out of .git, with no git binary involved.

    Preferred over shelling out because the two most common reasons a
    deployment shows no commit are that git is not installed at all, and
    that git refuses the repo with "detected dubious ownership" when the
    service user differs from the one that cloned it. Neither affects
    simply reading the files.
    """
    git_dir = _repo_root() / ".git"
    try:
        if git_dir.is_file():
            # A worktree or submodule: ".git" is a file pointing elsewhere.
            pointer = git_dir.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_dir = Path(pointer.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = (_repo_root() / git_dir).resolve()
        if not git_dir.is_dir():
            return ""

        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:_SHORT_LEN]  # detached HEAD holds the sha directly

        ref = head.split(":", 1)[1].strip()
        ref_path = git_dir / ref
        if ref_path.is_file():
            return ref_path.read_text(encoding="utf-8").strip()[:_SHORT_LEN]

        # Ref has been packed away by `git gc`.
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or " " not in line:
                    continue
                sha, name = line.split(" ", 1)
                if name.strip() == ref:
                    return sha.strip()[:_SHORT_LEN]
    except Exception:
        return ""
    return ""


def get_git_commit_short() -> str:
    global _resolution_note

    env_commit = str(os.getenv("BBS_GIT_COMMIT", "")).strip()
    if env_commit:
        _resolution_note = ""
        return env_commit[:12]

    from_files = _commit_from_git_dir()
    if from_files:
        _resolution_note = ""
        return from_files

    try:
        result = subprocess.run(
            # -c safe.directory survives "dubious ownership", which git
            # raises when the service user is not the one that cloned the
            # repo -- a normal state for a daemon, and otherwise silent.
            ["git", "-c", f"safe.directory={_repo_root()}",
             "rev-parse", "--short", "HEAD"],
            cwd=str(_repo_root()),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        commit = str(result.stdout or "").strip()
        if result.returncode == 0 and commit:
            _resolution_note = ""
            return commit
        _resolution_note = (str(result.stderr or "").strip().splitlines() or
                            ["git returned no commit"])[0][:200]
    except FileNotFoundError:
        _resolution_note = "git is not installed and .git could not be read"
    except Exception as exc:
        _resolution_note = f"{type(exc).__name__}: {exc}"[:200]

    if _resolution_note:
        logging.info(
            "Version: running commit unknown (%s). Every build will display "
            "%s. Set BBS_GIT_COMMIT to label this install.",
            _resolution_note, f"v{APP_VERSION}",
        )
    return ""


def get_version_resolution_note() -> str:
    """Why the commit is missing, or '' when it resolved."""
    return _resolution_note


def get_display_version() -> str:
    override = str(os.getenv("BBS_VERSION_DISPLAY", "")).strip()
    if override:
        return override

    commit = get_git_commit_short()
    if commit:
        return f"v{APP_VERSION} ({commit})"
    return f"v{APP_VERSION}"
