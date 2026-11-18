"""Server-owned GitLab bindings. No secrets are persisted or returned to clients."""
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.upstream import parse_endpoint


class GitLabConfigurationError(Exception):
    def __init__(self):
        super().__init__("GitLab integration is not configured correctly")


@dataclass(frozen=True)
class GitLabSettings:
    host: str
    token: str = field(repr=False)
    projects: dict[uuid.UUID, int]
    coverage_url: str
    min_percent: float = 80
    poll_seconds: int = 10
    max_run_seconds: int = 3600
    environments: dict[uuid.UUID, str] = field(default_factory=dict)

    def project(self, service_id: uuid.UUID) -> int:
        if service_id not in self.projects:
            raise GitLabConfigurationError()
        return self.projects[service_id]


def gitlab_enabled() -> bool:
    return os.getenv("GITLAB_ENABLED", "false").lower() == "true"


def gitlab_settings() -> GitLabSettings:
    try:
        if not gitlab_enabled():
            raise ValueError
        host = os.getenv("GITLAB_HOST", "https://gitlab.com").rstrip("/")
        parts = urlsplit(host)
        parse_endpoint(host)
        if parts.scheme != "https" or parts.path or parts.query or parts.fragment or parts.username:
            raise ValueError
        token = os.getenv("GITLAB_TOKEN", "")
        if not token or not token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in token):
            raise ValueError
        raw = json.loads(os.getenv("GITLAB_SERVICE_PROJECTS", "{}"))
        if not isinstance(raw, dict) or not raw:
            raise ValueError
        projects, environments = {}, {}
        for key, value in raw.items():
            service_id = uuid.UUID(key)
            if service_id in projects:
                raise ValueError
            project = value
            if isinstance(value, dict):
                if value.keys() - {"projectId", "environment"}:
                    raise ValueError
                project = value.get("projectId")
                environment = value.get("environment")
                if environment is not None:
                    if not isinstance(environment, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]{0,127}", environment):
                        raise ValueError
                    environments[service_id] = environment
            if type(project) is not int or not 0 < project < 2**63:
                raise ValueError
            projects[service_id] = project
        coverage_url = os.getenv("GITLAB_COVERAGE_URL", "").rstrip("/")
        parse_endpoint(coverage_url)
        minimum = float(os.getenv("COVERAGE_MIN_PERCENT", "80"))
        poll = int(os.getenv("GITLAB_POLL_SECONDS", "10"))
        maximum = int(os.getenv("GITLAB_MAX_RUN_SECONDS", "3600"))
        if not 0 <= minimum <= 100 or not 2 <= poll <= 300 or not 60 <= maximum <= 86400:
            raise ValueError
        return GitLabSettings(host, token, projects, coverage_url, minimum, poll, maximum, environments)
    except (ValueError, TypeError, AttributeError):
        raise GitLabConfigurationError() from None
