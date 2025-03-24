from flask import Flask, request, jsonify, make_response
from file_api import file_api
from user_api import user_api, login_user, register_user
import os
import logging
import jwt
from datetime import datetime
import traceback

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
        return None

    token = token.split(" ")[1]
    try:
        decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return decoded['user_id']
    except jwt.ExpiredSignatureError:
        logger.warning("Token verification failed: Token expired")
        return None
    except jwt.InvalidTokenError:
        logger.warning("Token verification failed: Invalid token")
        return None
    except Exception as e:
        logger.error(f"Token verification error: {str(e)}")
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

@app.route('/auth/logout', methods=['POST', 'OPTIONS'])
def auth_logout():
    if request.method == 'OPTIONS':
        return options_route('')
    
    # Import the function from user_api
    from user_api import logout_user
    return logout_user()

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

@app.route('/auth/forgot-password', methods=['POST', 'OPTIONS'])
def auth_forgot_password():
    if request.method == 'OPTIONS':
        return options_route('')
    
    # Import the function from user_api
    from user_api import forgot_password
    return forgot_password()

@app.route('/auth/token-refresh', methods=['POST', 'OPTIONS'])
def auth_token_refresh():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import the function from user_api
    from user_api import refresh_token
    return refresh_token(user_id)

# Dashboard Routes
@app.route('/all-documents', methods=['GET', 'OPTIONS'])
def all_documents_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import here to avoid circular imports
    from file_api import get_all_documents
    
    # Pass user_id explicitly
    return get_all_documents(user_id)

@app.route('/discovered-links', methods=['GET', 'OPTIONS'])
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
    
    # Pass user_id explicitly
    if source_url:
        return get_discovered_links(user_id, source_url=source_url)
    else:
        return get_discovered_links(user_id)

@app.route('/scrapped-sub-links', methods=['GET', 'POST', 'OPTIONS'])
def scrapped_sub_links_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    # Import the function
    from file_api import scrapped_sub_links
    
    # Call the function directly - no auth needed for this endpoint
    return scrapped_sub_links()

@app.route('/progress-bar', methods=['GET', 'OPTIONS'])
def progress_bar_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    if not source_url:
        return standardize_error_response('source_url parameter is required.', 'MISSING_PARAM', 400)
    
    # Import and call the function from file_api
    from file_api import get_progress_bar
    
    # Call with source_url parameter
    return get_progress_bar()

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
    
    # Don't pass user_id explicitly - let the decorator handle it
    return get_source_url_status()

@app.route('/queue-status', methods=['GET', 'OPTIONS'])
def queue_status_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import get_queue_status
    
    # Pass user_id explicitly
    return get_queue_status(user_id)

@app.route('/recursive-crawl', methods=['POST', 'OPTIONS'])
def recursive_crawl_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import recursive_crawl
    
    # DON'T pass user_id explicitly - let the decorator handle it
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
    
    # DON'T pass user_id explicitly - let the decorator handle it
    return process_all_links()

@app.route('/stop-crawling', methods=['POST', 'OPTIONS'])
def stop_crawling_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import stop_crawling
    
    # Pass user_id explicitly
    return stop_crawling(user_id)

@app.route('/stop-processing', methods=['POST', 'OPTIONS'])
def stop_processing_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import stop_processing_job
    
    # Pass user_id explicitly
    return stop_processing_job(user_id)

@app.route('/remove-from-queue', methods=['POST', 'OPTIONS'])
def remove_from_queue_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import remove_from_queue
    
    # Pass user_id explicitly
    return remove_from_queue(user_id)

# Real-time Stats API routes
@app.route('/realtime-stats/links-to-scrap', methods=['GET', 'OPTIONS'])
def realtime_links_to_scrap():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_links_to_scrap
    
    # Pass user_id explicitly
    return get_links_to_scrap(user_id)

@app.route('/realtime-stats/total-processed-links', methods=['GET', 'OPTIONS'])
def realtime_total_processed_links():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_total_processed_links
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_total_processed_links(user_id, source_url=source_url)
    else:
        return get_total_processed_links(user_id)

@app.route('/realtime-stats/scrapped-links', methods=['GET', 'OPTIONS'])
def realtime_scrapped_links():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_scrapped_links
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_scrapped_links(user_id, source_url=source_url)
    else:
        return get_scrapped_links(user_id)

@app.route('/realtime-stats/pending-links', methods=['GET', 'OPTIONS'])
def realtime_pending_links():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_pending_links
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_pending_links(user_id, source_url=source_url)
    else:
        return get_pending_links(user_id)

@app.route('/realtime-stats/total-words-scrapped', methods=['GET', 'OPTIONS'])
def realtime_total_words_scrapped():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_total_words_scrapped
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_total_words_scrapped(user_id, source_url=source_url)
    else:
        return get_total_words_scrapped(user_id)

# Handle for compatibility with old frontend routes
@app.route('/scrapped-links', methods=['GET', 'OPTIONS'])
def scrapped_links_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_scrapped_links
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_scrapped_links(user_id, source_url=source_url)
    else:
        return get_scrapped_links(user_id)

@app.route('/pending-links', methods=['GET', 'OPTIONS'])
def pending_links_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_pending_links
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_pending_links(user_id, source_url=source_url)
    else:
        return get_pending_links(user_id)

@app.route('/total-words-scrapped', methods=['GET', 'OPTIONS'])
def total_words_scrapped_redirect():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_total_words_scrapped
    
    # Pass user_id explicitly and source_url if provided
    if source_url:
        return get_total_words_scrapped(user_id, source_url=source_url)
    else:
        return get_total_words_scrapped(user_id)

@app.route('/details', methods=['GET', 'OPTIONS'])
def details_redirect():
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
    
    # Reuse source_url_status as details endpoint
    return get_source_url_status(user_id)

# Register blueprints
app.register_blueprint(file_api, url_prefix='/api/files')
app.register_blueprint(user_api, url_prefix='/api/users')

# Try to register chatbot_api if it exists
try:
    from chatbot_api import chatbot_api
    app.register_blueprint(chatbot_api, url_prefix='/api/chatbot')
    logger.info("Registered chatbot_api blueprint")
except ImportError:
    logger.info("chatbot_api not available, skipping registration")

# Log all registered routes
logger.info("Registered routes:")
for rule in app.url_map.iter_rules():
    logger.info(f"Route: {rule}, Methods: {rule.methods}")

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