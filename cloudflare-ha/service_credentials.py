"""Load and validate optional origin Access credentials, without logging values."""
import json
from pathlib import Path

ID_KEY = "ORIGIN_ACCESS_CLIENT_ID"
SECRET_KEY = "ORIGIN_ACCESS_CLIENT_SECRET"


def validate(values):
    client_id, secret = values.get(ID_KEY), values.get(SECRET_KEY)
    if client_id is None and secret is None:
        return False
    if not isinstance(client_id, str) or not client_id.endswith(".access") or len(client_id) > 256:
        raise ValueError("Origin Access Client ID is missing or invalid")
    if not isinstance(secret, str) or len(secret) < 32 or len(secret) > 1024:
        raise ValueError("Origin Access Client Secret is missing or invalid")
    if any(char in client_id + secret for char in ("\r", "\n", "\x00")):
        raise ValueError("Invalid characters in origin Access credentials")
    return True


def headers(values):
    if not validate(values):
        return {}
    return {"CF-Access-Client-Id": values[ID_KEY], "CF-Access-Client-Secret": values[SECRET_KEY]}
