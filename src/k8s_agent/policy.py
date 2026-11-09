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

# The remediator's tools (SPEC 3.4), all as the `remediator` identity: three reads, and the one
# tool that changes anything, which runs only after a person approves its exact arguments.
REMEDIATOR_READ_TOOLS = frozenset({"get_service", "list_pods", "list_endpoint_slices"})
FIX_TOOL = "patch_service_selector"

# Namespace holding the agent's ServiceAccounts (deploy/rbac.yaml). Never deleted by scenarios.
AGENT_NAMESPACE = "k8s-agent"
