"""
Development entry point.

    python run.py            # http://0.0.0.0:5110

The Werkzeug reloader is disabled on purpose: it starts the app twice, which
would open two switcher sessions and two UDP listeners. The interactive
Werkzeug debugger is disabled too: it offers a Python console to anyone who
can reach the port.
"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5110")),
        debug=app.config.get("DEBUG", False),
        use_debugger=False,
        use_reloader=False,
        threaded=True,
    )
