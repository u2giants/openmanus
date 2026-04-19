FROM python:3.11-slim
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN git clone https://github.com/FoundationAgents/OpenManus.git .
COPY custom_tools/ ./custom_tools/
COPY fidelity_mcp/ ./fidelity_mcp/
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
