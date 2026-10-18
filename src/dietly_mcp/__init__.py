"""dietly-mcp — unofficial Dietly (dietly.pl) API client, CLI and MCP server.

Phase 3 (F3) added the **write** half: the MCP tools ``dietly_switch``,
``dietly_apply`` and ``dietly_revert`` plus their CLI counterparts, all sharing the
:mod:`dietly_mcp.operations` policy (dry run by default, target must be offered,
every attempt audited, reverts audited too).

0.2.0 exposes the **full nutrition** Dietly returns: every meal/option payload
carries a ``macros`` dict (protein, carbs, fat, fiber, sugar, salt, saturates in
grams, plus ``weight`` and ``kcalText``) beside the top-level ``kcal``, and
blank allergen entries are stripped everywhere.
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
