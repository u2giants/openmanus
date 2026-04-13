#!/bin/sh
# Substitute env vars into config.toml at container startup
mkdir -p /app/user_tools
sed -i "s|__OPENAI_API_KEY__|${OPENAI_API_KEY:-placeholder}|g" /app/config/config.toml
sed -i "s|__DAYTONA_API_KEY__|${DAYTONA_API_KEY:-not-configured}|g" /app/config/config.toml
sed -i "s|__BROWSER_CDP_URL__|${BROWSER_CDP_URL:-http://novnc:9222}|g" /app/config/config.toml
sed -i "s|__OPENAI_API_BASE_URL__|${OPENAI_API_BASE_URL:-https://openrouter.ai/api/v1}|g" /app/config/config.toml
exec python server.py
