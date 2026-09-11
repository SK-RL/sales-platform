#!/usr/bin/env bash
# Install (or reinstall) the weekly laptop backup pull as a macOS launchd job.
# launchd is the Mac's cron: unlike cron it runs a missed slot when the laptop
# wakes, so a closed lid on Sunday morning does not skip the week.
#   bash platform/scripts/install-laptop-backup-job.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.reventlabs.sales-platform.backup-pull"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/db-backups"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$REPO/platform/scripts/pull-backup-to-laptop.sh</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>10</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>$REPO/db-backups/pull.log</string>
  <key>StandardErrorPath</key><string>$REPO/db-backups/pull.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
  </dict>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null && echo "Installed: $LABEL — every Sunday 10:00 local, log: $REPO/db-backups/pull.log"
