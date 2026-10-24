import os

from synchub.web import create_app

if __name__ == "__main__":
    app = create_app(start_scheduler=True)
    app.run(host="0.0.0.0", port=int(os.getenv("SYNCHUB_PORT", "5001")), debug=os.getenv("FLASK_DEBUG") == "1", use_reloader=False)
