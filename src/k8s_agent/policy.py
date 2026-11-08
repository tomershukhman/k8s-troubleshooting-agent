"""Safety policy constants. Not tunables: changing these changes the safety boundary."""

# The only MCP tools the investigator may use (SPEC §3.3). Enforced in the MCP server's
# config.toml and again when loading tools into the agent.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "namespaces_list",
        "pods_list_in_namespace",
        "pods_get",
        "pods_log",
        "events_list",
        "resources_get",
        "resources_list",
    }
)

# Namespace holding the agent's ServiceAccounts (`agent setup`). Never deleted by scenarios.
AGENT_NAMESPACE = "k8s-agent"

# Namespaces the agent must never be granted, whatever the config says: cluster system
# namespaces can hold sensitive ConfigMaps, and k8s-agent holds the agent's own identities.
FORBIDDEN_NAMESPACES: frozenset[str] = frozenset(
    {"kube-system", "kube-public", "kube-node-lease", AGENT_NAMESPACE}
)
