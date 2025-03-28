from flask import Flask, request, jsonify, make_response
from file_api import file_api
from user_api import user_api, login_user, register_user
from chatbot_api import chatbot_api
import os
import logging
import jwt
from datetime import datetime
import traceback
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-fallback')

app = Flask(__name__)

allowed_origins = [
    'http://localhost:3000',
    'https://smart-crawler-fe.vercel.app',
    'https://smartcrawl-7joylrzf4-arnabpodder-ebiwcoms-projects.vercel.app'
]

cors_origins = os.getenv('CORS_ORIGINS', '')
if cors_origins:
    additional_origins = [origin.strip() for origin in cors_origins.split(',') if origin.strip()]
    allowed_origins.extend(additional_origins)

CORS(app, resources={r"/*": {"origins": allowed_origins, "supports_credentials": True, "allow_headers": ["Authorization", "Content-Type"]}})

from file_api import initialize_connection_pool
from user_api import initialize_connection_pool as initialize_user_connection_pool
from oracle_chatbot import connect_to_jsondb, initialize_json_database

initialize_connection_pool()
initialize_user_connection_pool()

chatbot_jsondb_connection = connect_to_jsondb()
if chatbot_jsondb_connection:
    initialize_json_database(chatbot_jsondb_connection)
    print("Chatbot database initialized successfully")
    chatbot_jsondb_connection.close()
else:
    print("WARNING: Failed to initialize chatbot database connection")

def standardize_error_response(error, code=None, status_code=500):
    if isinstance(error, Exception):
        error_message = str(error)
        traceback_str = traceback.format_exc()
    else:
        error_message = error
        traceback_str = None

    response = {
        'status': 'error',
        'message': error_message,
        'timestamp': datetime.now().isoformat()
    }

    if code:
        response['code'] = code

    if traceback_str and status_code >= 500:
        logger.error(f"Server error: {error_message}\n{traceback_str}")
        response['error_details'] = traceback_str

    return jsonify(response), status_code

def verify_token():
    token = request.headers.get('Authorization')

    if not token or not token.startswith("Bearer "):
        return None

    token = token.split(" ")[1]
    try:
        decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return decoded['user_id']
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_route(path):
    response = make_response()
    response.headers['Content-Type'] = 'text/plain'
    response.headers['Content-Length'] = '0'
    response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/')
def home():
    return "Backend is working!", 200

@app.route('/health')
def health_check():
    return {
        "status": "healthy",
        "version": "1.0.0"
    }, 200

@app.route('/api/users/auth/login', methods=['POST', 'OPTIONS'])
def auth_login_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    return login_user()

@app.route('/api/users/auth/register', methods=['POST', 'OPTIONS'])
def auth_register_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    return register_user()

@app.route('/api/users/auth/get-user-details', methods=['GET', 'OPTIONS'])
def auth_get_user_details_direct():
    if request.method == 'OPTIONS':
        return options_route('')

    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)

    from user_api import get_user_details
    return get_user_details(user_id)

@app.route('/api/users/auth/logout', methods=['POST', 'OPTIONS'])
def auth_logout_direct():
    if request.method == 'OPTIONS':
        return options_route('')

    token = request.headers.get('Authorization')
    if not token or not token.startswith("Bearer "):
        return standardize_error_response('Token missing or invalid.', 'AUTH_REQUIRED', 401)

    token = token.split(" ")[1]

    from user_api import invalidate_token
    try:
        invalidate_token(token)
        return jsonify({"status": "success", "message": "Logged out successfully"}), 200
    except Exception as e:
        return standardize_error_response(e, 'LOGOUT_FAILED', 500)

app.register_blueprint(file_api, url_prefix='/api/files')
app.register_blueprint(chatbot_api, url_prefix='/api/chatbot')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
