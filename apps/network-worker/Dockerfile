FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# TShark for offline analysis of uploaded captures (PCAP Analyzer). CLI only: no GUI.
# dumpcap is removed: the platform never captures live traffic, and nothing may gain
# capture privileges through this image.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark=4.4.18-0+deb13u1 \
    && rm -f /usr/bin/dumpcap \
    && rm -rf /var/lib/apt/lists/*

COPY worker.py .
COPY scheduling.py .
COPY inventory ./inventory
COPY tasks ./tasks
COPY vault ./vault
COPY db ./db
COPY health ./health
COPY topology ./topology
COPY telemetry ./telemetry
COPY interfaces ./interfaces
COPY pcap ./pcap
COPY changes ./changes
COPY coordination ./coordination
COPY ansible ./ansible
COPY collections-requirements.yml .

RUN mkdir -p /usr/share/ansible/collections && \
    ansible-galaxy collection install \
    -r collections-requirements.yml \
    -p /usr/share/ansible/collections

RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 \
            --gid 10001 \
            --create-home \
            --shell /usr/sbin/nologin \
            appuser && \
    chown -R appuser:appgroup /app

USER appuser

CMD ["celery", "-A", "worker.app", "worker", "--loglevel=INFO"]
