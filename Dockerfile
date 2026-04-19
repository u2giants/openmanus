FROM python:3.11-slim
RUN apt-get update && apt-get install -y git curl unzip && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rclone.zip \
    && unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && mv /tmp/rclone/rclone-*-linux-amd64/rclone /usr/local/bin/rclone \
    && chmod +x /usr/local/bin/rclone \
    && rm -rf /tmp/rclone /tmp/rclone.zip
WORKDIR /app
RUN git clone https://github.com/FoundationAgents/OpenManus.git .
COPY custom_tools/ ./custom_tools/
COPY fidelity_mcp/ ./fidelity_mcp/
COPY fidelity_browser_tool.py ./bundled_tools/fidelity_browser_tool.py
COPY config.toml ./config/config.toml
COPY entrypoint.sh ./entrypoint.sh
COPY server.py ./server.py
COPY launch_visible_chrome.sh ./launch_visible_chrome.sh
RUN chmod +x ./entrypoint.sh ./launch_visible_chrome.sh
RUN sed -i 's/pillow~=11.1.0/pillow/' requirements.txt && pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir structlog "daytona-sdk" fastapi "uvicorn[standard]" httpx websockets
RUN pip install --no-cache-dir playwright && playwright install chromium && (playwright install-deps chromium || true)
# Shim: daytona-sdk installs as 'daytona_sdk' module but code imports 'from daytona import ...'
RUN mkdir -p /usr/local/lib/python3.11/site-packages/daytona && \
    echo 'from daytona_sdk import *' > /usr/local/lib/python3.11/site-packages/daytona/__init__.py && \
    echo 'from daytona_sdk import Daytona, DaytonaConfig, Sandbox, SandboxState' >> /usr/local/lib/python3.11/site-packages/daytona/__init__.py
EXPOSE 8000
CMD ["./entrypoint.sh"]
