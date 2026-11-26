"""Read-only discovery and validation for one independently identified storage stack."""
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

DEV_ID = re.compile(r"/dev/disk/by-id/[A-Za-z0-9_.:+-]+")
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_+-]{0,63}")
MD = re.compile(r"/dev/md[0-9]+")
MOUNT = re.compile(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+")
PROGRAM = "/usr/local/sbin/storage-lab-mdadm-event"
CONFIG = Path("/etc/mdadm/mdadm.conf")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*argv, codes=(0,)):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                            env={**os.environ, "LC_ALL": "C"})
    require(result.returncode in codes,
            f"{argv[0]}: {' '.join(argv[1:])}: {result.stderr.strip()}")
    return result


def integer(value):
    return int(Decimal(str(value).strip()))


def block_id(path):
    info = Path(path).stat()
    require(stat.S_ISBLK(info.st_mode), f"Not a block device: {path}")
    return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"


def signature_types(path):
    result = run("wipefs", "--no-act", "--json", "--output", "TYPE", path)
    return {row["type"] for row in json.loads(result.stdout)["signatures"]}


def blkid(path):
    result = run("blkid", "-p", "-o", "export", path, codes=(0, 2))
    require(result.returncode != 2 or not result.stderr.strip(),
            f"blkid could not determine signatures on {path}: {result.stderr.strip()}")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def md_properties(output):
    return dict((key.strip(), value.strip()) for key, value in
                (line.split(" : ", 1) for line in output.splitlines() if " : " in line))


def examine_member(path, expected):
    result = run("mdadm", "--examine", path, codes=(0, 1))
    if expected:
        props = md_properties(result.stdout)
        require(result.returncode == 0 and props.get("Array UUID") == expected,
                f"Unexpected md metadata on {path}")
    else:
        require(result.returncode == 1 and "No md superblock detected" in result.stderr
                and not result.stdout.strip(), f"Unexpected md metadata or probe failure on {path}")


def mounts(*flags):
    result = run("findmnt", *flags, "--json", "--list", "--output",
                 "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN", codes=(0, 1))
    if result.returncode == 1:
        require(not result.stderr.strip(), "findmnt failed")
        if not result.stdout.strip():
            return []
    return json.loads(result.stdout).get("filesystems") or []


def lvm_report(name, columns):
    result = run(name, "--readonly", "--config", 'devices { hints="none" }',
                 "--reportformat", "json", "--units", "b",
                 "--nosuffix", "-o", ",".join(columns))
    require(not result.stderr.strip(), f"Uncertain LVM state: {result.stderr.strip()}")
    rows = json.loads(result.stdout)["report"][0][name[:-1]]
    return [{key: str(value).strip() for key, value in row.items()} for row in rows]


def directives(spec, expected_md):
    text = CONFIG.read_text()
    lines = text.splitlines()
    begin = "# BEGIN STORAGE LAB MANAGED"
    end = "# END STORAGE LAB MANAGED"
    require(lines.count(begin) == lines.count(end) <= 1, "Broken managed mdadm.conf block")
    managed = []
    if begin in lines:
        start, stop = lines.index(begin), lines.index(end)
        require(start < stop, "Invalid mdadm.conf block order")
        managed = lines[start + 1:stop]
        lines = lines[:start] + lines[stop + 1:]
    records = []
    for line in lines + managed:
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        if line[0].isspace() and records:
            records[-1].extend(body.split())
        else:
            records.append(body.split())
    desired = [f"ARRAY {spec['md']} UUID={expected_md}"]
    if spec["monitor"]:
        desired.append(f"PROGRAM {PROGRAM}")
    for record in records:
        kind = record[0].upper()
        if kind == "PROGRAM":
            require(record == ["PROGRAM", PROGRAM] and spec["monitor"],
                    "Conflicting mdadm PROGRAM")
        if kind == "ARRAY":
            ids = [word[5:].lower() for word in record if word.lower().startswith("uuid=")]
            same = (len(record) > 1 and record[1] == spec["md"]) or expected_md in ids
            if same:
                require(len(record) > 1 and record[1] == spec["md"] and ids == [expected_md],
                        "Conflicting mdadm ARRAY path or UUID")
    # Each invocation updates one stack and preserves sibling ARRAY records.
    managed_arrays = []
    for line in managed:
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        words = body.split()
        require(words[0].upper() in ("ARRAY", "PROGRAM"),
                "Unexpected directive in managed mdadm block")
        if words[0].upper() == "ARRAY":
            require(len(words) == 3 and MD.fullmatch(words[1])
                    and re.fullmatch(r"UUID=(?:[0-9a-f]{8}:){3}[0-9a-f]{8}", words[2]),
                    "Invalid managed ARRAY directive")
            managed_arrays.append(" ".join(words))
    require(len(managed_arrays) == len(set(managed_arrays))
            and len({line.split()[1] for line in managed_arrays}) == len(managed_arrays)
            and len({line.split()[2] for line in managed_arrays}) == len(managed_arrays),
            "Duplicate managed ARRAY path or UUID")
    siblings = [line for line in managed_arrays if line.split()[1] != spec["md"]]
    unmanaged = {line.strip() for line in lines}
    for item in desired:
        # Respect identical existing unmanaged entries without copying them.
        kind = item.split()[0]
        if kind == "ARRAY":
            matching = [line for line in lines if line.split() and
                        line.split()[0].upper() == "ARRAY" and spec["md"] in line.split()]
        else:
            matching = [line for line in lines if line.split() and
                        line.split()[0].upper() == "PROGRAM"]
        require(len(matching) <= 1, f"Duplicate unmanaged {kind}")
        if matching:
            unmanaged.add(item)
    result = siblings + [line for line in desired if line not in unmanaged]
    arrays = sorted(line for line in result if line.startswith("ARRAY "))
    return arrays + [line for line in result if line.startswith("PROGRAM ")]


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def plan_ids(spec, members):
    """Derive durable storage IDs separately from approval of an execution plan."""
    raid_identity = {
        "machine_id": spec["machine_id"],
        "lab_id": spec["lab_id"],
        # Physical identity and slot order matter. A by-id alias does not.
        "slots": [[serial, int(size)] for _, serial, size in members],
        "level": int(spec["level"]),
        "layout": spec["layout"],
        "chunk_kib": int(spec["chunk"]),
        "metadata": "1.2",
    }
    derived_md = uuid.uuid5(uuid.NAMESPACE_OID,
                            "storage-lab:md:v2:" + canonical_json(raid_identity)).hex
    formatted_md = ":".join(derived_md[i:i+8] for i in range(0, 32, 8))
    md_uuid = spec["expected_md_uuid"] or formatted_md
    require(re.fullmatch(r"(?:[0-9a-f]{8}:){3}[0-9a-f]{8}", md_uuid),
            "Invalid expected md UUID")

    xfs_identity = {"md_uuid": md_uuid, "vg": spec["vg"], "lv": spec["lv"]}
    derived_xfs = str(uuid.uuid5(uuid.NAMESPACE_OID,
                                 "storage-lab:xfs:v2:" + canonical_json(xfs_identity)))
    xfs_uuid = spec["expected_xfs_uuid"] or derived_xfs
    require(str(uuid.UUID(xfs_uuid)) == xfs_uuid, "Invalid expected XFS UUID")

    approval_spec = {key: value for key, value in spec.items()
                     if key not in ("members", "expected_md_uuid", "expected_xfs_uuid")}
    approval_spec.update(level=int(spec["level"]), chunk=int(spec["chunk"]),
                         lv_mib=int(spec["lv_mib"]))
    approval_payload = {
        "version": 2,
        "spec": approval_spec,
        "members": members,
        "md_uuid": md_uuid,
        "xfs_uuid": xfs_uuid,
    }
    approval = hashlib.sha256(canonical_json(approval_payload).encode()).hexdigest()
    return md_uuid, xfs_uuid, approval


def inspect(s):
    require(re.search(r'(?m)^VERSION_ID="?13"?$', Path("/etc/os-release").read_text()),
            "Debian 13 required")
    require(re.fullmatch(r"[0-9a-f]{32}", s["machine_id"] or "") and
            Path("/etc/machine-id").read_text().strip() == s["machine_id"],
            "Machine ID mismatch or missing inventory value")
    require(re.fullmatch(r"[A-Za-z0-9_-]{16,80}", s["lab_id"] or ""),
            "A unique lab marker must be configured in inventory")
    marker = Path("/etc/storage-lab-id")
    require(marker.is_file() and not marker.is_symlink() and marker.stat().st_uid == 0
            and not marker.stat().st_mode & 0o022
            and marker.read_text().strip() == s["lab_id"],
            "Missing or invalid root-owned lab marker")
    require(MD.fullmatch(s["md"]) and NAME.fullmatch(s["vg"]) and NAME.fullmatch(s["lv"]),
            "Invalid md or LVM name")
    require(MOUNT.fullmatch(s["mount"]) and s["mount"].startswith(("/srv/", "/data/")),
            "Storage mount must be a dedicated path below /srv or /data")
    require(s["mount_options"] == "defaults", "Only the required default mount policy is supported")
    level = int(s["level"])
    count = len(s["members"])
    chunk = int(s["chunk"])
    lv_mib = int(s["lv_mib"])
    require(level in (5, 6, 10) and 4 <= count <= 6 and (level != 10 or count % 2 == 0),
            "Use four to six disks, an even count for RAID 10, and RAID 5/6/10")
    require(s["layout"] == ("near=2" if level == 10 else "left-symmetric"), "Unsupported layout")
    require(chunk in (64, 128, 256, 512, 1024) and lv_mib > 0 and lv_mib % 4 == 0,
            "Unsupported chunk size or LV size")
    required = ["lsblk", "wipefs", "blkid", "mdadm", "pvs", "vgs", "lvs",
                "findmnt", "mkfs.xfs", "update-initramfs"]
    missing = [name for name in required if not shutil.which(name)]
    require(not missing, f"Preinstall tools on the isolated VM: {', '.join(missing)}")
    live = mounts()
    configured = mounts("--fstab")
    busy = {row.get("maj:min") for row in live}
    swaps = Path("/proc/swaps").read_text().splitlines()[1:]
    for line in swaps:
        path = Path(line.split()[0])
        if path.exists() and stat.S_ISBLK(path.stat().st_mode):
            busy.add(block_id(path))
    md_path = Path(s["md"])
    md_sys = Path("/sys/class/block") / md_path.name / "md"
    md_exists = md_sys.is_dir()
    require(md_exists or not os.path.lexists(md_path), "Target md path already exists")
    actual_devices = []
    for member in s["members"]:
        path = member["by_id"]
        require(DEV_ID.fullmatch(path) and Path(path).is_symlink(), f"Invalid by-id path: {path}")
        device = str(Path(path).resolve(strict=True))
        identity = block_id(device)
        result = run("lsblk", "--bytes", "--json", "--tree", "--output",
                     "PATH,TYPE,SIZE,SERIAL,MAJ:MIN,RO", device)
        rows = json.loads(result.stdout)["blockdevices"]
        require(len(rows) == 1, f"Ambiguous lsblk result: {path}")
        row = rows[0]
        require(row["type"] == "disk" and not row["ro"] and row["maj:min"] == identity
                and member["serial"] and str(row["serial"] or "").strip() == str(member["serial"])
                and integer(row["size"]) == integer(member["size_bytes"]) and identity not in busy,
                f"Unexpected disk identity, size or usage: {path}")
        sys_device = (Path("/sys/dev/block") / identity).resolve(strict=True)
        require(not any((child / "partition").is_file() for child in sys_device.iterdir()),
                f"Disk has partitions: {path}")
        require(not any((row.get("source") or "").startswith("/dev/") and
                        Path(row["source"]).resolve() == Path(device) for row in configured),
                f"Disk has a persistent mount entry: {path}")
        holders = {child.name for child in (sys_device / "holders").iterdir()}
        require(holders == ({md_path.name} if md_exists else set()),
                f"Unexpected active holders on {path}: {sorted(holders)}")
        actual_devices.append((path, device, identity, str(member["serial"]), integer(row["size"])))
    require(len({d[2] for d in actual_devices}) == count and
            len({d[3] for d in actual_devices}) == count, "Duplicate disk or serial")
    identity = [[path, serial, size] for path, _, _, serial, size in actual_devices]
    md_uuid, xfs_uuid, approval = plan_ids(s, identity)
    actual_uuid = None
    if md_exists:
        md_id = block_id(md_path)
        require(md_id not in busy, "md is mounted directly or is active swap")
        props = md_properties(run("mdadm", "--detail", str(md_path)).stdout)
        actual_uuid = props.get("UUID")
        expected = {"Raid Level": f"raid{level}", "Version": "1.2",
                    "Raid Devices": str(count), "Layout": s["layout"],
                    "Intent Bitmap": "Internal"}
        require(actual_uuid == md_uuid and all(props.get(k) == v for k, v in expected.items())
                and (md_sys / "sync_action").read_text().strip() == "idle"
                and (md_sys / "degraded").read_text().strip() == "0"
                and integer((md_sys / "chunk_size").read_text()) == chunk * 1024,
                "Existing RAID UUID, geometry, bitmap or health differs")
        actual_slots = list(md_sys.glob("dev-*"))
        require(len(actual_slots) == count, "Unexpected RAID members or spares")
        for slot, (_, device, ident, _, _) in enumerate(actual_devices):
            node = md_sys / ("dev-" + Path(device).name)
            require(node.is_dir() and block_id(device) == ident
                    and (node / "slot").read_text().strip() == str(slot)
                    and "in_sync" in (node / "state").read_text().strip().split(","),
                    f"Unexpected member in slot {slot}")
    else:
        require(not s["expected_md_uuid"] and not s["expected_xfs_uuid"],
                "Expected existing stack is absent; creation requires a blank new-stack plan")
    for path, device, _, _, _ in actual_devices:
        examine_member(device, md_uuid if md_exists else None)
        signatures = signature_types(device)
        probe = blkid(device)
        require((signatures == {"linux_raid_member"} and probe.get("TYPE") == "linux_raid_member")
                if md_exists else (not signatures and not probe),
                f"Unexpected signatures on {path}")

    pv = lvm_report("pvs", ("pv_name", "vg_name"))
    vg = [row for row in lvm_report("vgs", ("vg_name", "vg_extent_size", "vg_free"))
          if row["vg_name"] == s["vg"]]
    all_lvs = [row for row in lvm_report("lvs", ("vg_name", "lv_name", "lv_size", "lv_attr"))
               if row["vg_name"] == s["vg"]]
    md_pvs = [row for row in pv if md_exists and
              Path(row["pv_name"]).resolve() == md_path.resolve()]
    vg_pvs = [row for row in pv if row["vg_name"] == s["vg"]]
    require(len(vg) <= 1 and len(md_pvs) <= 1 and len(all_lvs) <= 1
            and (not vg or (md_exists and md_pvs == vg_pvs and len(vg_pvs) == 1))
            and (not md_pvs or md_pvs[0]["vg_name"] in ("", s["vg"])),
            "VG/PV membership does not match the approved md device")
    if md_exists:
        probe = blkid(str(md_path))
        require(signature_types(str(md_path)) == ({"LVM2_member"} if md_pvs else set())
                and (probe.get("TYPE") == "LVM2_member" if md_pvs else not probe),
                "Unexpected or unregistered signature on md")
    if vg and not all_lvs:
        require(integer(vg[0]["vg_free"]) >= lv_mib * 2**20,
                "The approved LV does not fit in the VG")
    lv_path = f"/dev/{s['vg']}/{s['lv']}"
    xfs = False
    lv_id = None
    if all_lvs:
        require(vg and all_lvs[0]["lv_name"] == s["lv"] and
                all_lvs[0]["lv_attr"].startswith("-") and
                integer(all_lvs[0]["lv_size"]) == lv_mib * 2**20,
                "Unexpected LV name, type or size")
        lv_id = block_id(lv_path)
        probe = blkid(lv_path)
        signatures = signature_types(lv_path)
        xfs = signatures == {"xfs"} and probe.get("TYPE") == "xfs" and probe.get("UUID") == xfs_uuid
        require(xfs or (not signatures and not probe), "Unexpected LV signature or XFS UUID")
    else:
        require(not os.path.lexists(lv_path), "Target LV path exists unexpectedly")

    mount = Path(s["mount"])
    require(mount.resolve() == mount, "Symlink in mount path")
    exact = [row for row in live if row["target"] == str(mount)]
    require(not any(row["target"].startswith(str(mount) + "/") or
                    (lv_id and row.get("maj:min") == lv_id and row["target"] != str(mount))
                    for row in live), "Mount subtree or LV is already used")
    require(not exact or (len(exact) == 1 and xfs and exact[0]["maj:min"] == lv_id
                          and exact[0]["fstype"] == "xfs" and
                          "rw" in exact[0]["options"].split(",")), "Conflicting active mount")
    require(exact or not mount.exists() or (mount.is_dir() and not any(mount.iterdir())),
            "Mount path must be absent or an empty directory")
    fstab = [row for row in configured if row["target"] == str(mount) or
             row["source"] == f"UUID={xfs_uuid}"]
    require(not fstab or (len(fstab) == 1 and xfs and fstab[0]["target"] == str(mount)
                          and fstab[0]["source"] == f"UUID={xfs_uuid}"
                          and fstab[0]["fstype"] == "xfs"
                          and fstab[0]["options"] == s["mount_options"]),
            "Conflicting fstab entry")
    if s["monitor"]:
        monitor = run("systemctl", "show", "--property=LoadState", "--value", "mdmonitor.service")
        require(monitor.stdout.strip() == "loaded", "Packaged mdmonitor.service is not loaded")
    return {"approval": approval, "host": s["host"], "address": s["address"],
            "machine_id": s["machine_id"], "lab_id": s["lab_id"], "devices": actual_devices,
            "level": level, "layout": s["layout"], "chunk_kib": chunk, "metadata": "1.2",
            "bitmap": "internal", "vg": s["vg"], "lv": s["lv"], "lv_mib": lv_mib,
            "mount": s["mount"], "planned_md_uuid": md_uuid, "actual_md_uuid": actual_uuid,
            "planned_xfs_uuid": xfs_uuid, "md_exists": md_exists, "vg_exists": bool(vg),
            "lv_exists": bool(all_lvs), "lv_path": lv_path, "xfs_exists": xfs,
            "mounted": bool(exact), "fstab_matches": bool(fstab),
            "mdadm_directives": directives(s, md_uuid)}


if __name__ == "__main__":
    try:
        spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
        print(json.dumps(inspect(spec)))
    except (ValueError, KeyError, OSError, RuntimeError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Storage preflight refused: {exc}", file=sys.stderr)
        sys.exit(1)
