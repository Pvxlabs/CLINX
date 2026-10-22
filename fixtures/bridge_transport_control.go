// Migration transport instrumentation for the pinned upstream Bridge.
// It neither runs Codex work nor infers business success from an RPC ACK.
package codexappserver

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

var errAdmissionClosed = errors.New("BRIDGE_ADMISSION_FENCED")

type transportRequest struct {
	ID            int64      `json:"id"`
	Generation    string     `json:"generation"`
	Method        string     `json:"method"`
	PayloadSHA256 string     `json:"payload_sha256"`
	State         string     `json:"state"`
	Observation   bool       `json:"observation"`
	RemoteError   bool       `json:"remote_error"`
	AdmittedAt    time.Time  `json:"admitted_at"`
	AckAt         *time.Time `json:"ack_at,omitempty"`
}

type transportGap struct {
	ID     json.RawMessage `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	State  string          `json:"state"`
}

type transportSnapshot struct {
	ProtocolGaps       []transportGap     `json:"protocol_gaps"`
	GapOverflow        bool               `json:"protocol_gap_overflow"`
	RecycleAuthority   string             `json:"recycle_authority"`
	RemoteControl      string             `json:"remote_control"`
	Generation         string             `json:"generation"`
	PID                int                `json:"pid"`
	ObservedAt         time.Time          `json:"observed_at"`
	Coverage           string             `json:"coverage"`
	Admission          string             `json:"admission"`
	FenceID            string             `json:"fence_id,omitempty"`
	Ready              bool               `json:"ready"`
	Requests           []transportRequest `json:"requests"`
	Pending            []int64            `json:"pending_ids"`
	Uncertain          []int64            `json:"uncertain_ids"`
	Discardable        []int64            `json:"discardable_observation_ids"`
	Turns              map[string]string  `json:"native_turns"`
	Blockers           []string           `json:"blockers"`
	TransportQuiescent bool               `json:"instrumented_transport_quiescent"`
}

type transportControl struct {
	gaps          []transportGap
	gapOverflow   bool
	remoteControl string
	mu            sync.Mutex
	instance      string
	epoch         uint64
	pid           int
	ready         bool
	fenceID       string
	requests      map[int64]*transportRequest
	turns         map[string]string
	unknown       bool
	// All metadata is retained until the parent exits; capacity rejects new
	// work before writing instead of evicting unresolved or terminal facts.
	capacity int
}

func newTransportControl() *transportControl {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		panic(err)
	}
	return &transportControl{instance: hex.EncodeToString(id[:]),
		requests: make(map[int64]*transportRequest), turns: make(map[string]string), capacity: 4096}
}

func (c *transportControl) generation() string { return fmt.Sprintf("%s/%d", c.instance, c.epoch) }

func (c *transportControl) open() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fenceID != "" {
		return errAdmissionClosed
	}
	return nil
}

func (c *transportControl) starting() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fenceID != "" {
		return errAdmissionClosed
	}
	c.epoch++
	c.ready = false
	c.pid = 0
	return nil
}

func (c *transportControl) process(pid int, ready bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.pid = pid
	c.ready = ready
}

func (c *transportControl) begin(id int64, method string, params map[string]any, data []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.fenceID != "" {
		return errAdmissionClosed
	}
	if len(c.requests) >= c.capacity {
		return errors.New("BRIDGE_AUDIT_CAPACITY")
	}
	if _, exists := c.requests[id]; exists {
		return errors.New("BRIDGE_REQUEST_ID_CONFLICT")
	}
	observation := false
	if method == "getAuthStatus" {
		observation = params["refreshToken"] == false && params["includeToken"] == false && len(params) == 2
	} else if method == "account/read" {
		observation = params["refreshToken"] == false && len(params) == 1
	}
	hash := sha256.Sum256(data)
	c.requests[id] = &transportRequest{ID: id, Generation: c.generation(), Method: method,
		PayloadSHA256: hex.EncodeToString(hash[:]), State: "ADMITTED", Observation: observation,
		AdmittedAt: time.Now().UTC()}
	return nil
}

func (c *transportControl) written(id int64, state string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if r := c.requests[id]; r != nil && r.AckAt == nil {
		r.State = state
	}
}

func (c *transportControl) fault() { c.mu.Lock(); defer c.mu.Unlock(); c.unknown = true }

func (c *transportControl) gap(id any, method, state string) {
	c.unknown = true // caller holds c.mu
	if len(c.gaps) >= c.capacity {
		c.gapOverflow = true
		return
	}
	raw, _ := json.Marshal(id)
	c.gaps = append(c.gaps, transportGap{raw, method, state})
}

// Decode IDs without float coercion or string/number equivalence. Malformed
// responses never settle a pending request, even when an int cast would match.
func (c *transportControl) response(envelope rpcEnvelope, raw json.RawMessage) (int64, bool) {
	if envelope.Method != "" {
		return 0, false
	}
	var header struct {
		ID     json.RawMessage `json:"id"`
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	}
	var id int64
	if json.Unmarshal(raw, &header) != nil || json.Unmarshal(header.ID, &id) != nil || len(header.ID) == 0 || string(header.ID) == "null" || ((len(header.Result) > 0) == (len(header.Error) > 0)) || string(header.Error) == "null" {
		c.mu.Lock()
		c.gap(header.ID, "", "MALFORMED_OR_UNMATCHED_RESPONSE")
		c.mu.Unlock()
		return 0, false
	}
	c.ack(id, envelope.Result, envelope.Error != nil)
	return id, true
}

func (c *transportControl) ack(id int64, result json.RawMessage, remoteError bool) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	r := c.requests[id]
	if r == nil {
		c.gap(id, "", "UNMATCHED_ACK")
		return false
	}
	if r.AckAt != nil {
		c.gap(id, r.Method, "DUPLICATE_OR_CONFLICTING_ACK")
		return true
	} // duplicate/conflicting ACK requires review
	now := time.Now().UTC()
	r.AckAt = &now
	r.State = "ACK_OBSERVED"
	r.RemoteError = remoteError
	if !remoteError && r.Method == "turn/start" {
		var response struct {
			Turn struct {
				ID string `json:"id"`
			} `json:"turn"`
		}
		if json.Unmarshal(result, &response) != nil || response.Turn.ID == "" {
			c.unknown = true
		} else if _, exists := c.turns[response.Turn.ID]; !exists {
			c.turns[response.Turn.ID] = "AWAIT_NATIVE_TERMINAL"
		}
	}
	// Unknown methods and remote errors may have side effects despite an ACK.
	if remoteError || (!r.Observation && r.Method != "initialize" && r.Method != "turn/start") {
		c.unknown = true
	}
	// Injection/authentication can outlive the ACK; do not clear their effects
	// without a separately supported reconciliation contract.
	if !remoteError && (r.Method == "thread/inject_items" || r.Method == "account/login/start") {
		c.unknown = true
	}
	return true
}

func (c *transportControl) notice(envelope rpcEnvelope, raw ...json.RawMessage) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if envelope.ID != nil {
		var id any = envelope.ID
		if len(raw) > 0 {
			var header struct {
				ID json.RawMessage `json:"id"`
			}
			if json.Unmarshal(raw[0], &header) == nil {
				id = header.ID
			}
		}
		state := "SERVER_REQUEST_UNRECONCILED"
		if envelope.Method == "" {
			state = "UNMATCHED_ACK"
		}
		c.gap(id, envelope.Method, state)
	}
	if envelope.Method == "remoteControl/status/changed" {
		var status struct {
			Status string `json:"status"`
		}
		if json.Unmarshal(envelope.Params, &status) != nil || status.Status != "disabled" {
			c.unknown = true
			c.remoteControl = "UNKNOWN_OR_ENABLED"
		} else {
			c.remoteControl = "DISABLED_OBSERVED"
		}
		return
	}
	if envelope.Method != "turn/started" && envelope.Method != "turn/completed" {
		return
	}
	var p struct {
		TurnID string `json:"turnId"`
		Turn   struct {
			ID     string `json:"id"`
			Status string `json:"status"`
		} `json:"turn"`
	}
	if json.Unmarshal(envelope.Params, &p) != nil {
		c.unknown = true
		return
	}
	if p.Turn.ID == "" {
		p.Turn.ID = p.TurnID
	}
	if p.Turn.ID == "" {
		c.unknown = true
		return
	}
	if envelope.Method == "turn/completed" && !isTerminalTurnStatus(p.Turn.Status) {
		c.unknown = true
		return
	}
	if envelope.Method == "turn/completed" {
		c.turns[p.Turn.ID] = "NATIVE_TERMINAL_OBSERVED"
	} else if c.turns[p.Turn.ID] != "NATIVE_TERMINAL_OBSERVED" {
		c.turns[p.Turn.ID] = "AWAIT_NATIVE_TERMINAL"
	}
}

func (c *transportControl) snapshotLocked() transportSnapshot {
	s := transportSnapshot{
		ProtocolGaps: append([]transportGap{}, c.gaps...), GapOverflow: c.gapOverflow, RecycleAuthority: "NOT_GRANTED_TRANSPORT_EVIDENCE_ONLY", RemoteControl: c.remoteControl, Generation: c.generation(), PID: c.pid, ObservedAt: time.Now().UTC(),
		Coverage:  "INSTRUMENTED_PARENT_LIFETIME_ONLY_NO_RETROACTIVE_COVERAGE",
		Admission: "OPEN", FenceID: c.fenceID, Ready: c.ready,
		Requests: []transportRequest{}, Pending: []int64{}, Uncertain: []int64{}, Discardable: []int64{},
		Turns: make(map[string]string), Blockers: []string{}}
	if c.fenceID == "" {
		s.Blockers = append(s.Blockers, "ADMISSION_OPEN")
	} else {
		s.Admission = "FENCED"
	}
	if !c.ready || c.pid == 0 {
		s.Blockers = append(s.Blockers, "GENERATION_NOT_READY")
	}
	if c.remoteControl != "DISABLED_OBSERVED" {
		s.Blockers = append(s.Blockers, "REMOTE_CONTROL_NOT_PROVEN_DISABLED")
	}
	if c.unknown {
		s.Blockers = append(s.Blockers, "UNRECONCILED_PROTOCOL_OR_SIDE_EFFECT")
	}
	for _, r := range c.requests {
		copy := *r
		s.Requests = append(s.Requests, copy)
		switch r.State {
		case "ACK_OBSERVED", "NOT_SENT", "NOTIFICATION_WRITTEN":
		case "WAITING_ACK", "UNCERTAIN":
			if r.Observation {
				s.Discardable = append(s.Discardable, r.ID)
			} else {
				s.Uncertain = append(s.Uncertain, r.ID)
			}
		default:
			s.Pending = append(s.Pending, r.ID)
		}
	}
	for id, state := range c.turns {
		s.Turns[id] = state
		if state != "NATIVE_TERMINAL_OBSERVED" {
			s.Blockers = append(s.Blockers, "NATIVE_TURN_NONTERMINAL")
		}
	}
	if len(s.Pending) > 0 {
		s.Blockers = append(s.Blockers, "WRITE_OR_ADMISSION_PENDING")
	}
	if len(s.Uncertain) > 0 {
		s.Blockers = append(s.Blockers, "SIDE_EFFECT_ACK_UNCERTAIN")
	}
	sort.Slice(s.Requests, func(i, j int) bool { return s.Requests[i].ID < s.Requests[j].ID })
	sort.Slice(s.Pending, func(i, j int) bool { return s.Pending[i] < s.Pending[j] })
	sort.Slice(s.Uncertain, func(i, j int) bool { return s.Uncertain[i] < s.Uncertain[j] })
	sort.Slice(s.Discardable, func(i, j int) bool { return s.Discardable[i] < s.Discardable[j] })
	s.TransportQuiescent = len(s.Blockers) == 0
	return s
}

func (b *Bridge) TransportSnapshot() transportSnapshot {
	b.transport.mu.Lock()
	defer b.transport.mu.Unlock()
	return b.transport.snapshotLocked()
}

func (b *Bridge) FenceTransport(expectedGeneration, fenceID string) (transportSnapshot, error) {
	c := b.transport
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.generation() != expectedGeneration || expectedGeneration == "" {
		return c.snapshotLocked(), errors.New("BRIDGE_GENERATION_CAS_CONFLICT")
	}
	if fenceID == "" || len(fenceID) > 128 {
		return c.snapshotLocked(), errors.New("BRIDGE_FENCE_ID_REQUIRED")
	}
	if c.fenceID != "" && c.fenceID != fenceID {
		return c.snapshotLocked(), errors.New("BRIDGE_FENCE_CAS_CONFLICT")
	}
	if !c.ready {
		return c.snapshotLocked(), errors.New("BRIDGE_STARTUP_NOT_QUIESCENT")
	}
	c.fenceID = fenceID // monotonic, no timeout-based reopening or automatic release
	return c.snapshotLocked(), nil
}

// No listener is created and no route is mounted by default. Installation
// authority must separately approve the authenticated private operator route.
// This token authenticates the operator transport, not production change scope.
func (b *Bridge) TransportMaintenanceHandler(token string) (http.Handler, error) {
	if len(token) < 32 {
		return nil, errors.New("OPERATOR_TOKEN_REQUIRED")
	}
	digest := sha256.Sum256([]byte("Bearer " + token))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		supplied := sha256.Sum256([]byte(r.Header.Get("Authorization")))
		if subtle.ConstantTimeCompare(digest[:], supplied[:]) != 1 {
			http.Error(w, "unauthorized", 401)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		if r.Method == "GET" && r.URL.Path == "/snapshot" {
			_ = json.NewEncoder(w).Encode(b.TransportSnapshot())
			return
		}
		if r.Method != "POST" || r.URL.Path != "/fence" {
			http.NotFound(w, r)
			return
		}
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
			http.Error(w, "json required", 415)
			return
		}
		var body struct {
			Generation string `json:"generation"`
			FenceID    string `json:"fence_id"`
		}
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&body) != nil || decoder.Decode(new(any)) != io.EOF {
			http.Error(w, "invalid request", 400)
			return
		}
		snapshot, err := b.FenceTransport(body.Generation, body.FenceID)
		if err != nil {
			w.WriteHeader(409)
		}
		_ = json.NewEncoder(w).Encode(struct {
			Snapshot transportSnapshot `json:"snapshot"`
			Error    string            `json:"error,omitempty"`
		}{snapshot, errorText(err)})
	}), nil
}
func errorText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
