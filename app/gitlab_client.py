"""Bounded GitLab v4 requests, with no redirects, retries or caller-selected origins."""
import asyncio
import json
import re
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


class GitLabClient:
    def __init__(self, settings: GitLabSettings):
        self.settings = settings

    async def request(self, method: str, path: str, *, params=None, payload=None):
        try:
            async with asyncio.timeout(15):
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

    def pipeline_info(self, data) -> PipelineInfo:
        try:
            ident, status, url = data["id"], data["status"], data["web_url"]
            parts = urlsplit(url)
            if (type(ident) is not int or not 0 < ident < 2**63 or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", status)
                    or parts.scheme != "https" or parts.netloc != urlsplit(self.settings.host).netloc
                    or parts.query or parts.fragment or parts.username or not parts.path
                    or len(url) > 2048
                    or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
                raise ValueError
            return PipelineInfo(ident, url, status)
        except (ValueError, TypeError, KeyError):
            raise GitLabError("gitlab_invalid_response", ambiguous=True) from None

    async def launch(self, project: int, ref: str, run_id) -> PipelineInfo:
        variables = {"COVERAGE_RUN_ID": str(run_id), "COVERAGE_URL": self.settings.coverage_url,
                     "COVERAGE_MIN_PERCENT": str(self.settings.min_percent)}
        data = await self.request("POST", f"projects/{project}/pipeline", payload={
            "ref": ref, "variables": [{"key": k, "value": v, "variable_type": "env_var"}
                                      for k, v in variables.items()],
        })
        return self.pipeline_info(data)

    async def pipeline(self, project: int, pipeline_id: int) -> PipelineInfo:
        info = self.pipeline_info(await self.request("GET", f"projects/{project}/pipelines/{pipeline_id}"))
        if info.id != pipeline_id:
            raise GitLabError("gitlab_invalid_response")
        return info

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
            info = self.pipeline_info(item)
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
