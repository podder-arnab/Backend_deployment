import os
import json
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime
import traceback

from dotenv import load_dotenv
import google.generativeai as genai
import oracledb
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts import PromptTemplate
from langchain_community.vectorstores import OracleVS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain.docstore.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Load environment variables
load_dotenv()
# Different usernames for different databases
ORACLE_USER_VECTDB = os.getenv("ORACLE_USER_VECTDB", "ANIRUDDHA1")
ORACLE_USER_JSONDB = os.getenv("ORACLE_USER_JSONDB", "ANIRUDDHA")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "OracleDatabase&2025")
ORACLE_DSN_VECTDB = os.getenv("ORACLE_DSN_VECTDB", "vectdb_high")
ORACLE_DSN_JSONDB = os.getenv("ORACLE_DSN_JSONDB", "jsondb_high")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
VALID_API_KEYS = os.getenv("VALID_API_KEYS", "").split(",")
VECTDB_TABLE_NAME = os.getenv("VECTDB_TABLE_NAME", "vector_files_with_10000_chunk_new")

# Constants for chunking
CHUNK_SIZE = 10000
CHUNK_OVERLAP = 300

# Data Models
class Message:
    def __init__(self, role: str, content: str, timestamp: datetime = None):
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.utcnow()
    
    def to_dict(self):
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat()
        }

class ChatSession:
    def __init__(self, session_id: str, title: str = "New Conversation"):
        self.session_id = session_id
        self.messages = []
        self.created_at = datetime.utcnow()
        self.last_updated = datetime.utcnow()
        self.title = title
    
    def to_dict(self):
        return {
            "session_id": self.session_id,
            "messages": [msg.to_dict() for msg in self.messages],
            "created_at": self.created_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "title": self.title
        }

class ChatResponse:
    def __init__(self, answer: str, sources: List[str], session_id: str, title: str):
        self.answer = answer
        self.sources = sources
        self.session_id = session_id
        self.title = title
    
    def to_dict(self):
        return {
            "answer": self.answer,
            "sources": self.sources,
            "session_id": self.session_id,
            "title": self.title
        }

# Database Connection Functions
def connect_to_vectdb():
    try:
        connection = oracledb.connect(
            user=ORACLE_USER_VECTDB,
            password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN_VECTDB,
            config_dir="Wallet_VECTDB",
            wallet_location="Wallet_VECTDB",
            wallet_password=ORACLE_PASSWORD
        )
        print("Connected to Vector Database!")
        return connection
    except Exception as e:
        print(f"Error connecting to Vector DB: {e}")
        traceback.print_exc()
        return None

def connect_to_jsondb():
    try:
        connection = oracledb.connect(
            user=ORACLE_USER_JSONDB,
            password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN_JSONDB,
            config_dir="Wallet_jsondb",
            wallet_location="Wallet_jsondb",
            wallet_password=ORACLE_PASSWORD
        )
        print("Connected to JSON Database!")
        return connection
    except Exception as e:
        print(f"Error connecting to JSON DB: {e}")
        traceback.print_exc()
        return None

def initialize_json_database(jsondb_connection):
    try:
        with jsondb_connection.cursor() as cursor:
            # Check if table exists in the current user's schema
            cursor.execute("""
                SELECT COUNT(*) 
                FROM USER_TABLES 
                WHERE TABLE_NAME = 'CHAT_SESSIONS'
            """)
            count = cursor.fetchone()[0]
            
            if count == 0:
                print(f"CHAT_SESSIONS table doesn't exist for user {ORACLE_USER_JSONDB}. Creating it now...")
                # Create the table in the current user's schema
                cursor.execute("""
                    CREATE TABLE CHAT_SESSIONS (
                        ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        JSON_DATA CLOB CHECK (JSON_DATA IS JSON),
                        CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Optionally create an index for better JSON query performance
                cursor.execute("""
                    CREATE SEARCH INDEX chat_sessions_json_idx ON CHAT_SESSIONS (JSON_DATA) 
                    FOR JSON
                """)
                
                jsondb_connection.commit()
                print(f"CHAT_SESSIONS table created successfully in {ORACLE_USER_JSONDB}'s schema!")
            else:
                print(f"CHAT_SESSIONS table already exists in {ORACLE_USER_JSONDB}'s schema.")
                
        return True
    except Exception as e:
        print(f"Error initializing JSON database: {e}")
        traceback.print_exc()
        return False

def process_scrapped_text_to_vector_store(jsondb_connection, user_id=None, source_level_url=None):
    """
    Process scrapped text data from the SCRAPPED_TEXT table to the vector store,
    vectorizing ALL documents associated with the given parent source_level_url
    
    Args:
        jsondb_connection: Oracle connection to the JSON database
        user_id: Optional filter by user_id
        source_level_url: Parent URL to filter all child documents by
    
    Returns:
        Dict with status and message
    """
    try:
        # Initialize vector database connection
        vectdb_connection = connect_to_vectdb()
        if not vectdb_connection:
            return {"status": "error", "message": "Failed to connect to vector database"}
        
        # Initialize embeddings model
        embeddings = GoogleGenerativeAIEmbeddings(
            google_api_key=GOOGLE_API_KEY,
            model="models/text-embedding-004"
        )
        
        # Initialize vector store
        vector_store = OracleVS(
            client=vectdb_connection,
            embedding_function=embeddings,
            table_name=VECTDB_TABLE_NAME,
            distance_strategy=DistanceStrategy.COSINE,
        )
        
        cursor = jsondb_connection.cursor()
        
        # Build query based on what we know about the schema from the screenshot
        query = """
            SELECT SCRAPPED_CONTENT, CONTENT_LINK, TOP_LEVEL_SOURCE, TITLE
            FROM SCRAPPED_TEXT
            WHERE 1=1
        """
        
        params = {}
        
        # If source_level_url is provided, filter by TOP_LEVEL_SOURCE
        if source_level_url:
            query += " AND TOP_LEVEL_SOURCE = :source_level_url"
            params["source_level_url"] = source_level_url
        
        if user_id:
            query += " AND USER_ID = :user_id"
            params["user_id"] = user_id
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        if not rows:
            return {
                "status": "info", 
                "message": f"No scrapped text data found for source_level_url: {source_level_url}"
            }
        
        # Create documents from scrapped text
        documents = []
        for row in rows:
            content_lob, link, top_level_source, title = row
            
            # Convert Oracle LOB object to string
            if isinstance(content_lob, oracledb.LOB):
                content = content_lob.read()
            else:
                content = str(content_lob)
            
            # Create metadata as a JSON object (matching your vector store schema)
            metadata = {
                "url": link,
                "source": source_level_url if source_level_url else top_level_source,
                "title": title
            }
            
            documents.append(Document(page_content=content, metadata=metadata))
        
        # Split documents into chunks
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            length_function=len,
            is_separator_regex=False,
        )
        
        chunks = text_splitter.split_documents(documents)
        
        # Process in batches
        BATCH_SIZE = 10
        total_batches = (len(chunks) + BATCH_SIZE - 1) // BATCH_SIZE
        
        successful_chunks = 0
        failed_chunks = 0
        
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            
            try:
                vector_store.add_documents(batch)
                print(f"Batch {batch_num}/{total_batches} processed successfully")
                successful_chunks += len(batch)
            except Exception as e:
                print(f"Error processing batch {batch_num}: {e}")
                traceback.print_exc()
                failed_chunks += len(batch)
            
            # Sleep to avoid rate limits
            time.sleep(2)
        
        return {
            "status": "success", 
            "message": f"Processed {successful_chunks} chunks from {len(documents)} documents for source_level_url: {source_level_url}",
            "document_count": len(documents),
            "successful_chunks": successful_chunks,
            "failed_chunks": failed_chunks,
            "source_level_url": source_level_url
        }
    
    except Exception as e:
        print(f"Error processing scrapped text: {e}")
        traceback.print_exc()
        return {"status": "error", "message": str(e), "source_level_url": source_level_url}
    finally:
        if vectdb_connection:
            vectdb_connection.close()

# Session Management Functions
async def create_new_session(db_conn, title="New Conversation"):
    cursor = None
    try:
        cursor = db_conn.cursor()
        session_id = str(int(time.time()))
        session = ChatSession(session_id=session_id, title=title)
        
        insert_query = """
        INSERT INTO CHAT_SESSIONS (JSON_DATA)
        VALUES (:1)
        """
        cursor.execute(insert_query, (json.dumps(session.to_dict()),))
        db_conn.commit()
        print(f"Session {session_id} created successfully.")
        return session_id, session.title
    except Exception as e:
        print("Error creating session:", e)
        traceback.print_exc()
        if db_conn:
            db_conn.rollback()
        return None, None
    finally:
        if cursor:
            cursor.close()

async def get_session(db_conn, session_id):
    """
    Get a specific chat session by its ID with improved error handling
    
    Args:
        db_conn: Oracle database connection
        session_id: The ID of the session to retrieve
        
    Returns:
        dict: The session data or None if not found
    """
    cursor = None
    try:
        cursor = db_conn.cursor()
        print(f"Retrieving session with ID: {session_id}")
        
        # Try the standard query first
        try:
            select_query = """
            SELECT JSON_DATA
            FROM CHAT_SESSIONS
            WHERE JSON_VALUE(JSON_DATA, '$.session_id') = :session_id
            """
            cursor.execute(select_query, session_id=session_id)  # Use named parameter
            result = cursor.fetchone()
            
            if result:
                print(f"Found session {session_id} using JSON_VALUE")
                data = result[0]
                
                # Handle different data types
                if isinstance(data, oracledb.LOB):
                    data = data.read()
                    
                if isinstance(data, str):
                    return json.loads(data)
                elif isinstance(data, bytes):
                    return json.loads(data.decode('utf-8'))
                else:
                    return data
            else:
                print(f"Session {session_id} not found with JSON_VALUE query")
                
                # Try a more direct approach with LIKE query as fallback
                print("Trying fallback query with LIKE...")
                fallback_query = """
                SELECT JSON_DATA
                FROM CHAT_SESSIONS
                WHERE JSON_DATA LIKE '%"session_id":"' || :session_id || '"%'
                   OR JSON_DATA LIKE '%"session_id": "' || :session_id || '"%'
                """
                cursor.execute(fallback_query, session_id=session_id)  # Use named parameter
                fallback_result = cursor.fetchone()
                
                if fallback_result:
                    print(f"Found session {session_id} using LIKE query")
                    data = fallback_result[0]
                    
                    # Handle different data types
                    if isinstance(data, oracledb.LOB):
                        data = data.read()
                        
                    if isinstance(data, str):
                        return json.loads(data)
                    elif isinstance(data, bytes):
                        return json.loads(data.decode('utf-8'))
                    else:
                        return data
                    
                # Last resort: scan all rows
                print("Trying last resort - scanning all sessions...")
                cursor.execute("SELECT JSON_DATA FROM CHAT_SESSIONS")
                all_rows = cursor.fetchall()
                print(f"Scanning {len(all_rows)} rows to find session {session_id}")
                
                for row in all_rows:
                    try:
                        data = row[0]
                        if isinstance(data, oracledb.LOB):
                            data = data.read()
                            
                        if isinstance(data, str):
                            parsed = json.loads(data)
                        elif isinstance(data, bytes):
                            parsed = json.loads(data.decode('utf-8'))
                        else:
                            parsed = data
                            
                        if parsed.get('session_id') == session_id:
                            print(f"Found session {session_id} by scanning all rows")
                            return parsed
                    except Exception as row_error:
                        print(f"Error processing row during scan: {str(row_error)}")
                        continue
                
                print(f"Session {session_id} not found after trying all methods")
                return None
                
        except Exception as query_error:
            print(f"Error in main query: {str(query_error)}")
            traceback.print_exc()
            
            # Try a simplified query if JSON_VALUE fails
            try:
                cursor.execute("SELECT JSON_DATA FROM CHAT_SESSIONS")
                all_rows = cursor.fetchall()
                
                for row in all_rows:
                    try:
                        data = row[0]
                        if isinstance(data, oracledb.LOB):
                            data = data.read()
                            
                        if isinstance(data, str):
                            parsed = json.loads(data)
                        elif isinstance(data, bytes):
                            parsed = json.loads(data.decode('utf-8'))
                        else:
                            parsed = data
                            
                        if parsed.get('session_id') == session_id:
                            return parsed
                    except:
                        continue
                        
                return None
            except Exception as fallback_error:
                print(f"Fallback query also failed: {str(fallback_error)}")
                return None
    except Exception as e:
        print(f"Unexpected error in get_session: {str(e)}")
        traceback.print_exc()
        return None
    finally:
        if cursor:
            cursor.close()

async def update_session(db_conn, session_id, user_message, assistant_response):
    """
    Update a chat session with new messages with improved error handling and verification
    
    Args:
        db_conn: Oracle database connection
        session_id: The ID of the session to update
        user_message: The user's message to add
        assistant_response: The assistant's response to add
        
    Returns:
        bool: True if successful, False otherwise
    """
    cursor = None
    try:
        print(f"Updating session {session_id} with new messages")
        
        # Get the current session
        session = await get_session(db_conn, session_id)
        if not session:
            print(f"Session {session_id} not found, creating a new session")
            # Create a new session since the specified one doesn't exist
            new_session_id = str(int(time.time()))
            new_session = ChatSession(session_id=new_session_id, title="New Conversation")
            session = new_session.to_dict()
            session_id = new_session_id
            
            # Insert the new session
            cursor = db_conn.cursor()
            insert_query = """
            INSERT INTO CHAT_SESSIONS (JSON_DATA)
            VALUES (:1)
            """
            cursor.execute(insert_query, (json.dumps(session),))
            db_conn.commit()
            print(f"Created new session {session_id} as fallback")
        
        # Create message objects
        message_user = {
            "role": "user",
            "content": user_message,
            "timestamp": datetime.utcnow().isoformat()
        }

        message_assistant = {
            "role": "assistant",
            "content": assistant_response,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Append new messages to existing messages
        current_messages = session.get("messages", [])
        updated_messages = current_messages + [message_user, message_assistant]
        
        # Update timestamp
        last_updated = datetime.utcnow().isoformat()
        
        # Try first with JSON_MERGEPATCH
        cursor = db_conn.cursor()
        try:
            update_query = """
            UPDATE CHAT_SESSIONS
            SET JSON_DATA = JSON_MERGEPATCH(JSON_DATA, :1)
            WHERE JSON_VALUE(JSON_DATA, '$.session_id') = :2
            """
            
            patch_data = json.dumps({
                "messages": updated_messages,
                "last_updated": last_updated
            })
            
            cursor.execute(update_query, [patch_data, session_id])
            rows_updated = cursor.rowcount
            
            if rows_updated > 0:
                db_conn.commit()
                print(f"Updated session {session_id} using JSON_MERGEPATCH, {rows_updated} rows affected")
                
                # Verify the update
                updated_session = await get_session(db_conn, session_id)
                if updated_session and len(updated_session.get('messages', [])) == len(updated_messages):
                    print(f"Verified update success: session now has {len(updated_messages)} messages")
                    return True
                else:
                    print("WARNING: Session update verification failed")
            else:
                print(f"No rows updated with JSON_MERGEPATCH, trying alternative approach")
                
                # Try alternative approach by replacing the entire document
                try:
                    # Update the session object and replace it entirely
                    session['messages'] = updated_messages
                    session['last_updated'] = last_updated
                    
                    # Find the row by ID using a LIKE clause as fallback
                    select_id_query = """
                    SELECT ID FROM CHAT_SESSIONS
                    WHERE JSON_DATA LIKE '%"session_id":"' || :1 || '"%'
                       OR JSON_DATA LIKE '%"session_id": "' || :1 || '"%'
                    """
                    cursor.execute(select_id_query, [session_id])
                    id_result = cursor.fetchone()
                    
                    if id_result:
                        row_id = id_result[0]
                        
                        # Update by ID (more reliable)
                        update_by_id_query = """
                        UPDATE CHAT_SESSIONS
                        SET JSON_DATA = :1
                        WHERE ID = :2
                        """
                        cursor.execute(update_by_id_query, [json.dumps(session), row_id])
                        rows_updated = cursor.rowcount
                        
                        if rows_updated > 0:
                            db_conn.commit()
                            print(f"Updated session {session_id} by replacing document, {rows_updated} rows affected")
                            return True
                        else:
                            print(f"Failed to update session {session_id} by ID")
                    else:
                        print(f"Couldn't find row ID for session {session_id}, attempting insert as new session")
                        
                        # Last resort: Insert as new session with same session_id
                        insert_query = """
                        INSERT INTO CHAT_SESSIONS (JSON_DATA)
                        VALUES (:1)
                        """
                        cursor.execute(insert_query, [json.dumps(session)])
                        db_conn.commit()
                        print(f"Inserted session {session_id} as new document")
                        return True
                except Exception as alt_error:
                    print(f"Alternative update approach failed: {str(alt_error)}")
                    traceback.print_exc()
                    
                    if db_conn:
                        db_conn.rollback()
                    return False
        except Exception as update_error:
            print(f"Error updating session: {str(update_error)}")
            traceback.print_exc()
            
            if db_conn:
                db_conn.rollback()
            return False
    except Exception as e:
        print(f"Unexpected error in update_session: {str(e)}")
        traceback.print_exc()
        
        if db_conn:
            db_conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()

async def update_session_title(db_conn, session_id, new_title):
    cursor = None
    try:
        cursor = db_conn.cursor()
        update_query = """
        UPDATE CHAT_SESSIONS
        SET JSON_DATA = JSON_MERGEPATCH(JSON_DATA, :1)
        WHERE JSON_VALUE(JSON_DATA, '$.session_id') = :2
        """
        
        patch_data = json.dumps({
            "title": new_title,
            "last_updated": datetime.utcnow().isoformat()
        })
        
        cursor.execute(update_query, [patch_data, session_id])
        db_conn.commit()
        
        return True
    except Exception as e:
        print(f"Error updating session title: {e}")
        traceback.print_exc()
        if db_conn:
            db_conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()

async def get_all_conversations(db_conn):
    """
    Get all chat conversations with improved error handling and debugging
    
    Args:
        db_conn: Oracle database connection
        
    Returns:
        list: List of conversation summary objects
    """
    cursor = None
    try:
        cursor = db_conn.cursor()
        
        # First check if the table exists and has data
        cursor.execute("""
        SELECT COUNT(*) 
        FROM USER_TABLES 
        WHERE TABLE_NAME = 'CHAT_SESSIONS'
        """)
        table_exists = cursor.fetchone()[0] > 0
        
        if not table_exists:
            print("CHAT_SESSIONS table doesn't exist in the current schema")
            return []
            
        # Check if there are any rows in the table
        cursor.execute("SELECT COUNT(*) FROM CHAT_SESSIONS")
        row_count = cursor.fetchone()[0]
        print(f"Found {row_count} rows in CHAT_SESSIONS table")
        
        if row_count == 0:
            print("CHAT_SESSIONS table exists but is empty")
            return []
            
        # Try to retrieve all conversations with a more robust query
        # that doesn't rely on JSON_VALUE for sorting
        try:
            cursor.execute("""
            SELECT JSON_DATA 
            FROM CHAT_SESSIONS
            ORDER BY CREATED_AT DESC
            """)
            
            print(f"Query executed, retrieving conversations...")
            rows = cursor.fetchall()
            print(f"Fetched {len(rows)} rows from database")
            
            conversations = []
            error_count = 0
            
            for idx, row in enumerate(rows):
                try:
                    session_data = row[0]
                    
                    # Handle different types of data returned by Oracle
                    if isinstance(session_data, oracledb.LOB):
                        session_data = session_data.read()
                        
                    if isinstance(session_data, str):
                        session_data = json.loads(session_data)
                    elif isinstance(session_data, bytes):
                        session_data = json.loads(session_data.decode('utf-8'))
                    
                    # Extract session ID and title with fallbacks
                    session_id = session_data.get("session_id", f"unknown-{idx}")
                    title = session_data.get("title", "Untitled Conversation")
                    
                    # Extract messages safely
                    messages = session_data.get("messages", [])
                    last_message = ""
                    if messages and len(messages) > 0:
                        # Get the last message, preferring assistant's message
                        for msg in reversed(messages):
                            if isinstance(msg, dict) and 'content' in msg:
                                last_message = msg.get('content', '')[:50] + "..."  # Truncate for summary
                                break
                    
                    # Get the last_updated time
                    last_updated = session_data.get("last_updated", 
                                    session_data.get("created_at", "Unknown"))
                    
                    conversations.append({
                        "session_id": session_id,
                        "title": title,
                        "last_message": last_message,
                        "last_updated": last_updated,
                        "message_count": len(messages)
                    })
                    
                except Exception as row_error:
                    error_count += 1
                    print(f"Error processing row {idx}: {str(row_error)}")
                    # Continue to next row instead of failing completely
            
            if error_count > 0:
                print(f"Encountered errors processing {error_count} out of {len(rows)} rows")
            
            return conversations
            
        except Exception as query_error:
            print(f"Error executing main query: {str(query_error)}")
            traceback.print_exc()
            
            # Fallback to basic query without JSON functions or sorting
            print("Trying fallback query...")
            cursor.execute("SELECT JSON_DATA FROM CHAT_SESSIONS")
            rows = cursor.fetchall()
            
            print(f"Fallback query retrieved {len(rows)} rows")
            conversations = []
            
            for idx, row in enumerate(rows):
                try:
                    # Handle data carefully with minimal assumptions
                    data = row[0]
                    if isinstance(data, oracledb.LOB):
                        data = data.read()
                    
                    if isinstance(data, str):
                        try:
                            parsed = json.loads(data)
                            conversations.append({
                                "session_id": parsed.get("session_id", f"unknown-{idx}"),
                                "title": parsed.get("title", "Untitled"),
                                "last_message": "Message retrieval not available in fallback mode",
                                "last_updated": "Unknown"
                            })
                        except json.JSONDecodeError:
                            print(f"Row {idx} contains invalid JSON")
                except Exception as row_error:
                    print(f"Error processing fallback row {idx}: {str(row_error)}")
            
            return conversations
    except Exception as e:
        print(f"Unexpected error in get_all_conversations: {str(e)}")
        traceback.print_exc()
        return []
    finally:
        if cursor:
            cursor.close()

# Chatbot Functions
def verify_api_key(api_key):
    """Verify if the provided API key is valid"""
    if not VALID_API_KEYS or api_key in VALID_API_KEYS:
        return True
    return False

async def process_chat_request(db_conn, message, session_id=None, api_key=None, source_level_url=None):
    """
    Process a chat request, filtering by source_level_url in the METADATA JSON field
    with simpler query approach
    
    Args:
        db_conn: Database connection
        message: User message
        session_id: Optional session ID
        api_key: API key for authorization
        source_level_url: URL to restrict the knowledge retrieval context
        
    Returns:
        ChatResponse object
    """
    try:
        # Verify API key if required
        if VALID_API_KEYS and not verify_api_key(api_key):
            raise Exception("Invalid API Key")
        
        # Handle session creation/retrieval
        session = None
        cursor = None
        
        try:
            cursor = db_conn.cursor()
            
            if session_id:
                # Try to retrieve the existing session
                select_query = """
                SELECT JSON_DATA
                FROM CHAT_SESSIONS
                WHERE JSON_VALUE(JSON_DATA, '$.session_id') = :1
                """
                cursor.execute(select_query, [session_id])
                result = cursor.fetchone()
                
                if result and result[0]:
                    # Parse the JSON string into a Python object
                    session_data = result[0]
                    if isinstance(session_data, str):
                        session = json.loads(session_data)
                    else:
                        session = session_data
                    title = session.get("title", "New Conversation")
                else:
                    # Session ID was provided but doesn't exist. Create a new one.
                    title = f"Conversation about {source_level_url}" if source_level_url else "New Conversation"
                    new_session_id = str(int(time.time()))
                    new_session = ChatSession(session_id=new_session_id, title=title)
                    
                    # Insert the session into the database
                    insert_query = """
                    INSERT INTO CHAT_SESSIONS (JSON_DATA)
                    VALUES (:1)
                    """
                    session_json = json.dumps(new_session.to_dict())
                    cursor.execute(insert_query, (session_json,))
                    db_conn.commit()
                    
                    print(f"New session {new_session_id} created successfully.")
                    session_id = new_session_id
                    session = new_session.to_dict()
            else:
                # No session ID provided. Create a new session.
                title = f"Conversation about {source_level_url}" if source_level_url else "New Conversation"
                new_session_id = str(int(time.time()))
                new_session = ChatSession(session_id=new_session_id, title=title)
                
                # Insert the session into the database
                insert_query = """
                INSERT INTO CHAT_SESSIONS (JSON_DATA)
                VALUES (:1)
                """
                session_json = json.dumps(new_session.to_dict())
                cursor.execute(insert_query, (session_json,))
                db_conn.commit()
                
                print(f"New session {new_session_id} created successfully.")
                session_id = new_session_id
                session = new_session.to_dict()
        finally:
            if cursor:
                cursor.close()
        
        # Check if session creation/retrieval was successful
        if not session:
            raise Exception(f"Unable to create or retrieve session")
            
        title = session.get("title", "New Conversation")
        
        # Initialize vector database connection
        vectdb_connection = connect_to_vectdb()
        if not vectdb_connection:
            raise Exception("Failed to connect to vector database")
        
        try:
            # Initialize embeddings and vector store
            embeddings = GoogleGenerativeAIEmbeddings(
                google_api_key=GOOGLE_API_KEY,
                model="models/text-embedding-004"
            )
            
            vector_store = OracleVS(
                client=vectdb_connection,
                embedding_function=embeddings,
                table_name=VECTDB_TABLE_NAME,
                distance_strategy=DistanceStrategy.COSINE,
            )
            
            # Get query embedding and search Oracle Vector Store based on source_level_url if provided
            if source_level_url:
                print(f"Searching for content related to parent URL: {source_level_url}")
                
                # Approach 1: Use the Langchain search but filter the results afterward
                docs = []
                
                try:
                    # Get general documents
                    all_docs = vector_store.similarity_search(message, k=20)  # Get more to filter from
                    
                    # Filter them by URL
                    for doc in all_docs:
                        # Handle LOB objects
                        if isinstance(doc.page_content, oracledb.LOB):
                            doc.page_content = doc.page_content.read()
                        
                        # Check the metadata for matching URL
                        metadata = doc.metadata
                        if isinstance(metadata, dict):
                            metadata_str = str(metadata)
                        elif isinstance(metadata, str):
                            metadata_str = metadata
                        elif isinstance(metadata, oracledb.LOB):
                            metadata_str = metadata.read()
                        else:
                            metadata_str = str(metadata)
                        
                        # If the source_level_url appears in the metadata, include this document
                        if source_level_url in metadata_str:
                            docs.append(doc)
                        
                    print(f"Found {len(docs)} documents matching {source_level_url} out of {len(all_docs)} total")
                    
                    # If not enough matches, include some general docs
                    if len(docs) < 3:
                        remaining_slots = 5 - len(docs)
                        general_docs = [d for d in all_docs if d not in docs][:remaining_slots]
                        docs.extend(general_docs)
                        print(f"Added {len(general_docs)} general documents to supplement results")
                
                except Exception as e:
                    print(f"Error in document filtering: {e}")
                    traceback.print_exc()
                    # Fall back to standard search
                    docs = vector_store.similarity_search(message, k=5)
                    for doc in docs:
                        if isinstance(doc.page_content, oracledb.LOB):
                            doc.page_content = doc.page_content.read()
            else:
                # Standard search without URL filtering
                docs = vector_store.similarity_search(message, k=5)
                
                # Handle LOB objects
                for doc in docs:
                    if isinstance(doc.page_content, oracledb.LOB):
                        doc.page_content = doc.page_content.read()
            
            # Extract contents and sources
            contexts = [doc.page_content for doc in docs]
            sources = []
            for doc in docs:
                if isinstance(doc.metadata, dict) and "url" in doc.metadata:
                    url = doc.metadata["url"]
                    sources.append(url)
                elif isinstance(doc.metadata, str):
                    try:
                        metadata_dict = json.loads(doc.metadata)
                        url = metadata_dict.get("url", "N/A")
                        sources.append(url)
                    except:
                        sources.append("N/A")
                else:
                    sources.append("N/A")
            
            if not contexts:
                response_message = "No relevant information found in the knowledge base."
                if source_level_url:
                    response_message += f" for the domain: {source_level_url}"
                
                response = ChatResponse(
                    answer=response_message,
                    sources=[],
                    session_id=session_id,
                    title=title
                )
                await update_session(db_conn, session_id, message, response.answer)
                return response
            
            # Prepare documents and chat history
            doc_objects = [Document(page_content=text) for text in contexts]
            
            # Extract chat history
            chat_history = "\n".join([
                f"{msg['role']}: {msg['content']}"
                for msg in session.get("messages", [])[-4:] if session.get("messages", [])
            ])
            
            # Define prompt template that acknowledges the parent URL
            source_context = ""
            if source_level_url:
                source_context = f"\nFocus on information specifically from: {source_level_url} and its related pages."
            
            prompt_template = f"""
            Previous conversation:
            {{chat_history}}

            Use the following context to answer the question. Consider the previous conversation for context.{source_context}
            If the answer is not in the provided context, say: "Answer is not available in the context."
            Do not provide incorrect information.

            Context: {{context}}
            Question: {{question}}
            Answer:
            """
            
            # Initialize model and chain
            model = ChatGoogleGenerativeAI(model="gemini-2.0-flash", temperature=0.7)
            prompt = PromptTemplate(
                template=prompt_template,
                input_variables=["context", "question", "chat_history"]
            )
            
            # Use load_qa_chain
            chain = load_qa_chain(model, chain_type="stuff", prompt=prompt)
            
            # Get response using the chain
            chain_response = chain(
                {
                    "input_documents": doc_objects,
                    "question": message,
                    "chat_history": chat_history
                },
                return_only_outputs=True
            )
            
            answer_text = chain_response["output_text"]
            
            # If source_level_url is provided and answer was found, add attribution
            if source_level_url and "Answer is not available in the context" not in answer_text:
                source_note = f"\n\nThis information is sourced from: {source_level_url} and its related pages."
                answer_text += source_note
            
            # Update session with new messages
            await update_session(db_conn, session_id, message, answer_text)
            
            # Only include sources if the answer was found in the context
            final_sources = sources if "Answer is not available in the context" not in answer_text else []
            
            # Return response
            return ChatResponse(
                answer=answer_text,
                sources=final_sources,
                session_id=session_id,
                title=title
            )
            
        finally:
            if vectdb_connection:
                vectdb_connection.close()
    
    except Exception as e:
        print(f"Error processing chat request: {e}")
        traceback.print_exc()
        return ChatResponse(
            answer=f"An error occurred while processing your request: {str(e)}",
            sources=[],
            session_id=session_id if session_id else "error",
            title="Error"
        )