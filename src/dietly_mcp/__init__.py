"""dietly-mcp — unofficial Dietly (dietly.pl) API client, CLI and MCP server.

Phase 3 (F3) adds the **write** half: the MCP tools ``dietly_switch``,
``dietly_apply`` and ``dietly_revert`` plus their CLI counterparts, all sharing the
:mod:`dietly_mcp.operations` policy (dry run by default, target must be offered,
every attempt audited, reverts audited too). The read-only tools from phase 2 are
unchanged.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
