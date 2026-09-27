#!/bin/bash
# Double-click this to refresh the dashboard now, instead of waiting for the
# 6:30am scheduled run. Opens the dashboard when it finishes.
cd "$(dirname "$0")" || exit 1
echo "Refreshing the training dashboard..."
echo
./.venv/bin/python run_daily.py "$@"
status=$?
echo
if [ $status -eq 0 ]; then
  open dashboard.html
  echo "Done. The phone copy on claude.ai does not update from here —"
  echo "ask Claude Code to republish it when you want that refreshed."
else
  echo "The run failed (exit $status). See logs/daily_sync.err.log"
fi
echo
read -r -p "Press return to close."
