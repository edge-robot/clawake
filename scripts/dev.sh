#!/usr/bin/env bash
# Devcontainer launcher for py-clawake-dev, owned by onshape-architekt.
#
#   ./scripts/dev.sh [--rebuild] [command ...]     (default command: bash)
#   ./scripts/dev.sh claude --agent py-clawake-dev
#
# Every invocation starts a fresh, ephemeral container (--rm), so single-file
# read-only mounts never show stale content after host-side edits.
#
# The checkout may be a git worktree. Its .git file points to the common git dir by
# absolute host path, so that dir is mounted at the identical path inside the container
# (read-write for objects/refs, with config, info/ and hooks/ read-only on top).
set -euo pipefail

IMAGE=localhost/clawake-dev:latest
NAME_PREFIX=clawake-dev
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WS_ROOT=$(cd "$REPO/.." && pwd)
FOREIGN="$WS_ROOT/clawonshape"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/onshape-devcontainer/claude.env"
W=/workspaces
OWN="$W/clawake"

die() { echo "dev.sh: $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (run 'claude setup-token' and store CLAUDE_CODE_OAUTH_TOKEN=<token>)"
[[ "$(stat -c %a "$ENV_FILE")" == 600 ]] || die "$ENV_FILE must have mode 0600"
[[ -d "$FOREIGN" ]] || die "missing foreign repo $FOREIGN"
git_name=$(git -C "$REPO" config user.name) || die "git user.name is not set"
git_email=$(git -C "$REPO" config user.email) || die "git user.email is not set"
git_common=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)
# Use the host's timezone so dates in prompt/report file names, docs and memory match
# the architect's (the containers share the host clock; only the zone differs).
host_tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
host_tz=${host_tz:-Europe/Berlin}

if [[ "${1:-}" == --rebuild ]]; then
  shift
  podman build --target dev -t "$IMAGE" -f "$REPO/Containerfile" "$REPO"
elif ! podman image exists "$IMAGE"; then
  podman build --target dev -t "$IMAGE" -f "$REPO/Containerfile" "$REPO"
fi

# Mount points that must exist before they can be overlaid.
mkdir -p "$REPO/.claude/agents" "$REPO/.claude/agent-memory" "$REPO/docs/prompts" \
  "$REPO/robotics-team/env" "$REPO/personal-team/env"
touch "$REPO/.claude/settings.json"

ro() { printf -- '--volume=%s:%s:ro' "$1" "$2"; }

args=(
  --rm -it --init
  --name "$NAME_PREFIX-$$"
  --userns=keep-id
  --cap-drop=all
  --security-opt=no-new-privileges
  --env-file "$ENV_FILE"
  -e CLAUDE_DEVCONTAINER=1
  -e TZ="$host_tz"
  -e HOME=/home/dev
  -e GIT_AUTHOR_NAME="$git_name" -e GIT_AUTHOR_EMAIL="$git_email"
  -e GIT_COMMITTER_NAME="$git_name" -e GIT_COMMITTER_EMAIL="$git_email"
  -e GIT_CONFIG_COUNT=2
  -e GIT_CONFIG_KEY_0=core.hooksPath -e GIT_CONFIG_VALUE_0=/opt/devcontainer/hooks
  -e GIT_CONFIG_KEY_1=safe.directory -e GIT_CONFIG_VALUE_1='*'
  -v clawake-dev-home:/home/dev:U
  # Own repo read-write ...
  -v "$REPO:$OWN:rw"
  # ... with team env files (real secrets) hidden (podman orders mounts by depth, so
  # these tmpfs mounts sit above the read-only team overlays below) ...
  --mount "type=tmpfs,destination=$OWN/robotics-team/env,tmpfs-mode=0700"
  --mount "type=tmpfs,destination=$OWN/personal-team/env,tmpfs-mode=0700"
  # ... and architect-owned paths read-only on top.
  "$(ro "$REPO/docs/architecture.md" "$OWN/docs/architecture.md")"
  "$(ro "$REPO/docs/project-overview.md" "$OWN/docs/project-overview.md")"
  "$(ro "$REPO/docs/user_journey.md" "$OWN/docs/user_journey.md")"
  "$(ro "$REPO/robotics-team" "$OWN/robotics-team")"
  "$(ro "$REPO/personal-team" "$OWN/personal-team")"
  "$(ro "$REPO/.claude/settings.json" "$OWN/.claude/settings.json")"
  "$(ro "$REPO/.claude/agents" "$OWN/.claude/agents")"
  "$(ro "$REPO/Containerfile" "$OWN/Containerfile")"
  "$(ro "$REPO/.devcontainer" "$OWN/.devcontainer")"
  "$(ro "$REPO/scripts/dev.sh" "$OWN/scripts/dev.sh")"
  # Foreign repo: only the reviewed, versioned parts Clawake has to match, read-only.
  # Never the whole checkout (state/, agent workspaces and packed .tgz stay invisible).
  "$(ro "$FOREIGN/deploy" "$W/clawonshape/deploy")"
  "$(ro "$FOREIGN/docs" "$W/clawonshape/docs")"
  "$(ro "$FOREIGN/scripts" "$W/clawonshape/scripts")"
  -w "$OWN"
)

[[ -f "$REPO/docs/ADR.md" ]] && args+=("$(ro "$REPO/docs/ADR.md" "$OWN/docs/ADR.md")")

# Worktree support: mount the common git dir at its host path when it lives outside REPO.
case "$git_common" in
  "$REPO"/*) ;;
  *)
    args+=(
      -v "$git_common:$git_common:rw"
      "$(ro "$git_common/config" "$git_common/config")"
      "$(ro "$git_common/info" "$git_common/info")"
      "$(ro "$git_common/hooks" "$git_common/hooks")"
    )
    ;;
esac

[[ $# -gt 0 ]] || set -- bash
# Herdr recognises agents by argv[0]; podman hides the containerised claude process from
# it, so present the launcher as "claude" when it starts Claude Code.
if [[ "$1" == claude ]]; then
  exec -a claude podman run "${args[@]}" "$IMAGE" "$@"
fi
exec podman run "${args[@]}" "$IMAGE" "$@"
