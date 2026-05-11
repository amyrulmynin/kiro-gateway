#!/bin/sh
set -e

# Fix ownership on mounted volumes (they come as root from Docker)
chown -R kiro:kiro /app/data /app/debug_logs 2>/dev/null || true

# Drop privileges and exec the main command as kiro user
exec gosu kiro "$@"
