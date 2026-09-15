import os
import hvac
from celery import Celery

REDIS_URL = os.getenv(
    "CELERY_BROKER_URL",
    "redis://redis-master.network-platform.svc.cluster.local:6379/0",
)

VAULT_ADDR = os.getenv(
    "VAULT_ADDR",
    "http://vault.security.svc:8200",
)

VAULT_ROLE = "network-automation-worker"

app = Celery(
    "network_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
)


@app.task(name="network_worker.health_check")
def health_check():
    return {
        "status": "ok",
        "worker": "network-automation-worker",
    }


def get_vault_client():
    with open(
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
        "r"
    ) as token_file:
        jwt = token_file.read()

    client = hvac.Client(url=VAULT_ADDR)

    response = client.auth.kubernetes.login(
        role=VAULT_ROLE,
        jwt=jwt,
    )

    client.token = response["auth"]["client_token"]

    return client


@app.task(name="network_worker.vault_test")
def vault_test():
    client = get_vault_client()

    secret = client.secrets.kv.v2.read_secret_version(
        mount_point="kv",
        path="network/dell-os10",
        raise_on_deleted_version=True,
    )

    secret_data = secret["data"]["data"]

    # Do NOT return credential values.
    return {
        "status": "ok",
        "vault_authenticated": True,
        "secret_path": "kv/network/dell-os10",
        "available_fields": list(secret_data.keys()),
    }
