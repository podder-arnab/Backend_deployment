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
            config_dir="Wallet_VECTDB_",
            wallet_location="Wallet_VECTDB_",
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

def process_scrapped_text_to_vector_store(jsondb_connection, user_id=None, url=None):
    """
    Process scrapped text data from the SCRAPPED_TEXT table to the vector store
    
    Args:
        jsondb_connection: Oracle connection to the JSON database
        user_id: Optional filter by user_id
        url: Optional filter by url (TOP_LEVEL_SOURCE)
    
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
        
        # Build query with filters
        query = """
            SELECT SCRAPPED_CONTENT, CONTENT_LINK, TOP_LEVEL_SOURCE, TITLE
            FROM SCRAPPED_TEXT
            WHERE 1=1
        """
        
        params = {}
        if user_id:
            query += " AND USER_ID = :user_id"
            params["user_id"] = user_id
        
        if url:
            query += " AND TOP_LEVEL_SOURCE = :url"
            params["url"] = url
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        if not rows:
            return {"status": "info", "message": "No scrapped text data found"}
        
        # Create documents from scrapped text
        documents = []
        for row in rows:
            content, link, source, title = row
            metadata = {
                "url": link,
                "source": source,
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
        
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            
            try:
                vector_store.add_documents(batch)
                print(f"Batch {batch_num}/{total_batches} processed successfully")
            except Exception as e:
                print(f"Error processing batch {batch_num}: {e}")
                traceback.print_exc()
            
            # Sleep to avoid rate limits
            time.sleep(2)
        
        return {
            "status": "success", 
            "message": f"Processed {len(chunks)} chunks from {len(documents)} documents",
            "document_count": len(documents),
            "chunk_count": len(chunks)
        }
    
    except Exception as e:
        print(f"Error processing scrapped text: {e}")
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
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
    cursor = None
    try:
        cursor = db_conn.cursor()
        select_query = """
        SELECT JSON_DATA
        FROM CHAT_SESSIONS
        WHERE JSON_VALUE(JSON_DATA, '$.session_id') = :1
        """
        cursor.execute(select_query, [session_id])
        result = cursor.fetchone()
        
        if result and result[0]:
            return json.loads(result[0]) if isinstance(result[0], str) else result[0]
        return None
    except Exception as e:
        print("Error fetching session:", e)
        traceback.print_exc()
        return None
    finally:
        if cursor:
            cursor.close()

async def update_session(db_conn, session_id, user_message, assistant_response):
    cursor = None
    try:
        # Get the current session
        session = await get_session(db_conn, session_id)
        if not session:
            raise Exception(f"Session not found: {session_id}")
        
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
        
        # Update the session
        cursor = db_conn.cursor()
        update_query = """
        UPDATE CHAT_SESSIONS
        SET JSON_DATA = JSON_MERGEPATCH(JSON_DATA, :1)
        WHERE JSON_VALUE(JSON_DATA, '$.session_id') = :2
        """
        
        patch_data = json.dumps({
            "messages": updated_messages,
            "last_updated": datetime.utcnow().isoformat()
        })
        
        cursor.execute(update_query, [patch_data, session_id])
        db_conn.commit()
        
        return True
    except Exception as e:
        print(f"Error updating session: {e}")
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
    cursor = None
    try:
        cursor = db_conn.cursor()
        cursor.execute("""
        SELECT JSON_DATA 
        FROM CHAT_SESSIONS
        ORDER BY JSON_VALUE(JSON_DATA, '$.last_updated') DESC
        """)
        
        rows = cursor.fetchall()
        conversations = []
        
        for row in rows:
            session_data = row[0]
            if isinstance(session_data, str):
                session_data = json.loads(session_data)
                
            messages = session_data.get("messages", [])
            last_message = messages[-1]["content"] if messages else ""
            
            conversations.append({
                "session_id": session_data.get("session_id", "Unknown"),
                "title": session_data.get("title", "Untitled"),
                "last_message": last_message,
                "last_updated": session_data.get("last_updated", "Unknown")
            })
        
        return conversations
    except Exception as e:
        print(f"Error getting conversations: {e}")
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

async def process_chat_request(db_conn, message, session_id=None, api_key=None):
    """
    Process a chat request
    
    Args:
        db_conn: Database connection
        message: User message
        session_id: Optional session ID
        api_key: API key for authorization
        
    Returns:
        ChatResponse object
    """
    try:
        # Verify API key if required
        if VALID_API_KEYS and not verify_api_key(api_key):
            raise Exception("Invalid API Key")
        
        # Handle session creation/retrieval
        if session_id:
            session = await get_session(db_conn, session_id)
            if not session:
                # Session ID was provided but doesn't exist. Create a new one.
                session_id, title = await create_new_session(db_conn)
                session = await get_session(db_conn, session_id)
            else:
                title = session.get("title", "New Conversation")
        else:
            # No session ID provided. Create a new session.
            session_id, title = await create_new_session(db_conn)
            session = await get_session(db_conn, session_id)
        
        # Check if session retrieval failed.
        if not session:
            raise Exception(f"Session not found after creation: {session_id}")
        
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
            
            # Get query embedding and search Oracle Vector Store
            docs = vector_store.similarity_search(
                message,
                k=5  # Number of documents to return
            )
            
            contexts = [doc.page_content for doc in docs]
            sources = [doc.metadata.get("url", "N/A") for doc in docs]
            
            if not contexts:
                response = ChatResponse(
                    answer="No relevant information found in the knowledge base.",
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
            
            # Define prompt template
            prompt_template = """
            Previous conversation:
            {chat_history}

            Use the following context to answer the question. Consider the previous conversation for context.
            If the answer is not in the provided context, say: "Answer is not available in the context."
            Do not provide incorrect information.

            Context: {context}
            Question: {question}
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