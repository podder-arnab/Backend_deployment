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
def chat():  # Remove async keyword
    """
    Process a chat message and get a response
    
    Request body:
    - message: The user message
    - session_id: (Optional) The session ID to continue an existing conversation
    - api_key: API key for authentication
    - source_level_url: (Optional) URL to restrict the knowledge retrieval context
    """
    try:
        data = request.json
        message = data.get('message')
        session_id = data.get('session_id')
        api_key = data.get('api_key')
        source_level_url = data.get('source_level_url')  # New parameter
        
        if not message:
            return jsonify({
                'status': 'error',
                'message': 'Message is required',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Log the request with source_level_url for debugging
        print(f"Chat request: message='{message}', source_level_url='{source_level_url}', session_id='{session_id}'")
        
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
            # For async functions, we can use asyncio.run() to run them synchronously
            import asyncio
            response = asyncio.run(process_chat_request(db_conn, message, session_id, api_key, source_level_url))
            
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
@api_key_required
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
def get_conversations():  # Remove async keyword
    """
    Get all chat conversations
    
    Query parameters:
    - api_key: API key for authentication
    """
    try:
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Get all conversations - use asyncio.run for async function
            import asyncio
            conversations = asyncio.run(get_all_conversations(db_conn))
            
            return jsonify({
                'status': 'success',
                'conversations': conversations,
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
            'timestamp': datetime.now().isoformat()
        }), 500

@chatbot_api.route('/conversation/<session_id>', methods=['GET'])
@api_key_required
def get_conversation(session_id):  # Remove async keyword
    """
    Get a specific chat conversation
    
    Path parameters:
    - session_id: The session ID
    
    Query parameters:
    - api_key: API key for authentication
    """
    try:
        # Create a new database connection for this request
        db_conn = connect_to_jsondb()
        if not db_conn:
            return jsonify({
                'status': 'error',
                'message': 'Failed to connect to database',
                'timestamp': datetime.now().isoformat()
            }), 500
        
        try:
            # Get the conversation - use asyncio.run for async function
            import asyncio
            session = asyncio.run(get_session(db_conn, session_id))
            
            if not session:
                return jsonify({
                    'status': 'error',
                    'message': 'Conversation not found',
                    'timestamp': datetime.now().isoformat()
                }), 404
            
            return jsonify({
                'status': 'success',
                'session': session,
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