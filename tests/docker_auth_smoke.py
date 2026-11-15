"""Standalone Linux Docker smoke: host-network Nginx TLS → loopback → Docker NAT → Uvicorn.

Run explicitly with `python tests/docker_auth_smoke.py`; CI runs this after pytest.
Uses a unique Compose project, temporary TLS certificate and test-only credentials.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = "auth-smoke-test-only-key-xxxxxxxxxxxxxxxxxxxxxxxx"
ORIGIN = "https://graphql.rebyte.fun"
PASSWORD = "auth smoke test password 7849"


def main():
    project = "coverage_auth_smoke_" + uuid.uuid4().hex[:12]
    nginx = project + "_nginx"
    env = {**os.environ, "FORWARDED_ALLOW_IPS": "127.0.0.1"}
    compose = ["docker", "compose", "-p", project, "-f", str(ROOT / "deploy/compose.auth-smoke.yaml")]

    def run(args, *, check=True):
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        if check and result.returncode:
            raise RuntimeError(f"Command failed: {args[0:3]}\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    try:
        print("Creating isolated Docker auth smoke stack", flush=True)
        run([*compose, "create", "db"])
        network = json.loads(run(["docker", "network", "inspect", project + "_backend"]))[0]
        env["FORWARDED_ALLOW_IPS"] = network["IPAM"]["Config"][0]["Gateway"]
        run([*compose, "up", "-d", "--build", "api"])
        bound = run([*compose, "port", "api", "8000"])
        assert bound.startswith("127.0.0.1:"), "Smoke API must be loopback-only"
        api_port = int(bound.rsplit(":", 1)[1])
        with httpx.Client(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    if client.get(f"http://127.0.0.1:{api_port}/health").status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated API startup timed out")
                time.sleep(0.25)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            tls_port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory(prefix="coverage-nginx-auth-") as directory:
            path = Path(directory)
            run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=graphql.rebyte.fun", "-keyout", str(path / "key.pem"), "-out", str(path / "cert.pem")])
            snippet = (ROOT / "deploy/nginx/coverage-proxy.conf").read_text()
            (path / "coverage-proxy.conf").write_text(snippet.replace("127.0.0.1:5081", f"127.0.0.1:{api_port}"))
            (path / "nginx.conf").write_text(f"""
pid /tmp/nginx.pid;
events {{}}
http {{
    access_log off;
    error_log /dev/stderr warn;
    server {{
        listen 127.0.0.1:{tls_port} ssl;
        server_name graphql.rebyte.fun;
        ssl_certificate /etc/nginx/cert.pem;
        ssl_certificate_key /etc/nginx/key.pem;
        location / {{ include /etc/nginx/coverage-proxy.conf; }}
    }}
}}
""")
            options = ["--network", "host", "--read-only", "--tmpfs", "/var/cache/nginx", "--tmpfs", "/var/run",
                       "--tmpfs", "/tmp", "-v", f"{path}:/etc/nginx:ro"]
            run(["docker", "run", "--rm", *options, "nginx:stable-alpine", "nginx", "-t"])
            run(["docker", "run", "-d", "--name", nginx, *options, "nginx:stable-alpine"])
            # TLS verification is disabled only for this loopback/self-signed test.
            with httpx.Client(base_url=f"https://127.0.0.1:{tls_port}", verify=False, trust_env=False, timeout=5,
                              headers={"Host": "graphql.rebyte.fun", "Origin": ORIGIN}) as browser:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if browser.get("/health").status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Isolated Nginx startup timed out")
                    time.sleep(0.25)
                response = browser.post("/api/auth/users", json={"username": "smokeadmin", "password": PASSWORD, "role": "admin"},
                                        headers={"X-Coverage-Api-Key": TEST_KEY})
                assert response.status_code == 201, "Test account creation failed"
                # Attacker headers must be replaced by the real TLS scheme/peer at Nginx.
                response = browser.post("/api/auth/login", json={"username": "smokeadmin", "password": PASSWORD}, headers={
                    "X-Forwarded-Proto": "http", "X-Forwarded-For": "203.0.113.200",
                    "Forwarded": "proto=http;host=attacker.invalid", "X-Forwarded-Host": "attacker.invalid",
                })
                assert response.status_code == 200, "TLS scheme did not survive the Nginx/Docker hop"
                assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
                csrf = response.json()["csrfToken"]
                response = browser.post("/api/services", json={"key": "auth-smoke", "name": "Auth smoke", "endpointUrl": "https://93.184.216.34/graphql"}, headers={"X-CSRF-Token": csrf})
                assert response.status_code == 201, "Authenticated CSRF write failed"
                assert browser.post("/api/auth/logout").status_code == 403
                assert browser.post("/api/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"}).status_code == 403
                assert browser.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
                assert browser.get("/api/auth/session").json()["user"] is None
            # A different transport peer cannot promote HTTP to HTTPS with headers.
            probe = "import httpx; r=httpx.post('http://127.0.0.1:8000/api/auth/login', headers={'Origin':'https://graphql.rebyte.fun','X-Forwarded-Proto':'https'}, json={'username':'smokeadmin','password':'test'}, trust_env=False); print(r.status_code)"
            assert run([*compose, "exec", "-T", "api", "python", "-c", probe]) == "403"
        print("Nginx TLS -> host loopback -> Docker NAT -> Uvicorn login/cookie/CSRF/logout: PASS", flush=True)
    finally:
        run(["docker", "rm", "-f", nginx], check=False)
        run([*compose, "down", "--volumes", "--remove-orphans"], check=False)


if __name__ == "__main__":
    main()
