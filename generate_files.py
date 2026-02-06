#!/usr/bin/env python3
"""Generate 1024 small files, one per unique 10-bit Kademlia prefix.

For ipfs add --cid-version=1 --raw-leaves:
  multihash = 0x12 0x20 + SHA256(file_content)
  kademlia_id = SHA256(multihash)
  10-bit prefix = first 10 bits of kademlia_id

This is a coupon collector problem: ~1024 * ln(1024) ≈ 7100 attempts expected.
"""

import hashlib
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "generated_files")
TARGET = 1024  # 2^10


def kad_prefix_10(content: bytes) -> int:
    """Compute the 10-bit Kademlia prefix for raw-leaf file content."""
    content_hash = hashlib.sha256(content).digest()
    multihash = b"\x12\x20" + content_hash
    kad_id = hashlib.sha256(multihash).digest()
    return (kad_id[0] << 2) | (kad_id[1] >> 6)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # Clear existing files
    for f in os.listdir(OUT_DIR):
        os.remove(os.path.join(OUT_DIR, f))

    seen: dict[int, str] = {}  # prefix -> content string
    attempt = 0

    while len(seen) < TARGET:
        content = f"{attempt}\n"
        prefix = kad_prefix_10(content.encode())
        if prefix not in seen:
            seen[prefix] = content
        attempt += 1

    # Write files
    for prefix in sorted(seen):
        fname = f"prefix-{prefix:04d}.txt"
        fpath = os.path.join(OUT_DIR, fname)
        with open(fpath, "w") as f:
            f.write(seen[prefix])

    print(f"Generated {len(seen)}/{TARGET} files in {OUT_DIR}/ ({attempt} attempts)")


if __name__ == "__main__":
    main()
