FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    openssl \
    smbclient \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Plugin is baked in — mount /data for persistent DB + key
COPY entrypoint.sh  /entrypoint.sh
COPY healthcheck.py /healthcheck.py
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]

ENV NASNAP_DATA=/data

CMD ["/entrypoint.sh"]
