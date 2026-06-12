FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Plugin is baked in — mount /data for persistent DB + key
VOLUME ["/data"]
EXPOSE 5000

ENV NASNAP_DATA=/data \
    PORT=5000

CMD ["sh", "-c", "mkdir -p /root/.ssh && chmod 700 /root/.ssh && gunicorn -w ${WORKERS:-2} -b 0.0.0.0:${PORT:-5000} --timeout 120 --access-logfile - 'app:create_app()'"]
