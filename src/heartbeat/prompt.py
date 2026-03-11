"""Heartbeat prompt builder.

Combines git activity data + Obsidian project metadata + Google Calendar events
for Claude to generate a smart daily focus message.
"""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timedelta
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


def _obsidian_goals_path() -> Path:
    """Resolve Obsidian Goals path from env or common locations."""
    if env := os.environ.get("OBSIDIAN_GOALS_PATH"):
        return Path(env)
    for candidate in [
        Path.home() / "Dropbox" / "Obsidian" / "Goals",
        Path.home() / "projects" / "obsidian" / "Goals",
    ]:
        if candidate.is_dir():
            return candidate
    return Path.home() / "Dropbox" / "Obsidian" / "Goals"


def _extract_tasks(filepath: Path) -> List[Dict[str, Any]]:
    """Extract task checkboxes from a markdown file."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    tasks = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("- [ ] "):
            tasks.append({"text": line[6:], "done": False})
        elif line.startswith("- [x] "):
            tasks.append({"text": line[6:], "done": True})
    return tasks


def _resolve_project_link(raw: str) -> str:
    """Extract project name from Obsidian link like '[[Moral Code]]'."""
    match = re.match(r'\[\[(.+?)]]', raw.strip('" '))
    return match.group(1) if match else raw.strip('" ')


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


def gather_obsidian_goals() -> List[Dict[str, Any]]:
    """Read all Obsidian goal files with their tasks."""
    goals = []
    goals_path = _obsidian_goals_path()
    if not goals_path.is_dir():
        return goals

    for f in sorted(goals_path.glob("*.md")):
        meta = parse_frontmatter(f)
        if not meta or meta.get("type") != "goal":
            continue

        project_name = ""
        if meta.get("project"):
            project_name = _resolve_project_link(str(meta["project"]))

        tasks = _extract_tasks(f)
        open_tasks = [t for t in tasks if not t["done"]]

        if not open_tasks:
            continue  # skip completed goals

        goals.append({
            "name": f.stem,
            "project": project_name,
            "status": meta.get("status", "not started"),
            "importance": meta.get("importance", 0) or 0,
            "complexity": meta.get("complexity", 0) or 0,
            "tasks": open_tasks,
        })

    return goals


async def gather_calendar_events() -> str:
    """Fetch today's Google Calendar events via gws CLI."""
    if not shutil.which("gws"):
        return "(Google Calendar not configured)"

    today = datetime.now().strftime("%Y-%m-%dT00:00:00Z")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    try:
        proc = await asyncio.create_subprocess_exec(
            "gws", "calendar", "events", "list",
            "--params", json.dumps({
                "calendarId": "primary",
                "timeMin": today,
                "timeMax": tomorrow,
                "singleEvents": True,
                "orderBy": "startTime",
            }),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return f"(calendar error: {stderr.decode()[:100]})"

        data = json.loads(stdout.decode())
        items = data.get("items", [])
        if not items:
            return "No events today — clear schedule for deep work!"

        lines = []
        for event in items:
            summary = event.get("summary", "Untitled")
            start = event.get("start", {})
            time_str = start.get("dateTime", start.get("date", ""))
            if "T" in time_str:
                time_str = time_str[11:16]  # HH:MM
            lines.append(f"• {time_str} — {summary}")

        return "\n".join(lines)
    except Exception as e:
        return f"(calendar unavailable: {e})"


HEARTBEAT_PROMPT = """You are a personal project coach. Analyze the data below and write a Telegram message (max 300 words).

## CALENDAR (today's schedule)
{calendar_data}

## PROJECTS (ranked by priority)
{obsidian_data}

## GOALS & TASKS (milestones within projects, sorted by project priority x goal importance)
{goals_data}

## GIT ACTIVITY (what's actually happening)
{git_report}

## YOUR JOB

Drill down: Best Project → Best Goal → Highest Priority Task.
Suggest 1-3 SPECIFIC TASKS to do TODAY, not vague project names.

Rules:
- Pick tasks from goals belonging to the highest-priority projects
- Prefer tasks in projects with uncommitted changes (finish what's started)
- If a high-priority project is cooling down, nudge with its "why"
- If spread too thin (5+ active), suggest parking one
- NEVER list all projects. Focus on what matters TODAY.

## FORMAT (strict)

🌅 Good morning!

📅 Today's schedule:
• event time — event name (only if calendar has events; skip section if empty)

🎯 Today's tasks:
1. [emoji] **Project → Goal** — specific task
2. [emoji] **Project → Goal** — specific task

⚠️ Heads up:
• one-liner about risk/cooldown/spread

💡 Tip:
• one motivational or strategic insight

Use relevant emojis (🔥 momentum, 🧊 cooling, 📦 uncommitted, 🚀 launch, 🎸 music, 💰 earnings).
Tone: friendly coach, casual, direct. Can mix English and Russian. No guilt trips.
Keep it scannable — short lines, not paragraphs.
If calendar is empty, emphasize it's a great day for deep work."""


async def build_heartbeat_prompt() -> str:
    """Build the full prompt with calendar + git activity + Obsidian project/goal data."""
    git_report = generate_heartbeat_report()
    obsidian_projects = gather_obsidian_projects()
    obsidian_goals = gather_obsidian_goals()
    calendar_data = await gather_calendar_events()

    # Build project priority lookup
    project_priority = {p["name"]: p["priority"] for p in obsidian_projects}

    # Format projects concisely
    obsidian_lines = []
    for p in obsidian_projects:
        parts = [f"{p['name']} (priority={p['priority']}, status={p['status']})"]
        if p["why"]:
            parts.append(f"  why: {p['why']}")
        if p["next_action"]:
            parts.append(f"  next: {p['next_action']}")
        obsidian_lines.append("\n".join(parts))

    obsidian_data = "\n".join(obsidian_lines) if obsidian_lines else "(no scores yet)"

    # Sort goals by project priority * goal importance, format with tasks
    for g in obsidian_goals:
        g["effective_priority"] = project_priority.get(g["project"], 0) * g["importance"]

    sorted_goals = sorted(obsidian_goals, key=lambda g: g["effective_priority"], reverse=True)

    goal_lines = []
    for g in sorted_goals:
        proj_pri = project_priority.get(g["project"], 0)
        header = f"{g['project']} → {g['name']} (project_pri={proj_pri}, importance={g['importance']}, effective={g['effective_priority']})"
        task_list = "\n".join(f"  - [ ] {t['text']}" for t in g["tasks"])
        goal_lines.append(f"{header}\n{task_list}")

    goals_data = "\n".join(goal_lines) if goal_lines else "(no goals with open tasks)"

    return HEARTBEAT_PROMPT.format(
        calendar_data=calendar_data,
        obsidian_data=obsidian_data,
        goals_data=goals_data,
        git_report=git_report,
    )


WEEKLY_REVIEW_PROMPT = """You are a personal project coach doing a WEEKLY REVIEW. Analyze everything below and write a Telegram message (max 500 words).

## PROJECTS (ranked by priority)
{obsidian_data}

## GOALS & TASKS
{goals_data}

## GIT ACTIVITY (this week)
{git_report}

## YOUR JOB — WEEKLY REVIEW

This runs every Sunday. Help the user reflect and plan:

1. **What happened this week?**
   - Which projects got commits? How many?
   - Any goals completed or tasks checked off?
   - Any projects that went cold?

2. **Score check — are priorities still right?**
   - If a project with high priority got zero commits, ask why — lost interest? blocked? Should scores change?
   - If a low-priority project got lots of commits, maybe it deserves higher scores?
   - Flag any projects with priority=0 that are active

3. **Goals check**
   - Any goals with all tasks done? Suggest marking as done and creating next milestone
   - Any goals stale for 2+ weeks? Should they be revised or dropped?

4. **Plan next week**
   - Suggest top 2-3 goals to focus on
   - Recommend one project to park if spread too thin

## FORMAT (strict)

📊 Weekly Review

🔙 This week:
• project: X commits, [what happened]
• project: went cold

🎯 Scores check:
• any mismatches between priority and actual work?
• suggest specific score changes if needed

✅ Goals update:
• completed / stale / on track

📋 Next week focus:
1. [emoji] **Project → Goal** — why
2. [emoji] **Project → Goal** — why

Tone: reflective, honest, constructive. Mix English and Russian ok."""


def build_weekly_review_prompt() -> str:
    """Build the weekly review prompt."""
    git_report = generate_heartbeat_report()
    obsidian_projects = gather_obsidian_projects()
    obsidian_goals = gather_obsidian_goals()

    project_priority = {p["name"]: p["priority"] for p in obsidian_projects}

    obsidian_lines = []
    for p in obsidian_projects:
        parts = [f"{p['name']} (priority={p['priority']}, status={p['status']})"]
        if p["why"]:
            parts.append(f"  why: {p['why']}")
        obsidian_lines.append("\n".join(parts))

    obsidian_data = "\n".join(obsidian_lines) if obsidian_lines else "(no scores yet)"

    for g in obsidian_goals:
        g["effective_priority"] = project_priority.get(g["project"], 0) * g["importance"]

    sorted_goals = sorted(obsidian_goals, key=lambda g: g["effective_priority"], reverse=True)

    goal_lines = []
    for g in sorted_goals:
        proj_pri = project_priority.get(g["project"], 0)
        done = sum(1 for t in g["tasks"] if t.get("done"))
        total = len(g["tasks"]) + done  # open_tasks + done tasks
        header = f"{g['project']} → {g['name']} (effective={g['effective_priority']}, {done}/{total} tasks done)"
        task_list = "\n".join(f"  - [ ] {t['text']}" for t in g["tasks"])
        goal_lines.append(f"{header}\n{task_list}")

    goals_data = "\n".join(goal_lines) if goal_lines else "(no goals with open tasks)"

    return WEEKLY_REVIEW_PROMPT.format(
        obsidian_data=obsidian_data,
        goals_data=goals_data,
        git_report=git_report,
    )
