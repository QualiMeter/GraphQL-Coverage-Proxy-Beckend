"""Local operator recovery after independently confirming that no pipeline exists."""
import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.database import SessionFactory, engine
from app.gitlab_runs import now
from app.models import GitLabPipeline, TestRun


async def resolve_absent(run_id: uuid.UUID):
    async with SessionFactory() as session:
        run = await session.scalar(select(TestRun).where(TestRun.id == run_id).with_for_update())
        row = await session.scalar(select(GitLabPipeline).where(GitLabPipeline.run_id == run_id).with_for_update())
        if (row is None or run is None or run.status != "failed" or row.pipeline_id is not None
                or row.finished_at is not None or row.deadline_at > now()
                or (row.lease_until is not None and row.lease_until > now())):
            raise ValueError("Only an expired, failed, unknown launch without an active polling lease may be resolved")
        row.pipeline_status = "not_created"
        row.error_code = "operator_confirmed_not_created"
        row.finished_at = now()
        row.lease_token = None
        row.lease_until = None
        await session.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["resolve-absent"])
    parser.add_argument("run_id", type=uuid.UUID)
    parser.add_argument("--confirm-no-pipeline", action="store_true", required=True,
                        help="Confirm independent GitLab inspection, including pipeline variables and the run UUID")
    args = parser.parse_args()
    async def execute():
        try:
            await resolve_absent(args.run_id)
            print("Unknown launch resolved after operator confirmation")
            return 0
        except ValueError as exc:
            print(str(exc))
            return 1
        except Exception:
            print("Recovery storage unavailable")
            return 1
        finally:
            await engine.dispose()
    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
