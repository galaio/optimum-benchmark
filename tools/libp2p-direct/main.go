// libp2p-direct benchmark: measures raw TCP stream latency between
// Docker containers using libp2p host-to-host connections.
//
// Usage:
//   go run . -nodes 8 -size 102400 -count 10 -out results.tsv
//
// Each sender-receiver pair runs in a goroutine simulating a Docker
// network namespace. The timestamp is taken inside the Go process
// (time.Now) eliminating process-spawn overhead.

package main

import (
	"context"
	"crypto/rand"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"sync"
	"time"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/multiformats/go-multiaddr"
)

const protocolID = "/bench/direct/1.0.0"

var (
	flagNodes = flag.Int("nodes", 8, "number of receiver nodes")
	flagSize  = flag.Int("size", 102400, "message size in bytes")
	flagCount = flag.Int("count", 10, "messages per receiver")
	flagSleep = flag.Int("sleep", 500, "sleep between messages (ms)")
	flagOut   = flag.String("out", "", "output TSV file path")
)

type latencyRecord struct {
	msgIndex  int
	sizeBytes int
	latencyNs int64
}

func main() {
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	sender, err := libp2p.New(libp2p.ListenAddrStrings("/ip4/127.0.0.1/tcp/0"))
	if err != nil {
		log.Fatalf("failed to create sender host: %v", err)
	}
	defer sender.Close()

	fmt.Printf("Sender peer ID: %s\n", sender.ID())
	fmt.Printf("Sender addrs:   %v\n", sender.Addrs())

	receivers := make([]host.Host, *flagNodes)
	for i := 0; i < *flagNodes; i++ {
		h, err := libp2p.New(libp2p.ListenAddrStrings("/ip4/127.0.0.1/tcp/0"))
		if err != nil {
			log.Fatalf("failed to create receiver %d: %v", i, err)
		}
		defer h.Close()
		receivers[i] = h
	}

	payload := make([]byte, *flagSize)
	if _, err := rand.Read(payload); err != nil {
		log.Fatalf("failed to generate payload: %v", err)
	}

	var mu sync.Mutex
	var allRecords []latencyRecord

	var wgReceivers sync.WaitGroup
	for i, recv := range receivers {
		i := i
		recv := recv
		wgReceivers.Add(1)
		recv.SetStreamHandler(protocolID, func(s network.Stream) {
			defer s.Close()
			buf := make([]byte, *flagSize)
			for {
				_, err := io.ReadFull(s, buf)
				if err != nil {
					if err != io.EOF && err != io.ErrUnexpectedEOF {
						log.Printf("[recv-%d] read error: %v", i, err)
					}
					return
				}
			}
		})

		senderInfo := peer.AddrInfo{
			ID:    recv.ID(),
			Addrs: recv.Addrs(),
		}
		if err := sender.Connect(ctx, senderInfo); err != nil {
			log.Fatalf("sender cannot connect to receiver %d: %v", i, err)
		}
	}

	fmt.Printf("\nRunning benchmark: %d receivers, %d bytes, %d messages each\n\n",
		*flagNodes, *flagSize, *flagCount)

	for msgIdx := 0; msgIdx < *flagCount; msgIdx++ {
		var wg sync.WaitGroup
		for i, recv := range receivers {
			wg.Add(1)
			go func(recvIdx int, recvHost host.Host) {
				defer wg.Done()

				recvAddr := recvHost.Addrs()[0]
				fullAddr := fmt.Sprintf("%s/p2p/%s", recvAddr, recvHost.ID())
				ma, _ := multiaddr.NewMultiaddr(fullAddr)
				ai, _ := peer.AddrInfoFromP2pAddr(ma)

				start := time.Now()

				s, err := sender.NewStream(ctx, ai.ID, protocolID)
				if err != nil {
					log.Printf("[msg-%d][recv-%d] stream error: %v", msgIdx, recvIdx, err)
					return
				}

				_, err = s.Write(payload)
				if err != nil {
					log.Printf("[msg-%d][recv-%d] write error: %v", msgIdx, recvIdx, err)
					s.Close()
					return
				}
				s.Close()

				elapsed := time.Since(start)

				mu.Lock()
				allRecords = append(allRecords, latencyRecord{
					msgIndex:  msgIdx,
					sizeBytes: *flagSize,
					latencyNs: elapsed.Nanoseconds(),
				})
				mu.Unlock()

				fmt.Printf("  [msg %2d → recv %d] %v\n", msgIdx+1, recvIdx, elapsed)
			}(i, recv)
		}
		wg.Wait()

		if msgIdx < *flagCount-1 {
			time.Sleep(time.Duration(*flagSleep) * time.Millisecond)
		}
	}

	_ = wgReceivers

	if *flagOut != "" {
		f, err := os.Create(*flagOut)
		if err != nil {
			log.Fatalf("cannot create output file: %v", err)
		}
		defer f.Close()

		fmt.Fprintln(f, "msg_index\tsize_bytes\tlatency_ns")
		for _, r := range allRecords {
			fmt.Fprintf(f, "%d\t%d\t%d\n", r.msgIndex, r.sizeBytes, r.latencyNs)
		}
		fmt.Printf("\nResults written to %s (%d records)\n", *flagOut, len(allRecords))
	}

	if len(allRecords) > 0 {
		var total int64
		minLat := allRecords[0].latencyNs
		maxLat := allRecords[0].latencyNs
		for _, r := range allRecords {
			total += r.latencyNs
			if r.latencyNs < minLat {
				minLat = r.latencyNs
			}
			if r.latencyNs > maxLat {
				maxLat = r.latencyNs
			}
		}
		avg := time.Duration(total / int64(len(allRecords)))
		fmt.Printf("\nSummary: avg=%v  min=%v  max=%v  samples=%d\n",
			avg, time.Duration(minLat), time.Duration(maxLat), len(allRecords))
	}
}
