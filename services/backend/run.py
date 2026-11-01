"""Backend entrypoint: bootstrap the database, then serve the API over HTTPS."""
from app import create_app, bootstrap, start_background
from app.config import ssl_context

app = create_app()

if __name__ == "__main__":
    bootstrap()
    start_background()
    app.run(host="0.0.0.0", port=5000, ssl_context=ssl_context())
