#!/usr/bin/env python3
"""Audit Git-selected source and optionally export it. Never pushes or prints secrets."""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
import zipfile

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent


def git_files():
    # Use the real index when present, so accidentally tracked private files are caught.
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                           capture_output=True, text=True)
    if found.returncode == 0:
        if Path(found.stdout.strip()).resolve() != ROOT.resolve():
            raise RuntimeError("Run this source from its own repository root, not a parent repository")
        return subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="immich-publish-index-") as folder:
        subprocess.run(["git", "init", "--bare", folder], check=True, capture_output=True)
        return subprocess.check_output(["git", "--git-dir=" + folder, "--work-tree=" + str(ROOT),
                                        "ls-files", "-z", "--others", "--exclude-standard"], cwd=ROOT)


def private_path(name):
    p = Path(name)
    if any(x in p.parts for x in ("generated", ".venv", "node_modules", ".wrangler", "__pycache__")):
        return True
    if p.name.startswith(".env"):
        return not (p.name.endswith(".example"))
    if name.replace("\\", "/").endswith("/node/config.json"):
        return True
    return p.name.startswith((".dev.vars", "id_rsa", "id_ed25519")) or p.name in ("project.json", "wrangler.jsonc", "worker-secrets.json", "known_hosts", "authorized_keys", ".pgpass") or p.suffix in (".key", ".pem", ".p12", ".ppk", ".pfx", ".dump", ".zip") or name.endswith((".sql", ".sql.gz"))


def known_secrets():
    values = []
    def add(label, value):
        if isinstance(value, str) and len(value) >= 8:
            values.append((label, value))
    env = APP / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and re.search(r"TOKEN|PASSWORD|SECRET|PASSPHRASE", key, re.I):
                add(key.strip(), value.strip().strip("\"'"))
    for relative in ("worker-secrets.json", "primary/config.json", "backup/config.json"):
        path = APP / "generated" / relative
        if path.is_file():
            for key, value in json.loads(path.read_text(encoding="utf-8-sig")).items():
                if re.search(r"TOKEN|PASSWORD|SECRET|PASSPHRASE", key, re.I):
                    add(key, value)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", action="store_true", help="Export source into generated/github-source.zip")
    args = parser.parse_args()
    names = sorted(set(x.decode("utf-8") for x in git_files().split(b"\0") if x))
    secrets = known_secrets()
    patterns = {
        "Slack webhook": re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]{20,}(?=[\s\"'<>)]|$)"),
        "Private key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
        "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})"),
    }
    findings = []
    files = []
    for name in names:
        path = ROOT / name
        if path.is_symlink() or private_path(name):
            findings.append({"file": name, "reason": "Private path or symlink selected by Git"})
            continue
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError("Selected path escapes workspace")
        files.append(name)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            labels = [label for label, value in secrets if value in line]
            labels += [label for label, pattern in patterns.items() if pattern.search(line)]
            if labels:
                findings.append({"file": name, "line": line_no, "reason": ", ".join(sorted(set(labels)))})
    report = {"files": len(files), "findings": findings, "selected": files}
    output = APP / "generated"
    output.mkdir(exist_ok=True)
    (output / "github-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(files), "findings": findings}, ensure_ascii=True))
    if findings:
        raise SystemExit("STOP: review selected private paths/secrets before publishing")
    if args.zip:
        target = output / "github-source.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(ROOT / name, name)
        print("Exported generated/github-source.zip (source only; no deployment keys)")


if __name__ == "__main__":
    main()
