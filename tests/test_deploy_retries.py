"""Run the real shell flow with local Git and controlled Docker/HTTP failures."""
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(os.environ.get("DEPLOY_SCRIPT", "deploy/vps/graphql-coverage-deploy.sh"))


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


@pytest.fixture
def deploy(tmp_path):
    origin = tmp_path / "origin"
    command("git", "init", "-q", "-b", "main", str(origin))
    command("git", "config", "user.email", "test@example.invalid", cwd=origin)
    command("git", "config", "user.name", "Test", cwd=origin)

    def commit(value):
        (origin / "tracked").write_text(value)
        command("git", "add", "tracked", cwd=origin)
        command("git", "commit", "-qm", value, cwd=origin)
        return command("git", "rev-parse", "HEAD", cwd=origin)

    old = commit("old")
    app = tmp_path / "app"
    command("git", "clone", "-q", str(origin), str(app))
    new = commit("new")
    (app / "deploy").mkdir()
    (app / "deploy/.env").write_text("TEST=1\n")
    (app / "deploy/compose.production.yaml").touch()
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    script = tmp_path / "deploy.sh"
    source = SCRIPT.read_text()
    # Only relocate root-owned filesystem paths and permit this test's UID.
    source = source.replace('/opt/graphql-coverage', str(app)).replace(
        '/root/graphql-coverage-backups', str(backups)).replace(
        '/var/lib/graphql-coverage-deploy', str(state)).replace(
        '/run/lock/graphql-coverage-deploy.lock', str(tmp_path / 'lock')).replace(
        '"$EUID" -eq 0', f'"$EUID" -eq {os.geteuid()}')
    script.write_text(source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mocks = {
        "install": '#!/bin/bash\nargs=(); while (($#)); do case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac; done\nexec /usr/bin/install "${args[@]}"\n',
        "sleep": '#!/bin/bash\nexit 0\n',
        "curl": '#!/bin/bash\necho "curl ${*: -1}" >> "$CALLS"\n[[ "${FAIL_STEP:-}" != ready ]]\n',
    }
    mocks["docker"] = '''#!/bin/bash
shift 5 # compose --env-file FILE -f FILE
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
    build) [[ "${FAIL_STEP:-}" != build ]];;
    up) [[ "${FAIL_STEP:-}" != up ]];;
    port) echo '127.0.0.1:5081';;
    exec)
        if [[ "$*" == *pg_restore* ]]; then cat >/dev/null
        else [[ "${FAIL_STEP:-}" != backup ]] || exit 1; echo 'test dump'; fi;;
    config) exit 0;;
    *) exit 99;;
esac
'''
    for name, content in mocks.items():
        file = bin_dir / name
        file.write_text(content)
        file.chmod(0o755)
    calls = tmp_path / "calls"

    def run(sha=new, fail=""):
        return subprocess.run(["bash", str(script), sha], text=True, capture_output=True,
                              env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                                   "FAIL_STEP": fail, "CALLS": str(calls)})

    class Deployment:
        marker = state / "last-successful-sha"

        def log(self):
            return calls.read_text().splitlines() if calls.exists() else []

    result = Deployment()
    result.run, result.app, result.old, result.new = run, app, old, new
    return result


def test_success_records_only_ready_commit_and_repeat_is_read_only(deploy):
    assert deploy.run().returncode == 0
    assert deploy.marker.read_text().strip() == deploy.new
    assert deploy.marker.stat().st_mode & 0o777 == 0o600
    assert deploy.marker.parent.stat().st_mode & 0o777 == 0o700
    log = deploy.log()
    assert log.index('exec -T db pg_restore -l') < log.index('build api')
    assert log.index('up -d --no-deps api') < log.index('curl http://127.0.0.1:5081/api/services')
    assert deploy.run().returncode == 0
    assert deploy.log()[len(log):] == ['port api 8000', 'curl http://127.0.0.1:5081/health',
                                      'curl http://127.0.0.1:5081/api/services']


@pytest.mark.parametrize("failure", ["build", "up", "ready"])
def test_failed_deploy_retries_same_head_instead_of_false_success(deploy, failure):
    assert deploy.run(fail=failure).returncode != 0
    assert command("git", "rev-parse", "HEAD", cwd=deploy.app) == deploy.new
    assert not deploy.marker.exists()
    # Repeated failure must still fail and execute build again.
    assert deploy.run(fail=failure).returncode != 0
    assert deploy.log().count('build api') == 2
    assert deploy.run().returncode == 0
    assert deploy.log().count('build api') == 3
    assert deploy.marker.read_text().strip() == deploy.new


@pytest.mark.parametrize("record", [None, "", "corrupt\nrecord", "f" * 40])
def test_missing_or_corrupt_record_never_trusts_head(deploy, record):
    command("git", "fetch", "origin", "main", cwd=deploy.app)
    command("git", "merge", "--ff-only", deploy.new, cwd=deploy.app)
    if record is not None:
        deploy.marker.parent.mkdir()
        deploy.marker.write_text(record)
    assert deploy.run().returncode == 0
    assert 'build api' in deploy.log()
    assert deploy.marker.read_text().strip() == deploy.new


@pytest.mark.parametrize("sha", ["main", "--help", "A" * 40, "f" * 40])
def test_invalid_or_unknown_sha_is_rejected(deploy, sha):
    assert deploy.run(sha).returncode != 0
    assert not deploy.log()


def test_no_rollback_dirty_checkout_or_divergent_update(deploy):
    assert deploy.run().returncode == 0
    before = deploy.log()
    assert deploy.run(deploy.old).returncode != 0
    assert deploy.log() == before
    (deploy.app / 'tracked').write_text('dirty')
    assert deploy.run().returncode != 0
    assert deploy.log() == before
    command('git', 'checkout', '--', 'tracked', cwd=deploy.app)
    command('git', '-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
            'commit', '--allow-empty', '-qm', 'local divergence', cwd=deploy.app)
    assert deploy.run().returncode != 0
    assert deploy.log() == before


def test_backup_failure_stops_before_source_update(deploy):
    assert deploy.run(fail='backup').returncode != 0
    assert command('git', 'rev-parse', 'HEAD', cwd=deploy.app) == deploy.old
    assert 'build api' not in deploy.log()
    assert not deploy.marker.exists()


def test_busy_lock_prevents_all_deployment_work(deploy):
    import fcntl
    lock = deploy.app.parent / 'lock'
    with lock.open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert deploy.run().returncode != 0
    assert not deploy.log()
    assert command('git', 'rev-parse', 'HEAD', cwd=deploy.app) == deploy.old


def test_success_record_ahead_of_checkout_does_not_skip_deployment(deploy):
    # Even an intact journal is insufficient when source checkout differs.
    command('git', 'fetch', 'origin', 'main', cwd=deploy.app)
    deploy.marker.parent.mkdir()
    deploy.marker.write_text(deploy.new + '\n')
    assert deploy.run().returncode == 0
    assert 'build api' in deploy.log()
    assert command('git', 'rev-parse', 'HEAD', cwd=deploy.app) == deploy.new


def test_unhealthy_previously_successful_commit_is_rebuilt_and_record_invalidated(deploy):
    assert deploy.run().returncode == 0
    assert deploy.run(fail='ready').returncode != 0
    assert not deploy.marker.exists()
    assert deploy.log().count('build api') == 2
    assert deploy.run().returncode == 0
    assert deploy.log().count('build api') == 3


def test_existing_commit_outside_origin_main_is_rejected(deploy):
    command('git', '-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
            'commit', '--allow-empty', '-qm', 'not on origin/main', cwd=deploy.app)
    local_sha = command('git', 'rev-parse', 'HEAD', cwd=deploy.app)
    assert deploy.run(local_sha).returncode != 0
    assert not deploy.log()


@pytest.mark.parametrize('suffix', [b'', b'\0', b'\n\n'])
def test_record_must_match_exact_atomic_write_format(deploy, suffix):
    command('git', 'fetch', 'origin', 'main', cwd=deploy.app)
    command('git', 'merge', '--ff-only', deploy.new, cwd=deploy.app)
    deploy.marker.parent.mkdir()
    deploy.marker.write_bytes(deploy.new.encode() + suffix)
    assert deploy.run().returncode == 0
    assert 'build api' in deploy.log()
    assert deploy.marker.read_bytes() == deploy.new.encode() + b'\n'
