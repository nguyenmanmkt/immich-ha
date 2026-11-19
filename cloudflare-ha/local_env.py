"""Small literal .env reader. Never executes or interpolates file contents."""
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def load_env(path=None, environ=None):
    path = Path(path or ROOT / ".env")
    values = {}
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
                raise ValueError(f"Invalid .env entry on line {number}")
            if value.startswith(('"', "'")):
                if len(value) < 2 or not value.endswith(value[0]):
                    raise ValueError(f"Invalid quoted .env value on line {number}")
                value = value[1:-1]
            if any(char in value for char in ("\r", "\n", "\x00")):
                raise ValueError(f"Invalid .env characters on line {number}")
            values[key] = value
    values.update(os.environ if environ is None else environ)
    return values


def origin_credentials(values, environment):
    from service_credentials import ID_KEY, SECRET_KEY, validate
    result = dict(values)
    # A supplied pair replaces the saved pair together; never mix secret versions.
    if environment.get(ID_KEY) or environment.get(SECRET_KEY):
        result[ID_KEY] = environment.get(ID_KEY, "")
        result[SECRET_KEY] = environment.get(SECRET_KEY, "")
    validate(result)
    return result
