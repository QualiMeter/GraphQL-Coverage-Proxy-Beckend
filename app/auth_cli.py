import argparse
import asyncio
import getpass
import sys

from app.auth import bootstrap_admin, cleanup_auth
from app.database import SessionFactory, engine


async def run(args, password=None):
    try:
        async with SessionFactory() as session:
            if args.command == "bootstrap-admin":
                await bootstrap_admin(session, args.username, password)
                print("Initial administrator created")
            else:
                await cleanup_auth(session)
                print("Expired authentication records removed")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Local coverage authentication maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.add_argument("username")
    commands.add_parser("cleanup")
    args = parser.parse_args()
    password = None
    if args.command == "bootstrap-admin":
        if not sys.stdin.isatty():
            parser.exit(1, "Bootstrap requires an interactive terminal; passwords are not accepted via stdin/argv/env\n")
        password = getpass.getpass("Initial administrator password (12–128 characters): ")
        if password != getpass.getpass("Repeat password: "):
            parser.exit(1, "Passwords do not match\n")
    try:
        asyncio.run(run(args, password))
    except Exception:
        # Exception text may contain database DSNs or input validation values.
        parser.exit(1, "Authentication maintenance failed; check the target DB, migrations and account state\n")


if __name__ == "__main__":
    main()
