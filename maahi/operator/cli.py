"""Command-line entrypoint for the Maahi Operator.

    python -m maahi.operator serve        # run the command-center (default)
    python -m maahi.operator brief        # print today's executive brief
    python -m maahi.operator status        # systems + config overview
    python -m maahi.operator chat "..."   # one-shot terminal chat
    python -m maahi.operator pending       # actions awaiting approval
    python -m maahi.operator approve <id>  # run a parked action
    python -m maahi.operator reject  <id>
    python -m maahi.operator doctor        # config + connector readiness
"""
from __future__ import annotations

import argparse
import sys


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port)
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    from .core import get_operator

    brief = get_operator().brief(synthesize=not args.no_synth)
    print(brief.markdown())
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    from .core import get_operator

    st = get_operator().status()
    print(f"Maahi Operator — owner {st['owner']}")
    print(f"  brain: {'online' if st['brain_online'] else 'OFFLINE (set ANTHROPIC_API_KEY)'}"
          f" | model {st['model']} | autonomy {st['autonomy']}")
    print(f"  systems: {st['configured_count']}/{st['total_count']} connected"
          f" | {st['tool_count']} tools | {st['pending_count']} pending")
    for c in st["connectors"]:
        mark = "●" if c["configured"] else "○"
        miss = "" if c["configured"] else f"  (set {', '.join(c['missing_env'])})"
        print(f"   {mark} {c['label']:<14} {c['capability_count']:>2} caps{miss}")
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    from .core import get_operator

    message = " ".join(args.message).strip()
    if not message:
        print("Say something: maahi chat \"what's my day look like\"")
        return 1
    op = get_operator()
    for ev in op.chat_stream(message):
        if ev.type == "text":
            sys.stdout.write(ev.data.get("text", ""))
            sys.stdout.flush()
        elif ev.type == "tool_start":
            sys.stderr.write(f"\n  · {ev.data.get('name')}…")
            sys.stderr.flush()
        elif ev.type == "confirm":
            sys.stderr.write(f"\n  ⏸ needs approval: {ev.data.get('summary')}")
        elif ev.type == "error":
            sys.stderr.write(f"\n[error] {ev.data.get('message')}\n")
    print()
    return 0


def _cmd_pending(_: argparse.Namespace) -> int:
    from .core import get_operator

    items = get_operator().pending()
    if not items:
        print("Nothing waiting for approval.")
        return 0
    for it in items:
        print(f"  {it['id']}  [{it['risk']}]  {it['summary']}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    from .core import get_operator

    print(get_operator().approve(args.id).get("summary", "done"))
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    from .core import get_operator

    print(get_operator().reject(args.id).get("summary", "rejected"))
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    from .config import get_operator_config
    from .core import get_operator

    cfg = get_operator_config()
    print("Maahi Operator — doctor\n")
    for k, v in cfg.redacted().items():
        print(f"  {k:16} {v}")
    print()
    if not cfg.has_brain:
        print("  ⚠ ANTHROPIC_API_KEY not set — chat + brief synthesis are offline.")
    st = get_operator().status()
    ready = [c["label"] for c in st["connectors"] if c["configured"]]
    notready = [(c["label"], c["missing_env"]) for c in st["connectors"]
                if not c["configured"]]
    print(f"\n  connected: {', '.join(ready) or 'none'}")
    for label, miss in notready:
        print(f"  to add {label}: set {', '.join(miss)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maahi.operator",
                                     description="Maahi — autonomous Chief of Staff")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the command-center server")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=_cmd_serve)

    p_brief = sub.add_parser("brief", help="print today's executive brief")
    p_brief.add_argument("--no-synth", action="store_true",
                         help="skip the Claude narrative")
    p_brief.set_defaults(func=_cmd_brief)

    sub.add_parser("status", help="systems + config overview").set_defaults(
        func=_cmd_status)

    p_chat = sub.add_parser("chat", help="one-shot terminal chat")
    p_chat.add_argument("message", nargs="+")
    p_chat.set_defaults(func=_cmd_chat)

    sub.add_parser("pending", help="actions awaiting approval").set_defaults(
        func=_cmd_pending)
    p_ok = sub.add_parser("approve", help="run a parked action")
    p_ok.add_argument("id")
    p_ok.set_defaults(func=_cmd_approve)
    p_no = sub.add_parser("reject", help="drop a parked action")
    p_no.add_argument("id")
    p_no.set_defaults(func=_cmd_reject)

    sub.add_parser("doctor", help="config + connector readiness").set_defaults(
        func=_cmd_doctor)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Default to serving the command-center.
        return _cmd_serve(argparse.Namespace(host=None, port=None))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
