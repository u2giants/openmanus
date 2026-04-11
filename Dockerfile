FROM python:3.11-slim
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN git clone https://github.com/FoundationAgents/OpenManus.git .
COPY custom_tools/ ./custom_tools/
COPY config.toml ./config.toml
RUN pip install --no-cache-dir --ignore-installed -r requirements.txt
EXPOSE 8000
CMD ["python", "main.py"]
