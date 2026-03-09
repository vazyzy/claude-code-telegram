"""Heartbeat prompt builder.

Combines git activity data + Obsidian project metadata
for Claude to generate a smart daily focus message.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .gather import generate_heartbeat_report


def _obsidian_projects_path() -> Path:
    """Resolve Obsidian Projects path from env or common locations."""
    if env := os.environ.get("OBSIDIAN_PROJECTS_PATH"):
        return Path(env)
    # Try common locations
    for candidate in [
        Path.home() / "Dropbox" / "Obsidian" / "Projects",
        Path.home() / "projects" / "obsidian" / "Projects",
    ]:
        if candidate.is_dir():
            return candidate
    return Path.home() / "Dropbox" / "Obsidian" / "Projects"


def parse_frontmatter(filepath: Path) -> Optional[Dict[str, Any]]:
    """Extract YAML frontmatter from an Obsidian markdown file."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not match:
        return None

    try:
        return yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None


def gather_obsidian_projects() -> List[Dict[str, Any]]:
    """Read all Obsidian project files and return their metadata."""
    projects = []
    obsidian_path = _obsidian_projects_path()
    if not obsidian_path.is_dir():
        return projects

    for f in sorted(obsidian_path.glob("*.md")):
        if f.name == "Dashboard.md":
            continue

        meta = parse_frontmatter(f)
        if not meta or meta.get("type") != "project":
            continue

        interest = meta.get("interest", 0) or 0
        impact = meta.get("impact", 0) or 0
        earnings_plan = meta.get("earnings_plan", 0) or 0
        confidence = meta.get("confidence", 0) or 0
        effort = meta.get("effort", 1) or 1

        priority = round((interest * 2 + impact + earnings_plan + confidence) / effort, 1)

        projects.append({
            "name": f.stem,
            "status": meta.get("status", "unknown"),
            "domain": meta.get("domain", ""),
            "why": meta.get("why", ""),
            "next_action": meta.get("next_action", ""),
            "interest": interest,
            "impact": impact,
            "earnings_plan": earnings_plan,
            "confidence": confidence,
            "effort": effort,
            "priority": priority,
        })

    return sorted(projects, key=lambda p: p["priority"], reverse=True)


HEARTBEAT_PROMPT = """You are a personal project coach. Analyze the data below and write a short, actionable Telegram message (max 300 words, minimal formatting).

## OBSIDIAN PROJECT SCORES (user's priorities)
Priority = (Interest x2 + Impact + Earnings + Confidence) / Effort
{obsidian_data}

## GIT ACTIVITY (what's actually happening)
{git_report}

## YOUR JOB

1. Suggest 1-3 projects to focus on TODAY. Pick based on:
   - Highest priority score + recent momentum (commits)
   - If a project has a next_action, suggest that specifically
   - Prefer projects with uncommitted changes (finish what's started)

2. If a high-priority project is cooling down (no commits in 7+ days but had momentum), give a gentle nudge with the "why" from their notes

3. If the user seems spread too thin (5+ active projects with commits this week), suggest parking one

4. NEVER list all projects. Focus on what matters TODAY.

Tone: friendly coach, casual, direct. Can mix English and Russian. No guilt trips."""


def build_heartbeat_prompt() -> str:
    """Build the full prompt with git activity + Obsidian scores."""
    git_report = generate_heartbeat_report()
    obsidian_projects = gather_obsidian_projects()

    # Format obsidian data concisely
    obsidian_lines = []
    for p in obsidian_projects:
        parts = [f"{p['name']} (priority={p['priority']}, status={p['status']})"]
        if p["why"]:
            parts.append(f"  why: {p['why']}")
        if p["next_action"]:
            parts.append(f"  next: {p['next_action']}")
        obsidian_lines.append("\n".join(parts))

    obsidian_data = "\n".join(obsidian_lines) if obsidian_lines else "(no scores filled in yet)"

    return HEARTBEAT_PROMPT.format(
        obsidian_data=obsidian_data,
        git_report=git_report,
    )
