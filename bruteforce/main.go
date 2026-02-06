package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"fmt"

	"github.com/mr-tron/base58"
)

func main() {
	for i := 0; ; i++ {
		pub, priv, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			panic(err)
		}

		// Marshal public key to libp2p protobuf: Type=Ed25519(1), Data=32-byte pubkey
		marshalledPub := append([]byte{0x08, 0x01, 0x12, 0x20}, pub...)

		// Peer ID = identity multihash (code=0x00, length=36) of marshalled pubkey
		peerIDBytes := append([]byte{0x00, 0x24}, marshalledPub...)

		// Kademlia ID = SHA256(peer_id_bytes)
		kadID := sha256.Sum256(peerIDBytes)

		if kadID[0] == 0x00 {
			// Marshal private key: Type=Ed25519(1), Data=seed(32)+pubkey(32)=64 bytes
			privData := append(priv.Seed(), pub...)
			marshalledPriv := append([]byte{0x08, 0x01, 0x12, 0x40}, privData...)

			fmt.Printf("Found after %d attempts!\n", i+1)
			fmt.Printf("PeerID:  %s\n", base58.Encode(peerIDBytes))
			fmt.Printf("PrivKey: %s\n", base64.StdEncoding.EncodeToString(marshalledPriv))
			fmt.Printf("KadID:   %x\n", kadID)
			return
		}
	}
}
