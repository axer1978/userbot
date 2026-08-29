FROM python:3.12-slim

# tzdata so active hours honour the timezone in config.json
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The panel has no login. Binding 0.0.0.0 is safe ONLY because compose
# publishes the port to the host's loopback, never to a public interface.
ENV ADMIN_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# assistant.db and config.json live here, mounted as a volume so they survive
# a rebuild.
VOLUME ["/app/data"]

CMD ["python", "main.py"]
