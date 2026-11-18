"""Bounded GitLab v4 requests with read-only retries and no redirects."""
import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx

from app.gitlab_config import GitLabSettings
from app.upstream import PinnedTransport, UnsafeEndpoint, validate_endpoint


class GitLabError(Exception):
    def __init__(self, code: str, *, ambiguous: bool = False):
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


@dataclass(frozen=True)
class PipelineInfo:
    id: int
    url: str
    status: str
    commit_sha: str | None = None
    ref: str | None = None


COMMIT_SHA_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
JOBS_PAGE_SIZE = 50
GET_ATTEMPTS = 3
REQUEST_BUDGET_SECONDS = 15
GET_ATTEMPT_BUDGET_SECONDS = 4.5


@dataclass(frozen=True)
class JobInfo:
    job_id: int
    name: str
    stage: str
    status: str
    url: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def gitlab_id(value):
    return type(value) is int and 0 < value < 2**63


def label(value, max_length):
    return (isinstance(value, str) and 1 <= len(value) <= max_length
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def job_timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError
    return stamp


class GitLabClient:
    def __init__(self, settings: GitLabSettings):
        self.settings = settings

    async def request(self, method: str, path: str, *, params=None, payload=None):
        if method != "GET":
            # Pipeline creation can have succeeded even when the response was
            # lost. Never replay a mutation.
            return await self._request_once(method, path, params=params, payload=payload)

        # Retrying a read is safe, but multiple full HTTP timeouts would exceed
        # the API and background monitor budgets. Include DNS, TCP, TLS, body
        # and backoff in one deadline. Revalidate DNS/SSRF for every attempt.
        deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
        last_error = GitLabError("gitlab_network_error")
        for attempt in range(GET_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(min(GET_ATTEMPT_BUDGET_SECONDS, remaining)):
                    return await self._request_once(method, path, params=params, payload=payload)
            except TimeoutError:
                last_error = GitLabError("gitlab_network_error")
            except GitLabError as exc:
                # Do not retry authorization, validation, policy, or HTTP
                # errors (including rate limiting). Only transient transport
                # failures qualify.
                if exc.code != "gitlab_network_error":
                    raise
                last_error = exc
            if attempt + 1 < GET_ATTEMPTS:
                await asyncio.sleep(min(0.15 * (2**attempt),
                                        max(0, deadline - time.monotonic())))
        raise last_error

    async def _request_once(self, method: str, path: str, *, params=None, payload=None):
        try:
            async with asyncio.timeout(REQUEST_BUDGET_SECONDS):
                destination = await validate_endpoint(self.settings.host + "/api/v4/" + path)
                async with httpx.AsyncClient(
                    transport=PinnedTransport(destination), trust_env=False,
                    follow_redirects=False, timeout=10,
                ) as client:
                    async with client.stream(method, destination.url, params=params, json=payload,
                                             headers={"PRIVATE-TOKEN": self.settings.token}) as response:
                        # A 5xx or timeout can occur after GitLab has created the pipeline.
                        if response.status_code >= 500 or response.status_code in {408, 429}:
                            raise GitLabError("gitlab_unavailable", ambiguous=method == "POST")
                        if not 200 <= response.status_code < 300:
                            codes = {401: "gitlab_unauthorized", 403: "gitlab_forbidden",
                                     404: "gitlab_not_found", 400: "gitlab_rejected",
                                     422: "gitlab_rejected"}
                            raise GitLabError(codes.get(response.status_code, "gitlab_rejected"))
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 1024 * 1024:
                                raise GitLabError("gitlab_invalid_response", ambiguous=method == "POST")
                        return json.loads(body)
        except GitLabError:
            raise
        except (httpx.RequestError, TimeoutError):
            raise GitLabError("gitlab_network_error", ambiguous=method == "POST") from None
        except UnsafeEndpoint:
            # Validation happens before credentials or request bytes are sent.
            raise GitLabError("gitlab_destination_unavailable") from None
        except (ValueError, TypeError):
            raise GitLabError("gitlab_invalid_response", ambiguous=method == "POST") from None

    def valid_link(self, url):
        if not isinstance(url, str) or len(url) > 2048 or any(
            ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url
        ):
            return False
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.netloc == urlsplit(self.settings.host).netloc
                and not parts.query and not parts.fragment and not parts.username and bool(parts.path))

    def pipeline_info(self, data, *, project=None, ref=None) -> PipelineInfo:
        try:
            ident, status, url = data["id"], data["status"], data["web_url"]
            sha, actual_ref = data.get("sha"), data.get("ref")
            if (not gitlab_id(ident) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", status)
                    or not self.valid_link(url)
                    or (sha is not None and not re.fullmatch(COMMIT_SHA_PATTERN, sha))
                    or (actual_ref is not None and (not label(actual_ref, 255) or (ref is not None and actual_ref != ref)))
                    or (project is not None and data.get("project_id", project) != project)
                    or data.get("tag") is True):
                raise ValueError
            return PipelineInfo(ident, url, status, sha, actual_ref)
        except (ValueError, TypeError, KeyError):
            raise GitLabError("gitlab_invalid_response", ambiguous=True) from None

    async def launch(self, project: int, ref: str, run_id) -> PipelineInfo:
        variables = {"COVERAGE_RUN_ID": str(run_id), "COVERAGE_URL": self.settings.coverage_url,
                     "COVERAGE_MIN_PERCENT": str(self.settings.min_percent)}
        data = await self.request("POST", f"projects/{project}/pipeline", payload={
            "ref": ref, "variables": [{"key": k, "value": v, "variable_type": "env_var"}
                                      for k, v in variables.items()],
        })
        return self.pipeline_info(data, project=project, ref=ref)

    async def pipeline(self, project: int, pipeline_id: int) -> PipelineInfo:
        info = self.pipeline_info(await self.request("GET", f"projects/{project}/pipelines/{pipeline_id}"),
                                  project=project)
        if info.id != pipeline_id:
            raise GitLabError("gitlab_invalid_response")
        return info

    async def jobs(self, project: int, pipeline_id: int, page: int) -> tuple[list[JobInfo], bool]:
        data = await self.request("GET", f"projects/{project}/pipelines/{pipeline_id}/jobs", params={
            "page": page, "per_page": JOBS_PAGE_SIZE, "include_retried": "true",
        })
        try:
            if not isinstance(data, list) or len(data) > JOBS_PAGE_SIZE:
                raise ValueError
            jobs = []
            seen = set()
            for item in data:
                ident, name, stage, status, url = (
                    item["id"], item["name"], item["stage"], item["status"], item["web_url"],
                )
                pipeline = item["pipeline"]
                if (not gitlab_id(ident) or ident in seen or not label(name, 255) or not label(stage, 255)
                        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", status) or not self.valid_link(url)
                        or pipeline["id"] != pipeline_id or pipeline["project_id"] != project):
                    raise ValueError
                seen.add(ident)
                jobs.append(JobInfo(
                    ident, name, stage, status, url, job_timestamp(item["created_at"]),
                    job_timestamp(item["started_at"]) if item.get("started_at") is not None else None,
                    job_timestamp(item["finished_at"]) if item.get("finished_at") is not None else None,
                ))
        except (TypeError, ValueError, KeyError):
            raise GitLabError("gitlab_invalid_response") from None
        return jobs, len(data) == JOBS_PAGE_SIZE

    async def branches(self, project: int, page: int):
        data = await self.request("GET", f"projects/{project}/repository/branches",
                                  params={"page": page, "per_page": 100})
        if not isinstance(data, list) or len(data) > 100 or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            or not 1 <= len(item["name"]) <= 255 for item in data
        ):
            raise GitLabError("gitlab_invalid_response")
        return [item["name"] for item in data]

    async def check_branch(self, project: int, ref: str):
        data = await self.request("GET", f"projects/{project}/repository/branches/{quote(ref, safe='')}")
        if not isinstance(data, dict) or data.get("name") != ref:
            raise GitLabError("gitlab_invalid_response")

    async def reconcile(self, project: int, ref: str, run_id, created_at: datetime, page: int):
        # Bounded page scans survive restarts; only exact run UUID matches count.
        data = await self.request("GET", f"projects/{project}/pipelines", params={
            "ref": ref, "source": "api", "created_after": (created_at - timedelta(minutes=1)).isoformat(),
            # Three requests * 15 seconds fit the 60-second polling budget even
            # on a slow GitLab. Persist the page after every small scan.
            "order_by": "id", "sort": "asc", "page": page, "per_page": 2,
        })
        if not isinstance(data, list) or len(data) > 2:
            raise GitLabError("gitlab_invalid_response")
        for item in data:
            info = self.pipeline_info(item, project=project, ref=ref)
            try:
                variables = await self.request("GET", f"projects/{project}/pipelines/{info.id}/variables")
            except GitLabError as exc:
                # Old pipelines may have lost their variables file. They must
                # not prevent scanning subsequent candidates for our run UUID.
                if exc.code == "gitlab_not_found":
                    continue
                raise
            if not isinstance(variables, list):
                raise GitLabError("gitlab_invalid_response")
            if any(isinstance(v, dict) and v.get("key") == "COVERAGE_RUN_ID"
                   and v.get("value") == str(run_id) for v in variables):
                return info, 1
        return None, page + 1 if len(data) == 2 else 1
