#!/usr/bin/env bash
# One-command install for lexsearch: CLI symlink, omp agent and skill, config, first index.
# Run it from the clone. Re-running is safe: identical files are left alone, and
# differing files are only replaced with --force. Honours $HOME and LEXSEARCH_CONFIG,
# so it can be tested against a temporary home directory.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [--vault PATH] [--code PATH]... [--force]

  --vault PATH   markdown notes folder to index
  --code PATH    git repo to index (repeat for more repos)
  --force        replace differing files and config values
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
OMP_DIR="$HOME/.omp/agent"
CONFIG="${LEXSEARCH_CONFIG:-$HOME/.config/lexsearch/config.json}"

VAULT=""
CODE=()
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --vault) [ $# -ge 2 ] || die "--vault needs a path"; VAULT="$2"; shift 2 ;;
    --vault=*) VAULT="${1#*=}"; shift ;;
    --code) [ $# -ge 2 ] || die "--code needs a path"; CODE+=("$2"); shift 2 ;;
    --code=*) CODE+=("${1#*=}"); shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 not found; install Python 3 first"
python3 -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")' 2>/dev/null \
  || die "python3 has no SQLite FTS5 support; use a Python build that includes it (for example Homebrew python3)"

# Copy SRC to DST. Identical files are left alone; differing ones need --force.
place_file() {
  local src="$1" dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    say "ok       $dst (unchanged)"
    return
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if [ "$FORCE" -eq 0 ]; then
      say "skipped  $dst (exists and differs; rerun with --force to replace)"
      return
    fi
    rm -f "$dst"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    say "replaced $dst"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  say "wrote    $dst"
}

# 1. CLI: symlink so `git pull` in the clone updates the installed command.
mkdir -p "$BIN_DIR"
LINK="$BIN_DIR/lexsearch"
TARGET="$HERE/lexsearch.py"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$TARGET" ]; then
  say "ok       $LINK -> $TARGET (unchanged)"
elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
  if [ "$FORCE" -eq 0 ]; then
    say "skipped  $LINK (exists; rerun with --force to replace)"
  else
    rm -f "$LINK"
    ln -s "$TARGET" "$LINK"
    say "replaced $LINK -> $TARGET"
  fi
else
  ln -s "$TARGET" "$LINK"
  say "linked   $LINK -> $TARGET"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on PATH; add: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# 2. omp agent and skill.
place_file "$HERE/omp/agents/retriever.md" "$OMP_DIR/agents/retriever.md"
place_file "$HERE/omp/skills/lexsearch/SKILL.md" "$OMP_DIR/skills/lexsearch/SKILL.md"

# 3. Glossary: only ever created, never overwritten, because it is personal.
GLOSSARY="$OMP_DIR/glossary.md"
if [ -e "$GLOSSARY" ]; then
  say "kept     $GLOSSARY (exists; never overwritten)"
else
  mkdir -p "$OMP_DIR"
  cp "$HERE/glossary.example.md" "$GLOSSARY"
  say "wrote    $GLOSSARY (copied from glossary.example.md; add your own terms)"
fi

# 4. Config: merge the flags into the existing file. Keys not mentioned stay as they are.
if [ -n "$VAULT" ] && [ ! -d "$VAULT" ]; then
  warn "vault folder does not exist yet: $VAULT"
fi
for dir in ${CODE[@]+"${CODE[@]}"}; do
  [ -d "$dir" ] || warn "code folder does not exist yet: $dir"
done
python3 - "$CONFIG" "$FORCE" "$VAULT" ${CODE[@]+"${CODE[@]}"} <<'PY'
import json
import os
import sys

path, force, vault, codes = sys.argv[1], sys.argv[2] == "1", sys.argv[3], sys.argv[4:]


def norm(p):
    return os.path.abspath(os.path.expanduser(p))


if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {path} is not readable JSON ({exc}); fix or remove it, then rerun")
    if not isinstance(cfg, dict):
        sys.exit(f"error: {path} must hold a JSON object; fix or remove it, then rerun")
else:
    cfg = {}
before = json.dumps(cfg, sort_keys=True)

cfg.setdefault("vault", None)
cfg.setdefault("sessions", "~/.omp/agent/sessions")
cfg.setdefault("code", [])

if vault:
    new, old = norm(vault), cfg["vault"]
    if old in (None, new) or force:
        cfg["vault"] = new
    else:
        print(f"skipped  config vault stays {old} (rerun with --force to change it to {new})")

if codes:
    wanted = [norm(c) for c in codes]
    if force:
        cfg["code"] = list(dict.fromkeys(wanted))
    else:
        current = list(cfg["code"])
        cfg["code"] = current + [c for c in dict.fromkeys(wanted) if c not in current]

if os.path.exists(path) and json.dumps(cfg, sort_keys=True) == before:
    print(f"ok       {path} (unchanged)")
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote    {path}")
PY

# 5. First index. Calls the clone's script directly, so PATH does not matter here.
say ""
say "indexing..."
python3 "$HERE/lexsearch.py" index

say ""
say "Installed. Try: lexsearch search \"<words you remember>\""
