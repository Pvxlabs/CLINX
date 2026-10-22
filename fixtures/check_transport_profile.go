// Read-only compatibility check: parse configurations, never construct an app.
// Credentials enter through stdin and are never printed or written to a file.
package main

import (
	"encoding/json"
	"os"
	"reflect"

	"github.com/openai/tunnel-client/pkg/config"
	"github.com/openai/tunnel-client/pkg/runtimeconfig"
)

func main() {
	var input struct {
		Args []string
		Env  map[string]string
	}
	if json.NewDecoder(os.Stdin).Decode(&input) != nil {
		os.Exit(2)
	}
	lookup := func(key string) (string, bool) { value, ok := input.Env[key]; return value, ok }
	full, fullErr := config.Load(input.Args, lookup)
	runtime, runtimeErr := runtimeconfig.Load(input.Args, runtimeconfig.FlavorRuntime, lookup)
	checks := map[string]bool{"full_profile_parsed": fullErr == nil, "runtime_profile_parsed": runtimeErr == nil}
	if fullErr == nil && runtimeErr == nil {
		checks["control_plane_equal"] = reflect.DeepEqual(full.ControlPlane, runtime.ControlPlane)
		checks["mcp_equal"] = reflect.DeepEqual(full.MCP, runtime.MCP)
		checks["health_equal"] = reflect.DeepEqual(full.Health, runtime.Health)
		checks["logging_equal"] = reflect.DeepEqual(full.Logging, runtime.Logging)
		checks["process_equal"] = reflect.DeepEqual(full.Process, runtime.Process)
		checks["tls_equal"] = reflect.DeepEqual(full.TLS, runtime.TLS)
	}
	_ = json.NewEncoder(os.Stdout).Encode(checks)
	for _, pass := range checks {
		if !pass {
			os.Exit(2)
		}
	}
}
