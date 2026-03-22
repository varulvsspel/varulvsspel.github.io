#!/usr/bin/env bash
set -euo pipefail
INTERVAL="${1:-60}"
while true; do
  git fetch origin
  git pull --rebase --autostash
  out="$(python3 sync_archive.py --limit-threads 5 2>&1)"
  printf '%s\n' "$out"
  case "$out" in
    *"RESULT=none"*)
      echo "Inget ändrat. Skippar commit/push"
      ;;
    *"RESULT=sync_only"*)
      if ! git diff --quiet -- data archive.json; then
        git add data archive.json
        git commit -m "synkar trådar"
      else
        echo "Sync-only, men inget diffar"
      fi
      ;;
    *"RESULT=votes_changed"*)
      if ! git diff --quiet -- data archive.json; then
        git add data archive.json
        git commit -m "uppdaterar röster"
        git push
      else
        echo "Röständring rapporterad, men inget diffar"
      fi
      ;;
    *)
      echo "Okänd status från sync_archive.py" >&2
      exit 1
      ;;
  esac
  sleep "$INTERVAL"
done
