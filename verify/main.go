package main

import (
	"bufio"
	"crypto/sha256"
	"fmt"
	"os"
	"strings"

	"github.com/ipfs/go-cid"
)

func main() {
	f, err := os.Open("../cids.txt")
	if err != nil {
		fmt.Fprintf(os.Stderr, "open cids.txt: %v\n", err)
		os.Exit(1)
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		parts := strings.Fields(scanner.Text())
		if len(parts) == 0 {
			continue
		}
		c, err := cid.Decode(parts[0])
		if err != nil {
			fmt.Fprintf(os.Stderr, "skipping %s: %v\n", parts[0], err)
			continue
		}
		kadID := sha256.Sum256(c.Hash())
		fmt.Printf("%x %s\n", kadID, parts[0])
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "read error: %v\n", err)
		os.Exit(1)
	}
}
