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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Get environment variables
SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-fallback')

# Create the Flask app
app = Flask(__name__)

# Comprehensive CORS configuration
allowed_origins = [
    'http://localhost:3000',    # Local React development
    'http://localhost:3001',    # Additional local development port
    'https://smart-crawler-fe.vercel.app',
    'https://smart-crawler-6ghm35ek8-aniruddha-mukherjees-projects-00946ecf.vercel.app',
    'https://your-production-domain.com'  # Add your production domain
]

# Add any additional origins from environment variables
cors_origins = os.getenv('CORS_ORIGINS', '')
if cors_origins:
    additional_origins = [origin.strip() for origin in cors_origins.split(',') if origin.strip()]
    allowed_origins.extend(additional_origins)

# Enhanced CORS configuration
CORS(app, 
    resources={r"/*": {
        "origins": allowed_origins,
        "supports_credentials": True,
        "allow_headers": [
            "Content-Type", 
            "Authorization", 
            "X-Requested-With",
            "Access-Control-Allow-Origin",
            "Access-Control-Allow-Credentials"
        ],
        "methods": ["OPTIONS", "GET", "POST", "PUT", "DELETE", "PATCH"]
    }}
)

# Initialize database connection pools
from file_api import initialize_connection_pool
from user_api import initialize_connection_pool as initialize_user_connection_pool
from oracle_chatbot import connect_to_jsondb, initialize_json_database

# Initialize connection pools
initialize_connection_pool()
initialize_user_connection_pool()

# Initialize the chatbot JSON database
chatbot_jsondb_connection = connect_to_jsondb()
if chatbot_jsondb_connection:
    initialize_json_database(chatbot_jsondb_connection)
    print("Chatbot database initialized successfully")
else:
    print("WARNING: Failed to initialize chatbot database connection")
    
# Close the connection after initialization
if chatbot_jsondb_connection:
    chatbot_jsondb_connection.close()

# Standardized error response function
def standardize_error_response(error, code=None, status_code=500):
    """
    Create a standardized error response
    
    Args:
        error: The error message or exception
        code: Error code for frontend handling
        status_code: HTTP status code
        
    Returns:
        JSON response with error details and status code
    """
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
        # Include traceback in response for server errors
        logger.error(f"Server error: {error_message}\n{traceback_str}")
        response['error_details'] = traceback_str
    
    resp = jsonify(response), status_code
    return resp

# Helper function to verify JWT tokens
def verify_token():
    """Verify JWT token and get user_id"""
    token = request.headers.get('Authorization')
    
    if not token or not token.startswith("Bearer "):
        print("Invalid token format")
        return None

    token = token.split(" ")[1]
    try:
        # Use the exact same SECRET_KEY as used in token generation
        decoded = jwt.decode(token, os.getenv('SECRET_KEY', SECRET_KEY), algorithms=['HS256'])
        return decoded['user_id']
    except jwt.ExpiredSignatureError:
        print("Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        print(f"Invalid Token Error: {e}")
        return None

# Enhanced OPTIONS handler
@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_route(path):
    response = make_response()
    
    # Get the request origin
    origin = request.headers.get('Origin')
    
    # Check if the origin is allowed
    if origin in allowed_origins or any(origin.startswith(allowed_origin) for allowed_origin in allowed_origins):
        response.headers.add("Access-Control-Allow-Origin", origin)
    
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS,PATCH')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    response.headers['Content-Type'] = 'text/plain'
    response.headers['Content-Length'] = '0'
    return response

# Root routes
@app.route('/')
def home():
    return "Backend is working!", 200

@app.route('/health')
def health_check():
    return {
        "status": "healthy",
        "version": "1.0.0"
    }, 200

# [Rest of the routes remain the same as in the original file]
# ... (include all the existing routes from login to chatbot)

# Register blueprints
app.register_blueprint(file_api, url_prefix='/api/files')
app.register_blueprint(user_api, url_prefix='/api/users')
app.register_blueprint(chatbot_api, url_prefix='/api/chatbot')

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return standardize_error_response('Resource not found', 'NOT_FOUND', 404)

@app.errorhandler(500)
def internal_error(error):
    return standardize_error_response(error, 'SERVER_ERROR', 500)

# Import necessary functions for direct route handlers
from oracle_chatbot import (
    process_chat_request, 
    get_all_conversations, 
    get_session, 
    update_session_title
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_ENV", "production") == "development"
    
    logger.info(f"Starting server on port {port}, debug mode: {debug_mode}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)