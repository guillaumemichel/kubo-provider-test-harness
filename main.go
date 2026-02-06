package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/ipfs/go-cid"
)

const (
	peerID            = "12D3KooWPGUHammYxStT9qMmKidZBUChutLLLXjmumoXhQRofhNp"
	privKey           = "CAESQCDaw5OT66egT4ShrkA7WoFY6FT7NSGPvOlG3Phh3qGZx9fy2KzoCFA2VkLQUtLIiv4rbiDmpff4wlwUwolvgiE="
	emptyDirCID       = "QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn"
	reprovideInterval = 10 * time.Minute
)

var ipfsPath string

func ipfsEnv() []string {
	return append(os.Environ(), "IPFS_PATH="+ipfsPath)
}

func ipfs(args ...string) (string, error) {
	cmd := exec.Command("ipfs", args...)
	cmd.Env = ipfsEnv()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return "", fmt.Errorf("%s: %s", err, ee.Stderr)
		}
		return "", err
	}
	return string(out), nil
}

func mustIpfs(args ...string) string {
	out, err := ipfs(args...)
	if err != nil {
		fmt.Fprintf(os.Stderr, "ipfs %s: %v\n", strings.Join(args, " "), err)
		os.Exit(1)
	}
	return out
}

type entry struct {
	cid   string
	count int
}

func run() error {
	dir, _ := os.Getwd()
	ipfsPath = filepath.Join(dir, ".ipfs")
	generatedDir := filepath.Join(dir, "generated_files")

	// 1. Init + configure
	os.RemoveAll(ipfsPath)
	fmt.Println("Initializing IPFS node…")
	mustIpfs("init", "--empty-repo")
	intervalStr := fmt.Sprintf("%dm", int(reprovideInterval.Minutes()))
	mustIpfs("config", "--json", "Provide.DHT.Interval", fmt.Sprintf(`"%s"`, intervalStr))
	mustIpfs("config", "Provide.Strategy", "pinned")
	mustIpfs("config", "Addresses.API", "/ip4/127.0.0.1/tcp/5401")
	mustIpfs("config", "Addresses.Gateway", "/ip4/127.0.0.1/tcp/8480")
	mustIpfs("config", "--json", "Addresses.Swarm",
		`["/ip4/0.0.0.0/tcp/4401","/ip6/::/tcp/4401","/ip4/0.0.0.0/udp/4401/quic-v1","/ip6/::/udp/4401/quic-v1"]`)
	mustIpfs("config", "Plugins.Plugins.telemetry.Config.Mode", "off")

	// Identity must be set by editing config file directly (API blocks private key changes)
	configPath := filepath.Join(ipfsPath, "config")
	configData, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("read config: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(configData, &cfg); err != nil {
		return fmt.Errorf("parse config: %w", err)
	}
	cfg["Identity"] = map[string]string{"PeerID": peerID, "PrivKey": privKey}
	configData, _ = json.MarshalIndent(cfg, "", "  ")
	if err := os.WriteFile(configPath, configData, 0o600); err != nil {
		return fmt.Errorf("write config: %w", err)
	}
	fmt.Printf("Configured: interval=%s, strategy=pinned, ports=5401/4401/8480\n", intervalStr)

	// 2. Add files offline
	fmt.Println("\nAdding generated_files/ recursively…")
	out := mustIpfs("add", "-r", "-q", "--cid-version=1", "--raw-leaves", generatedDir)
	lines := strings.Split(strings.TrimSpace(out), "\n")
	fileCIDs := lines[:len(lines)-1]
	dirCID := lines[len(lines)-1]

	// Build tracking map: multihash bytes (as string key) -> entry
	tracked := make(map[string]*entry)
	for _, s := range append(fileCIDs, dirCID, emptyDirCID) {
		c, err := cid.Decode(s)
		if err != nil {
			return fmt.Errorf("bad CID %s: %w", s, err)
		}
		tracked[string(c.Hash())] = &entry{cid: s}
	}
	total := len(tracked)
	fmt.Printf("Tracking %d CIDs\n", total)

	// 3. Start daemon
	fmt.Println("\nStarting daemon…")
	daemon := exec.Command("ipfs", "daemon")
	daemon.Env = append(ipfsEnv(), "GOLOG_LOG_LEVEL=dht=debug,dht/provider=debug")
	stdoutPipe, _ := daemon.StdoutPipe()
	stderrPipe, _ := daemon.StderrPipe()
	if err := daemon.Start(); err != nil {
		return fmt.Errorf("daemon start: %w", err)
	}
	shutdown := func() {
		daemon.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { daemon.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
			daemon.Process.Kill()
			daemon.Wait()
		}
	}
	defer shutdown()

	// Handle Ctrl+C
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		fmt.Println("\nInterrupted, shutting down…")
		shutdown()
		os.Exit(1)
	}()

	// Wait for "Daemon is ready"
	stdoutSc := bufio.NewScanner(stdoutPipe)
	ready := false
	for stdoutSc.Scan() {
		fmt.Printf("  %s\n", stdoutSc.Text())
		if strings.Contains(stdoutSc.Text(), "Daemon is ready") {
			ready = true
			break
		}
	}
	if !ready {
		return fmt.Errorf("daemon never became ready")
	}
	go func() {
		for stdoutSc.Scan() {
		}
	}()

	// 4. Monitor provide logs
	var (
		mu         sync.Mutex
		advertised int
		records    int
	)

	go func() {
		sc := bufio.NewScanner(stderrPipe)
		sc.Buffer(make([]byte, 0, 1<<20), 1<<20)
		for sc.Scan() {
			line := sc.Text()
			if !strings.Contains(line, "sent provider record") {
				continue
			}
			idx := strings.Index(line, "{")
			if idx == -1 {
				continue
			}
			var rec struct {
				Keys   []string `json:"keys"`
				Prefix string   `json:"prefix"`
			}
			if json.Unmarshal([]byte(line[idx:]), &rec) != nil {
				continue
			}

			mu.Lock()
			records++
			newCnt := 0
			for _, k := range rec.Keys {
				raw, err := base64.StdEncoding.DecodeString(k)
				if err != nil {
					continue
				}
				if e, ok := tracked[string(raw)]; ok {
					if e.count == 0 {
						advertised++
						newCnt++
					}
					e.count++
				}
			}
			fmt.Printf("  [provide #%d] prefix=%s keys=%d new=%d | %d/%d\n",
				records, rec.Prefix, len(rec.Keys), newCnt, advertised, total)
			mu.Unlock()
		}
	}()

	// Print distribution every minute until Ctrl+C
	fmt.Printf("\nMonitoring provides (Ctrl+C to stop)…\n")
	start := time.Now()
	for {
		time.Sleep(1 * time.Minute)
		elapsed := time.Since(start).Round(time.Second)

		mu.Lock()
		fmt.Printf("\n%s\n", strings.Repeat("=", 60))
		fmt.Printf("STATUS (%s elapsed, reprovide interval: %s)\n", elapsed, reprovideInterval)
		fmt.Printf("  Total provide records: %d\n", records)
		fmt.Printf("  CIDs advertised: %d/%d\n", advertised, total)
		dist := make(map[int]int)
		for _, e := range tracked {
			dist[e.count]++
		}
		fmt.Printf("  Advertisement count distribution:\n")
		for n := 0; n <= 100; n++ {
			if cnt, ok := dist[n]; ok {
				fmt.Printf("    %dx: %d CIDs\n", n, cnt)
			}
		}
		fmt.Printf("%s\n", strings.Repeat("=", 60))
		mu.Unlock()
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
