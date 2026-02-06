#!/usr/bin/env python3
"""Test that Kubo advertises all CIDs to the network.

Steps:
1. Clear any existing .ipfs directory
2. Initialize IPFS with Provide.DHT.Interval = 1h, strategy = all
3. Add files from generated_files/ OFFLINE (before daemon starts)
4. Start Kubo daemon with provider/dht logging at debug
5. Monitor logs until all root CIDs are advertised
6. Shut down
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from cid import make_cid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IPFS_PATH = os.path.join(SCRIPT_DIR, ".ipfs")
GENERATED_DIR = os.path.join(SCRIPT_DIR, "generated_files")

# The empty directory CID created by ipfs init
EMPTY_DIR_CID = "QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn"
EMPTY_DIR_MH = make_cid(EMPTY_DIR_CID).multihash


def ipfs_env(**extra):
    env = {**os.environ, "IPFS_PATH": IPFS_PATH}
    env.update(extra)
    return env


def ipfs(*args):
    return subprocess.run(
        ["ipfs", *args], env=ipfs_env(), capture_output=True, text=True
    )


def mh_of(cid_str):
    return make_cid(cid_str).multihash


def kad_id(mh: bytes) -> bytes:
    return hashlib.sha256(mh).digest()


def kad_prefix_byte(mh: bytes) -> int:
    return kad_id(mh)[0]


def main():
    # ── 1. Clear .ipfs ──────────────────────────────────────────────────
    if os.path.exists(IPFS_PATH):
        print(f"Removing {IPFS_PATH}")
        shutil.rmtree(IPFS_PATH)

    # ── 2. Init + configure ─────────────────────────────────────────────
    print("Initializing IPFS node…")
    r = ipfs("init", "--empty-repo")
    if r.returncode != 0:
        sys.exit(f"ipfs init failed: {r.stderr}")
    for line in r.stdout.strip().splitlines():
        print(f"  {line}")

    # Kubo 0.40+ uses Provide.DHT.Interval instead of the deprecated Reprovider.Interval
    ipfs("config", "--json", "Provide.DHT.Interval", '"10m"')
    ipfs("config", "Provide.Strategy", "pinned")
    # Use alternate ports so we don't conflict with a running IPFS daemon
    ipfs("config", "Addresses.API", "/ip4/127.0.0.1/tcp/5401")
    ipfs("config", "Addresses.Gateway", "/ip4/127.0.0.1/tcp/8480")
    swarm_addrs = [
        "/ip4/0.0.0.0/tcp/4401",
        "/ip6/::/tcp/4401",
        "/ip4/0.0.0.0/udp/4401/quic-v1",
        "/ip6/::/udp/4401/quic-v1",
    ]
    ipfs("config", "--json", "Addresses.Swarm", str(swarm_addrs).replace("'", '"'))
    ipfs("config", "Plugins.Plugins.telemetry.Config.Mode", "off")
    # Pre-bruteforced Ed25519 identity whose Kademlia ID starts with 0x00
    # KadID: 002c42c3e2efb0402a06afd3171c4c4fc16839eb6da04e63daf1a7b530fa3ff9
    PEER_ID = "12D3KooWPGUHammYxStT9qMmKidZBUChutLLLXjmumoXhQRofhNp"
    PRIV_KEY = "CAESQCDaw5OT66egT4ShrkA7WoFY6FT7NSGPvOlG3Phh3qGZx9fy2KzoCFA2VkLQUtLIiv4rbiDmpff4wlwUwolvgiE="
    identity_json = json.dumps({"PeerID": PEER_ID, "PrivKey": PRIV_KEY})
    ipfs("config", "--json", "Identity", identity_json)
    print(
        "Set Provide.DHT.Interval = 8min, using alternate ports (API:5401, Swarm:4401, GW:8480)"
    )

    # ── 3. Add files OFFLINE (before daemon starts) ─────────────────────
    print(f"\nAdding generated_files/ recursively (offline)…")
    r = ipfs("add", "-r", "-q", "--cid-version=1", "--raw-leaves", GENERATED_DIR)
    if r.returncode != 0:
        sys.exit(f"ipfs add failed: {r.stderr}")
    all_cids = r.stdout.strip().splitlines()
    # Last CID is the directory itself; the rest are the individual files
    file_cids = all_cids[:-1]
    dir_cid = all_cids[-1]
    print(f"  Added {len(file_cids)} files + 1 directory (dir CID: {dir_cid})")

    # Build root multihash set (file CIDs only)
    root_mhs: set[bytes] = set()
    root_mh_to_cid: dict[bytes, str] = {}
    for c_str in file_cids:
        mh = mh_of(c_str)
        root_mhs.add(mh)
        root_mh_to_cid[mh] = c_str

    # Discover "other" blocks by diffing refs local against root CIDs
    other_mhs: set[bytes] = set()
    r = ipfs("refs", "local")
    if r.returncode == 0:
        all_local = set(r.stdout.strip().splitlines())
        all_local_mhs = set()
        for c_str in all_local:
            try:
                all_local_mhs.add(mh_of(c_str))
            except Exception:
                pass
        other_mhs.update(all_local_mhs - root_mhs - {EMPTY_DIR_MH})

    print(
        f"\nTracking: {len(root_mhs)} root CIDs, {len(other_mhs)} other blocks, 1 empty dir"
    )

    # Precompute Kademlia prefix for each root multihash
    root_mh_kad_prefix: dict[bytes, int] = {}
    for mh in root_mhs:
        root_mh_kad_prefix[mh] = kad_prefix_byte(mh)

    # ── Tracking state ────────────────────────────────────────────────
    # Provide tracking (protected by lock):
    root_advertised: set[bytes] = set()
    other_advertised: set[bytes] = set()
    emptydir_advertised = False
    unknown_advertised: set[bytes] = set()
    total_provide_records = 0
    seen_provide_prefixes: set[str] = (
        set()
    )  # binary prefix strings from provide records

    lock = threading.Lock()

    # Unpin the default empty directory created by ipfs init (offline)
    ipfs("pin", "rm", EMPTY_DIR_CID)

    # ── 4. Start daemon with debug logging ──────────────────────────────
    log_level = ",".join(f"{s}=debug" for s in ["dht", "dht/provider"])
    print(f"\nStarting IPFS daemon (log levels: {log_level})…")
    daemon = subprocess.Popen(
        ["ipfs", "daemon"],
        env=ipfs_env(GOLOG_LOG_LEVEL=log_level),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def shutdown_daemon():
        daemon.terminate()
        try:
            daemon.wait(timeout=15)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()

    # Collect stderr lines in a shared buffer
    stderr_lines: list[str] = []
    stderr_lock = threading.Lock()

    def drain_stderr():
        for line in daemon.stderr:
            with stderr_lock:
                stderr_lines.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    # Wait for daemon to be ready
    ready = False
    for line in daemon.stdout:
        line = line.strip()
        if line:
            print(f"  {line}")
        if "Daemon is ready" in line:
            ready = True
            break
    if not ready:
        time.sleep(2)
        with stderr_lock:
            err = "".join(stderr_lines)
        shutdown_daemon()
        sys.exit(f"Daemon failed to start.\nstderr: {err}")

    # ── Log monitor ───────────────────────────────────────────────────
    def monitor_logs():
        nonlocal total_provide_records, emptydir_advertised
        seen_idx = 0
        while daemon.poll() is None:
            with stderr_lock:
                new_lines = stderr_lines[seen_idx:]
                seen_idx = len(stderr_lines)
            for line in new_lines:
                # if "dht/provider" in line:
                #     print(line)

                if "sent provider record" not in line:
                    continue
                json_start = line.find("{")
                if json_start == -1:
                    continue
                try:
                    record = json.loads(line[json_start:])
                except json.JSONDecodeError:
                    continue

                keys = record.get("keys", [])
                prefix = record.get("prefix", "?")
                n_root = n_other = n_emptydir = n_unknown = 0
                n_new_root = 0

                for key_b64 in keys:
                    mh = base64.b64decode(key_b64)
                    with lock:
                        if mh in root_mhs:
                            n_root += 1
                            if mh not in root_advertised:
                                root_advertised.add(mh)
                                n_new_root += 1
                        elif mh in other_mhs:
                            n_other += 1
                            other_advertised.add(mh)
                        elif mh == EMPTY_DIR_MH:
                            n_emptydir += 1
                            emptydir_advertised = True
                        else:
                            n_unknown += 1
                            unknown_advertised.add(mh)

                with lock:
                    total_provide_records += 1
                    seen_provide_prefixes.add(prefix)
                    r_done = len(root_advertised)
                    r_total = len(root_mhs) if root_mhs else "?"

                parts = []
                if n_root:
                    parts.append(f"root={n_root}(new={n_new_root})")
                if n_other:
                    parts.append(f"other={n_other}")
                if n_emptydir:
                    parts.append(f"emptydir={n_emptydir}")
                if n_unknown:
                    parts.append(f"unknown={n_unknown}")

                print(
                    f"  [provide #{total_provide_records}] prefix={prefix} "
                    f"keys={len(keys)} {' '.join(parts)}  "
                    f"| root progress: {r_done}/{r_total}"
                )
            time.sleep(0.5)

    log_thread = threading.Thread(target=monitor_logs, daemon=True)
    log_thread.start()

    # Drain stdout so the daemon doesn't block on pipe buffer
    threading.Thread(target=lambda: [None for _ in daemon.stdout], daemon=True).start()

    # ── 5. Wait for all root CIDs to be advertised ─────────────────────
    print(f"\nWaiting for {len(root_mhs)} root CIDs to be advertised…")
    start = time.time()
    last_progress = 0
    stall_reported = False
    while True:
        with lock:
            n_root_done = len(root_advertised)
            n_root_total = len(root_mhs)
            n_other_done = len(other_advertised)
            n_other_total = len(other_mhs)
            n_unknown = len(unknown_advertised)
        if n_root_done >= n_root_total and n_root_total > 0:
            break
        elapsed = time.time() - start

        # Detect stall: no new root CIDs for 60s
        if n_root_done > last_progress:
            last_progress = n_root_done
            stall_reported = False
        elif not stall_reported and n_root_done == last_progress and elapsed > 120:
            stall_reported = True
            with lock:
                missing_mhs = root_mhs - root_advertised
            print(
                f"\n  *** STALL DIAGNOSTIC: {len(missing_mhs)} root CIDs not yet advertised ***"
            )
            # Group missing roots by their 8-bit Kademlia prefix
            missing_by_prefix: dict[int, list[str]] = {}
            for mh in missing_mhs:
                kp = root_mh_kad_prefix[mh]
                cid_str = root_mh_to_cid.get(mh, mh.hex())
                missing_by_prefix.setdefault(kp, []).append(cid_str)
            print(f"  Missing root CID Kademlia prefixes (hex):")
            for kp in sorted(missing_by_prefix):
                for cid_str in missing_by_prefix[kp]:
                    kid = kad_id(mh_of(cid_str))
                    print(
                        f"    prefix=0x{kp:02x} ({kp:08b})  kadID={kid.hex()[:16]}…  CID={cid_str}"
                    )
            # Show which provide prefixes were seen
            with lock:
                prefixes_copy = sorted(seen_provide_prefixes)
            print(f"  Provide prefixes seen ({len(prefixes_copy)} total):")
            for p in prefixes_copy:
                print(f"    {p}")
            print()

        print(
            f"  root={n_root_done}/{n_root_total}  "
            f"other={n_other_done}/{n_other_total}  "
            f"emptydir={'yes' if emptydir_advertised else 'no'}  "
            f"unknown={n_unknown}  "
            f"({elapsed:.0f}s elapsed)"
        )
        time.sleep(10)

    elapsed = time.time() - start
    print(f"\nAll {n_root_total} root CIDs advertised in {elapsed:.0f}s!")

    # Final summary
    with lock:
        print(f"\n{'=' * 60}")
        print(f"SUMMARY")
        print(f"  Root CIDs advertised:    {len(root_advertised)}/{len(root_mhs)}")
        print(f"  Other blocks advertised: {len(other_advertised)}/{len(other_mhs)}")
        print(f"  Empty dir advertised:    {emptydir_advertised}")
        print(f"  Unknown MHs advertised:  {len(unknown_advertised)}")
        print(f"  Total provide records:   {total_provide_records}")
        if unknown_advertised:
            print(f"  Unknown multihashes:")
            for mh in sorted(unknown_advertised):
                print(f"    {mh.hex()}")
        print(f"{'=' * 60}")

    # ── 6. Shut down ────────────────────────────────────────────────────
    print("Shutting down daemon…")
    shutdown_daemon()
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
