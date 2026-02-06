#!/usr/bin/env python3
"""Select one CID per unique 8-bit Kademlia identifier prefix.

Reads CIDs from cids.txt, computes the Kademlia ID (SHA256 of the CID's
multihash), and keeps the first CID encountered for each of the 256 possible
first-byte prefixes. Writes the selected CIDs to output.txt.
"""

import hashlib
import sys

from cid import make_cid


def kademlia_id(cid_str: str) -> bytes:
    c = make_cid(cid_str)
    return hashlib.sha256(c.multihash).digest()


def verify():
    """Print '<kad_id_hex> <cid>' for every CID, for diffing against Go output."""
    with open("cids.txt") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            cid_str = parts[0]
            try:
                kid = kademlia_id(cid_str)
            except Exception as e:
                print(f"skipping {cid_str}: {e}", file=sys.stderr)
                continue
            print(f"{kid.hex()} {cid_str}")


def main():
    seen: dict[int, str] = {}  # prefix byte -> cid string

    with open("cids.txt") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            cid_str = parts[0]
            try:
                kid = kademlia_id(cid_str)
            except Exception as e:
                print(f"Skipping {cid_str}: {e}", file=sys.stderr)
                continue
            prefix = kid[0]
            if prefix not in seen:
                seen[prefix] = cid_str

    with open("output.txt", "w") as f:
        for prefix in sorted(seen):
            f.write(seen[prefix] + "\n")

    print(f"Wrote {len(seen)}/256 CIDs to output.txt")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--verify":
        verify()
    else:
        main()
