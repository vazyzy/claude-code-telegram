"""Project heartbeat data gatherer.

Scans ~/projects/ for git activity and file modifications,
producing a structured report for AI interpretation.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECTS_DIR = Path.home() / "projects"

# Skip work-related directories
SKIP_DIRS = {"gitlaw", "gitlaw-ai", "claude-marketplace", "skills", "eaglelaw-spec", "venv"}


def run_git(repo: Path, *args: str, timeout: int = 5) -> Optional[str]:
    """Run a git command in a repo, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def fetch_remote(repo: Path) -> None:
    """Fetch latest from all remotes (quiet, best-effort)."""
    run_git(repo, "fetch", "--all", "--quiet", timeout=15)


def get_git_info(repo: Path) -> Optional[Dict[str, Any]]:
    """Extract git activity info from a repo."""
    # Check both local and remote latest commit
    last_commit = run_git(repo, "log", "-1", "--format=%H|%aI|%s", "--all")
    if not last_commit:
        return None

    parts = last_commit.split("|", 2)
    if len(parts) < 3:
        return None

    commit_hash, commit_date_str, commit_msg = parts

    # Count commits in last 7 and 30 days (across all branches including remote)
    commits_7d = run_git(repo, "rev-list", "--count", "--since=7 days ago", "--all")
    commits_30d = run_git(repo, "rev-list", "--count", "--since=30 days ago", "--all")

    # Current branch
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    # Uncommitted changes
    status = run_git(repo, "status", "--porcelain")
    has_uncommitted = bool(status)

    # Check local vs remote divergence
    local_head = run_git(repo, "rev-parse", "HEAD")
    remote_head = run_git(repo, "rev-parse", f"origin/{branch}") if branch else None
    if local_head and remote_head and local_head != remote_head:
        ahead = run_git(repo, "rev-list", "--count", f"origin/{branch}..HEAD")
        behind = run_git(repo, "rev-list", "--count", f"HEAD..origin/{branch}")
        sync_status = {
            "ahead": int(ahead) if ahead else 0,
            "behind": int(behind) if behind else 0,
        }
    else:
        sync_status = None

    # Recent branches with activity (local + remote)
    branches_raw = run_git(
        repo, "for-each-ref", "--sort=-committerdate",
        "--format=%(refname:short)|%(committerdate:iso)",
        "--count=5", "refs/heads/", "refs/remotes/origin/"
    )
    recent_branches = []
    seen_branch_names = set()
    if branches_raw:
        for line in branches_raw.splitlines():
            b_parts = line.split("|", 1)
            if len(b_parts) == 2:
                bname = b_parts[0].replace("origin/", "")
                if bname not in seen_branch_names and bname != "HEAD":
                    seen_branch_names.add(bname)
                    recent_branches.append({"name": b_parts[0], "last_commit": b_parts[1].strip()})

    # Days since last commit
    try:
        commit_dt = datetime.fromisoformat(commit_date_str)
        days_since = (datetime.now(timezone.utc) - commit_dt).days
    except ValueError:
        days_since = -1

    result = {
        "last_commit_hash": commit_hash[:8],
        "last_commit_date": commit_date_str,
        "last_commit_message": commit_msg,
        "days_since_last_commit": days_since,
        "commits_last_7d": int(commits_7d) if commits_7d else 0,
        "commits_last_30d": int(commits_30d) if commits_30d else 0,
        "current_branch": branch,
        "has_uncommitted_changes": has_uncommitted,
        "recent_branches": recent_branches,
    }

    if sync_status:
        result["sync_status"] = sync_status

    return result


def get_dir_info(path: Path) -> Dict[str, Any]:
    """Get basic info for a non-git directory."""
    file_count = sum(1 for _ in path.rglob("*") if _.is_file())

    # Find most recently modified file
    latest_mtime = 0.0
    latest_file = ""
    for f in path.rglob("*"):
        if f.is_file() and not f.name.startswith("."):
            try:
                mt = f.stat().st_mtime
                if mt > latest_mtime:
                    latest_mtime = mt
                    latest_file = str(f.relative_to(path))
            except OSError:
                continue

    return {
        "file_count": file_count,
        "last_modified_file": latest_file,
        "last_modified_date": (
            datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
            if latest_mtime > 0 else None
        ),
    }


def gather_all_projects() -> List[Dict[str, Any]]:
    """Scan all personal projects and return structured data."""
    projects = []

    for entry in sorted(PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue

        project: Dict[str, Any] = {
            "name": entry.name,
            "path": str(entry),
        }

        if (entry / ".git").is_dir():
            project["type"] = "git"
            fetch_remote(entry)
            git_info = get_git_info(entry)
            if git_info:
                project.update(git_info)
        else:
            project["type"] = "directory"
            project.update(get_dir_info(entry))

        projects.append(project)

    return projects


def generate_heartbeat_report() -> str:
    """Generate a JSON report of all project activity."""
    projects = gather_all_projects()

    now = datetime.now(timezone.utc)

    # Classify projects by activity
    active = []
    cooling = []
    cold = []
    ideas = []

    for p in projects:
        if p["type"] == "directory":
            ideas.append(p)
        elif p.get("days_since_last_commit", 999) <= 7:
            active.append(p)
        elif p.get("days_since_last_commit", 999) <= 30:
            cooling.append(p)
        else:
            cold.append(p)

    report = {
        "generated_at": now.isoformat(),
        "summary": {
            "total": len(projects),
            "active": len(active),
            "cooling_down": len(cooling),
            "cold": len(cold),
            "ideas_no_git": len(ideas),
        },
        "active": active,
        "cooling_down": cooling,
        "cold": cold,
        "ideas": ideas,
    }

    return json.dumps(report, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    print(generate_heartbeat_report())
