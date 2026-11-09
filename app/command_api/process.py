"""Run with: python -m app.command_api.process"""
import uvicorn
from app.command_api.config import WorkforceCommandSettings
from app.command_api.server import create_command_app

def main() -> None:
    settings = WorkforceCommandSettings()
    settings.validate_listener()
    app = create_command_app(settings=settings)
    uvicorn.run(
        app, host=settings.WORKFORCE_COMMAND_HOST,
        port=settings.WORKFORCE_COMMAND_PORT, log_level="info",
    )

if __name__ == "__main__":
    main()
