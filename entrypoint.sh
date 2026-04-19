#!/bin/sh
# Substitute env vars into config.toml at container startup
mkdir -p /app/user_tools
sed -i "s|__OPENAI_API_KEY__|${OPENAI_API_KEY:-placeholder}|g" /app/config/config.toml
sed -i "s|__DAYTONA_API_KEY__|${DAYTONA_API_KEY:-not-configured}|g" /app/config/config.toml
sed -i "s|__BROWSER_CDP_URL__|${BROWSER_CDP_URL:-http://novnc:9222}|g" /app/config/config.toml
sed -i "s|__OPENAI_API_BASE_URL__|${OPENAI_API_BASE_URL:-https://openrouter.ai/api/v1}|g" /app/config/config.toml

# Seed bundled tools into the user_tools volume (always overwrite so updates deploy cleanly).
for f in /app/bundled_tools/*.py; do
  [ -f "$f" ] && cp "$f" /app/user_tools/
done

exec python server.py
