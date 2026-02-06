#!/usr/bin/env python3
"""Generate 256 text files, one for each 8-bit Kademlia ID prefix.

For each file the script:
  1. Generates short text content
  2. Computes the CIDv1-raw (matching `ipfs add --cid-version=1`) by SHA-256
     hashing the raw bytes
  3. Derives the Kademlia ID (SHA-256 of the CID's multihash)
  4. Checks whether the first byte (8-bit prefix) is still needed

Files are written to generated_files/ and a summary (CID + KadID hex) is
written to gen2_output.txt.
"""

import hashlib
import os
import sys

from cid import make_cid
from multihash import encode as mh_encode

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(SCRIPT_DIR, "generated_files")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "gen2_output.txt")


def content_to_cid(data: bytes):
    """Return a CIDv1-raw object for raw file content."""
    digest = hashlib.sha256(data).digest()
    mh = mh_encode(digest, "sha2-256")
    return make_cid(1, "raw", mh)


def kademlia_id(cid_obj) -> bytes:
    return hashlib.sha256(cid_obj.multihash).digest()


def main():
    os.makedirs(FILES_DIR, exist_ok=True)

    # prefix byte -> (filename, cid_str, kad_hex, content)
    found: dict[int, tuple[str, str, str, bytes]] = {}
    counter = 0

    while len(found) < 256:
        content = f"ipfs-test-{counter}\n".encode()
        cid_obj = content_to_cid(content)
        kid = kademlia_id(cid_obj)
        prefix = kid[0]

        if prefix not in found:
            cid_str = str(cid_obj)
            filename = f"prefix-{prefix:02x}.txt"
            found[prefix] = (filename, cid_str, kid.hex(), content)

        counter += 1

    print(f"Needed {counter} iterations to cover all 256 prefixes")

    # Write the text files
    for prefix in sorted(found):
        filename, _, _, content = found[prefix]
        path = os.path.join(FILES_DIR, filename)
        with open(path, "wb") as f:
            f.write(content)

    # Write the summary: one line per prefix with CID and KadID
    with open(OUTPUT_FILE, "w") as f:
        for prefix in range(256):
            _, cid_str, kad_hex, _ = found[prefix]
            cid_b32 = make_cid(cid_str).encode("base32").decode()
            f.write(f"{cid_b32} {kad_hex}\n")

    print(f"Wrote 256 files to {FILES_DIR}/")
    print(f"Wrote CID + KadID list to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
