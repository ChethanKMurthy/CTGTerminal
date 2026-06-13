#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Promote ONE queued improvement from the local `roadmap` branch onto `main`,
# stamp it with TODAY's date (a genuine, current-dated commit — never backdated),
# push it, and log it. Designed to be fired once per day by launchd.
#
# State: scripts/release_queue.txt (ordered commit SHAs) + .release_progress (idx)
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

QUEUE="scripts/release_queue.txt"
PROG=".release_progress"
mkdir -p logs
exec >>logs/release.log 2>&1
echo "=== $(date) — daily release ==="

[ -f "$QUEUE" ] || { echo "no release queue found"; exit 0; }

idx=$(cat "$PROG" 2>/dev/null || echo 0)
total=$(grep -c . "$QUEUE")
if [ "$idx" -ge "$total" ]; then
  echo "queue exhausted ($idx/$total) — nothing to release today"
  exit 0
fi

sha=$(sed -n "$((idx + 1))p" "$QUEUE")
echo "releasing item $((idx + 1))/$total  ->  $sha"

git checkout -q main
git pull -q --ff-only origin main 2>/dev/null || true

# Apply the queued change. (no -x so the message stays clean)
if ! git cherry-pick "$sha"; then
  git cherry-pick --abort 2>/dev/null || true
  echo "!! cherry-pick conflict on $sha — skipping today, needs manual review"
  exit 1
fi

# Re-stamp author date to NOW so the contribution lands on the actual day,
# and fold a changelog line into the same commit (keeps history clean).
subj=$(git log -1 --format=%s)
printf -- "- %s: %s\n" "$(date +%Y-%m-%d)" "$subj" >> docs/CHANGELOG.md
git add docs/CHANGELOG.md
git commit -q --amend --no-edit --date=now

git push -q origin main
echo "$((idx + 1))" > "$PROG"
echo "released: $subj"

# Best-effort: reload the running service so the improvement takes effect.
launchctl kickstart -k "gui/$(id -u)/com.ctg.alpha" 2>/dev/null || true
echo "done."
