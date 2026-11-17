import asyncio
import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.gitlab_client import GitLabClient, GitLabError
from app.gitlab_config import GitLabConfigurationError, GitLabSettings, gitlab_settings
from app.upstream import PinnedTransport

SERVICE = uuid.uuid4()
TOKEN = "test-only-gitlab-token"


@pytest.fixture
def config():
    return GitLabSettings("https://gitlab.example", TOKEN, {SERVICE: 123}, "https://coverage.example/coverage")


@pytest.fixture
def gitlab_http(monkeypatch):
    async def dns(self, host, port, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", dns)
    requests = []

    def install(handler):
        transport = httpx.MockTransport(handler)

        async def handle(self, request):
            requests.append(request)
            return await transport.handle_async_request(request)

        monkeypatch.setattr(PinnedTransport, "handle_async_request", handle)
        return requests
    return install


def data(status="pending", ident=42):
    return {"id": ident, "status": status, "web_url": f"https://gitlab.example/demo/-/pipelines/{ident}"}


async def test_launch_sends_exact_ci_variables_and_header_token(config, gitlab_http, caplog):
    requests = gitlab_http(lambda request: httpx.Response(201, json=data()))
    run_id = uuid.uuid4()
    result = await GitLabClient(config).launch(123, "feature/a", run_id)
    assert result.id == 42
    request = requests[0]
    assert request.url.path == "/api/v4/projects/123/pipeline"
    assert request.headers["private-token"] == TOKEN
    payload = json.loads(request.content)
    assert payload["ref"] == "feature/a"
    assert {v["key"]: v["value"] for v in payload["variables"]} == {
        "COVERAGE_RUN_ID": str(run_id), "COVERAGE_URL": config.coverage_url, "COVERAGE_MIN_PERCENT": "80",
    }
    assert "token" not in str(request.url)
    assert "COVERAGE_API_KEY" not in request.content.decode()
    assert TOKEN not in repr(config) + caplog.text


@pytest.mark.parametrize("status,ambiguous", [(400, False), (401, False), (403, False), (404, False),
                                             (422, False), (429, True), (500, True), (503, True), (302, False)])
async def test_status_errors_are_safe_and_never_retried(status, ambiguous, config, gitlab_http, caplog):
    requests = gitlab_http(lambda request: httpx.Response(status, text="sensitive upstream token: " + TOKEN))
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid.uuid4())
    assert caught.value.ambiguous is ambiguous
    assert len(requests) == 1
    assert TOKEN not in str(caught.value) + caplog.text


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
async def test_ambiguous_network_errors(config, gitlab_http, error):
    def handler(request):
        raise error(TOKEN)
    requests = gitlab_http(handler)
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid.uuid4())
    assert caught.value.ambiguous
    assert len(requests) == 1


@pytest.mark.parametrize("response", [b"invalid", b"{}", b"[]", b"x" * (1024 * 1024 + 1)])
async def test_unparseable_success_is_ambiguous(config, gitlab_http, response):
    gitlab_http(lambda request: httpx.Response(201, content=response))
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid.uuid4())
    assert caught.value.ambiguous


@pytest.mark.parametrize("status", ["pending", "running", "success", "failed", "canceled", "manual", "scheduled", "waiting_for_resource", "future_status"])
async def test_read_status_does_not_map_unknown_values_to_failure(config, gitlab_http, status):
    gitlab_http(lambda request: httpx.Response(200, json=data(status)))
    assert (await GitLabClient(config).pipeline(123, 42)).status == status


async def test_reconcile_matches_uuid_and_scans_pages(config, gitlab_http):
    run_id = uuid.uuid4()
    def handler(request):
        if request.url.path.endswith("/variables"):
            ident = int(request.url.path.split("/")[-2])
            return httpx.Response(200, json=[{"key": "COVERAGE_RUN_ID", "value": str(run_id) if ident == 23 else "latest"}])
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[data(ident=n) for n in ([1, 2] if page == 1 else [23])])
    requests = gitlab_http(handler)
    client = GitLabClient(config)
    stamp = datetime.now(timezone.utc)
    info, page = await client.reconcile(123, "main", run_id, stamp, 1)
    assert info is None and page == 2
    info, page = await client.reconcile(123, "main", run_id, stamp, page)
    assert info.id == 23
    assert all(request.method == "GET" for request in requests)
    assert requests[0].url.params["source"] == "api"


@pytest.mark.parametrize("url", ["https://evil.example/pipeline", "https://gitlab.example/p?token=secret",
                                  "https://user:pass@gitlab.example/p", "http://gitlab.example/p"])
def test_pipeline_links_cannot_leak_credentials_or_redirect(config, url):
    with pytest.raises(GitLabError):
        GitLabClient(config).pipeline_info({**data(), "web_url": url})


@pytest.mark.parametrize("host", ["http://gitlab.example", "https://u:p@gitlab.example", "https://gitlab.example/path",
                                   "https://gitlab.example?token=secret", "https://gitlab.example/#x"])
def test_config_rejects_non_origin_hosts(monkeypatch, host):
    monkeypatch.setenv("GITLAB_ENABLED", "true")
    monkeypatch.setenv("GITLAB_HOST", host)
    with pytest.raises(GitLabConfigurationError):
        gitlab_settings()


def test_config_uses_service_uuid_binding_and_never_token_prefix_inference(monkeypatch):
    monkeypatch.setenv("GITLAB_ENABLED", "true")
    monkeypatch.setenv("GITLAB_HOST", "https://gitlab.example")
    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)
    monkeypatch.setenv("GITLAB_SERVICE_PROJECTS", json.dumps({str(SERVICE): 123}))
    monkeypatch.setenv("GITLAB_COVERAGE_URL", "https://coverage.example/coverage")
    assert gitlab_settings().project(SERVICE) == 123
    with pytest.raises(GitLabConfigurationError):
        gitlab_settings().project(uuid.uuid4())


async def test_missing_old_variables_file_does_not_block_reconciliation(config, gitlab_http):
    run_id = uuid.uuid4()
    def handler(request):
        if request.url.path.endswith("/1/variables"):
            return httpx.Response(404)
        if request.url.path.endswith("/2/variables"):
            return httpx.Response(200, json=[{"key": "COVERAGE_RUN_ID", "value": str(run_id)}])
        return httpx.Response(200, json=[data(ident=1), data(ident=2)])
    gitlab_http(handler)
    result, _ = await GitLabClient(config).reconcile(123, "main", run_id, datetime.now(timezone.utc), 1)
    assert result.id == 2


async def test_branch_name_is_encoded_and_response_checked(config, gitlab_http):
    requests = gitlab_http(lambda request: httpx.Response(200, json={"name": "feat/a"}))
    await GitLabClient(config).check_branch(123, "feat/a")
    assert b"feat%2Fa" in requests[0].url.raw_path
    with pytest.raises(GitLabError):
        await GitLabClient(config).check_branch(123, "another")
