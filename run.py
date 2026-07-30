"""
Application entry point.

    python run.py          # development
    flask --app run run    # flask CLI
"""
import os
import logging
from app import create_app

# Suppress noisy Chrome DevTools probe that hits /.well-known/appspecific/...
class _SuppressChromeDevTools(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/.well-known/appspecific/" not in record.getMessage()

logging.getLogger("werkzeug").addFilter(_SuppressChromeDevTools())

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
