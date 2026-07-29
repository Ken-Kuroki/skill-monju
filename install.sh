#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SKILL_NAME="monju"

usage() {
  cat <<'EOF'
Install the Monju skill as a symbolic link for one or more coding agents.

Usage:
  ./install.sh [options]

Options:
  -a, --agent AGENT   Target agent: codex, cursor, claude, all
                      Repeatable. Default: interactive prompt.
  -s, --scope SCOPE   Install scope: personal (default) or project
  -p, --project PATH  Project root for project scope (default: current directory)
  -d, --dest PATH     Override the install directory (must end with the skill name
                      or be the parent skills directory; see --list)
  -f, --force         Replace or remove a symlink to a different target
  -u, --uninstall     Remove installed symlinks instead of creating them;
                      never removes regular files or directories
  -l, --list          Show supported agents and default install paths
  -n, --dry-run       Print actions without changing the filesystem
  -h, --help          Show this help

Examples:
  ./install.sh --agent codex
  ./install.sh --agent cursor --agent claude
  ./install.sh --agent all
  ./install.sh --agent cursor --scope project --project /path/to/repo
  ./install.sh --agent codex --uninstall
EOF
}

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

agent_label() {
  case "$1" in
    codex) printf 'Codex' ;;
    cursor) printf 'Cursor' ;;
    claude) printf 'Claude Code' ;;
    *) die "unknown agent: $1" ;;
  esac
}

agent_parent_dir() {
  local agent="$1"
  local scope="$2"
  local project_root="$3"

  case "${agent}:${scope}" in
    codex:personal) printf '%s/.codex/skills' "${HOME}" ;;
    codex:project) die "Codex does not support project-scoped skills; use personal scope." ;;
    cursor:personal) printf '%s/.cursor/skills' "${HOME}" ;;
    cursor:project) printf '%s/.cursor/skills' "${project_root}" ;;
    claude:personal) printf '%s/.claude/skills' "${HOME}" ;;
    claude:project) printf '%s/.claude/skills' "${project_root}" ;;
    *) die "unsupported agent/scope combination: ${agent}/${scope}" ;;
  esac
}

default_install_path() {
  local agent="$1"
  local scope="$2"
  local project_root="$3"
  printf '%s/%s' "$(agent_parent_dir "${agent}" "${scope}" "${project_root}")" "${SKILL_NAME}"
}

validate_repo() {
  [[ -f "${REPO_ROOT}/SKILL.md" ]] || die "SKILL.md not found in ${REPO_ROOT}"
}

resolve_dest_path() {
  local agent="$1"
  local scope="$2"
  local project_root="$3"
  local dest_override="${4:-}"

  if [[ -n "${dest_override}" ]]; then
    local dest="${dest_override}"
    while [[ "${dest}" != "/" && "${dest}" == */ ]]; do
      dest="${dest%/}"
    done
    if [[ "${dest}" == */skills ]]; then
      dest="${dest}/${SKILL_NAME}"
    fi
    [[ "$(basename "${dest}")" == "${SKILL_NAME}" ]] ||
      die "--dest must end with /${SKILL_NAME} or name a parent directory ending in /skills"
    printf '%s' "${dest}"
    return
  fi

  default_install_path "${agent}" "${scope}" "${project_root}"
}

ensure_parent_dir() {
  local parent="$1"
  local dry_run="$2"

  if [[ -d "${parent}" ]]; then
    return
  fi

  if [[ "${dry_run}" == "true" ]]; then
    log "mkdir -p ${parent}"
    return
  fi

  mkdir -p "${parent}"
}

install_link() {
  local dest="$1"
  local force="$2"
  local dry_run="$3"
  local uninstall="$4"

  validate_repo
  if [[ "${uninstall}" != "true" ]]; then
    ensure_parent_dir "$(dirname "${dest}")" "${dry_run}"
  fi

  if [[ -L "${dest}" ]]; then
    local current
    current="$(readlink "${dest}")"
    if [[ "${uninstall}" == "true" ]]; then
      if [[ "${current}" != "${REPO_ROOT}" && "${force}" != "true" ]]; then
        die "${dest} links to ${current}, not ${REPO_ROOT}; use --force to remove that symlink"
      fi
      if [[ "${dry_run}" == "true" ]]; then
        log "rm ${dest}"
      else
        rm "${dest}"
        log "removed ${dest}"
      fi
      return
    fi

    if [[ "${current}" == "${REPO_ROOT}" ]]; then
      log "already linked: ${dest} -> ${REPO_ROOT}"
      return
    fi

    [[ "${force}" == "true" ]] || die "${dest} already links to ${current}; use --force to replace"
    if [[ "${dry_run}" == "true" ]]; then
      log "rm ${dest}"
    else
      rm "${dest}"
    fi
  elif [[ -e "${dest}" ]]; then
    die "${dest} exists and is not a symlink; refusing to remove or replace it"
  elif [[ "${uninstall}" == "true" ]]; then
    log "not installed: ${dest}"
    return
  fi

  if [[ "${uninstall}" == "true" ]]; then
    return
  fi

  if [[ "${dry_run}" == "true" ]]; then
    log "ln -s ${REPO_ROOT} ${dest}"
  else
    ln -s "${REPO_ROOT}" "${dest}"
    log "linked ${dest} -> ${REPO_ROOT}"
  fi
}

list_targets() {
  cat <<EOF
Supported agents and default install paths:

  codex   (personal only)
          ${HOME}/.codex/skills/${SKILL_NAME}

  cursor  (personal)
          ${HOME}/.cursor/skills/${SKILL_NAME}

  cursor  (project)
          <project>/.cursor/skills/${SKILL_NAME}

  claude  (personal)
          ${HOME}/.claude/skills/${SKILL_NAME}

  claude  (project)
          <project>/.claude/skills/${SKILL_NAME}

Source repository:
  ${REPO_ROOT}
EOF
}

prompt_agents() {
  # Menus go to stderr so they remain visible under $(...).
  cat >&2 <<'EOF'
Select install target(s):

  1) codex
  2) cursor
  3) claude
  4) all
  5) cancel

EOF
  local choice
  read -r -p "Choice [1-5]: " choice </dev/tty
  case "${choice}" in
    1) printf '%s\n' codex ;;
    2) printf '%s\n' cursor ;;
    3) printf '%s\n' claude ;;
    4) printf '%s\n' "codex cursor claude" ;;
    5) printf '%s\n' "__cancel__" ;;
    *) die "invalid choice: ${choice}" ;;
  esac
}

prompt_scope() {
  local agent="$1"
  if [[ "${agent}" == "codex" ]]; then
    printf '%s' personal
    return
  fi

  # Menus go to stderr so they remain visible under $(...).
  cat >&2 <<EOF

Scope for $(agent_label "${agent}"):

  1) personal  (available in all projects)
  2) project   (only in one repository)

EOF
  local choice
  read -r -p "Choice [1-2, default 1]: " choice </dev/tty
  case "${choice}" in
    ""|1) printf '%s' personal ;;
    2) printf '%s' project ;;
    *) die "invalid choice: ${choice}" ;;
  esac
}

main() {
  local agents=()
  local scope="personal"
  local project_root
  project_root="$(pwd)"
  local dest_override=""
  local force="false"
  local dry_run="false"
  local uninstall="false"
  local interactive="true"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -a|--agent)
        [[ $# -ge 2 ]] || die "--agent requires a value"
        interactive="false"
        if [[ "$2" == "all" ]]; then
          agents=(codex cursor claude)
        else
          agents+=("$2")
        fi
        shift 2
        ;;
      -s|--scope)
        [[ $# -ge 2 ]] || die "--scope requires a value"
        scope="$2"
        shift 2
        ;;
      -p|--project)
        [[ $# -ge 2 ]] || die "--project requires a value"
        project_root="$(cd "$2" && pwd)"
        shift 2
        ;;
      -d|--dest)
        [[ $# -ge 2 ]] || die "--dest requires a value"
        dest_override="$2"
        shift 2
        ;;
      -f|--force)
        force="true"
        shift
        ;;
      -u|--uninstall)
        uninstall="true"
        shift
        ;;
      -l|--list)
        list_targets
        exit 0
        ;;
      -n|--dry-run)
        dry_run="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
  done

  case "${scope}" in
    personal|project) ;;
    *) die "scope must be personal or project" ;;
  esac

  if [[ "${interactive}" == "true" ]]; then
    if ! { : </dev/tty; } 2>/dev/null; then
      die "interactive selection requires a terminal; pass --agent explicitly"
    fi
    read -r -a agents <<< "$(prompt_agents)"
    if [[ ${#agents[@]} -eq 1 && "${agents[0]}" == "__cancel__" ]]; then
      exit 0
    fi
    if [[ "${scope}" == "personal" && ${#agents[@]} -eq 1 && "${agents[0]}" != "codex" ]]; then
      scope="$(prompt_scope "${agents[0]}")"
    elif [[ "${scope}" == "personal" && ${#agents[@]} -gt 1 ]]; then
      log "Using personal scope for all selected agents."
    fi
  fi

  [[ ${#agents[@]} -gt 0 ]] || die "no agent selected"

  local agent
  for agent in "${agents[@]}"; do
    case "${agent}" in
      codex|cursor|claude) ;;
      *) die "unsupported agent: ${agent}" ;;
    esac

    local effective_scope="${scope}"
    if [[ "${agent}" == "codex" && "${scope}" == "project" ]]; then
      log "note: Codex only supports personal scope; using ${HOME}/.codex/skills/${SKILL_NAME}"
      effective_scope="personal"
    fi

    local dest
    dest="$(resolve_dest_path "${agent}" "${effective_scope}" "${project_root}" "${dest_override}")"
    log "$(
      if [[ "${uninstall}" == "true" ]]; then
        printf 'uninstall %s (%s)' "$(agent_label "${agent}")" "${dest}"
      else
        printf 'install %s (%s)' "$(agent_label "${agent}")" "${dest}"
      fi
    )"
    install_link "${dest}" "${force}" "${dry_run}" "${uninstall}"
  done
}

main "$@"
