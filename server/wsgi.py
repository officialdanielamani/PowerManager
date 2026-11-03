from app import create_app

app = create_app()

if __name__ == "__main__":
    # Development server only. In Docker, gunicorn is used (see gunicorn.conf.py).
    app.run(host="0.0.0.0", port=5000, threaded=True)
