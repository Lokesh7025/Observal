from models.agent import Agent
from services.config_generator import generate_config


def generate_agent_config(agent: Agent, ide: str) -> dict:
    """Generate IDE-specific config for an agent, bundling prompt + MCP configs."""
    mcp_configs = {}
    for link in agent.mcp_links:
        listing = link.mcp_listing
        cfg = generate_config(listing, ide)
        if ide == "claude-code":
            mcp_configs[listing.name] = cfg
        elif "mcpServers" in cfg:
            mcp_configs.update(cfg["mcpServers"])

    if ide == "claude-code":
        setup_commands = [c.get("command", "") for c in mcp_configs.values() if isinstance(c, dict) and "command" in c]
        return {
            "rules_file": {
                "path": f".claude/rules/{agent.name}.md",
                "content": agent.prompt,
            },
            "mcp_setup_commands": setup_commands,
            "mcpServers": {
                name: cfg for name, cfg in mcp_configs.items()
                if isinstance(cfg, dict) and "mcpServers" not in cfg and "command" not in cfg
            },
        }

    if ide == "kiro":
        return {
            "rules_file": {
                "path": f".kiro/rules/{agent.name}.md",
                "content": agent.prompt,
            },
            "mcp_json": {"mcpServers": mcp_configs},
        }

    if ide == "gemini-cli":
        return {
            "rules_file": {
                "path": "GEMINI.md",
                "content": agent.prompt,
            },
            "mcp_config": {"mcpServers": mcp_configs},
        }

    # Default (cursor, vscode, etc.)
    return {
        "rules_file": {
            "path": f".rules/{agent.name}.md",
            "content": agent.prompt,
        },
        "mcp_config": {"mcpServers": mcp_configs},
    }
