# Vulture whitelist — intentionally "unused" names that are framework-required.

from rocannon.config import Config
from rocannon.server import _AuditMiddleware

# Pydantic validators require `cls` as first argument
Config.inventories_must_exist  # type: ignore[attr-defined]
Config.modules_must_not_be_empty  # type: ignore[attr-defined]
Config.validate_inventories_not_empty  # type: ignore[attr-defined]

# FastMCP middleware lifecycle method
_AuditMiddleware.on_call_tool  # type: ignore[attr-defined]
