"""Reference CLI for the Retinue rooms HTTP API.

Usage (env: RETINUE_ROOMS_URL, RETINUE_ROOMS_API_KEY):

    python -m plugins.platforms.retinue_rooms.cli create "Ops room" --members scout,editor --lead scout
    python -m plugins.platforms.retinue_rooms.cli list
    python -m plugins.platforms.retinue_rooms.cli send <room-id> "@scout what changed today?"
    python -m plugins.platforms.retinue_rooms.cli watch <room-id>
    python -m plugins.platforms.retinue_rooms.cli chat <room-id>   # interactive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.request


def _base_url() -> str:
    return (os.getenv("RETINUE_ROOMS_URL") or "http://127.0.0.1:8643").rstrip("/")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = _base_url() + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    key = (os.getenv("RETINUE_ROOMS_API_KEY") or "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310 — operator-configured URL
        return json.loads(resp.read().decode("utf-8"))


def _print_message(msg: dict) -> None:
    kind = msg.get("kind")
    speaker = msg.get("speaker", "?")
    label = {"agent": f"{speaker} (agent)", "system": "· room"}.get(kind, speaker)
    print(f"[{label}] {msg.get('text', '')}")


def _watch(room_id: str, since: int, stop: threading.Event | None = None) -> None:
    while stop is None or not stop.is_set():
        data = _request("GET", f"/rooms/{room_id}/transcript?since={since}&wait=25")
        for msg in data.get("messages", []):
            _print_message(msg)
            since = max(since, int(msg.get("seq", since)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retinue-rooms")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a room")
    p_create.add_argument("name")
    p_create.add_argument("--members", required=True, help="comma-separated profile names")
    p_create.add_argument("--lead", default=None)
    p_create.add_argument("--max-agent-turns", type=int, default=None)

    sub.add_parser("list", help="list rooms")

    p_send = sub.add_parser("send", help="post a user message")
    p_send.add_argument("room")
    p_send.add_argument("text")
    p_send.add_argument("--from", dest="from_name", default=os.getenv("USER", "User"))

    p_watch = sub.add_parser("watch", help="stream the transcript")
    p_watch.add_argument("room")
    p_watch.add_argument("--since", type=int, default=0)

    p_chat = sub.add_parser("chat", help="interactive send+watch")
    p_chat.add_argument("room")
    p_chat.add_argument("--from", dest="from_name", default=os.getenv("USER", "User"))

    args = parser.parse_args(argv)

    if args.cmd == "create":
        result = _request(
            "POST",
            "/rooms",
            {
                "name": args.name,
                "members": [m.strip() for m in args.members.split(",") if m.strip()],
                "lead": args.lead,
                "max_agent_turns": args.max_agent_turns,
            },
        )
        print(json.dumps(result, indent=2))
    elif args.cmd == "list":
        for room in _request("GET", "/rooms").get("rooms", []):
            lead = f" lead={room['lead']}" if room.get("lead") else ""
            print(f"{room['id']}  \"{room['name']}\"  members={','.join(room['members'])}{lead}")
    elif args.cmd == "send":
        result = _request(
            "POST", f"/rooms/{args.room}/messages", {"text": args.text, "from": args.from_name}
        )
        print(f"sent (seq {result.get('seq')}), planned turns: {result.get('planned')}")
    elif args.cmd == "watch":
        try:
            _watch(args.room, args.since)
        except KeyboardInterrupt:
            pass
    elif args.cmd == "chat":
        stop = threading.Event()
        room_meta = _request("GET", f"/rooms/{args.room}")
        print(f"— room \"{room_meta.get('name')}\" · members: {', '.join(room_meta.get('members', []))}")
        print("— type a message and press enter; ctrl-d or ctrl-c to leave")
        watcher = threading.Thread(target=_watch, args=(args.room, 0, stop), daemon=True)
        watcher.start()
        try:
            for line in sys.stdin:
                text = line.strip()
                if text:
                    _request(
                        "POST",
                        f"/rooms/{args.room}/messages",
                        {"text": text, "from": args.from_name},
                    )
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
