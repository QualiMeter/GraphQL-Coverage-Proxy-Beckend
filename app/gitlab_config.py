"""Server-owned GitLab bindings. No secrets are persisted or returned to clients."""
import json
import os
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
        projects = {uuid.UUID(key): value for key, value in raw.items()}
        if not projects or any(type(value) is not int or not 0 < value < 2**63 for value in projects.values()):
            raise ValueError
        coverage_url = os.getenv("GITLAB_COVERAGE_URL", "").rstrip("/")
        parse_endpoint(coverage_url)
        minimum = float(os.getenv("COVERAGE_MIN_PERCENT", "80"))
        poll = int(os.getenv("GITLAB_POLL_SECONDS", "10"))
        maximum = int(os.getenv("GITLAB_MAX_RUN_SECONDS", "3600"))
        if not 0 <= minimum <= 100 or not 2 <= poll <= 300 or not 60 <= maximum <= 86400:
            raise ValueError
        return GitLabSettings(host, token, projects, coverage_url, minimum, poll, maximum)
    except (ValueError, TypeError, AttributeError):
        raise GitLabConfigurationError() from None
