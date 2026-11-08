"""Safety policy constants. Not tunables: changing these changes the safety boundary."""

# The only MCP tools the investigator may use (SPEC §3.3). Enforced in the MCP server's
# config.toml and again when loading tools into the agent.
READ_ONLY_TOOLS = frozenset(
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

# Namespace holding the agent's ServiceAccounts (deploy/rbac.yaml). Never deleted by scenarios.
AGENT_NAMESPACE = "k8s-agent"
