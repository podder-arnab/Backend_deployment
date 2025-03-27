from flask import Flask, request, jsonify, make_response
from file_api import file_api
from user_api import user_api, login_user, register_user
from chatbot_api import chatbot_api  # Import the chatbot API blueprint
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

CORS(app)

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
        decoded = jwt.decode(token, os.getenv('SECRET_KEY'), algorithms=['HS256'])
        return decoded['user_id']
    except jwt.ExpiredSignatureError:
        print("Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        print(f"Invalid Token Error: {e}")
        return None

# Helper function to add CORS headers to responses
def add_cors_to_response(response):
    """Add CORS headers to a response object"""
    if hasattr(response, 'headers'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response



# CORS handling for all responses
@app.after_request
def add_cors_headers(response):
    return add_cors_to_response(response)

# Handle all OPTIONS requests globally
@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_route(path):
    response = make_response()
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
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

# Auth Routes
@app.route('/auth/login', methods=['POST', 'OPTIONS'])
def auth_login_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    return login_user()

@app.route('/auth/register', methods=['POST', 'OPTIONS'])
def auth_register_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    return register_user()

@app.route('/auth/get-user-details', methods=['GET', 'OPTIONS'])
def auth_get_user_details():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import the function from user_api
    from user_api import get_user_details
    return get_user_details(user_id)

# File Routes
@app.route('/recursive-crawl', methods=['POST', 'OPTIONS'])
def recursive_crawl_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    # Import and call the function from file_api
    from file_api import recursive_crawl
    
    # The decorator will handle authentication and pass user_id
    return recursive_crawl()

@app.route('/process-all-links', methods=['POST', 'OPTIONS'])
def process_all_links_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import process_all_links
    return process_all_links(user_id)

@app.route('/all-documents', methods=['GET', 'OPTIONS'])
def all_documents_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import get_all_documents
    return get_all_documents(user_id)

@app.route('/get-discovered-links', methods=['GET', 'OPTIONS'])
def discovered_links_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_discovered_links
    
    if source_url:
        return get_discovered_links(user_id, source_url=source_url)
    else:
        return get_discovered_links(user_id)
    
@app.route('/scrapped-sub-links', methods=['POST', 'OPTIONS'])
def scrapped_sub_links_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    from file_api import scrapped_sub_links
    return scrapped_sub_links()

@app.route('/source-url-status', methods=['GET', 'OPTIONS'])
def source_url_status_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    if not source_url:
        return standardize_error_response('source_url parameter is required.', 'MISSING_PARAM', 400)
    
    # Import and call the function from file_api
    from file_api import get_source_url_status
    return get_source_url_status(user_id, source_url)

# Register blueprints
app.register_blueprint(file_api, url_prefix='/api/files')
app.register_blueprint(user_api, url_prefix='/api/users')
app.register_blueprint(chatbot_api, url_prefix='/api/chatbot')  # Register the chatbot API blueprint

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return standardize_error_response('Resource not found', 'NOT_FOUND', 404)

@app.errorhandler(500)
def internal_error(error):
    return standardize_error_response(error, 'SERVER_ERROR', 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_ENV", "production") == "development"
    
    logger.info(f"Starting server on port {port}, debug mode: {debug_mode}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)