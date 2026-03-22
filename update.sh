#!/usr/bin/env bash
set -euo pipefail
INTERVAL="${1:-60}"
BRANCH="$(git branch --show-current)"
log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}
while true; do
  log "=== nytt varv börjar ==="
  log "branch: $BRANCH"
  log "fetchar från origin"
  git fetch --prune origin
  if git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
    log "rebasing mot origin/$BRANCH"
    git rebase --autostash "origin/$BRANCH"
  else
    log "hittar inte origin/$BRANCH, skippar rebase"
  fi
  log "status före sync:"
  git status --short --branch || true
  log "kör sync_archive.py --limit-threads 5"
  python3 sync_archive.py --limit-threads 5
  log "kollar diff efter sync"
  data_changed=0
  archive_changed=0
  if ! git diff --quiet -- data; then
    data_changed=1
  fi
  if ! git diff --quiet -- archive.json archive_no_tag.json; then
    archive_changed=1
  fi
  if [[ "$data_changed" -eq 0 && "$archive_changed" -eq 0 ]]; then
    log "inget ändrat, ingen commit"
  else
    log "ändringar hittade: data=$data_changed archive=$archive_changed"
    git add data archive.json archive_no_tag.json
    if git diff --cached --quiet; then
      log "inget låg staged trots diff-koll, skippar commit"
    else
      if [[ "$archive_changed" -eq 1 ]]; then
        log "committar röständringar"
        git commit -m "uppdaterar röster"
        log "pushar till origin/$BRANCH"
        git push origin "$BRANCH"
      else
        log "committar bara lokala html/index-ändringar"
        git commit -m "synkar trådar lokalt"
      fi
    fi
  fi
  log "status efter varv:"
  git status --short --branch || true
  log "sover ${INTERVAL}s"
  sleep "$INTERVAL"
done
