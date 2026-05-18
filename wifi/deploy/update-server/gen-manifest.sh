#!/usr/bin/env bash
# gen-manifest.sh — Walk /var/lib/helen-updates/ and emit manifest.json.
# Run after dropping a new release into a versioned folder.
#
# Usage:
#   sudo bash gen-manifest.sh > /var/lib/helen-updates/manifest.json
set -euo pipefail

ROOT="${1:-/var/lib/helen-updates}"

emit_artefact() {
  local file="$1"
  local rel="${file#$ROOT/}"
  local size sha
  size=$(stat -c %s "$file" 2>/dev/null || stat -f %z "$file")
  if [ -f "$file.sha256" ]; then
    sha=$(awk '{print $1}' "$file.sha256")
  else
    sha=$(sha256sum "$file" | awk '{print $1}')
  fi
  printf '      {"path": "/%s", "size": %s, "sha256": "%s"}' "$rel" "$size" "$sha"
}

emit_component() {
  local component="$1"
  local first=1
  printf '  "%s": {\n    "versions": [\n' "$component"
  if [ -d "$ROOT/$component" ]; then
    for ver_dir in "$ROOT/$component"/*/; do
      [ -d "$ver_dir" ] || continue
      local ver
      ver=$(basename "$ver_dir")
      [ "$ver" = "stable" ] && continue
      [ $first -eq 0 ] && printf ',\n'
      first=0
      printf '      {\n        "version": "%s",\n        "released": "%s",\n        "files": [\n' \
        "$ver" "$(date -u -r "$ver_dir" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
      local file_first=1
      while IFS= read -r f; do
        [ -f "$f" ] || continue
        case "$f" in
          *.sha256) continue ;;
        esac
        [ $file_first -eq 0 ] && printf ',\n'
        file_first=0
        emit_artefact "$f"
      done < <(find "$ver_dir" -maxdepth 1 -type f | sort)
      printf '\n        ]\n      }'
    done
  fi
  printf '\n    ],\n    "stable": "%s"\n  }' \
    "$( [ -L "$ROOT/$component/stable" ] && readlink "$ROOT/$component/stable" || echo "" )"
}

cat <<EOF
{
  "manifest_version": 1,
  "generated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "channel": "stable",
EOF

components=(server rendezvous desktop mobile admin)
first=1
for c in "${components[@]}"; do
  [ $first -eq 0 ] && printf ',\n'
  first=0
  emit_component "$c"
done
printf '\n}\n'
