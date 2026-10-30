"""Avvio della demo:  python run.py   (poi apri http://localhost:5050)"""
import os
from pathlib import Path


def _load_env(path: Path) -> None:
    """Carica KEY=valore da .env (senza sovrascrivere le variabili già impostate)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_env(Path(__file__).resolve().parent / ".env")

from app import create_app  # noqa: E402  (dopo il caricamento di .env)

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    print(f"\n  {app.config['APP_NAME']} — modalità {app.config['MODE'].upper()}")
    print(f"  Apri: http://localhost:{port}   (operatore demo: operatore@demo.local / demo1234)\n")
    app.run(host=host, port=port, debug=False)
