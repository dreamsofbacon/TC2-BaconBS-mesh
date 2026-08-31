import logging
import os
import subprocess
from pathlib import Path


# The patch number is the length of the mainline, so every commit is a
# distinct version with nothing to remember. APP_VERSION sat at "0.1.6" for
# 171 commits because it was a literal nobody edited and no automation
# touched.
APP_VERSION_BASE = "0.1"

# Counted with --first-parent, which is what makes the number mean the same
# thing in two different clones.
#
# Counting every reachable commit instead (plain `rev-list --count HEAD`)
# counts whatever each clone happens to have merged in, so a fork with its
# own commits reports a different number for byte-identical files. That is
# not hypothetical: it is what someone running this from a fork actually
# saw. This repository's own two counts disagree by 81, because 49 merge
# commits brought side branches along with them.
#
# The commit hash remains the real identity. The number is an ordinal for
# telling at a glance which of two builds is newer.
_FIRST_PARENT_ONLY = True

# Added to the mainline count. Only reason it exists: the previous scheme
# counted every ancestor and had already reached 415, while the mainline was
# 334 -- so switching would have made every deployed node appear to go
# backwards by 81. This lands the changeover on 0.1.500 and keeps the
# sequence monotonic.
#
# Never change this again. Changing it renumbers every past and future
# release, which is the exact confusion it was added to end.
_BUILD_OFFSET = 166

# Used only when the count cannot be determined at all -- no git and no
# cached stamp, e.g. an install from a zip. Refreshed by the cache write
# below whenever a real count IS available, so a deployed node keeps a
# truthful number even if git later becomes unusable.
_FALLBACK_BUILD = 500

_BUILD_CACHE = "_build_version.py"

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
            "Version: running commit unknown (%s). Set BBS_GIT_COMMIT to "
            "label this install.", _resolution_note,
        )
    return ""


def get_version_resolution_note() -> str:
    """Why the commit is missing, or '' when it resolved."""
    return _resolution_note


def _cached_build_number() -> int:
    """Last commit count this install successfully resolved.

    Deliberately a generated file rather than a committed one: a committed
    count would change on every commit, which changes the count. This is
    written on any run that can read git and read on any run that cannot,
    so a node keeps a truthful version after being packaged, moved, or run
    somewhere git no longer works.
    """
    try:
        path = _repo_root() / _BUILD_CACHE
        if not path.is_file():
            return 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("BUILD ="):
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 0


def _write_build_cache(build: int) -> None:
    try:
        lines = [
            "# Generated. The commit count this install last resolved.",
            f"BUILD = {int(build)}",
            "",
        ]
        (_repo_root() / _BUILD_CACHE).write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass  # read-only install; the live git value is still used


def _count_command() -> list:
    command = ["git", "-c", f"safe.directory={_repo_root()}",
               "rev-list", "--count"]
    if _FIRST_PARENT_ONLY:
        command.append("--first-parent")
    command.append("HEAD")
    return command


def get_build_number() -> int:
    """Mainline commit count plus the offset: the patch number."""
    env_build = str(os.getenv("BBS_BUILD_NUMBER", "")).strip()
    if env_build.isdigit():
        return int(env_build)

    try:
        result = subprocess.run(
            _count_command(),
            cwd=str(_repo_root()), capture_output=True, text=True,
            timeout=5, check=False,
        )
        count = str(result.stdout or "").strip()
        if result.returncode == 0 and count.isdigit() and int(count) > 0:
            build = int(count) + _BUILD_OFFSET
            if build != _cached_build_number():
                _write_build_cache(build)
            return build
    except Exception:
        pass

    cached = _cached_build_number()
    return cached if cached else _FALLBACK_BUILD


def get_app_version() -> str:
    return f"{APP_VERSION_BASE}.{get_build_number()}"


def get_display_version() -> str:
    override = str(os.getenv("BBS_VERSION_DISPLAY", "")).strip()
    if override:
        return override

    version = get_app_version()
    commit = get_git_commit_short()
    if commit:
        return f"v{version} ({commit})"
    return f"v{version}"


# Back-compat: some callers and tests read APP_VERSION directly.
APP_VERSION = get_app_version()


if __name__ == "__main__":
    # So docker/build.sh and the publish workflow ask THIS module for the
    # number instead of each re-implementing the git command. Three
    # implementations is how an image ends up numbered differently from the
    # source it was built from.
    import sys

    if "--build-number" in sys.argv:
        print(get_build_number())
    elif "--commit" in sys.argv:
        print(get_git_commit_short())
    else:
        print(get_display_version())
