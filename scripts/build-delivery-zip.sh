#!/usr/bin/env bash
# Builds the delivery ZIP for the consegna (POSIX counterpart of build-delivery-zip.ps1).
#
# Wraps `git archive`, which ships ONLY tracked files:
#   * anything .gitignore'd (user_credentials.json, .venv/, caches, outputs.txt,
#     debug.log) can never end up in the archive — no secret can leak;
#   * files marked `export-ignore` in .gitattributes (CLAUDE.md, docs/lessons.md)
#     are dropped even though they are tracked.
#
# The archive reflects HEAD, not the working tree: commit before building.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

if [ -n "$(git status --porcelain)" ]; then
    echo "Uncommitted changes — 'git archive' exports HEAD, so these would NOT be included:" >&2
    git status --short >&2
    echo "Commit (or stash) first, then re-run." >&2
    exit 1
fi

out="$repo/snap4city-mobility-mcp-consegna.zip"
rm -f "$out"
git archive --format=zip --prefix=snap4city-mobility-mcp/ -o "$out" HEAD

entries="$(unzip -Z1 "$out")"

for f in CLAUDE.md docs/lessons.md user_credentials.json; do
    if grep -qx "snap4city-mobility-mcp/$f" <<<"$entries"; then
        echo "Archive contains a file that must not ship: $f" >&2
        exit 1
    fi
done

for f in README.md LICENSE api.py relazione/relazione.tex docs/snap4city-api-notes.md; do
    if ! grep -qx "snap4city-mobility-mcp/$f" <<<"$entries"; then
        echo "Archive is missing an expected file: $f" >&2
        exit 1
    fi
done

for dir in docs/diagrams screenshots examples src tests frontend; do
    printf '%-16s %3s file\n' "$dir" "$(grep -c "^snap4city-mobility-mcp/$dir/" <<<"$entries" || true)"
done

if ! grep -qx "snap4city-mobility-mcp/relazione/relazione.pdf" <<<"$entries"; then
    echo "NOTE: relazione.pdf is not in the archive — compile it (see relazione/README.md), commit it, and re-run."
fi

printf '%s entries -> %s\n' "$(wc -l <<<"$entries")" "$out"
