from flask import Blueprint, request, jsonify
import os
import json
from datetime import datetime
import traceback
from functools import wraps
from oracle_chatbot import (
    connect_to_jsondb, 
    initialize_json_database, 
    process_scrapped_text_to_vector_store,
    process_chat_request,
    get_all_conversations,
    get_session,
    update_session_title,
    verify_api_key
)

# Create the blueprint
chatbot_api = Blueprint('chatbot_api', __name__)

# Middleware for API key verification
def api_key_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.json.get('api_key') if request.is_json else request.args.get('api_key')
        
        if not api_key or not verify_api_key(api_key):
            return jsonify({
                'status': 'error',
                'message': 'Invalid or missing API key',
                'timestamp': datetime.now().isoformat()
            }), 401
            
        return f(*args, **kwargs)
    return decorated

# Initialize the Oracle JSON database on module load
jsondb_connection = connect_to_jsondb()
if jsondb_connection:
    initialize_json_database(jsondb_connection)
    print("Chatbot API initialized successfully")
else:
    print("WARNING: Failed to initialize chatbot database connection")

# Option 1: Remove async/await from Flask routes
# Modify the chatbot_api.py file to remove async from routes

@chatbot_api.route('/chat', methods=['POST'])
@api_key_required
def chat():
    """
    Process a chat message and get a response
    
    Required Request body:
    - message: The user message
    - source_level_url: The URL context for the conversation (REQUIRED)
    - session_id: (Optional) The session ID to continue an existing conversation
    - api_key: API key for authentication
    """
    try:
        data = request.json
        
        # Validate required parameters
        message = data.get('message')
        source_level_url = data.get('source_level_url')
        
        if not message:
            return jsonify({
                'status': 'error',
                'message': 'Message is required',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        if not source_level_url:
            return jsonify({
                'status': 'error',
                'message': 'source_level_url is required',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        session_id = data.get('session_id')
        api_key = data.get('api_key')
        
        # Extract user_id from the Authorization header
        user_id = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                from app import SECRET_KEY
                import jwt
                decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
                user_id = decoded.get('user_id')
            except Exception as jwt_error:
                print(f"Could not extract user_id from token: {str(jwt_error)}")
        
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Process the chat request with the source_level_url
            import asyncio
            
            # Add source_level_url to the asyncio.run call
            response = asyncio.run(process_chat_request(
                db_conn, 
                message, 
                session_id, 
                api_key, 
                source_level_url,  # Added source_level_url
                user_id
            ))
            
            # Prepare the response
            result = {
                'status': 'success',
                'answer': response.answer,
                'sources': response.sources,
                'session_id': response.session_id,
                'title': response.title,
                'timestamp': datetime.now().isoformat(),
                'source_level_url': source_level_url  # Include the URL in the response
            }
            
            return jsonify(result)
        finally:
            db_conn.close()
    
    except Exception as e:
        print(f"Error in chat endpoint: {e}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500


# Apply the same pattern to other async endpoints
@chatbot_api.route('/vectorize', methods=['POST'])
def vectorize_scrapped_text():  # Remove async keyword
    """
    Process scrapped text data into the vector store
    
    Request body:
    - user_id: (Optional) Filter by user_id
    - source_level_url: URL to filter documents by and associate with vectorized data
    - api_key: API key for authentication
    """
    try:
        data = request.json
        user_id = data.get('user_id')
        source_level_url = data.get('source_level_url')
        
        if not source_level_url:
            return jsonify({
                'status': 'error',
                'message': 'source_level_url is required in the request body',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Process the scrapped text data filtered by source_level_url
            result = process_scrapped_text_to_vector_store(db_conn, user_id, source_level_url)
            
            result['timestamp'] = datetime.now().isoformat()
            result['source_level_url'] = source_level_url  # Include in response
            return jsonify(result)
        finally:
            db_conn.close()
    
    except Exception as e:
        print(f"Error in vectorize endpoint: {e}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@chatbot_api.route('/conversations', methods=['GET'])
@api_key_required
def get_conversations():
    """
    Get all chat conversations for a specific source URL
    
    Required Query parameters:
    - source_level_url: The specific URL to filter conversations
    - api_key: API key for authentication
    
    Optional Query parameters:
    - user_id: (Optional) Filter conversations by user ID
    """
    try:
        # Extract required source_level_url
        source_level_url = request.args.get('source_level_url')
        
        # Validate source_level_url
        if not source_level_url:
            return jsonify({
                'status': 'error',
                'message': 'source_level_url is required',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Extract user_id from query parameters 
        user_id = request.args.get('user_id')
        
        # If not in query params, try to extract from Authorization header
        if not user_id:
            auth_header = request.headers.get('Authorization')
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
                try:
                    from app import SECRET_KEY  # Import the secret key
                    import jwt
                    decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
                    user_id = decoded.get('user_id')
                    print(f"Extracted user_id {user_id} from JWT token")
                except Exception as jwt_error:
                    print(f"Could not extract user_id from token: {str(jwt_error)}")
                    # Continue without user_id
        
        print(f"Getting conversations for source_level_url: {source_level_url}, user_id: {user_id}")
        
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Get all conversations with strict filtering
            import asyncio
            conversations = asyncio.run(get_all_conversations(db_conn, user_id))
            
            # Further filter conversations to match the source_level_url
            filtered_conversations = [
                conv for conv in conversations 
                if source_level_url in str(conv)
            ]
            
            print(f"Retrieved {len(filtered_conversations)} conversations for {source_level_url}")
            
            # Include debug info in the response during testing
            debug_info = {
                'db_connection': 'successful',
                'conversations_retrieved': len(filtered_conversations),
                'source_level_url': source_level_url
            }
            
            return jsonify({
                'status': 'success',
                'conversations': filtered_conversations,
                'debug': debug_info,
                'source_level_url': source_level_url,
                'timestamp': datetime.now().isoformat()
            })
        finally:
            db_conn.close()
    
    except Exception as e:
        print(f"Error in get_conversations endpoint: {e}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'source_level_url': source_level_url,
            'timestamp': datetime.now().isoformat()
        }), 500

@chatbot_api.route('/conversation/<session_id>', methods=['GET'])
@api_key_required
def get_conversation(session_id):
    """
    Get a specific chat conversation with improved error handling
    
    Path parameters:
    - session_id: The session ID
    
    Query parameters:
    - api_key: API key for authentication
    """
    try:
        print(f"Getting conversation for session ID: {session_id}")
        
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Check if session exists - FIXED: Use named parameters instead of positional
            cursor = db_conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM CHAT_SESSIONS
                WHERE JSON_DATA LIKE '%"session_id":"' || :session_id || '"%'
                   OR JSON_DATA LIKE '%"session_id": "' || :session_id || '"%'
            """, session_id=session_id)  # Use named parameter instead of list
            session_count = cursor.fetchone()[0]
            print(f"Found {session_count} sessions matching ID {session_id}")
            cursor.close()
            
            # Get the conversation with improved function
            import asyncio
            session = asyncio.run(get_session(db_conn, session_id))
            
            if not session:
                return jsonify({
                    'status': 'error',
                    'message': 'Conversation not found',
                    'debug': {'session_count': session_count},
                    'timestamp': datetime.now().isoformat()
                }), 404
            
            # For debugging, count messages
            message_count = len(session.get('messages', []))
            print(f"Retrieved session with {message_count} messages")
            
            return jsonify({
                'status': 'success',
                'session': session,
                'debug': {
                    'message_count': message_count,
                    'session_count': session_count
                },
                'timestamp': datetime.now().isoformat()
            })
        finally:
            db_conn.close()
    
    except Exception as e:
        print(f"Error in get_conversation endpoint: {e}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@chatbot_api.route('/conversation/<session_id>/title', methods=['PUT'])
@api_key_required
def update_conversation_title(session_id):  # Remove async keyword
    """
    Update the title of a chat conversation
    
    Path parameters:
    - session_id: The session ID
    
    Request body:
    - title: The new title
    - api_key: API key for authentication
    """
    try:
        data = request.json
        new_title = data.get('title')
        
        if not new_title:
            return jsonify({
                'status': 'error',
                'message': 'Title is required',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Update the title - use asyncio.run for async function
            import asyncio
            result = asyncio.run(update_session_title(db_conn, session_id, new_title))
            
            if not result:
                return jsonify({
                    'status': 'error',
                    'message': 'Failed to update title',
                    'timestamp': datetime.now().isoformat()
                }), 500
            
            return jsonify({
                'status': 'success',
                'message': 'Title updated successfully',
                'session_id': session_id,
                'title': new_title,
                'timestamp': datetime.now().isoformat()
            })
        finally:
            db_conn.close()
    
    except Exception as e:
        print(f"Error in update_conversation_title endpoint: {e}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500