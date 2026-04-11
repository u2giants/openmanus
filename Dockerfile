FROM python:3.11-slim
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN git clone https://github.com/FoundationAgents/OpenManus.git .
COPY custom_tools/ ./custom_tools/
COPY config.toml ./config/config.toml
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh
RUN sed -i 's/pillow~=11.1.0/pillow/' requirements.txt && pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir structlog
EXPOSE 8000
CMD ["./entrypoint.sh"]
