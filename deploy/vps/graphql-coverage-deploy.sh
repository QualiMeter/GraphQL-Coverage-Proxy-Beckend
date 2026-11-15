#!/usr/bin/env bash
# Installed root-owned as /usr/local/sbin/graphql-coverage-deploy.
# Usage: sudo -n /usr/local/sbin/graphql-coverage-deploy <tested-main-commit-sha>
set -Eeuo pipefail
umask 077

readonly APP_DIR="/opt/graphql-coverage"
readonly ENV_FILE="$APP_DIR/deploy/.env"
readonly COMPOSE_FILE="$APP_DIR/deploy/compose.production.yaml"
readonly STATE_DIR="/var/lib/graphql-coverage-deploy"
readonly SUCCESS_FILE="$STATE_DIR/last-successful-sha"
readonly BACKUP_DIR="/root/graphql-coverage-backups"

log() {
    printf '[graphql-coverage-deploy] %s\n' "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

[[ "$EUID" -eq 0 ]] || fail "Root is required for this controlled deploy script."
[[ "$#" -eq 1 && "$1" =~ ^[0-9a-f]{40}$ ]] || fail "Pass one 40-character commit SHA."
readonly DEPLOY_SHA="$1"

for command_name in git docker curl flock mktemp; do
    command -v "$command_name" >/dev/null || fail "Missing command: $command_name"
done

# The existing compose project/volume names are never changed.
[[ -d "$APP_DIR/.git" ]] || fail "Git worktree not found: $APP_DIR"
[[ -s "$ENV_FILE" && -f "$COMPOSE_FILE" ]] || fail "Production Compose/.env is missing."

exec 9>/run/lock/graphql-coverage-deploy.lock
flock -n 9 || fail "Another GraphQL Coverage deployment is running."

cd "$APP_DIR"
[[ "$(git branch --show-current)" == "main" ]] || fail "Worktree must be on main."
[[ -z "$(git status --porcelain --untracked-files=no)" ]] ||
    fail "Tracked files have local changes; refusing to overwrite them."

log "Fetching main."
git fetch --no-tags origin main
git cat-file -e "$DEPLOY_SHA^{commit}" || fail "Requested commit was not fetched."
git merge-base --is-ancestor "$DEPLOY_SHA" origin/main ||
    fail "Requested commit is not on origin/main."

readonly CURRENT_SHA="$(git rev-parse HEAD)"
# HEAD records source checkout, not whether build/start/readiness succeeded.
[[ "$DEPLOY_SHA" == "$CURRENT_SHA" ]] || {
    git merge-base --is-ancestor "$CURRENT_SHA" "$DEPLOY_SHA" ||
        fail "Deployment is not a fast-forward from current main."
}
install -d -m 0700 -o root -g root "$STATE_DIR"
success_sha=""
if [[ -f "$SUCCESS_FILE" && ! -L "$SUCCESS_FILE" ]]; then
    success_sha="$(cat "$SUCCESS_FILE")" || success_sha=""
    if [[ ! "$success_sha" =~ ^[0-9a-f]{40}$ ]] ||
       ! git cat-file -e "$success_sha^{commit}" 2>/dev/null; then
        log "Ignoring invalid deployment success record; deployment must be verified again."
        success_sha=""
    fi
fi
if [[ -n "$success_sha" ]]; then
    git merge-base --is-ancestor "$success_sha" "$DEPLOY_SHA" ||
        fail "Refusing to deploy behind or divergent from the last successful deployment."
fi

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

ready() {
    local bound_address api_port healthy attempt
    bound_address="$(compose port api 8000 | tail -n 1)"
    [[ "$bound_address" == "127.0.0.1:"* ]] || return 1
    api_port="$(printf '%s\n' "$bound_address" | awk -F: '{print $NF}')"
    [[ "$api_port" =~ ^[0-9]+$ ]] || return 1
    local BASE_URL="http://127.0.0.1:$api_port"

    log "Waiting for API and database read checks."
    healthy=0
    for attempt in $(seq 1 45); do
        if curl --fail --silent --max-time 3 "$BASE_URL/health" >/dev/null &&
           curl --fail --silent --max-time 3 "$BASE_URL/api/services" >/dev/null; then
            healthy=1
            break
        fi
        sleep 2
    done

    [[ "$healthy" -eq 1 ]]
}

if [[ "$CURRENT_SHA" == "$DEPLOY_SHA" && "$success_sha" == "$DEPLOY_SHA" ]] && ready; then
    log "Deployment already verified: $DEPLOY_SHA."
    exit 0
fi

# Back up the current DB before updating source or running Alembic migrations.
compose config --quiet
install -d -m 0700 -o root -g root "$BACKUP_DIR"
backup_file="$(mktemp "$BACKUP_DIR/coverage-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX.dump")"
backup_verified=0
cleanup_incomplete_backup() {
    if [[ "$backup_verified" -ne 1 ]]; then
        rm -f -- "$backup_file"
    fi
}
trap cleanup_incomplete_backup EXIT

log "Backing up PostgreSQL."
compose exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_file" ||
    fail "pg_dump failed; source code was not updated."
compose exec -T db pg_restore -l < "$backup_file" >/dev/null ||
    fail "Backup validation failed; source code was not updated."
[[ -s "$backup_file" ]] || fail "Backup is empty."
backup_verified=1
log "Backup validated: $backup_file"

# Invalidate before any source/container change, including retries of the same SHA.
rm -f -- "$SUCCESS_FILE"

log "Updating application to tested commit $DEPLOY_SHA."
git merge --ff-only "$DEPLOY_SHA"
compose config --quiet

# Build first so the existing API container remains available during compilation.
log "Building API image."
compose build api
log "Recreating only API (never the PostgreSQL container or volume)."
compose up -d --no-deps api

ready || fail "Readiness checks failed; investigate API logs before another deploy."
# Atomic rename in a root-only directory; interrupted writes never look successful.
success_tmp="$(mktemp "$STATE_DIR/.success-XXXXXXXX")"
printf '%s\n' "$DEPLOY_SHA" > "$success_tmp"
mv -f -- "$success_tmp" "$SUCCESS_FILE"
log "Deployment successful: $DEPLOY_SHA"
log "PostgreSQL backup retained: $backup_file"
