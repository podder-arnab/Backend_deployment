from flask import Flask, request, jsonify, make_response
from file_api import file_api
from user_api import user_api, login_user, register_user
from chatbot_api import chatbot_api  # Import the chatbot API blueprint
import os
import logging
import jwt
from datetime import datetime
import traceback
from flask_cors import CORS  # Import Flask-CORS instead of custom middleware

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

# Configure CORS with Flask-CORS
allowed_origins = [
    'http://localhost:3000',  # Local development
    'https://smart-crawler-fe.vercel.app',
    'https://smart-crawler-6ghm35ek8-aniruddha-mukherjees-projects-00946ecf.vercel.app'
]

# Add any CORS_ORIGINS from environment variables
cors_origins = os.getenv('CORS_ORIGINS', '')
if cors_origins:
    additional_origins = [origin.strip() for origin in cors_origins.split(',') if origin.strip()]
    allowed_origins.extend(additional_origins)

# Apply CORS to the app with configuration
CORS(app, resources={r"/*": {"origins": allowed_origins, "supports_credentials": True}})

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

# Handle all OPTIONS requests globally
@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_route(path):
    response = make_response()
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

# Auth Routes
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
    
    # Import the function from user_api
    from user_api import get_user_details
    return get_user_details(user_id)

# File Routes
@app.route('/api/files/recursive-crawl', methods=['POST', 'OPTIONS'])
def recursive_crawl_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import recursive_crawl
    return recursive_crawl(user_id)

@app.route('/api/files/process-all-links', methods=['POST', 'OPTIONS'])
def process_all_links_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import process_all_links
    return process_all_links(user_id)

@app.route('/api/files/all-documents', methods=['GET', 'OPTIONS'])
def all_documents_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import get_all_documents
    return get_all_documents(user_id)

@app.route('/api/files/get-discovered-links', methods=['GET', 'OPTIONS'])
def discovered_links_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Get the source_url parameter
    source_url = request.args.get('source_url')
    
    # Import and call the function from file_api
    from file_api import get_discovered_links
    return get_discovered_links(user_id)

@app.route('/api/files/scrapped-sub-links', methods=['POST', 'OPTIONS'])
def scrapped_sub_links_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    from file_api import scrapped_sub_links
    return scrapped_sub_links()

@app.route('/api/files/source-url-status', methods=['GET', 'OPTIONS'])
def source_url_status_direct():
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
    return get_source_url_status(user_id)

@app.route('/api/files/progress-bar', methods=['GET', 'OPTIONS'])
def progress_bar_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    # Get source URL from query parameters
    source_url = request.args.get('source_url')
    
    if not source_url:
        return standardize_error_response('source_url parameter is required.', 'MISSING_PARAM', 400)
    
    # Import and call the function from file_api
    from file_api import get_progress_bar
    return get_progress_bar()

@app.route('/api/files/get-scrapped-links', methods=['GET', 'OPTIONS'])
def get_scrapped_links_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import realtime_scrapped_links
    return realtime_scrapped_links(user_id)

@app.route('/api/files/get-pending-links', methods=['GET', 'OPTIONS'])
def get_pending_links_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import realtime_pending_links
    return realtime_pending_links(user_id)

@app.route('/api/files/get-total-words-scrapped', methods=['GET', 'OPTIONS'])
def get_total_words_scrapped_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import realtime_total_words_scrapped
    return realtime_total_words_scrapped(user_id)

@app.route('/api/files/get-total-words', methods=['GET', 'OPTIONS'])
def get_total_words_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import get_total_words
    return get_total_words(user_id)

@app.route('/api/files/vectorization-ready', methods=['GET', 'OPTIONS'])
def vectorization_ready_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import check_vectorization_ready
    return check_vectorization_ready(user_id)

@app.route('/api/files/vectorization-status', methods=['GET', 'OPTIONS'])
def vectorization_status_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    user_id = verify_token()
    if not user_id:
        return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
    
    # Import and call the function from file_api
    from file_api import get_vectorization_status
    return get_vectorization_status(user_id)

# Chatbot Routes
@app.route('/api/chatbot/chat', methods=['POST', 'OPTIONS'])
def chatbot_chat_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    try:
        data = request.json
        message = data.get('message')
        session_id = data.get('session_id')
        api_key = data.get('api_key')
        source_level_url = data.get('source_level_url')
        
        # Extract user_id from the Authorization header
        user_id = verify_token()
        
        # Create a new database connection for this request
        jsondb_connection = connect_to_jsondb()
        if not jsondb_connection:
            return standardize_error_response('Failed to connect to database', 'DB_ERROR', 500)
        
        try:
            # Process the chat request with the source_level_url and user_id
            import asyncio
            
            # Pass user_id to process_chat_request
            response = asyncio.run(process_chat_request(jsondb_connection, message, session_id, api_key, source_level_url, user_id))
            
            # Prepare the response
            result = {
                'status': 'success',
                'answer': response.answer,
                'sources': response.sources,
                'session_id': response.session_id,
                'title': response.title,
                'timestamp': datetime.now().isoformat()
            }
            
            # Include source_level_url in response if it was provided
            if source_level_url:
                result['source_level_url'] = source_level_url
                
            return jsonify(result)
        finally:
            jsondb_connection.close()
    
    except Exception as e:
        print(f"Error in chat endpoint: {e}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'CHAT_ERROR', 500)

@app.route('/api/chatbot/conversations', methods=['GET', 'OPTIONS'])
def chatbot_conversations_direct():
    if request.method == 'OPTIONS':
        return options_route('')
    
    try:
        # Extract user_id from query parameters 
        user_id = request.args.get('user_id')
        
        # If not in query params, try to extract from Authorization header
        if not user_id:
            user_id = verify_token()
            if not user_id:
                return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
        
        # Create a new database connection for this request
        jsondb_connection = connect_to_jsondb()
        if not jsondb_connection:
            return standardize_error_response('Failed to connect to database', 'DB_ERROR', 500)
        
        try:
            # Get all conversations with improved function, passing user_id
            import asyncio
            conversations = asyncio.run(get_all_conversations(jsondb_connection, user_id))
            
            return jsonify({
                'status': 'success',
                'conversations': conversations,
                'timestamp': datetime.now().isoformat()
            })
        finally:
            jsondb_connection.close()
    
    except Exception as e:
        print(f"Error in get_conversations endpoint: {e}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'CONVERSATIONS_ERROR', 500)

@app.route('/api/chatbot/conversation/<session_id>', methods=['GET', 'OPTIONS'])
def chatbot_conversation_direct(session_id):
    if request.method == 'OPTIONS':
        return options_route('')
    
    try:
        # Verify the token
        user_id = verify_token()
        if not user_id:
            return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
        
        # Create a new database connection for this request
        jsondb_connection = connect_to_jsondb()
        if not jsondb_connection:
            return standardize_error_response('Failed to connect to database', 'DB_ERROR', 500)
        
        try:
            # Get the conversation with improved function
            import asyncio
            session = asyncio.run(get_session(jsondb_connection, session_id))
            
            if not session:
                return standardize_error_response('Conversation not found', 'NOT_FOUND', 404)
            
            return jsonify({
                'status': 'success',
                'session': session,
                'timestamp': datetime.now().isoformat()
            })
        finally:
            jsondb_connection.close()
    
    except Exception as e:
        print(f"Error in get_conversation endpoint: {e}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'CONVERSATION_ERROR', 500)

@app.route('/api/chatbot/conversation/<session_id>/title', methods=['PUT', 'OPTIONS'])
def chatbot_update_title_direct(session_id):
    if request.method == 'OPTIONS':
        return options_route('')
    
    try:
        # Verify the token
        user_id = verify_token()
        if not user_id:
            return standardize_error_response('Unauthorized access. Valid token required.', 'AUTH_REQUIRED', 401)
        
        data = request.json
        new_title = data.get('title')
        
        if not new_title:
            return standardize_error_response('Title is required', 'MISSING_PARAM', 400)
        
        # Create a new database connection for this request
        jsondb_connection = connect_to_jsondb()
        if not jsondb_connection:
            return standardize_error_response('Failed to connect to database', 'DB_ERROR', 500)
        
        try:
            # Update the title - use asyncio.run for async function
            import asyncio
            result = asyncio.run(update_session_title(jsondb_connection, session_id, new_title))
            
            if not result:
                return standardize_error_response('Failed to update title', 'UPDATE_ERROR', 500)
            
            return jsonify({
                'status': 'success',
                'message': 'Title updated successfully',
                'session_id': session_id,
                'title': new_title,
                'timestamp': datetime.now().isoformat()
            })
        finally:
            jsondb_connection.close()
    
    except Exception as e:
        print(f"Error in update_conversation_title endpoint: {e}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'UPDATE_TITLE_ERROR', 500)

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

# Import necessary functions for direct route handlers
# These imports are placed here to avoid circular imports
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