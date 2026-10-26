import os


class Config:
    # If SECRET_KEY is not set in the environment, app.py creates a random one
    # and keeps it in data/.secret_key (not uploaded to GitHub).
    SECRET_KEY = os.getenv("SECRET_KEY")