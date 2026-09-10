#!/usr/bin/env bash
#
# Insert one release entry at the top of docs/RELEASES.md.
#
# Extracted from an inline workflow step so the push job can RE-RUN it
# against freshly-pulled content instead of trying to rebase a commit
# that is guaranteed to conflict.
#
# Why that matters: every release entry is inserted at the same anchor
# (<!-- RELEASES_LOG_START -->). When two deploys land back to back,
# both commits touch the identical line range, so `git pull --rebase`
# hits a content conflict 100% of the time. The old retry loop assumed
# "the release entry is content-independent of whatever else landed on
# main" — true for ordinary merges, false for the one case the retry
# exists to handle.
#
# Regenerating is conflict-free by construction: we throw our commit
# away, take main as-is, and re-insert on top of whatever is now there.
#
# Usage: append-release-entry.sh <short_sha> <tag> <subject>
set -euo pipefail

SHORT_SHA="${1:?short sha required}"
TAG="${2:?tag required}"
SUBJECT="${3:?subject required}"
DATE_UTC="$(date -u +%Y-%m-%d)"
FILE="docs/RELEASES.md"

# A failed awk leaves a partial .tmp behind. The push step runs
# `git add docs/RELEASES.md` and `git status` in the same tree, so a
# stray file there is noise at best and a confusing diff at worst.
trap 'rm -f "${FILE}.tmp"' EXIT

# `|` is a sed delimiter elsewhere in the pipeline and newlines would
# break the two-line entry format, so flatten both.
SUBJECT_ESCAPED=$(printf '%s' "$SUBJECT" | tr '\n' ' ' | sed 's/|/\\|/g')

# Build the entry as three separate scalars rather than one multi-line
# awk variable. `awk -v x="a\nb"` is rejected outright by BSD awk
# ("newline in string") while GNU/mawk accept it — the exact class of
# dialect breakage the awk-over-sed choice below was meant to avoid.
HEADING="## ${DATE_UTC} · ${SHORT_SHA} · ${TAG}"

# awk rather than sed: multi-line insertion is dialect-specific
# (GNU vs BSD) and this runs on ubuntu-latest today but shouldn't
# silently break if that changes.
awk -v heading="$HEADING" -v subject="$SUBJECT_ESCAPED" '
  /<!-- RELEASES_LOG_START -->/ {
    print
    print ""
    print heading
    print subject
    print ""
    inserted = 1
    next
  }
  { print }
  END {
    if (!inserted) {
      # Losing release history silently is worse than failing the step.
      print "append-release-entry: RELEASES_LOG_START marker not found" > "/dev/stderr"
      exit 1
    }
  }
' "$FILE" > "${FILE}.tmp"

mv "${FILE}.tmp" "$FILE"
echo "Appended entry for ${SHORT_SHA} (${TAG})"
