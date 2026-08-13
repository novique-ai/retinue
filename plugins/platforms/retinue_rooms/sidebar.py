"""Sidebar layout — room order, team separators, agent order.

Persisted as ``$HERMES_HOME/retinue_sidebar.json`` so grouping is not
only a browser localStorage preference. Team membership is implied by
position: an agent belongs to the nearest preceding team separator.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from typing import Any, Dict, Iterable, List, Optional

SIDEBAR_FILENAME = "retinue_sidebar.json"

_TEAM_SLUG_RE = re.compile(r"[^a-z0-9]+")
_lock = threading.Lock()


def _empty() -> Dict[str, Any]:
    return {"rooms": [], "items": []}


def new_team_id(label: str) -> str:
    slug = _TEAM_SLUG_RE.sub("-", (label or "team").lower()).strip("-")[:24] or "team"
    return f"{slug}-{uuid.uuid4().hex[:4]}"


def layout_path(home_dir: str) -> str:
    return os.path.join(home_dir, SIDEBAR_FILENAME)


def load(home_dir: str) -> Dict[str, Any]:
    path = layout_path(home_dir)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _empty()
    return normalize(raw)


def save(home_dir: str, layout: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = normalize(layout)
    path = layout_path(home_dir)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
        os.replace(tmp, path)
    return cleaned


def normalize(raw: Any) -> Dict[str, Any]:
    """Coerce a client/disk payload into the persisted shape."""
    if not isinstance(raw, dict):
        return _empty()
    rooms: List[str] = []
    seen_rooms = set()
    for item in raw.get("rooms") or []:
        rid = str(item or "").strip()
        if not rid or rid in seen_rooms:
            continue
        rooms.append(rid)
        seen_rooms.add(rid)

    items: List[Dict[str, Any]] = []
    seen_agents: set[str] = set()
    seen_teams: set[str] = set()
    incoming = raw.get("items")
    if not isinstance(incoming, list):
        incoming = _legacy_items(raw)
    for item in incoming:
        parsed = _parse_item(item)
        if parsed is None:
            continue
        if parsed["kind"] == "team":
            if parsed["id"] in seen_teams:
                continue
            seen_teams.add(parsed["id"])
            items.append(parsed)
        else:
            if parsed["slug"] in seen_agents:
                continue
            seen_agents.add(parsed["slug"])
            items.append({"kind": "agent", "slug": parsed["slug"]})
    return {"rooms": rooms, "items": items}


def _legacy_items(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept the older {teams, agents:[{slug, team}]} shape if we see it."""
    teams = raw.get("teams") or []
    agents = raw.get("agents") or []
    if not isinstance(teams, list) and not isinstance(agents, list):
        return []
    by_team: Dict[Optional[str], List[str]] = {}
    order: List[Optional[str]] = []
    team_labels: Dict[str, str] = {}
    for team in teams if isinstance(teams, list) else []:
        if not isinstance(team, dict):
            continue
        tid = str(team.get("id") or "").strip()
        label = str(team.get("label") or tid).strip()
        if not tid:
            continue
        team_labels[tid] = label or tid
        if tid not in by_team:
            by_team[tid] = []
            order.append(tid)
    if None not in by_team:
        by_team[None] = []
        order.insert(0, None)
    for agent in agents if isinstance(agents, list) else []:
        if not isinstance(agent, dict):
            continue
        slug = str(agent.get("slug") or "").strip()
        if not slug:
            continue
        team = agent.get("team")
        team_id = str(team).strip() if team else None
        if team_id and team_id not in by_team:
            by_team[team_id] = []
            order.append(team_id)
            team_labels.setdefault(team_id, team_id)
        by_team.setdefault(team_id, []).append(slug)
    items: List[Dict[str, Any]] = []
    for tid in order:
        if tid is not None:
            items.append({"kind": "team", "id": tid, "label": team_labels.get(tid, tid)})
        for slug in by_team.get(tid, []):
            items.append({"kind": "agent", "slug": slug})
    return items


def _parse_item(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    if kind == "team" or (not kind and (item.get("id") or item.get("label"))):
        label = str(item.get("label") or "").strip()
        tid = str(item.get("id") or "").strip() or (new_team_id(label) if label else "")
        if not tid or not label:
            return None
        return {"kind": "team", "id": tid, "label": label}
    if kind in ("agent", "", "bot"):
        slug = str(item.get("slug") or item.get("id") or "").strip()
        if not slug:
            return None
        return {"kind": "agent", "slug": slug}
    return None


def resolve(
    layout: Dict[str, Any],
    room_ids: Iterable[str],
    agent_slugs: Iterable[str],
) -> Dict[str, Any]:
    """Keep persisted order, drop unknowns, append anything new."""
    known_rooms = [str(r) for r in room_ids if r]
    known_agents = [str(a) for a in agent_slugs if a]
    room_set = set(known_rooms)
    agent_set = set(known_agents)
    rooms = [r for r in layout.get("rooms") or [] if r in room_set]
    rooms.extend(r for r in known_rooms if r not in rooms)

    items: List[Dict[str, Any]] = []
    seen_agents: set[str] = set()
    for item in layout.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "team":
            items.append(
                {
                    "kind": "team",
                    "id": str(item.get("id") or ""),
                    "label": str(item.get("label") or item.get("id") or "team"),
                }
            )
            continue
        slug = str(item.get("slug") or "")
        if slug in agent_set and slug not in seen_agents:
            items.append({"kind": "agent", "slug": slug})
            seen_agents.add(slug)
    for slug in known_agents:
        if slug not in seen_agents:
            items.append({"kind": "agent", "slug": slug})
            seen_agents.add(slug)
    return {"rooms": rooms, "items": items}


def team_for_agents(items: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Nearest preceding team id for each agent slug (None if ungrouped)."""
    current: Optional[str] = None
    out: Dict[str, Optional[str]] = {}
    for item in items:
        if item.get("kind") == "team":
            current = str(item.get("id") or "") or None
            continue
        slug = str(item.get("slug") or "")
        if slug:
            out[slug] = current
    return out


def load_resolved(
    home_dir: str, room_ids: Iterable[str], agent_slugs: Iterable[str]
) -> Dict[str, Any]:
    return resolve(load(home_dir), room_ids, agent_slugs)
