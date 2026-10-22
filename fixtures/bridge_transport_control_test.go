package codexappserver

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type auditWriter struct {
	messages chan []byte
	entered  chan struct{}
	release  chan struct{}
	short    bool
}

func (w *auditWriter) Write(p []byte) (int, error) {
	if w.entered != nil {
		close(w.entered)
		<-w.release
	}
	w.messages <- append([]byte(nil), p...)
	if w.short {
		return 1, nil
	}
	return len(p), nil
}
func (w *auditWriter) Close() error { return nil }
func controlFixture(t *testing.T) (*Bridge, *auditWriter, *io.PipeWriter) {
	t.Helper()
	b := NewBridge(nil, nil)
	b.transport.starting()
	b.transport.process(900001, true)
	b.transport.notice(rpcEnvelope{Method: "remoteControl/status/changed", Params: json.RawMessage(`{"status":"disabled"}`)})
	w := &auditWriter{messages: make(chan []byte, 1024)}
	b.stdin = w
	reader, writer := io.Pipe()
	done := make(chan struct{})
	go func() { b.readStdout(reader); close(done) }()
	t.Cleanup(func() { writer.Close(); reader.Close(); <-done })
	return b, w, writer
}
func wireID(t *testing.T, w *auditWriter) int64 {
	t.Helper()
	select {
	case p := <-w.messages:
		var msg struct {
			ID int64 `json:"id"`
		}
		if json.Unmarshal(p, &msg) != nil || msg.ID == 0 {
			t.Fatal(string(p))
		}
		return msg.ID
	case <-time.After(time.Second):
		t.Fatal("no wire request")
	}
	return 0
}
func requestAsync(b *Bridge, ctx context.Context, method string, params map[string]any) <-chan error {
	done := make(chan error, 1)
	go func() { _, err := b.requestNoEnsure(ctx, method, params); done <- err }()
	return done
}
func await(t *testing.T, predicate func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for !predicate() {
		if time.Now().After(deadline) {
			t.Fatal("condition timeout")
		}
		time.Sleep(time.Millisecond)
	}
}
func fence(t *testing.T, b *Bridge) transportSnapshot {
	t.Helper()
	s, err := b.FenceTransport(b.TransportSnapshot().Generation, "fixture-fence")
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func ackWire(t *testing.T, stdout *io.PipeWriter, id int64, result string) {
	t.Helper()
	if _, err := fmt.Fprintf(stdout, "{\"id\":%d,\"result\":%s}\n", id, result); err != nil {
		t.Fatal(err)
	}
}

func TestBridgeControlLateAckAndNativeTerminal(t *testing.T) {
	b, w, stdout := controlFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := requestAsync(b, ctx, "turn/start", map[string]any{"threadId": "fixture-only"})
	id := wireID(t, w)
	cancel()
	if <-done == nil {
		t.Fatal("timeout accepted")
	}
	s := fence(t, b)
	if len(s.Uncertain) != 1 || s.Uncertain[0] != id || s.TransportQuiescent {
		t.Fatal(s)
	}
	ackWire(t, stdout, id, `{"turn":{"id":"fixture-turn"}}`)
	await(t, func() bool { return len(b.TransportSnapshot().Turns) == 1 })
	if b.TransportSnapshot().TransportQuiescent {
		t.Fatal("ACK mistaken for terminal")
	}
	fmt.Fprintln(stdout, `{"method":"turn/completed","params":{"turn":{"id":"fixture-turn","status":"completed"}}}`)
	await(t, func() bool { return b.TransportSnapshot().TransportQuiescent })
	s = b.TransportSnapshot()
	if len(s.Requests) != 1 || s.Requests[0].ID != id || len(s.Requests[0].PayloadSHA256) != 64 {
		t.Fatal(s)
	}
	ackWire(t, stdout, id, `{"turn":{"id":"fixture-turn"}}`)
	await(t, func() bool { return !b.TransportSnapshot().TransportQuiescent })
}

func TestBridgeControlObservationAndUnknown(t *testing.T) {
	for _, tc := range []struct {
		name, method string
		params       map[string]any
		discardable  bool
	}{
		{"auth", "getAuthStatus", map[string]any{"refreshToken": false, "includeToken": false}, true},
		{"account", "account/read", map[string]any{"refreshToken": false}, true},
		{"refresh", "account/read", map[string]any{"refreshToken": true}, false},
		{"extra", "getAuthStatus", map[string]any{"refreshToken": false, "includeToken": false, "other": false}, false},
		{"unknown", "future/mutation", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, w, _ := controlFixture(t)
			ctx, cancel := context.WithCancel(context.Background())
			done := requestAsync(b, ctx, tc.method, tc.params)
			wireID(t, w)
			cancel()
			<-done
			s := fence(t, b)
			if s.TransportQuiescent != tc.discardable || ((len(s.Discardable) == 1) != tc.discardable) {
				t.Fatal(s)
			}
		})
	}
	b, w, stdout := controlFixture(t)
	done := requestAsync(b, context.Background(), "future/mutation", nil)
	id := wireID(t, w)
	ackWire(t, stdout, id, `{}`)
	if <-done != nil {
		t.Fatal("wire ACK failed")
	}
	if fence(t, b).TransportQuiescent {
		t.Fatal("unknown ACK discarded")
	}
}

func TestBridgeControlFenceCAS(t *testing.T) {
	b, _, _ := controlFixture(t)
	generation := b.TransportSnapshot().Generation
	if _, err := b.FenceTransport("stale", "x"); err == nil {
		t.Fatal("stale accepted")
	}
	fence(t, b)
	if _, err := b.FenceTransport(generation, "fixture-fence"); err != nil {
		t.Fatal(err)
	}
	if _, err := b.FenceTransport(generation, "other"); err == nil {
		t.Fatal("conflict accepted")
	}
	if err := b.transport.starting(); err == nil {
		t.Fatal("fenced restart permitted")
	}
	if err := b.EnsureStarted(context.Background()); err == nil {
		t.Fatal("fenced warmup permitted")
	}
	if err := b.notify(context.Background(), "initialized", nil); err == nil {
		t.Fatal("fenced notification permitted")
	}
	b.transport.process(0, false)
	if b.TransportSnapshot().TransportQuiescent {
		t.Fatal("exit mistaken for idle")
	}
	c := newTransportControl()
	c.starting()
	old := c.generation()
	c.fault()
	c.starting()
	if old == c.generation() || !c.unknown {
		t.Fatal("generation or UNKNOWN lost")
	}
}

func TestBridgeControlBlockedWriteBoundedFence(t *testing.T) {
	b, w, _ := controlFixture(t)
	w.entered = make(chan struct{})
	w.release = make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	done := requestAsync(b, ctx, "turn/start", nil)
	<-w.entered
	start := time.Now()
	s := fence(t, b)
	elapsed := time.Since(start)
	if elapsed > 100*time.Millisecond || s.TransportQuiescent || len(s.Pending) != 1 {
		t.Fatal(elapsed, s)
	}
	t.Logf("stalled write snapshot+fence elapsed=%s budget=100ms", elapsed)
	cancel()
	close(w.release)
	<-done
	if b.TransportSnapshot().TransportQuiescent {
		t.Fatal("uncertain write erased")
	}
}

func TestBridgeControlConcurrentFence(t *testing.T) {
	b, w, _ := controlFixture(t)
	var group sync.WaitGroup
	start := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	for i := 0; i < 64; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			<-start
			b.requestNoEnsure(ctx, "getAuthStatus", map[string]any{"includeToken": false, "refreshToken": false})
		}()
	}
	close(start)
	fence(t, b)
	cancel()
	group.Wait()
	before := len(w.messages)
	snapshot := b.TransportSnapshot()
	for i := 0; i < 64; i++ {
		if _, err := b.requestNoEnsure(context.Background(), "turn/start", nil); err == nil {
			t.Fatal("new request after fence")
		}
	}
	if len(w.messages) != before || len(b.TransportSnapshot().Requests) != len(snapshot.Requests) {
		t.Fatal("fence leaked admission")
	}
}

func TestBridgeControlShortWriteAndCapacity(t *testing.T) {
	b, w, _ := controlFixture(t)
	w.short = true
	if _, err := b.requestNoEnsure(context.Background(), "account/read", map[string]any{"refreshToken": false}); err == nil {
		t.Fatal("short write accepted")
	}
	if fence(t, b).TransportQuiescent {
		t.Fatal("broken framing discarded")
	}
	c := newTransportControl()
	c.capacity = 1
	c.begin(1, "unknown", nil, []byte("one"))
	c.written(1, "UNCERTAIN")
	if c.begin(2, "unknown", nil, nil) == nil || c.requests[1].State != "UNCERTAIN" {
		t.Fatal("audit evicted")
	}
}

func TestBridgeControlMaintenanceAuthAndNoLifecycle(t *testing.T) {
	b, _, _ := controlFixture(t)
	if _, err := b.TransportMaintenanceHandler(""); err == nil {
		t.Fatal("missing auth")
	}
	token := strings.Repeat("x", 32)
	handler, err := b.TransportMaintenanceHandler(token)
	if err != nil {
		t.Fatal(err)
	}
	run := func(method, path, body, auth string) int {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Authorization", auth)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w.Code
	}
	gen := b.TransportSnapshot().Generation
	body := fmt.Sprintf(`{"generation":%q,"fence_id":"fixture"}`, gen)
	if run("POST", "/fence", body, "") != 401 || b.TransportSnapshot().Admission != "OPEN" {
		t.Fatal("unauth mutation")
	}
	if run("GET", "/fence", "", "Bearer "+token) != 404 || b.TransportSnapshot().Admission != "OPEN" {
		t.Fatal("GET mutation")
	}
	if run("POST", "/fence", `{"generation":"stale","fence_id":"fixture"}`, "Bearer "+token) != 409 {
		t.Fatal("CAS bypass")
	}
	if run("POST", "/fence", body, "Bearer "+token) != 200 {
		t.Fatal("fence failed")
	}
	if b.cmd != nil || b.pid != 0 || b.running {
		t.Fatal("lifecycle action")
	}
	if run("GET", "/snapshot", "", "Bearer "+token) != http.StatusOK {
		t.Fatal("snapshot")
	}
}

func TestBridgeControlProtocolUnknownAndCompletionRace(t *testing.T) {
	b, w, stdout := controlFixture(t)
	done := requestAsync(b, context.Background(), "turn/start", nil)
	id := wireID(t, w)
	fmt.Fprintln(stdout, `{"method":"turn/completed","params":{"turn":{"id":"fixture","status":"completed"}}}`)
	ackWire(t, stdout, id, `{"turn":{"id":"fixture"}}`)
	<-done
	if !fence(t, b).TransportQuiescent {
		t.Fatal("completion-before-ACK lost")
	}
	fmt.Fprintln(stdout, `{"id":999,"method":"item/commandExecution/requestApproval","params":{}}`)
	await(t, func() bool { return !b.TransportSnapshot().TransportQuiescent })
	// Malformed native terminal must never close a turn.
	c := newTransportControl()
	c.notice(rpcEnvelope{Method: "turn/completed", Params: json.RawMessage(`{"turn":{"id":"fake","status":"running"}}`)})
	if !c.unknown {
		t.Fatal("unconfirmed terminal accepted")
	}
	// Real wire input is JSON lines; no app-server, Owner, provider or task launched.

}

func TestBridgeControlServerRequestInventory(t *testing.T) {
	b, _, stdout := controlFixture(t)
	fmt.Fprintln(stdout, `{"id":"server-approval-7","method":"item/commandExecution/requestApproval","params":{}}`)
	await(t, func() bool { return len(b.TransportSnapshot().ProtocolGaps) == 1 })
	s := fence(t, b)
	if s.TransportQuiescent || string(s.ProtocolGaps[0].ID) != `"server-approval-7"` {
		t.Fatal(s)
	}
	c := newTransportControl()
	c.starting()
	old := c.generation()
	c.starting()
	c.process(1, true)
	b.transport = c
	if _, err := b.FenceTransport(old, "old"); err == nil {
		t.Fatal("old generation credential accepted")
	}
	if fence(t, b).TransportQuiescent {
		t.Fatal("remote control not accounted")
	}
}

func TestBridgeControlExactRPCIDs(t *testing.T) {
	b, w, stdout := controlFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := requestAsync(b, ctx, "account/read", map[string]any{"refreshToken": false})
	id := wireID(t, w)
	fmt.Fprintf(stdout, `{"id":%d.5,"result":{}}`+"\n", id)
	fmt.Fprintf(stdout, `{"id":"%d","result":{}}`+"\n", id)
	fmt.Fprintf(stdout, `{"id":%d}`+"\n", id)
	fmt.Fprintln(stdout, `{"id":9007199254740993,"method":"approval","params":{}}`)
	await(t, func() bool {
		for _, gap := range b.TransportSnapshot().ProtocolGaps {
			if string(gap.ID) == "9007199254740993" {
				return true
			}
		}
		return false
	})
	s := b.TransportSnapshot()
	if s.Requests[0].AckAt != nil {
		t.Fatal("malformed ACK settled request")
	}
	found := false
	for _, gap := range s.ProtocolGaps {
		if string(gap.ID) == "9007199254740993" {
			found = true
		}
	}
	if !found {
		t.Fatal("large native request ID lost precision", s.ProtocolGaps)
	}
	cancel()
	<-done
	if fence(t, b).TransportQuiescent {
		t.Fatal("protocol uncertainty discarded")
	}
}
