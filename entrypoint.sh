#!/bin/sh
# Substitute env vars into config.toml at container startup
sed -i "s|__OPENAI_API_KEY__|${OPENAI_API_KEY:-placeholder}|g" /app/config/config.toml
sed -i "s|__DAYTONA_API_KEY__|${DAYTONA_API_KEY:-not-configured}|g" /app/config/config.toml
exec python main.py
