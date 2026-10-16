"""Console entrypoint — serves the UI over HTTPS."""
from app import create_app
from app.config import ssl_context

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, ssl_context=ssl_context())
