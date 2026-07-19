from flask import Flask
from threading import Thread
import logging
# Disable flask logging to keep console clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app = Flask(__name__)
import os
@app.route('/')
def home():
    return "Bot is alive!"
def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
