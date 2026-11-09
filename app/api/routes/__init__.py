"""Route modules untuk endpoint GET observabilitas."""
from .audit import router as audit_router
from .events import router as events_router
from .market_watches import router as market_watches_router
from .pipeline import router as pipeline_router
from .system import router as system_router
from .telemetry import router as telemetry_router
from .workforce import router as workforce_router
from .governance import router as governance_router

__all__ = [
    "audit_router", "events_router", "market_watches_router", "pipeline_router",
    "system_router", "telemetry_router", "workforce_router", "governance_router",
]
