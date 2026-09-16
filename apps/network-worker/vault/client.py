import os
import hvac

VAULT_ADDR = os.getenv(
    "VAULT_ADDR",
    "http://vault.security.svc:8200",
)

VAULT_ROLE = "network-automation-worker"


def get_vault_client():
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"

    with open(token_path, "r") as token_file:
        jwt = token_file.read().strip()

    client = hvac.Client(url=VAULT_ADDR)

    response = client.auth.kubernetes.login(
        role=VAULT_ROLE,
        jwt=jwt,
    )

    client.token = response["auth"]["client_token"]

    return client


def get_device_credentials(path):
    client = get_vault_client()

    secret = client.secrets.kv.v2.read_secret_version(
        mount_point="kv",
        path=path,
        raise_on_deleted_version=True,
    )

    return secret["data"]["data"]
