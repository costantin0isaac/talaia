"""Entry point: serve the application with a single uvicorn worker."""

import uvicorn

from talaia.settings import get_settings

HOST = "0.0.0.0"
PORT = 9999


def main() -> None:
    """Validate settings, then run the ASGI server.

    One worker only: each worker would run its own scheduler and duplicate every check.
    """
    get_settings()
    uvicorn.run(
        "talaia.api.app:create_app",
        factory=True,
        host=HOST,
        port=PORT,
        workers=1,
        log_config=None,
    )


if __name__ == "__main__":
    main()
