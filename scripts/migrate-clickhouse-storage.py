"""Move one stopped ClickHouse instance to its verified XFS volume."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=120)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def query(port, sql):
    return run("clickhouse-client", "--port", port, "--query", sql).strip()


def migrate(args):
    require(os.geteuid() == 0, "Root required")
    require(re.fullmatch(r"ch[1-3]r[1-2]", args.instance), "Invalid instance")
    require(Path("/etc/storage-lab-id").read_text().strip() == args.lab_id, "VM identity mismatch")
    source = Path("/var/lib/clickhouse-lab") / args.instance
    target = Path("/data") / args.instance
    config = Path("/etc/clickhouse-server") / args.instance / "config.xml"
    unit = "clickhouse-server@" + args.instance + ".service"
    original = config.read_bytes()
    mount = run("findmnt", "-n", "-o", "TARGET,FSTYPE,UUID", "--mountpoint", str(target)).split()
    require(mount == [str(target), "xfs", args.xfs_uuid], "Unexpected target filesystem")
    require(str(target) + "/clickhouse/" not in original.decode(), "Instance already migrated")
    require(str(source) + "/clickhouse/" in original.decode(), "Unexpected source configuration")
    require(not any(target.iterdir()), "Target is not empty")
    require(source.is_dir() and not source.is_symlink(), "Unexpected source directory")
    status = query(args.port, "SELECT count() FROM system.replicas WHERE is_readonly OR is_session_expired OR active_replicas < 2 OR queue_size > 0")
    require(status == "0", "Replicas are not healthy")
    query(args.port, "SYSTEM SYNC REPLICA analytics.events")
    expected = query(args.port, "SELECT count(),sum(cityHash64(*)) FROM analytics.events FORMAT TSV")
    backup = Path("/var/backups/itmo-storage") / (args.instance + "-" + uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700)
    dropin = Path("/etc/systemd/system") / (unit + ".d") / "storage.conf"
    require(not dropin.exists(), "Existing storage drop-in requires review")
    try:
        run("systemctl", "stop", unit)
        require(run("systemctl", "show", "--value", "-p", "MainPID", unit).strip() == "0", "Instance process did not stop")
        run("tar", "-C", "/", "-cpf", str(backup / "snapshot.tar"),
            str(source).lstrip("/"), str(config).lstrip("/"))
        shutil.copy2(config, backup / "config.xml")
        run("cp", "-a", str(source) + "/.", str(target))
        run("diff", "-qr", str(source), str(target))
        config.write_bytes(original.replace(str(source).encode(), str(target).encode()))
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text("[Unit]\nRequiresMountsFor=" + str(target) + "\nConditionPathIsMountPoint=" + str(target) + "\n[Service]\nSuccessExitStatus=143\n")
        run("systemctl", "daemon-reload")
        run("systemctl", "start", unit)
        for _ in range(60):
            try:
                query(args.port, "SELECT 1")
                break
            except subprocess.SubprocessError:
                time.sleep(2)
        query(args.port, "SYSTEM SYNC REPLICA analytics.events")
        actual = query(args.port, "SELECT count(),sum(cityHash64(*)) FROM analytics.events FORMAT TSV")
        require(actual == expected, "Row count or checksum changed")
        require(query(args.port, "SELECT count() FROM system.replicas WHERE is_readonly OR is_session_expired") == "0", "Replica is not writable")
        print(json.dumps({"instance": args.instance, "backup": str(backup),
                          "mount": str(target), "rows_checksum": actual, "status": "migrated"}))
    except Exception:
        run("systemctl", "stop", unit)
        config.write_bytes(original)
        if dropin.exists():
            dropin.unlink()
        run("systemctl", "daemon-reload")
        run("systemctl", "start", unit)
        print("Restored original configuration and source directory", flush=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("instance", "port", "lab-id", "xfs-uuid"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    with open("/run/lock/itmo-storage-migrate", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        migrate(args)
