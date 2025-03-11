from flask import Flask
from file_api import file_api
import os
from flask_cors import CORS
# Create the main Flask app
app = Flask(__name__)

CORS(app, resources={
    r"/realtime-stats/*": {"origins": "*"},
    r"/scrapped-sub-links": {"origins": "*"}
})

@app.route('/')
def home():
    return "Backend is working!", 200


# Register blueprints
app.register_blueprint(file_api)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))  # Default to 5000 if PORT is not set
    app.run(host="0.0.0.0", port=port)