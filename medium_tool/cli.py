from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .browser import BrowserSession, ManualIntervention
from .config import Settings
from .db import Database, utcnow
from .discovery import PublicationDiscovery
from .importer import StoryImporter
from .matching import MatchingEngine
from .reporting import Reporter
from .workflows import WorkflowError, Workflows


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Private Medium publication assistant")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create database and local directories")
    sub.add_parser("sync-stories", help="Import stories from configured Medium profile")
    discover = sub.add_parser("discover-publications", help="Discover and verify publications")
    discover.add_argument("--source", action="append", help="Additional/alternate discovery source URL")
    sub.add_parser("match", help="Score every story-publication pair")
    sub.add_parser("review", help="Show eligible matches awaiting review")
    approve = sub.add_parser("approve", help="Approve one exact story-publication match")
    approve.add_argument("match_id", type=int)
    reject = sub.add_parser("reject", help="Reject one exact story-publication match")
    reject.add_argument("match_id", type=int)
    apply = sub.add_parser("apply", help="Prepare or explicitly approve/send a writer application")
    apply.add_argument("publication_id", type=int)
    apply.add_argument("--approve", action="store_true", help="Approve the exact displayed application draft")
    apply.add_argument("--send", action="store_true", help="Send an already-approved application (live mode only)")
    submit = sub.add_parser("submit", help="Submit an explicitly approved match (live mode only)")
    submit.add_argument("match_id", type=int)
    sub.add_parser("status", help="Show database and queue counts")
    report = sub.add_parser("report", help="Generate the dry-run Markdown report")
    report.add_argument("--output", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings.load()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.migrate()
    try:
        if args.command == "init":
            result = {"database": str(settings.db_path), "dry_run": settings.dry_run}
        elif args.command == "sync-stories":
            result = asyncio.run(_sync(settings, db))
        elif args.command == "discover-publications":
            result = asyncio.run(_discover(settings, db, args.source))
        elif args.command == "match":
            matches = MatchingEngine(settings, db).run()
            result = {
                "scored": len(matches),
                "eligible": sum(1 for item in matches if item["eligible"]),
                "rejected": sum(1 for item in matches if not item["eligible"]),
            }
        elif args.command == "review":
            result = Workflows(settings, db).review()
        elif args.command == "approve":
            result = Workflows(settings, db).approve(args.match_id)
        elif args.command == "reject":
            result = Workflows(settings, db).reject(args.match_id)
        elif args.command == "apply":
            result = _application_command(settings, db, args)
        elif args.command == "submit":
            result = asyncio.run(_submit(settings, db, args.match_id))
        elif args.command == "status":
            result = Reporter(settings, db).status()
        elif args.command == "report":
            result = {"report": str(Reporter(settings, db).generate(args.output))}
        else:
            raise AssertionError(args.command)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (WorkflowError, ManualIntervention, RuntimeError) as exc:
        print(f"Stopped safely: {exc}", file=sys.stderr)
        return 2


async def _sync(settings: Settings, db: Database) -> dict:
    browser = BrowserSession(settings, db)
    async with browser.open():
        return await StoryImporter(settings, db, browser).sync()


async def _discover(settings: Settings, db: Database, sources: list[str] | None) -> dict:
    browser = BrowserSession(settings, db)
    async with browser.open():
        return await PublicationDiscovery(settings, db, browser).discover(sources)


def _application_command(settings: Settings, db: Database, args) -> dict:
    workflow = Workflows(settings, db)
    application = workflow.prepare_application(args.publication_id)
    if args.send and not args.approve:
        existing = db.one(
            """SELECT id,status FROM writer_applications WHERE publication_id=?
               ORDER BY id DESC LIMIT 1""",
            (args.publication_id,),
        )
        if not existing or existing["status"] != "approved":
            raise WorkflowError("Use --approve first, inspect the draft, then run --send separately.")
        return asyncio.run(_send_application(settings, db, existing["id"]))
    if args.approve:
        now = utcnow()
        db.execute(
            """UPDATE writer_applications SET status='approved',approved_at=?,updated_at=?
               WHERE id=? AND status='application-required'""",
            (now, now, application["application_id"]),
        )
        application["status"] = "approved"
        application["note"] = "Approved but not sent. Run apply <publication-id> --send in live mode."
    return application


async def _send_application(settings: Settings, db: Database, application_id: int) -> dict:
    browser = BrowserSession(settings, db)
    async with browser.open():
        return await Workflows(settings, db, browser).send_application(application_id)


async def _submit(settings: Settings, db: Database, match_id: int) -> dict:
    browser = BrowserSession(settings, db)
    async with browser.open():
        return await Workflows(settings, db, browser).submit(match_id)


if __name__ == "__main__":
    raise SystemExit(main())
