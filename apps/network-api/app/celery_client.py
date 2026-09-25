import os

from celery import Celery


CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL",
    "redis://redis-master.network-platform.svc.cluster.local:6379/0",
)

# The API only enqueues tasks by name and reads their results; it never imports
# worker code or runs device operations itself.
celery_app = Celery(
    "network_api",
    broker=CELERY_BROKER_URL,
    backend=CELERY_BROKER_URL,
)

celery_app.conf.update(
    broker_connection_timeout=5,
    redis_socket_connect_timeout=5,
    redis_socket_timeout=10,
)

PUBLISH_RETRY_POLICY = {
    "max_retries": 2,
    "interval_start": 0,
    "interval_step": 0.5,
    "interval_max": 1,
}
