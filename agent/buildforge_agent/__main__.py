import argparse
import json
import logging
import sys

from .config import Settings
from .engine import Engine
from .server import serve
from .store import Store


def main() -> int:
    parser = argparse.ArgumentParser(prog="buildforge_agent")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the Build Forge service (default)")
    check = sub.add_parser("check", help="test the incident log folder, the runner, the Kiro agent and Teams")
    check.add_argument("--teams", action="store_true", help="also post a test card to the Teams channel")
    check.add_argument("--no-kiro", action="store_true", help="don't spend a Kiro turn on a test analysis")
    extract = sub.add_parser("extract", help="show what would be sent to Kiro for a saved console log")
    extract.add_argument("logfile")
    show = sub.add_parser("show", help="print an incident log by its entry id")
    show.add_argument("id")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()

    if args.cmd == "extract":
        from . import errors
        text = open(args.logfile, encoding="utf-8", errors="replace").read()
        part = errors.extract(text, max_chars=s.max_error_chars)
        print(json.dumps({"stage": part["stage"], "node": part["node"], "stages": errors.stages(text, part["stage"]),
                          "chars": len(part["text"]), "of": len(text)}, indent=2))
        print("-" * 60)
        print(part["text"])
        return 0
    if args.cmd == "show":
        from .incidents import IncidentLog
        text = IncidentLog(s.log_dir).read(args.id)
        print(text or f"no incident log {args.id} in {s.log_dir}/incidents")
        return 0 if text else 1

    if not s.shared_secret:
        print("BF_SHARED_SECRET is not set (generate one with: openssl rand -hex 32)", file=sys.stderr)
        return 2
    engine = Engine(s, Store(s.data_dir / "buildforge.db"))
    if args.cmd == "check":
        from .checks import run_checks
        return run_checks(engine, post_teams=args.teams, ask_kiro=not args.no_kiro)
    n = engine.deck.backfill()
    if n:
        logging.getLogger("buildforge").info("solutions deck built from %d earlier analyses", n)
    engine.resume_pending()
    serve(engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
