from flask import Blueprint, request, jsonify
from functools import wraps
import jwt
from datetime import datetime,timedelta
import traceback
import oracledb  # Make sure this is installed
from dotenv import load_dotenv
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import time
import json
import validators
from threading import Lock, Thread, Event
from contextlib import contextmanager
from threading import Thread, Event


# Load environment variables from .env file
load_dotenv()

file_api = Blueprint('file_api', __name__)

# Global variables to track processing state
processing_events = {}
crawling_events = {}
active_user_jobs = {}  # Track which users have active jobs

# Oracle Database connection configuration
ORACLE_USER = os.environ.get('ORACLE_USER', 'VECTOR')
ORACLE_PASSWORD = os.environ.get('ORACLE_PASSWORD', 'OracleDatabase&2025')
ORACLE_DSN = os.environ.get('ORACLE_DSN', 'jsondb_high')
SECRET_KEY = os.environ.get('SECRET_KEY')

# Table names for Oracle
CONTENT_LINKS_TABLE = 'CONTENT_LINKS'
LINKS_TO_SCRAP_TABLE = 'LINKS_TO_SCRAP'
SCRAPPED_TEXT_TABLE = 'SCRAPPED_TEXT'
PROCESSING_QUEUE_TABLE = 'PROCESSING_QUEUE'
SOURCE_URLS_TABLE = 'SOURCE_URLS'
PROGRESS_HISTORY_TABLE = 'PROGRESS_HISTORY'



# Global variables to track processing state
processing_events = {}
crawling_events = {}
active_user_jobs = {}  # Track which users have active jobs

# Connection pool configuration
MAX_RETRIES = 3
RETRY_DELAY = 1
connection_pool = None

def initialize_connection_pool():
    """Initialize Oracle connection pool only if it doesn't exist"""
    global connection_pool
    if connection_pool is not None:
        return True
    
    try:
        connection_pool = oracledb.create_pool(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN,
            min=5,           # Increase minimum connections
            max=30,          # Increase maximum connections
            increment=5,     # Increase increment for better scalability
            wait_timeout=1000,
            max_lifetime_session=28800,
            config_dir="Wallet_jsondb",
            wallet_location="Wallet_jsondb",
            wallet_password=ORACLE_PASSWORD
        )
        print(f"Connection pool created successfully with {connection_pool.min} to {connection_pool.max} connections")
        return True
    except Exception as e:
        print(f"Error creating connection pool: {e}")
        traceback.print_exc()
        return False

@contextmanager
def get_db_connection():
    """Context manager for Oracle DB connections"""
    conn = None
    try:
        global connection_pool
        if connection_pool is None:
            initialize_connection_pool()
            
        if connection_pool:
            conn = connection_pool.acquire()
        else:
            conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
                config_dir="Wallet_jsondb",
                wallet_location="Wallet_jsondb",
                wallet_password=ORACLE_PASSWORD
            )
        yield conn
    except oracledb.DatabaseError as e:
        print(f"Database connection error: {e}")
        traceback.print_exc()
        raise
    finally:
        if conn:
            conn.close()


def initialize_tables():
    """Initialize all necessary tables if they don't exist with optimized indexes"""
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Map of table names to their creation SQL (unchanged)
                tables = {
                    CONTENT_LINKS_TABLE: """
                        CREATE TABLE {0} (
                            ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            SOURCE_URL VARCHAR2(2000) NOT NULL,
                            UNIQUE_LINKS CLOB CHECK (UNIQUE_LINKS IS JSON),
                            CRAWLED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            DEPTH NUMBER DEFAULT 0,
                            TOP_LEVEL_SOURCE VARCHAR2(2000),
                            USER_ID VARCHAR2(50)
                        )
                    """,
                    
                    LINKS_TO_SCRAP_TABLE: """
                        CREATE TABLE {0} (
                            ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            LINK VARCHAR2(2000) NOT NULL,
                            ADDED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            IS_CRAWLED NUMBER(1) DEFAULT 0,
                            IS_PROCESSED VARCHAR2(20) DEFAULT 'false',
                            SOURCE_URL VARCHAR2(2000),
                            TOP_LEVEL_SOURCE VARCHAR2(2000),
                            DEPTH NUMBER DEFAULT 0,
                            PROCESSED_AT TIMESTAMP,
                            HAS_TEXT_IN_URL NUMBER(1) DEFAULT 0,
                            USER_ID VARCHAR2(50),
                            CRAWLING_STARTED TIMESTAMP,
                            CRAWLED_AT TIMESTAMP,
                            LINKS_FOUND NUMBER,
                            LINKS_ADDED NUMBER,
                            ERROR CLOB,
                            TRACEBACK CLOB,
                            SKIPPED NUMBER(1) DEFAULT 0,
                            SKIP_REASON VARCHAR2(100),
                            CONSTRAINT UQ_LINK_USER UNIQUE (LINK, USER_ID)
                        )
                    """,
                    
                    SCRAPPED_TEXT_TABLE: """
                        CREATE TABLE {0} (
                            ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            SCRAPPED_CONTENT CLOB,
                            CONTENT_LINK VARCHAR2(2000) NOT NULL,
                            SCRAPE_DATE TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            LINK_ID NUMBER,
                            SOURCE_URL VARCHAR2(2000),
                            TOP_LEVEL_SOURCE VARCHAR2(2000),
                            DEPTH NUMBER DEFAULT 0,
                            TITLE VARCHAR2(1000),
                            USER_ID VARCHAR2(50),
                            WORD_COUNT NUMBER DEFAULT 0,
                            CONSTRAINT UQ_CONTENT_LINK_USER UNIQUE (CONTENT_LINK, USER_ID)
                        )
                    """,
                    
                    PROCESSING_QUEUE_TABLE: """
                        CREATE TABLE {0} (
                            ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            USER_ID VARCHAR2(50) NOT NULL,
                            SOURCE_URL VARCHAR2(2000) NOT NULL,
                            ADDED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PROCESSED NUMBER(1) DEFAULT 0,
                            PROCESSING_STARTED TIMESTAMP,
                            PROCESSING_COMPLETED TIMESTAMP,
                            PAGE_LIMIT NUMBER DEFAULT 10,
                            CONSTRAINT UQ_USER_SOURCE UNIQUE (USER_ID, SOURCE_URL)
                        )
                    """,
                    
                    SOURCE_URLS_TABLE: """
                        CREATE TABLE {0} (
                            ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            SOURCE_URL VARCHAR2(2000) NOT NULL,
                            USER_ID VARCHAR2(50),
                            TIMESTAMP TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PAGE_LIMIT NUMBER DEFAULT 10
                        )
                    """,
                    PROGRESS_HISTORY_TABLE: """
                        CREATE TABLE {0} (
                            ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                            SOURCE_URL VARCHAR2(2000) NOT NULL,
                            TIMESTAMP TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            CRAWL_PROGRESS NUMBER(5,2),
                            SCRAPE_PROGRESS NUMBER(5,2),
                            CRAWLED_COUNT NUMBER,
                            SCRAPED_COUNT NUMBER,
                            TOTAL_LINKS NUMBER,
                            OPERATION_STATUS VARCHAR2(50)
                        )
                      """
                }
                
                # Check and create each table if it doesn't exist
                for table_name, create_sql in tables.items():
                    cursor.execute(f"""
                        SELECT COUNT(*) 
                        FROM USER_TABLES
                        WHERE TABLE_NAME = '{table_name}'
                    """)
                    
                    if cursor.fetchone()[0] == 0:
                        cursor.execute(create_sql.format(table_name))
                        print(f"Created table {table_name}")
                        
                        # Create necessary indexes with improved structure
                        if table_name == LINKS_TO_SCRAP_TABLE:
                            # Create optimized index on common search fields
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_TOP_LEVEL 
                                ON {table_name} (TOP_LEVEL_SOURCE, USER_ID, IS_CRAWLED)
                            """)
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_PROCESSING 
                                ON {table_name} (TOP_LEVEL_SOURCE, USER_ID, IS_PROCESSED)
                            """)
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_LINK_USER
                                ON {table_name} (LINK, USER_ID)
                            """)
                            # New optimized indexes
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_CRAWL_QUEUE 
                                ON {table_name} (IS_CRAWLED, TOP_LEVEL_SOURCE, USER_ID, DEPTH, ADDED_AT)
                            """)
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_PROCESS_QUEUE 
                                ON {table_name} (IS_PROCESSED, TOP_LEVEL_SOURCE, USER_ID)
                            """)
                        elif table_name == PROCESSING_QUEUE_TABLE:
                            # Create optimized index for queue processing
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_QUEUE 
                                ON {table_name} (USER_ID, PROCESSED, PROCESSING_STARTED, ADDED_AT)
                            """)
                        elif table_name == SCRAPPED_TEXT_TABLE:
                            # Add index for content lookups
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_SOURCE 
                                ON {table_name} (TOP_LEVEL_SOURCE, USER_ID)
                            """)
                            # Add index for word count
                            cursor.execute(f"""
                                CREATE INDEX IDX_{table_name}_WORD_COUNT 
                                ON {table_name} (WORD_COUNT)
                            """)
                    else:
                        # If SCRAPPED_TEXT table already exists, check for WORD_COUNT column
                        if table_name == SCRAPPED_TEXT_TABLE:
                            cursor.execute("""
                                SELECT COUNT(*) FROM USER_TAB_COLUMNS 
                                WHERE TABLE_NAME = 'SCRAPPED_TEXT' AND COLUMN_NAME = 'WORD_COUNT'
                            """)
                            
                            column_exists = cursor.fetchone()[0] > 0
                            
                            if not column_exists:
                                # Add the WORD_COUNT column to an existing table
                                cursor.execute("""
                                    ALTER TABLE SCRAPPED_TEXT 
                                    ADD WORD_COUNT NUMBER DEFAULT 0
                                """)
                                
                                # Create index for the new column
                                cursor.execute(f"""
                                    CREATE INDEX IDX_{table_name}_WORD_COUNT 
                                    ON {table_name} (WORD_COUNT)
                                """)
                                
                                print(f"Added WORD_COUNT column to existing SCRAPPED_TEXT table")
                
                connection.commit()
                print("Database tables initialized successfully")
    except Exception as e:
        print(f"Error initializing tables: {e}")
        traceback.print_exc()

# Initialize tables on module load
initialize_tables()



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
        return None
    except jwt.InvalidTokenError:
        return None

# Authentication decorator
def token_required(f):
    """Decorator for verifying JWT tokens"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('Authorization')
        
        if not token or not token.startswith("Bearer "):
            print("No valid token found in decorator")
            return jsonify({
                'status': 'error',
                'message': 'Unauthorized access. Valid token required.',
                'timestamp': datetime.now().isoformat()
            }), 401

        token = token.split(" ")[1]
        
        try:
            decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            user_id = decoded['user_id']
            print(f"Token verified for user_id: {user_id}")
            
            # If the function expects only one argument (user_id)
            if f.__code__.co_argcount == 1:
                return f(user_id)
            
            # If the function can handle multiple arguments
            return f(user_id, *args, **kwargs)
        except jwt.ExpiredSignatureError:
            print("Token has expired")
            return jsonify({
                'status': 'error',
                'message': 'Token has expired',
                'timestamp': datetime.now().isoformat()
            }), 401
        except jwt.InvalidTokenError:
            print("Invalid token")
            return jsonify({
                'status': 'error',
                'message': 'Invalid token',
                'timestamp': datetime.now().isoformat()
            }), 401
        except Exception as e:
            print(f"Token verification error: {str(e)}")
            return jsonify({
                'status': 'error',
                'message': 'Authentication failed',
                'timestamp': datetime.now().isoformat()
            }), 401
            
    return wrapper

def is_valid_url(url):
    """Enhanced URL validation function"""
    try:
        return validators.url(url)
    except:
        # Some URLs might cause validators to raise exceptions
        return False
    
def is_valid_content_url(url):
    """Check if URL is likely to contain text content"""
    # Skip common non-text content URLs and query params that indicate non-content
    if re.search(r'\.(jpg|jpeg|png|gif|svg|webp|mp4|mp3|pdf|zip|exe|js|css|xml)$', url, re.IGNORECASE):
        return False

    # Skip common non-content paths
    if re.search(r'/(login|logout|signin|signout|register|cart|checkout|api)/?$', url, re.IGNORECASE):
        return False

    # Skip social media URLs
    if is_social_media_url(url):
        return False
        
    # Additional checks for common query parameters that indicate non-content
    if re.search(r'\?(utm_|ref=|source=|campaign=|medium=)', url, re.IGNORECASE):
        # Only strip these parameters rather than rejecting the URL completely
        try:
            base_url = url.split('?')[0]
            return True
        except:
            pass
            
    # Add check for fragment identifiers (anchors) which often point to the same content
    if '#' in url:
        # We could either strip the fragment or just accept the URL
        return True

    return True

def is_social_media_url(url):
    """Check if the URL is a social media URL"""
    social_media_domains = [
        'facebook.com', 'twitter.com', 'instagram.com', 'linkedin.com', 
        'youtube.com', 'tiktok.com', 'pinterest.com', 'reddit.com', 
        'snapchat.com', 'whatsapp.com', 'telegram.org', 'wechat.com', 
        'tumblr.com', 'flickr.com', 'vk.com', 'weibo.com'
    ]
    
    # Check if the URL contains any social media domain
    for domain in social_media_domains:
        if domain in url:
            return True
    return False

def contains_text_in_url(url):
    """Check if URL contains text content indicators"""
    # Look for words that suggest text content in the URL
    text_indicators = [
        'article', 'blog', 'post', 'news', 'story', 'content', 
        'text', 'page', 'read', 'view', 'doc', 'document', 
        'info', 'about', 'faq', 'help', 'guide', 'tutorial',
        'wiki', 'knowledge', 'learn', 'support'
    ]
    
    # Convert URL to lowercase for case-insensitive matching
    url_lower = url.lower()
    
    # Check if any text indicator appears in the URL
    for indicator in text_indicators:
        if indicator in url_lower:
            return True
            
    return False
def user_has_active_job(user_id):
    """
    Check if a user has an active job with improved logging and validation
    
    Args:
        user_id: The user ID to check
        
    Returns:
        bool: True if the user has any active jobs, False otherwise
    """
    if not user_id:
        print("Invalid user_id provided to user_has_active_job")
        return False
        
    has_active = user_id in active_user_jobs and len(active_user_jobs[user_id]) > 0
    
    if has_active:
        active_count = len(active_user_jobs[user_id])
        active_jobs = ", ".join(list(active_user_jobs[user_id])[:3])  # Show first few jobs
        print(f"User {user_id} has {active_count} active job(s): {active_jobs}...")
    else:
        print(f"User {user_id} has no active jobs")
        
    return has_active

def add_active_job(user_id, source_url):
    """
    Add a job to the active jobs list for a user with validation
    
    Args:
        user_id: The user ID
        source_url: The source URL for the job
        
    Returns:
        bool: True if added successfully, False otherwise
    """
    if not user_id or not source_url:
        print("Invalid parameters provided to add_active_job")
        return False
        
    if user_id not in active_user_jobs:
        active_user_jobs[user_id] = set()
    
    # Check if already in active jobs
    if source_url in active_user_jobs[user_id]:
        print(f"Job already active: {source_url} for user {user_id}")
        return False
        
    active_user_jobs[user_id].add(source_url)
    print(f"Added active job: {source_url} for user {user_id}")
    
    # Log total active jobs for this user
    job_count = len(active_user_jobs[user_id])
    print(f"User {user_id} now has {job_count} active job(s)")
    
    return True

def remove_active_job(user_id, source_url):
    """
    Remove a job from the active jobs list for a user
    
    Args:
        user_id: The user ID
        source_url: The source URL for the job
        
    Returns:
        bool: True if removed successfully, False otherwise
    """
    if not user_id or not source_url:
        print("Invalid parameters provided to remove_active_job")
        return False
        
    if user_id not in active_user_jobs:
        print(f"User {user_id} has no active jobs to remove")
        return False
    
    if source_url not in active_user_jobs[user_id]:
        print(f"Job not found in active jobs: {source_url} for user {user_id}")
        return False
    
    active_user_jobs[user_id].remove(source_url)
    print(f"Removed active job: {source_url} for user {user_id}")
    
    # Clean up if no more active jobs
    if len(active_user_jobs[user_id]) == 0:
        del active_user_jobs[user_id]
        print(f"User {user_id} now has no active jobs, removed from tracking")
    else:
        job_count = len(active_user_jobs[user_id])
        print(f"User {user_id} now has {job_count} active job(s)")
    
    return True
def auto_vectorize_data(user_id, source_url):
    """
    Automatically trigger vectorization for a completed URL
    
    Args:
        user_id: The user ID
        source_url: The source URL that has been processed
        
    Returns:
        dict: Results of the vectorization process
    """
    try:
        print(f"Auto-vectorizing data for URL: {source_url}, user: {user_id}")
        
        # Import the vectorize function from oracle_chatbot.py
        from oracle_chatbot import process_scrapped_text_to_vector_store, connect_to_jsondb
        
        # Create a connection to the JSON database
        jsondb_connection = connect_to_jsondb()
        if not jsondb_connection:
            print(f"Failed to connect to JSON database for vectorization")
            return {
                'status': 'error',
                'message': 'Failed to connect to vector database',
                'source_url': source_url
            }
        
        try:
            # Call the vectorization function with source_level_url filter
            result = process_scrapped_text_to_vector_store(
                jsondb_connection, 
                user_id=user_id, 
                source_level_url=source_url
            )
            
            print(f"Vectorization completed for {source_url}: {result}")
            return result
        finally:
            # Ensure connection is closed
            if jsondb_connection:
                jsondb_connection.close()
    
    except Exception as e:
        print(f"Error during auto-vectorization for {source_url}: {str(e)}")
        traceback.print_exc()
        return {
            'status': 'error',
            'message': str(e),
            'source_url': source_url
        }
def synchronize_active_jobs(user_id=None):
    """
    Synchronize in-memory active jobs tracking with database state
    to ensure consistency in queue processing.
    
    Args:
        user_id: Optional user ID to sync only for a specific user
        
    Returns:
        dict: Statistics about the synchronization
    """
    try:
        print(f"Synchronizing active jobs{' for user ' + str(user_id) if user_id else ''}")
        
        # Stats to return
        stats = {
            'added_to_memory': 0,
            'removed_from_memory': 0,
            'active_jobs_before': sum(len(jobs) for jobs in active_user_jobs.values()) if active_user_jobs else 0,
            'active_jobs_after': 0
        }
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Build query based on parameters
                query = """
                    SELECT USER_ID, SOURCE_URL FROM {0}
                    WHERE PROCESSED = 0 AND PROCESSING_STARTED IS NOT NULL
                """.format(PROCESSING_QUEUE_TABLE)
                
                params = {}
                
                if user_id:
                    query += " AND USER_ID = :user_id"
                    params['user_id'] = user_id
                    
                # Get currently active jobs from database
                cursor.execute(query, params)
                active_from_db = {}
                
                for row in cursor.fetchall():
                    db_user_id, db_source_url = row
                    if db_user_id not in active_from_db:
                        active_from_db[db_user_id] = set()
                    active_from_db[db_user_id].add(db_source_url)
                
                # If filtering by user_id, only process that user
                users_to_process = [user_id] if user_id else list(active_from_db.keys())
                
                # Add any missing users from in-memory tracking
                if not user_id:
                    users_to_process.extend([u for u in active_user_jobs.keys() if u not in users_to_process])
                
                # For each user, sync memory with database
                for uid in users_to_process:
                    if uid not in active_from_db:
                        active_from_db[uid] = set()
                    
                    if uid not in active_user_jobs:
                        active_user_jobs[uid] = set()
                    
                    # Items in DB but not in memory should be added to memory
                    for url in active_from_db[uid]:
                        if url not in active_user_jobs[uid]:
                            active_user_jobs[uid].add(url)
                            stats['added_to_memory'] += 1
                            print(f"Added to memory tracking: {url} for user {uid}")
                    
                    # Items in memory but not in DB should be removed from memory
                    urls_to_remove = []
                    for url in active_user_jobs[uid]:
                        if url not in active_from_db[uid]:
                            urls_to_remove.append(url)
                    
                    for url in urls_to_remove:
                        active_user_jobs[uid].remove(url)
                        stats['removed_from_memory'] += 1
                        print(f"Removed from memory tracking: {url} for user {uid}")
                    
                    # Clean up empty sets
                    if not active_user_jobs[uid]:
                        del active_user_jobs[uid]
                        print(f"Removed empty tracking for user {uid}")
        
        # Calculate final stats
        stats['active_jobs_after'] = sum(len(jobs) for jobs in active_user_jobs.values()) if active_user_jobs else 0
        
        print(f"Synchronization complete: {stats}")
        return stats
    
    except Exception as e:
        print(f"Error synchronizing active jobs: {str(e)}")
        traceback.print_exc()
        return {
            'error': str(e),
            'added_to_memory': 0,
            'removed_from_memory': 0,
            'active_jobs_before': 0,
            'active_jobs_after': 0
        }
def recover_stalled_jobs(user_id=None, max_age_minutes=30):
    """
    Check for and recover stalled jobs in the queue
    
    Args:
        user_id: Optional user ID to check only for a specific user
        max_age_minutes: Maximum age in minutes for a job to be considered stalled
        
    Returns:
        dict: Statistics about recovered jobs
    """
    try:
        print(f"Checking for stalled jobs{' for user ' + str(user_id) if user_id else ''}")
        
        # Stats to return
        stats = {
            'stalled_jobs_found': 0,
            'jobs_recovered': 0,
            'jobs_failed': 0
        }
        
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=max_age_minutes)
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Build query to find stalled jobs
                query = """
                    SELECT ID, USER_ID, SOURCE_URL, PAGE_LIMIT,
                           TO_CHAR(PROCESSING_STARTED, 'YYYY-MM-DD HH24:MI:SS') as START_TIME
                    FROM {0}
                    WHERE PROCESSED = 0 
                    AND PROCESSING_STARTED IS NOT NULL
                    AND PROCESSING_STARTED < :cutoff_time
                """.format(PROCESSING_QUEUE_TABLE)
                
                params = {'cutoff_time': cutoff_time}
                
                if user_id:
                    query += " AND USER_ID = :user_id"
                    params['user_id'] = user_id
                
                # Get stalled jobs
                cursor.execute(query, params)
                stalled_jobs = cursor.fetchall()
                
                stats['stalled_jobs_found'] = len(stalled_jobs)
                
                if stalled_jobs:
                    print(f"Found {len(stalled_jobs)} stalled jobs")
                    
                    # Process each stalled job
                    for job in stalled_jobs:
                        job_id, job_user_id, job_url, job_limit, job_start_time = job
                        
                        try:
                            print(f"Recovering stalled job: {job_url} for user {job_user_id}, started at {job_start_time}")
                            
                            # Clean up any active job tracking
                            if job_user_id in active_user_jobs and job_url in active_user_jobs[job_user_id]:
                                active_user_jobs[job_user_id].remove(job_url)
                                if not active_user_jobs[job_user_id]:
                                    del active_user_jobs[job_user_id]
                            
                            # Clean up any events
                            event_key = f"{job_user_id}:{job_url}"
                            if event_key in crawling_events:
                                crawling_events[event_key].set()
                                del crawling_events[event_key]
                            if event_key in processing_events:
                                processing_events[event_key].set()
                                del processing_events[event_key]
                            
                            # Reset the job to be picked up again
                            cursor.execute("""
                                UPDATE {0} SET PROCESSING_STARTED = NULL
                                WHERE ID = :id
                            """.format(PROCESSING_QUEUE_TABLE), id=job_id)
                            
                            connection.commit()
                            stats['jobs_recovered'] += 1
                            
                            print(f"Successfully reset stalled job: {job_url}")
                        except Exception as job_error:
                            print(f"Error recovering job {job_id} ({job_url}): {str(job_error)}")
                            stats['jobs_failed'] += 1
                else:
                    print("No stalled jobs found")
        
        # Synchronize active job tracking if we recovered any jobs
        if stats['jobs_recovered'] > 0:
            synchronize_active_jobs(user_id)
        
        print(f"Stalled job recovery complete: {stats}")
        return stats
        
    except Exception as e:
        print(f"Error recovering stalled jobs: {str(e)}")
        traceback.print_exc()
        return {
            'error': str(e),
            'stalled_jobs_found': 0,
            'jobs_recovered': 0,
            'jobs_failed': 0
        }    
# Queue Management Functions
def perform_queue_maintenance():
    """
    Perform maintenance on the queue to ensure it's in a consistent state.
    This function should be called at application startup.
    
    Returns:
        dict: Statistics about maintenance operations
    """
    try:
        print("Starting queue maintenance...")
        
        stats = {
            'stalled_jobs': 0,
            'memory_sync': {},
            'next_jobs_started': 0
        }
        
        # 1. Recover stalled jobs
        stalled_results = recover_stalled_jobs(max_age_minutes=15)  # Consider jobs stalled after 15 minutes
        stats['stalled_jobs'] = stalled_results
        
        # 2. Synchronize in-memory job tracking with database
        sync_results = synchronize_active_jobs()
        stats['memory_sync'] = sync_results
        
        # 3. For each user, check if they have no active jobs but queued jobs waiting
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Find users with queued jobs but no active processing
                cursor.execute("""
                    SELECT DISTINCT USER_ID FROM {0}
                    WHERE PROCESSED = 0 AND PROCESSING_STARTED IS NULL
                """.format(PROCESSING_QUEUE_TABLE))
                
                users_with_queued = [row[0] for row in cursor.fetchall()]
                
                for user_id in users_with_queued:
                    # Check if this user has any active jobs
                    if not user_has_active_job(user_id):
                        # No active jobs, but has queued jobs - start the next one
                        next_item = get_next_from_queue(user_id)
                        
                        if next_item:
                            next_url = next_item['source_url']
                            page_limit = next_item['page_limit']
                            print(f"Starting next queued job for inactive user {user_id}: {next_url}")
                            
                            # Start processing
                            result = start_crawling_and_processing(user_id, next_url, page_limit)
                            
                            if result:
                                stats['next_jobs_started'] += 1
                                print(f"Successfully started processing for queue item: {next_url}")
        
        print(f"Queue maintenance completed: {stats}")
        return stats
        
    except Exception as e:
        print(f"Error during queue maintenance: {str(e)}")
        traceback.print_exc()
        return {
            'error': str(e),
            'stalled_jobs': 0,
            'memory_sync': {},
            'next_jobs_started': 0
        }

# This function should be called when the application starts
def initialize_queue_system():
    """Initialize the queue system at application startup"""
    try:
        print("Initializing queue system...")
        
        # Reset in-memory tracking
        global active_user_jobs, crawling_events, processing_events
        active_user_jobs = {}
        crawling_events = {}
        processing_events = {}
        
        # Run maintenance to recover from any previous crashes
        maintenance_results = perform_queue_maintenance()
        
        print(f"Queue system initialized: {maintenance_results}")
        
        # Optional: Set up a background thread to periodically check for stalled jobs
        def maintenance_worker():
            while True:
                try:
                    time.sleep(300)  # Run every 5 minutes
                    print("Running scheduled queue maintenance...")
                    perform_queue_maintenance()
                except Exception as e:
                    print(f"Error in maintenance worker: {str(e)}")
        
        maintenance_thread = Thread(target=maintenance_worker, daemon=True)
        maintenance_thread.start()
        
        return True
    except Exception as e:
        print(f"Error initializing queue system: {str(e)}")
        traceback.print_exc()
        return False
    
def add_to_queue(user_id, source_url, additional_data=None):
    """Add a URL to the processing queue for a user with better error handling and logging"""
    try:
        print(f"Adding URL to queue: {source_url} for user {user_id}")
        
        # Validate input
        if not user_id or not source_url:
            print("Invalid input: user_id and source_url are required")
            return False
            
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Check if it already exists in the queue
                cursor.execute("""
                    SELECT ID, PROCESSED, PROCESSING_STARTED FROM {0}
                    WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
                """.format(PROCESSING_QUEUE_TABLE), 
                   user_id=user_id, source_url=source_url)
                
                existing_row = cursor.fetchone()
                
                if existing_row:
                    id, processed, started = existing_row
                    
                    if processed == 0:
                        # Item already in queue and not processed
                        if started:
                            print(f"URL already in progress: {source_url} for user {user_id}")
                        else:
                            print(f"URL already in queue: {source_url} for user {user_id}")
                        return False
                    else:
                        # Item was processed before, re-add it
                        print(f"URL was previously processed, re-adding to queue: {source_url}")
                        
                        # Delete old entry
                        cursor.execute("""
                            DELETE FROM {0}
                            WHERE ID = :id
                        """.format(PROCESSING_QUEUE_TABLE), id=id)
                        
                        # Continue to add new entry
                    
                # Set defaults
                page_limit = 10
                
                # Update with any additional data
                if additional_data:
                    if 'page_limit' in additional_data:
                        page_limit = additional_data['page_limit']
                
                # Insert queue item with retry logic
                retry_count = 0
                max_retries = 3
                
                while retry_count < max_retries:
                    try:
                        cursor.execute("""
                            INSERT INTO {0} (USER_ID, SOURCE_URL, ADDED_AT, PROCESSED, PAGE_LIMIT)
                            VALUES (:user_id, :source_url, CURRENT_TIMESTAMP, 0, :page_limit)
                        """.format(PROCESSING_QUEUE_TABLE),
                           user_id=user_id, source_url=source_url, page_limit=page_limit)
                        
                        connection.commit()
                        print(f"Added URL to queue: {source_url} for user {user_id}, page_limit: {page_limit}")
                        return True
                    except oracledb.DatabaseError as db_error:
                        error, = db_error.args
                        if error.code == 1:  # Constraint violation code
                            print(f"URL already in queue (constraint error): {source_url}")
                            return False
                        else:
                            # Other database error, retry
                            print(f"Database error adding to queue (attempt {retry_count+1}): {str(error)}")
                            retry_count += 1
                            time.sleep(0.5)  # Short delay before retry
                
                # If we reach here, all retries failed
                print(f"Failed to add URL to queue after {max_retries} attempts: {source_url}")
                return False
    except Exception as e:
        print(f"Error adding to queue: {str(e)}")
        traceback.print_exc()
        return False

# Add these functions to your file_api.py file, preferably near the other helper functions

def extract_domain(url):
    """Helper function to extract domain from URL"""
    try:
        # Remove protocol and path
        domain = re.sub(r'^https?://', '', url)
        domain = re.sub(r'/.*$', '', domain)
        return domain.lower()
    except:
        return None

def continuous_crawl_job(top_level_source_url, stop_event, user_id=None, page_limit=10):
    """
    Worker function to continuously crawl all links from the starting URL
    with improved batch processing, until all links are crawled or the page limit is reached.
    """
    connection = None
    cursor = None
    try:
        print(f"Starting crawl job for {top_level_source_url} for user {user_id}")
        
        # Extract the original domain for same-domain crawling
        original_domain = extract_domain(top_level_source_url)
        if not original_domain:
            print(f"Invalid domain extracted from URL: {top_level_source_url}")
            return {
                'error': f"Invalid domain extracted from URL: {top_level_source_url}"
            }
        
        print(f"Original domain for same-domain crawling: {original_domain}")

        # Stats counters
        links_added = 0
        errors_encountered = 0
        
        # Set to track consecutive empty runs (no new links added)
        consecutive_empty_runs = 0
        max_consecutive_empty_runs = 3  # After this many empty runs, terminate
        
        # Process URLs in small batches for better concurrency
        BATCH_SIZE = 3
        
        # Check if we have a limit
        if page_limit == 0:
            print("Page limit is set to 0 (unlimited)")
        else:
            print(f"Page limit is set to {page_limit} crawled pages")
        
        while not stop_event.is_set():
            try:
                with get_db_connection() as connection:
                    with connection.cursor() as cursor:
                        # CRITICAL: Check how many URLs have already been crawled for this user and source
                        cursor.execute("""
                            SELECT COUNT(*) FROM {0}
                            WHERE IS_CRAWLED = 1 AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                        """.format(LINKS_TO_SCRAP_TABLE), 
                           source_url=top_level_source_url, user_id=user_id)
                        
                        already_crawled_count = cursor.fetchone()[0]
                        
                        # Check if we've reached the page limit
                        if page_limit > 0 and already_crawled_count >= page_limit:
                            print(f"REACHED PAGE LIMIT: {already_crawled_count}/{page_limit} pages crawled. Strictly enforcing limit.")
                            break
                            
                        # Find out how many uncrawled links remain
                        cursor.execute("""
                            SELECT COUNT(*) FROM {0}
                            WHERE (IS_CRAWLED = 0 OR IS_CRAWLED IS NULL) 
                            AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                        """.format(LINKS_TO_SCRAP_TABLE), 
                           source_url=top_level_source_url, user_id=user_id)
                        
                        links_remaining = cursor.fetchone()[0]
                        
                        # If no uncrawled links remain, we're done
                        if links_remaining == 0:
                            print(f"No more uncrawled links for {top_level_source_url} for user {user_id}. Exiting crawl job.")
                            break
                        
                        # Calculate how many links we can process in this batch
                        batch_limit = min(BATCH_SIZE, page_limit - already_crawled_count if page_limit > 0 else BATCH_SIZE)
                        if batch_limit <= 0:
                            break
                            
                        # Find the next uncrawled links (batch)
                        cursor.execute("""
                            SELECT ID, LINK, NVL(DEPTH, 0) AS DEPTH 
                            FROM {0}
                            WHERE (IS_CRAWLED = 0 OR IS_CRAWLED IS NULL)
                            AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                            ORDER BY DEPTH ASC, ADDED_AT ASC
                            FETCH FIRST :batch_limit ROWS ONLY
                        """.format(LINKS_TO_SCRAP_TABLE), 
                           source_url=top_level_source_url, user_id=user_id, batch_limit=batch_limit)
                        
                        link_rows = cursor.fetchall()
                        
                        if not link_rows:
                            print(f"No more uncrawled links for {top_level_source_url} for user {user_id}. Exiting crawl job.")
                            break
                            
                        # Mark all of these links as being crawled
                        link_ids = [row[0] for row in link_rows]
                        placeholders = ','.join([f':id{i}' for i in range(len(link_ids))])
                        id_dict = {f'id{i}': id_val for i, id_val in enumerate(link_ids)}
                        
                        cursor.execute(f"""
                            UPDATE {LINKS_TO_SCRAP_TABLE} 
                            SET CRAWLING_STARTED = CURRENT_TIMESTAMP
                            WHERE ID IN ({placeholders})
                        """, **id_dict)
                        
                        connection.commit()
                        
                        batch_links_added = 0
                        batch_errors = 0
                        
                        # Process each link in the batch
                        for link_id, url_to_crawl, current_depth in link_rows:
                            if stop_event.is_set():
                                print(f"Stop event triggered during batch processing. Breaking out.")
                                break
                                
                            try:
                                # Add user agent to avoid being blocked
                                headers = {
                                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                                }
                                
                                # Make request to the URL with reduced timeout
                                print(f"Making HTTP request to: {url_to_crawl}")
                                response = requests.get(url_to_crawl, headers=headers, timeout=20)
                                response.raise_for_status()
                                
                                # Parse the HTML content
                                print(f"Parsing HTML content from: {url_to_crawl}")
                                soup = BeautifulSoup(response.text, 'html.parser')
                                
                                # Find all anchor tags
                                all_links = soup.find_all('a', href=True)
                                
                                # Base URL for resolving relative URLs
                                base_url = url_to_crawl
                                
                                # Check if the HTML has a base tag
                                base_tag = soup.find('base', href=True)
                                if base_tag:
                                    base_url = base_tag['href']
                                
                                # Extract URLs
                                unique_links = []
                                current_links_added = 0
                                
                                for link in all_links:
                                    href = link['href'].strip()
                                    
                                    # Skip empty hrefs, javascript:, mailto:, tel: links
                                    if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                                        continue
                                    
                                    try:
                                        # Convert relative URLs to absolute URLs
                                        full_url = urljoin(base_url, href)
                                        
                                        # Check if the URL belongs to the same domain
                                        link_domain = extract_domain(full_url)
                                        if link_domain != original_domain:
                                            continue
                                        
                                        # Skip invalid URLs, non-content URLs, and social media URLs
                                        if not is_valid_url(full_url) or not is_valid_content_url(full_url) or is_social_media_url(full_url):
                                            continue
                                        
                                        unique_links.append(full_url)
                                    except Exception as link_error:
                                        print(f"Error processing URL {href}: {str(link_error)}")
                                        continue
                                
                                # Remove duplicates
                                unique_links = list(set(unique_links))
                                print(f"Found {len(unique_links)} valid URLs on {url_to_crawl}")
                                
                                # Batch insert links if we have any
                                if unique_links:
                                    # Use a more efficient approach for bulk insertion
                                    values_list = []
                                    for link in unique_links[:100]:  # Limit to 100 links per page to prevent overload
                                        has_text = 1 if contains_text_in_url(link) else 0
                                        values_list.append({
                                            'link': link,
                                            'top_level_source': top_level_source_url,
                                            'user_id': user_id,
                                            'source_url': url_to_crawl,
                                            'depth': current_depth + 1,
                                            'has_text': has_text
                                        })
                                    
                                    # Use batch insert with MERGE to handle duplicates efficiently
                                    batch_insert_result = batch_insert_links(connection, values_list)
                                    if batch_insert_result > 0:
                                        current_links_added += batch_insert_result
                                        batch_links_added += batch_insert_result
                                
                                # Update the current link as crawled
                                cursor.execute("""
                                    UPDATE {0} SET
                                        IS_CRAWLED = 1,
                                        CRAWLED_AT = CURRENT_TIMESTAMP,
                                        LINKS_FOUND = :links_found,
                                        LINKS_ADDED = :links_added
                                    WHERE ID = :id
                                """.format(LINKS_TO_SCRAP_TABLE), 
                                    id=link_id,
                                    links_found=len(unique_links),
                                    links_added=current_links_added
                                )
                                
                                connection.commit()
                                
                            except requests.exceptions.RequestException as req_error:
                                # Handle request-specific errors (network, timeout, etc.)
                                print(f"Request error processing URL {url_to_crawl}: {str(req_error)}")
                                
                                # Update link as crawled with error
                                cursor.execute("""
                                    UPDATE {0} SET
                                        IS_CRAWLED = 1,
                                        CRAWLED_AT = CURRENT_TIMESTAMP,
                                        ERROR = :error
                                    WHERE ID = :id
                                """.format(LINKS_TO_SCRAP_TABLE), 
                                    id=link_id,
                                    error=str(req_error)[:4000]  # Limit error message length
                                )
                                connection.commit()
                                
                                batch_errors += 1
                                
                            except Exception as e:
                                # Catch any other unexpected errors
                                print(f"Unexpected error processing URL {url_to_crawl}: {str(e)}")
                                traceback.print_exc()
                                
                                # Update link as crawled with error
                                cursor.execute("""
                                    UPDATE {0} SET
                                        IS_CRAWLED = 1,
                                        CRAWLED_AT = CURRENT_TIMESTAMP,
                                        ERROR = :error,
                                        TRACEBACK = :traceback
                                    WHERE ID = :id
                                """.format(LINKS_TO_SCRAP_TABLE), 
                                    id=link_id,
                                    error=str(e)[:4000],
                                    traceback=traceback.format_exc()[:4000]
                                )
                                connection.commit()
                                
                                batch_errors += 1
                        
                        # Update stats after batch
                        links_added += batch_links_added
                        errors_encountered += batch_errors
                        
                        # Update consecutive empty runs counter
                        if batch_links_added > 0:
                            consecutive_empty_runs = 0
                        else:
                            consecutive_empty_runs += 1
                        
                        # Check if we've had too many consecutive runs with no new links
                        if consecutive_empty_runs >= max_consecutive_empty_runs:
                            print(f"No new links found for {max_consecutive_empty_runs} consecutive runs. Exiting crawl job.")
                            break
                            
                        # Check if we've now reached the limit after this batch
                        if page_limit > 0 and already_crawled_count + len(link_rows) >= page_limit:
                            print(f"REACHED PAGE LIMIT: {already_crawled_count + len(link_rows)}/{page_limit} pages crawled after batch. Exiting crawl job.")
                            break
                        
                        # Brief pause between batches - much shorter than before
                        time.sleep(0.1)
                
            except Exception as batch_error:
                print(f"Error processing batch: {str(batch_error)}")
                traceback.print_exc()
                errors_encountered += 1
                time.sleep(1)  # Brief delay on error before continuing
        
        # Final database connection to get final stats
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Get final count of crawled pages
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE IS_CRAWLED = 1 AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), 
                   source_url=top_level_source_url, user_id=user_id)
                
                final_crawled_count = cursor.fetchone()[0]
                
                print(f"Crawl job completed: {final_crawled_count} pages crawled (of {page_limit} limit), {links_added} links added, {errors_encountered} errors")
        
        # Return stats if we exit the loop
        return {
            'links_crawled': final_crawled_count,
            'links_added': links_added,
            'errors_encountered': errors_encountered,
            'page_limit': page_limit
        }
            
    except Exception as e:
        print(f"Error in continuous crawl job: {str(e)}")
        traceback.print_exc()
        return {
            'error': str(e),
            'traceback': traceback.format_exc()
        }

def start_crawling_and_processing(user_id, source_url, page_limit=10):
    """
    Start the crawling and processing for a URL simultaneously with a shorter processing delay
    and improved concurrency using Thread objects
    """
    # Add to active jobs list
    add_active_job(user_id, source_url)
    
    # Create stop event for crawling
    crawl_key = f"{user_id}:{source_url}"
    crawl_stop_event = Event()
    crawling_events[crawl_key] = crawl_stop_event
    
    # Create stop event for processing
    process_key = f"{user_id}:{source_url}"
    process_stop_event = Event()
    processing_events[process_key] = process_stop_event
    
    # Create a shared variable to track completion status
    completion_status = {
        'crawling_done': False,
        'processing_done': False,
        'in_final_cooldown': False,
        'links_before_cooldown': 0
    }

    # Define a wrapper function to start the crawling
    def start_crawl(url, stop_event, user_id, page_limit, completion_status):
        try:
            print(f"Starting crawl thread for {url}, user: {user_id}, limit: {page_limit} pages")
            result = continuous_crawl_job(url, stop_event, user_id, page_limit)
            print(f"Crawling completed with result: {result}")
            
            # Mark crawling as done
            completion_status['crawling_done'] = True
            print(f"Marked crawling as done for {url}, user: {user_id}")
            
            # When done, check if processing is also done
            if completion_status['processing_done']:
                # If both are done, start the final cool-down
                print(f"Both crawling and processing are done, starting final cooldown for {url}")
                start_final_cooldown(user_id, url, completion_status)
        except Exception as e:
            print(f"Error in crawl thread: {e}")
            traceback.print_exc()
            
            # Mark crawling as done even on error
            completion_status['crawling_done'] = True
            print(f"Marked crawling as done (with error) for {url}, user: {user_id}")
            
            # If there's an error, still check if we need to start cooldown
            if completion_status['processing_done']:
                print(f"Processing was already done, starting final cooldown for {url} despite crawling error")
                start_final_cooldown(user_id, url, completion_status)
    
    # Define a function for the processing thread with shorter delay
    def start_processing(url, stop_event, user_id, completion_status):
        try:
            # Use 5 seconds delay (reduced from 15)
            delay_seconds = 5
            print(f"Starting processing thread for {url}, user: {user_id} with {delay_seconds}s delay")
            result = continuous_processing_job(url, stop_event, delay_seconds, user_id)
            print(f"Processing completed with result: {result}")
            
            # Mark processing as done
            completion_status['processing_done'] = True
            print(f"Marked processing as done for {url}, user: {user_id}")
            
            # When done, check if crawling is also done
            if completion_status['crawling_done']:
                # If both are done, start the final cool-down
                print(f"Both crawling and processing are done, starting final cooldown for {url}")
                start_final_cooldown(user_id, url, completion_status)
            else:
                # Wait for crawling to finish
                print(f"Processing finished, waiting for crawling to complete for {url}")
        except Exception as e:
            print(f"Error in processing thread: {e}")
            traceback.print_exc()
            
            # Mark processing as done even on error
            completion_status['processing_done'] = True
            print(f"Marked processing as done (with error) for {url}, user: {user_id}")
            
            # If there's an error, still check if we need to start cooldown
            if completion_status['crawling_done']:
                print(f"Crawling was already done, starting final cooldown for {url} despite processing error")
                start_final_cooldown(user_id, url, completion_status)
    
    # Function to start the final cool-down period (shorter duration)
    def start_final_cooldown(user_id, url, completion_status):
        try:
            # Check if we're already in cooldown to prevent multiple cooldown processes
            if completion_status.get('in_final_cooldown', False):
                print(f"Already in final cooldown for {url}, user: {user_id}. Skipping duplicate cooldown.")
                return
                
            print(f"Starting final cooldown for {url}, user: {user_id}")
            completion_status['in_final_cooldown'] = True
            
            with get_db_connection() as connection:
                with connection.cursor() as cursor:
                    # Count total links before cooldown
                    cursor.execute("""
                        SELECT COUNT(*) FROM {0}
                        WHERE TOP_LEVEL_SOURCE = :url AND USER_ID = :user_id
                    """.format(LINKS_TO_SCRAP_TABLE), 
                       url=url, user_id=user_id)
                    
                    completion_status['links_before_cooldown'] = cursor.fetchone()[0]
                    
                    # Reduced cooldown from 40 to 20 seconds for faster completion
                    cooldown_duration = 20  # 20 seconds 
                    cooldown_start_time = time.time()
                    
                    # Check for new links every 5 seconds during the cool-down period
                    while time.time() - cooldown_start_time < cooldown_duration:
                        # Skip if the stop events are set
                        if crawl_key in crawling_events and crawling_events[crawl_key].is_set():
                            print(f"Crawling stop event is set during cooldown for {url}. Terminating cooldown.")
                            break
                            
                        if process_key in processing_events and processing_events[process_key].is_set():
                            print(f"Processing stop event is set during cooldown for {url}. Terminating cooldown.")
                            break
                        
                        # Calculate remaining time
                        elapsed = time.time() - cooldown_start_time
                        remaining = cooldown_duration - elapsed
                        print(f"In final cool-down period. {remaining:.1f} seconds remaining. Checking for new links...")
                        
                        # Check if any new links have been discovered
                        cursor.execute("""
                            SELECT COUNT(*) FROM {0}
                            WHERE TOP_LEVEL_SOURCE = :url AND USER_ID = :user_id
                        """.format(LINKS_TO_SCRAP_TABLE), 
                           url=url, user_id=user_id)
                        
                        current_link_count = cursor.fetchone()[0]
                        
                        # Check if any links still need processing
                        cursor.execute("""
                            SELECT COUNT(*) FROM {0}
                            WHERE TOP_LEVEL_SOURCE = :url AND USER_ID = :user_id
                            AND (IS_PROCESSED = 'false' OR IS_PROCESSED IS NULL)
                        """.format(LINKS_TO_SCRAP_TABLE), 
                           url=url, user_id=user_id)
                        
                        unprocessed_count = cursor.fetchone()[0]
                        
                        if current_link_count > completion_status['links_before_cooldown'] or unprocessed_count > 0:
                            print(f"New links discovered during cool-down! Links before: {completion_status['links_before_cooldown']}, Current: {current_link_count}, Unprocessed: {unprocessed_count}")
                            print(f"Restarting crawling and processing for {url}")
                            
                            # Reset completion status
                            completion_status['crawling_done'] = False
                            completion_status['processing_done'] = False
                            completion_status['in_final_cooldown'] = False
                            
                            # Start the crawling and processing again
                            if unprocessed_count > 0 or current_link_count > completion_status['links_before_cooldown']:
                                # Start the crawl thread again
                                new_crawl_thread = Thread(
                                    target=start_crawl,
                                    args=(url, crawl_stop_event, user_id, page_limit, completion_status),
                                    daemon=True
                                )
                                new_crawl_thread.start()
                                
                                # Start the process thread again
                                new_process_thread = Thread(
                                    target=start_processing,
                                    args=(url, process_stop_event, user_id, completion_status),
                                    daemon=True
                                )
                                new_process_thread.start()
                                
                                # Exit this cool-down function to let the new threads handle it
                                return
                        
                        # Sleep for 3 seconds before checking again (reduced from 5)
                        time.sleep(3)
                    
                    # Cool-down period has elapsed with no new links
                    print(f"Final cool-down period of {cooldown_duration} seconds has elapsed. No new links found. Completing job for {url}")
                    
                    # Now mark as complete and process next in queue
                    mark_as_complete_and_process_next(user_id, url)
                    
        except Exception as e:
            print(f"Error in final cool-down: {str(e)}")
            traceback.print_exc()
            
            # Even if there's an error, try to move to the next URL
            mark_as_complete_and_process_next(user_id, url)
    
    # Start the crawling in a background thread
    crawler_thread = Thread(
        target=start_crawl,
        args=(source_url, crawl_stop_event, user_id, page_limit, completion_status),
        daemon=True
    )
    crawler_thread.start()
    
    # Start the processing in a background thread (with shorter delay)
    processor_thread = Thread(
        target=start_processing,
        args=(source_url, process_stop_event, user_id, completion_status),
        daemon=True
    )
    processor_thread.start()
    
    print(f"Started simultaneous crawling and processing for {source_url}, user {user_id}")
    return True

def batch_insert_links(connection, links_data):
    """
    Perform a batch insert of links using a more efficient approach
    
    Args:
        connection: Database connection
        links_data: List of dictionaries with link data
        
    Returns:
        Number of successfully inserted links
    """
    if not links_data:
        return 0
        
    inserted_count = 0
    
    try:
        cursor = connection.cursor()
        
        # Oracle supports inserting multiple rows in a single statement
        # But we'll need to handle potential constraint violations
        for link_data in links_data:
            try:
                # Use MERGE to handle unique constraint with a single statement
                cursor.execute(f"""
                    MERGE INTO {LINKS_TO_SCRAP_TABLE} t
                    USING (
                        SELECT 
                            :link AS LINK, 
                            :top_level_source AS TOP_LEVEL_SOURCE, 
                            :user_id AS USER_ID,
                            :source_url AS SOURCE_URL,
                            :depth AS DEPTH,
                            :has_text AS HAS_TEXT_IN_URL
                        FROM DUAL
                    ) s
                    ON (t.LINK = s.LINK AND t.USER_ID = s.USER_ID)
                    WHEN NOT MATCHED THEN
                        INSERT (
                            LINK, TOP_LEVEL_SOURCE, USER_ID, SOURCE_URL, 
                            DEPTH, HAS_TEXT_IN_URL, ADDED_AT, 
                            IS_CRAWLED, IS_PROCESSED
                        ) VALUES (
                            s.LINK, s.TOP_LEVEL_SOURCE, s.USER_ID, s.SOURCE_URL,
                            s.DEPTH, s.HAS_TEXT_IN_URL, CURRENT_TIMESTAMP,
                            0, 'false'
                        )
                    WHEN MATCHED THEN
                        UPDATE SET 
                            DEPTH = LEAST(t.DEPTH, s.DEPTH)
                            WHERE s.DEPTH < t.DEPTH
                """,
                    link=link_data['link'],
                    top_level_source=link_data['top_level_source'],
                    user_id=link_data['user_id'],
                    source_url=link_data['source_url'],
                    depth=link_data['depth'],
                    has_text=link_data['has_text']
                )
                
                # Only count as an insert if a row was affected
                if cursor.rowcount > 0:
                    inserted_count += 1
                    
            except oracledb.DatabaseError as db_error:
                # Log but continue with other links
                error, = db_error.args
                print(f"Database error on MERGE for link {link_data['link']}: {error}")
                continue
                
        connection.commit()
        return inserted_count
        
    except Exception as e:
        print(f"Error in batch_insert_links: {e}")
        traceback.print_exc()
        connection.rollback()
        return inserted_count
    
def mark_as_complete_and_process_next(user_id, source_url):
    """Mark a URL as complete in the queue, trigger vectorization, and start processing the next one if available"""
    try:
        print(f"Marking URL as complete and checking for next URL: {source_url} for user {user_id}")
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Mark the current URL as complete
                cursor.execute("""
                    UPDATE {0} SET PROCESSED = 1, PROCESSING_COMPLETED = CURRENT_TIMESTAMP
                    WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
                """.format(PROCESSING_QUEUE_TABLE),
                   user_id=user_id, source_url=source_url)
                
                rows_updated = cursor.rowcount
                connection.commit()
                
                print(f"Marked URL as complete: {source_url} for user {user_id}, rows affected: {rows_updated}")
                
                # Update the active user jobs tracking
                if user_id in active_user_jobs:
                    if source_url in active_user_jobs[user_id]:
                        active_user_jobs[user_id].remove(source_url)
                        print(f"Removed {source_url} from active jobs for user {user_id}")
                    if not active_user_jobs[user_id]:
                        del active_user_jobs[user_id]
                        print(f"No more active jobs for user {user_id}, removed from tracking")
                
                # Clean up event objects
                crawl_key = f"{user_id}:{source_url}"
                process_key = f"{user_id}:{source_url}"
                
                # Only delete the events if they exist
                if crawl_key in crawling_events:
                    crawling_events[crawl_key].set()  # Set the event first to signal threads to terminate
                    del crawling_events[crawl_key]
                    print(f"Cleaned up crawling event for {crawl_key}")
                    
                if process_key in processing_events:
                    processing_events[process_key].set()  # Set the event first to signal threads to terminate
                    del processing_events[process_key]
                    print(f"Cleaned up processing event for {process_key}")
                
                # Count scrapped documents
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                """.format(SCRAPPED_TEXT_TABLE), 
                   source_url=source_url, user_id=user_id)
                
                scrapped_count = cursor.fetchone()[0]
                
                if scrapped_count > 0:
                    print(f"Found {scrapped_count} scrapped documents for {source_url}, triggering vectorization")
                    
                    # Start vectorization in a background thread to avoid blocking
                    def vectorize_thread():
                        try:
                            vectorize_result = auto_vectorize_data(user_id, source_url)
                            print(f"Background vectorization completed for {source_url}: {vectorize_result}")
                        except Exception as e:
                            print(f"Error in vectorization thread: {str(e)}")
                            traceback.print_exc()
                    
                    vectorization_thread = Thread(target=vectorize_thread, daemon=True)
                    vectorization_thread.start()
                    print(f"Started background vectorization for {source_url}")
                else:
                    print(f"No scrapped documents found for {source_url}, skipping vectorization")
                
                # Get the next URL from the queue - needs to happen after cleanup
                next_item = get_next_from_queue(user_id)
                
                if next_item:
                    next_url = next_item['source_url']
                    page_limit = next_item['page_limit']
                    print(f"Starting to process next URL: {next_url} with limit of {page_limit} pages for user {user_id}")
                    
                    # Ensure there are no lingering events for this URL before starting
                    next_crawl_key = f"{user_id}:{next_url}"
                    next_process_key = f"{user_id}:{next_url}"
                    
                    if next_crawl_key in crawling_events:
                        del crawling_events[next_crawl_key]
                    if next_process_key in processing_events:
                        del processing_events[next_process_key]
                    
                    # Start processing the next URL with its page limit
                    start_crawling_and_processing(user_id, next_url, page_limit)
                    return True
                else:
                    print(f"No more URLs in queue for user {user_id}")
                    return False
    except Exception as e:
        print(f"Error marking as complete and processing next: {str(e)}")
        traceback.print_exc()
        return False

def get_next_from_queue(user_id):
    """Get the next URL from the queue for a user with improved error handling"""
    try:
        print(f"Getting next URL from queue for user: {user_id}")
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Find the oldest unprocessed URL for this user
                cursor.execute("""
                    SELECT ID, SOURCE_URL, PAGE_LIMIT FROM {0}
                    WHERE USER_ID = :user_id AND PROCESSED = 0 AND PROCESSING_STARTED IS NULL
                    ORDER BY ADDED_AT ASC
                    FETCH FIRST 1 ROW ONLY
                """.format(PROCESSING_QUEUE_TABLE), 
                   user_id=user_id)
                
                row = cursor.fetchone()
                
                if row:
                    queue_id, source_url, page_limit = row
                    
                    # Mark as processing started with retry logic
                    retry_count = 0
                    max_retries = 3
                    
                    while retry_count < max_retries:
                        try:
                            cursor.execute("""
                                UPDATE {0} SET PROCESSING_STARTED = CURRENT_TIMESTAMP
                                WHERE ID = :id AND PROCESSING_STARTED IS NULL
                            """.format(PROCESSING_QUEUE_TABLE), 
                               id=queue_id)
                            
                            rows_affected = cursor.rowcount
                            connection.commit()
                            
                            if rows_affected > 0:
                                print(f"Successfully marked queue item {queue_id} as started for URL: {source_url}")
                                break
                            else:
                                # Item may have been picked up by another process
                                print(f"Queue item {queue_id} was already being processed by another worker")
                                retry_count += 1
                        except Exception as update_error:
                            print(f"Error updating queue item (attempt {retry_count+1}): {str(update_error)}")
                            retry_count += 1
                            time.sleep(0.5)  # Short delay before retry
                    
                    # Verify queue item is still available after update
                    cursor.execute("""
                        SELECT ID FROM {0}
                        WHERE ID = :id AND PROCESSING_STARTED IS NOT NULL AND PROCESSED = 0
                    """.format(PROCESSING_QUEUE_TABLE), 
                       id=queue_id)
                    
                    verify_row = cursor.fetchone()
                    
                    if verify_row:
                        print(f"Retrieved next URL from queue: {source_url} for user {user_id}")
                        
                        # Return both the URL and the page limit
                        return {
                            'source_url': source_url,
                            'page_limit': page_limit or 10  # Default to 10 if None
                        }
                    else:
                        print(f"Queue item {queue_id} could not be reserved, retrying with another item")
                        # Recursive call to get the next item
                        return get_next_from_queue(user_id)
                else:
                    print(f"No more URLs in queue for user {user_id}")
                    return None
    except Exception as e:
        print(f"Error getting next from queue: {str(e)}")
        traceback.print_exc()
        return None
def add_word_count_field():
    """
    Add a WORD_COUNT column to the SCRAPPED_TEXT table if it doesn't exist.
    This is a safe operation that can be run even if the column already exists.
    """
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Check if the column already exists
                cursor.execute("""
                    SELECT COUNT(*) FROM USER_TAB_COLUMNS 
                    WHERE TABLE_NAME = 'SCRAPPED_TEXT' AND COLUMN_NAME = 'WORD_COUNT'
                """)
                
                column_exists = cursor.fetchone()[0] > 0
                
                if not column_exists:
                    # Add the column
                    cursor.execute("""
                        ALTER TABLE SCRAPPED_TEXT 
                        ADD WORD_COUNT NUMBER DEFAULT 0
                    """)
                    
                    connection.commit()
                    print("Added WORD_COUNT column to SCRAPPED_TEXT table")
                else:
                    print("WORD_COUNT column already exists in SCRAPPED_TEXT table")
                    
                return not column_exists  # Return True if added, False if already existed
                
    except Exception as e:
        print(f"Error adding word count field: {str(e)}")
        traceback.print_exc()
        return False    

def calculate_word_count(text):
    """
    Calculate the number of words in a text.
    
    Args:
        text: The text to count words in
        
    Returns:
        int: Number of words
    """
    # Remove excessive whitespace and split by whitespace
    words = text.strip().split()
    return len(words)

def scrape_single_link(connection, link_doc, user_id=None):
    """Helper function to scrape a single link with word count tracking"""
    link = link_doc['link']
    print(f"Starting to scrape link: {link} for user: {user_id}")
    
    is_wiki = 'wikipedia.org' in link or 'wiki' in link.lower()
    
    # Get the top-level source and immediate source URLs
    top_level_source = link_doc.get('top_level_source', link_doc.get('source_url', 'unknown'))
    source_url = link_doc.get('source_url', 'unknown')
    
    # If user_id is not in the link_doc but is passed as a parameter, use the parameter
    if 'user_id' not in link_doc and user_id:
        link_doc_user_id = user_id
        print(f"Using passed user_id: {user_id}")
    else:
        link_doc_user_id = link_doc.get('user_id')
        print(f"Using link_doc user_id: {link_doc_user_id}")
        
    # Ensure we have a valid user_id
    if not link_doc_user_id and not user_id:
        error_msg = "No valid user_id found for link"
        print(f"Error: {error_msg}")
        return {
            'status': 'error',
            'link': link,
            'error': error_msg,
            'top_level_source': top_level_source
        }
    
    # Use the passed user_id if link_doc_user_id is not available
    if not link_doc_user_id:
        link_doc_user_id = user_id

    cursor = None
    try:
        cursor = connection.cursor()
        
        print(f"Got connection and cursor. Starting to scrape content from {link}")
        
        # Add user agent to avoid being blocked
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Make request to the URL with increased timeout
        print(f"Making HTTP request to: {link}")
        try:
            response = requests.get(link, headers=headers, timeout=60)
            response.raise_for_status()
            print(f"HTTP response status: {response.status_code}")
        except requests.exceptions.RequestException as req_error:
            print(f"Request error: {str(req_error)}")
            # Update the link record to mark it as processed with error
            cursor.execute(f"""
                UPDATE {LINKS_TO_SCRAP_TABLE} SET
                    IS_PROCESSED = 'Failed',
                    PROCESSED_AT = CURRENT_TIMESTAMP,
                    ERROR = :error,
                    USER_ID = :user_id,
                    TOP_LEVEL_SOURCE = :top_level_source
                WHERE ID = :id
            """,
                id=link_doc['id'],
                error=f"Request error: {str(req_error)}"[:4000],  # Limit for Oracle CLOB
                user_id=link_doc_user_id,
                top_level_source=top_level_source
            )
            connection.commit()
            raise
        
        # Parse the HTML content
        print("Parsing HTML content")
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get title
        title = soup.find('title')
        title_text = title.get_text().strip() if title else "Unknown Title"
        print(f"Page title: {title_text}")
        
        # Extract text based on the site type
        if is_wiki:
            print("Wiki page detected, using specialized extraction")
            # For Wikipedia, focus on the content div
            content_div = soup.find('div', {'id': 'mw-content-text'})
            if content_div:
                # Remove unwanted elements
                for unwanted in content_div.select('.thumb, .navbox, .infobox, table'):
                    if unwanted:
                        unwanted.extract()
                
                # Extract text from paragraphs
                paragraphs = content_div.find_all(['p', 'h2', 'h3', 'h4', 'h5', 'h6'])
                text_parts = []
                
                for p in paragraphs:
                    text = p.get_text().strip()
                    if text:
                        if p.name.startswith('h'):
                            text_parts.append(f"\n## {text}\n")
                        else:
                            text_parts.append(text)
                
                text = "\n\n".join(text_parts)
                text = f"# {title_text}\n\n{text}"
            else:
                print("Wiki content div not found, falling back to standard extraction")
                # Fallback to standard extraction
                for script in soup(["script", "style"]):
                    script.extract()
                text = soup.get_text(separator=' ', strip=True)
        else:
            print("Standard page extraction")
            # Standard extraction for non-Wikipedia sites
            for script in soup(["script", "style"]):
                script.extract()
            
            # Get text and clean it
            text = soup.get_text(separator=' ', strip=True)
            
            # Add title to the beginning
            text = f"# {title_text}\n\n{text}"
        
        # Remove excessive whitespace
        text = re.sub(r'\n\s*\n', '\n\n', text)
        
        # Calculate word count
        word_count = calculate_word_count(text)
        
        print(f"Extracted text length: {len(text)} characters, {word_count} words")
        
        # Check if content already exists to avoid duplicates
        cursor.execute(f"""
            SELECT COUNT(*) FROM {SCRAPPED_TEXT_TABLE}
            WHERE CONTENT_LINK = :link AND USER_ID = :user_id
        """, link=link, user_id=link_doc_user_id)
        
        if cursor.fetchone()[0] > 0:
            print(f"Content already exists for {link}, skipping insertion")
            
            # Get the content ID
            cursor.execute(f"""
                SELECT ID FROM {SCRAPPED_TEXT_TABLE}
                WHERE CONTENT_LINK = :link AND USER_ID = :user_id
            """, link=link, user_id=link_doc_user_id)
            
            content_id = cursor.fetchone()[0]
            
            # Update the word count
            cursor.execute(f"""
                UPDATE {SCRAPPED_TEXT_TABLE} 
                SET WORD_COUNT = :word_count
                WHERE ID = :id
            """, word_count=word_count, id=content_id)
            
            connection.commit()
            print(f"Updated word count ({word_count}) for existing content ID: {content_id}")
        else:
            # Insert into content collection
            print(f"Inserting content into database for {link}")
            try:
                # Create a variable to hold the returned ID
                content_id_var = cursor.var(int)
                
                cursor.execute(f"""
                    INSERT INTO {SCRAPPED_TEXT_TABLE} (
                        SCRAPPED_CONTENT, CONTENT_LINK, SCRAPE_DATE, LINK_ID,
                        SOURCE_URL, TOP_LEVEL_SOURCE, DEPTH, TITLE, USER_ID, WORD_COUNT
                    ) VALUES (
                        :content, :link, CURRENT_TIMESTAMP, :link_id,
                        :source_url, :top_level_source, :depth, :title, :user_id, :word_count
                    ) RETURNING ID INTO :content_id
                """,
                    content=text,
                    link=link,
                    link_id=link_doc['id'],
                    source_url=source_url,
                    top_level_source=top_level_source,
                    depth=link_doc.get('depth', 0),
                    title=title_text[:1000],  # Limit to column size
                    user_id=link_doc_user_id,
                    word_count=word_count,
                    content_id=content_id_var
                )

                content_id = content_id_var.getvalue()[0]
                connection.commit()
                print(f"Content inserted with ID: {content_id}, word count: {word_count}")
                
            except oracledb.DatabaseError as db_error:
                error, = db_error.args
                print(f"Database error inserting content: {str(error)}")
                traceback.print_exc()
                raise
        
        # Update the link as processed
        print(f"Updating link status to processed for {link}")
        try:
            cursor.execute(f"""
                UPDATE {LINKS_TO_SCRAP_TABLE} SET
                    IS_PROCESSED = 'true',
                    PROCESSED_AT = CURRENT_TIMESTAMP,
                    TOP_LEVEL_SOURCE = :top_level_source,
                    USER_ID = :user_id
                WHERE ID = :id
            """,
                id=link_doc['id'],
                top_level_source=top_level_source,
                user_id=link_doc_user_id
            )
            
            connection.commit()
            print(f"Link status update result: Link marked as processed")
        except oracledb.DatabaseError as update_error:
            error, = update_error.args
            print(f"Database error updating link status: {str(error)}")
            traceback.print_exc()
            raise
        
        print(f"Successfully processed {link}")
        return {
            'status': 'success',
            'link': link,
            'content_length': len(text),
            'word_count': word_count,
            'title': title_text,
            'content_id': str(content_id),
            'top_level_source': top_level_source,
            'source_url': source_url
        }
    
    except requests.exceptions.RequestException as e:
        error_msg = f"Request error: {str(e)}"
        print(f"Failed to scrape {link}: {error_msg}")
        traceback.print_exc()
        
        # Update the link as failed
        try:
            if cursor:
                cursor.execute(f"""
                    UPDATE {LINKS_TO_SCRAP_TABLE} SET
                        IS_PROCESSED = 'Failed',
                        PROCESSED_AT = CURRENT_TIMESTAMP,
                        ERROR = :error,
                        USER_ID = :user_id,
                        TOP_LEVEL_SOURCE = :top_level_source
                    WHERE ID = :id
                """,
                    id=link_doc['id'],
                    error=error_msg[:4000],  # Limit for Oracle CLOB
                    user_id=link_doc_user_id,
                    top_level_source=top_level_source
                )
                connection.commit()
        except Exception as update_error:
            print(f"Error updating link status: {str(update_error)}")
        
        return {
            'status': 'error',
            'link': link,
            'error': error_msg,
            'top_level_source': top_level_source
        }
    
    except Exception as e:
        error_msg = f"Processing error: {str(e)}"
        tb = traceback.format_exc()
        print(f"Failed to scrape {link}: {error_msg}")
        print(tb)
        
        # Update the link as failed
        try:
            if cursor:
                cursor.execute(f"""
                    UPDATE {LINKS_TO_SCRAP_TABLE} SET
                        IS_PROCESSED = 'Failed',
                        PROCESSED_AT = CURRENT_TIMESTAMP,
                        ERROR = :error,
                        TRACEBACK = :traceback,
                        USER_ID = :user_id,
                        TOP_LEVEL_SOURCE = :top_level_source
                    WHERE ID = :id
                """,
                    id=link_doc['id'],
                    error=error_msg[:4000],  # Limit for Oracle CLOB
                    traceback=tb[:4000],     # Limit for Oracle CLOB
                    user_id=link_doc_user_id,
                    top_level_source=top_level_source
                )
                connection.commit()
        except Exception as update_error:
            print(f"Error updating link status: {str(update_error)}")
        
        return {
            'status': 'error',
            'link': link,
            'error': error_msg,
            'traceback': tb,
            'top_level_source': top_level_source
        }
    finally:
        if cursor:
            cursor.close()

def continuous_processing_job(top_level_source_url, stop_event, delay_seconds=5, user_id=None):
    """
    Worker function to continuously process all the scraped links
    with improved batch processing and reduced delays.
    
    Args:
        top_level_source_url: The top-level source URL
        stop_event: Event to signal stopping the process
        delay_seconds: Initial delay before starting processing (reduced from 60 to 5)
        user_id: Optional user ID
    """
    try:
        print(f"Starting processing job for {top_level_source_url} with {delay_seconds}s delay for user {user_id}")
        
        # Wait for the specified delay before starting processing
        print(f"Waiting {delay_seconds} seconds before starting processing...")
        
        # Wait either for the delay or until the stop event is set
        if stop_event.wait(delay_seconds):
            print(f"Processing job for {top_level_source_url} was stopped during delay")
            return
            
        print(f"Delay complete, beginning processing for {top_level_source_url} for user {user_id}")
        
        # Stats counters
        links_processed = 0
        success_count = 0
        error_count = 0
        consecutive_empty_cycles = 0
        max_consecutive_empty_cycles = 5  # Allow more empty cycles before exiting
        
        # Batch size for processing - process more links at once
        batch_size = 10  # Process more links per batch for better efficiency
        
        # Keep processing until explicitly stopped
        while not stop_event.is_set():
            try:
                # Create a new DB connection for each iteration to prevent connection timeouts
                with get_db_connection() as connection:
                    with connection.cursor() as cursor:
                        # Find unprocessed links for this source URL
                        query = """
                            SELECT ID, LINK, NVL(DEPTH, 0) AS DEPTH, TOP_LEVEL_SOURCE, SOURCE_URL, USER_ID
                            FROM {0}
                            WHERE (IS_PROCESSED = 'false' OR IS_PROCESSED IS NULL)
                            AND TOP_LEVEL_SOURCE = :source_url
                        """.format(LINKS_TO_SCRAP_TABLE)
                        
                        # Add user_id filter if provided
                        params = {"source_url": top_level_source_url}
                        if user_id:
                            query += " AND USER_ID = :user_id"
                            params["user_id"] = user_id
                        
                        # Add row limit
                        query += " FETCH FIRST {0} ROWS ONLY".format(batch_size)
                        
                        # First, check the count to avoid unnecessary cursor creation
                        count_query = """
                            SELECT COUNT(*) FROM {0}
                            WHERE (IS_PROCESSED = 'false' OR IS_PROCESSED IS NULL)
                            AND TOP_LEVEL_SOURCE = :source_url
                        """.format(LINKS_TO_SCRAP_TABLE)
                        
                        if user_id:
                            count_query += " AND USER_ID = :user_id"
                        
                        cursor.execute(count_query, params)
                        unprocessed_count = cursor.fetchone()[0]
                        
                        if unprocessed_count == 0:
                            # No unprocessed links found, wait and check again
                            consecutive_empty_cycles += 1
                            print(f"No unprocessed links found. Empty cycle #{consecutive_empty_cycles}/{max_consecutive_empty_cycles}")
                            
                            if consecutive_empty_cycles >= max_consecutive_empty_cycles:
                                print(f"Reached maximum consecutive empty cycles ({max_consecutive_empty_cycles}). Exiting processing job.")
                                break
                            
                            # Wait for a short time before checking again - reduced from 5 seconds to 2
                            sleep_time = 2
                            print(f"Waiting {sleep_time} seconds before checking for new links...")
                            
                            # Use stop_event.wait instead of time.sleep to respond to stop events
                            if stop_event.wait(sleep_time):
                                print("Stop event detected during wait. Exiting processing job.")
                                break
                            
                            continue
                        
                        # Reset the consecutive empty cycles counter since we found links
                        consecutive_empty_cycles = 0
                        
                        # Get a batch of unprocessed links
                        cursor.execute(query, params)
                        unprocessed_links = cursor.fetchall()
                        print(f"Processing batch of {len(unprocessed_links)} links")
                        
                        batch_processed = 0
                        batch_success = 0
                        batch_errors = 0
                        
                        # Process each link in the batch
                        for link_row in unprocessed_links:
                            if stop_event.is_set():
                                print(f"Stop event triggered. Exiting processing job for {top_level_source_url}.")
                                break
                            
                            try:
                                link_id, link_url, depth, top_level, source, link_user_id = link_row
                                
                                # Ensure link has user_id before processing
                                effective_user_id = link_user_id or user_id
                                
                                if not effective_user_id:
                                    print(f"No user_id available for link: {link_url}, skipping")
                                    continue
                                    
                                if not link_user_id and user_id:
                                    # Update the document in the database to include user_id
                                    cursor.execute("""
                                        UPDATE {0} SET USER_ID = :user_id
                                        WHERE ID = :id
                                    """.format(LINKS_TO_SCRAP_TABLE), id=link_id, user_id=user_id)
                                    
                                    connection.commit()
                                
                                # Create a link_doc dictionary to match the interface
                                link_doc = {
                                    'id': link_id,
                                    'link': link_url,
                                    'depth': depth,
                                    'top_level_source': top_level,
                                    'source_url': source,
                                    'user_id': effective_user_id
                                }
        
                                # Process the link
                                result = scrape_single_link(connection, link_doc, effective_user_id)
                                
                                if result['status'] == 'success':
                                    batch_success += 1
                                    success_count += 1
                                else:
                                    # Already marked as failed in scrape_single_link
                                    batch_errors += 1
                                    error_count += 1
                                
                                batch_processed += 1
                                links_processed += 1
                                
                            except Exception as e:
                                # Catch any unexpected errors during processing
                                error_msg = f"Unexpected error processing link {link_url}: {str(e)}"
                                print(error_msg)
                                tb = traceback.format_exc()
                                traceback.print_exc()
                                
                                # Mark the link as failed
                                try:
                                    cursor.execute("""
                                        UPDATE {0} SET
                                            IS_PROCESSED = 'Failed',
                                            PROCESSED_AT = CURRENT_TIMESTAMP,
                                            ERROR = :error,
                                            TRACEBACK = :traceback,
                                            USER_ID = :user_id
                                        WHERE ID = :id
                                    """.format(LINKS_TO_SCRAP_TABLE),
                                        id=link_id,
                                        error=error_msg[:4000],
                                        traceback=tb[:4000],
                                        user_id=effective_user_id
                                    )
                                    connection.commit()
                                except Exception as update_error:
                                    print(f"Error updating link status: {str(update_error)}")
                                
                                batch_errors += 1
                                error_count += 1
                                batch_processed += 1
                                links_processed += 1
                            
                            # Much shorter sleep between links
                            time.sleep(0.1)
                        
                        print(f"Batch complete. Processed {batch_processed} links ({batch_success} successful, {batch_errors} errors).")
                        
                        # If the batch was smaller than the batch size, take a very short break before checking again
                        if batch_processed < batch_size:
                            sleep_time = 1  # Reduced from 3 seconds to 1
                            print(f"Processed less than batch size. Waiting {sleep_time} seconds before continuing...")
                            if stop_event.wait(sleep_time):
                                print("Stop event detected during wait. Exiting processing job.")
                                break
                
            except Exception as batch_error:
                print(f"Error processing batch: {str(batch_error)}")
                traceback.print_exc()
                
                # Shorter sleep before retrying
                time.sleep(2)
        
        print(f"Processing job completed or stopped: {links_processed} links processed, {success_count} successful, {error_count} failed")
        
        # Return stats if we exit the loop
        return {
            'links_processed': links_processed,
            'success_count': success_count,
            'error_count': error_count
        }
            
    except Exception as e:
        print(f"Error in continuous processing job: {str(e)}")
        traceback.print_exc()
        return {
            'error': str(e),
            'traceback': traceback.format_exc()
        }
               
@file_api.route('/recursive-crawl', methods=['POST'])
@token_required
def recursive_crawl(user_id, *args, **kwargs):  # Allow additional args
    try:
        print(f"Starting recursive crawl for user ID: {user_id}")
        
        # Get the URL and optional page limit from the request body
        data = request.get_json()
        print(f"Request data: {data}")
        
        if not data or 'url' not in data:
            print("URL is missing in request body")
            return standardize_error_response('URL is required in the POST body.', 'MISSING_URL', 400)
        
        # Get the top-level source URL provided by the user
        top_level_source_url = data['url']
        
        # Get the page limit with validation
        page_limit = data.get('limit', 10)  # Default to 10 if not provided
        
        # Validate page_limit is an integer and within allowed range
        try:
            page_limit = int(page_limit)
            if page_limit < 0:
                page_limit = 0  # No limit
            elif page_limit > 5000:
                page_limit = 5000  # Max allowed
        except (ValueError, TypeError):
            print(f"Invalid page limit: {page_limit}, using default of 10")
            page_limit = 10  # Default to 10 if invalid
            
        print(f"Top level source URL: {top_level_source_url}, Page limit: {page_limit}")
        
        # Validate URL format
        if not is_valid_url(top_level_source_url):
            print(f"Invalid URL format: {top_level_source_url}")
            return standardize_error_response(f'Invalid URL format: {top_level_source_url}', 'INVALID_URL', 400)
        
        # Perform immediate initial crawl to make the system feel more responsive
        initial_links = []
        try:
            # Add user agent to avoid being blocked
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            # Make immediate request to the URL with short timeout
            print(f"Making immediate HTTP request to: {top_level_source_url}")
            response = requests.get(top_level_source_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                # Extract the domain for same-domain filtering
                original_domain = extract_domain(top_level_source_url)
                
                # Get immediate links from the page
                soup = BeautifulSoup(response.text, 'html.parser')
                all_links = soup.find_all('a', href=True)
                
                # Quick extract of valid links
                for link in all_links:
                    href = link['href'].strip()
                    if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                        continue
                        
                    try:
                        full_url = urljoin(top_level_source_url, href)
                        link_domain = extract_domain(full_url)
                        
                        if link_domain != original_domain:
                            continue
                            
                        if not is_valid_url(full_url) or not is_valid_content_url(full_url):
                            continue
                            
                        initial_links.append(full_url)
                    except:
                        continue
                        
                initial_links = list(set(initial_links))
                print(f"Quick initial crawl found {len(initial_links)} links")
        except Exception as initial_error:
            print(f"Initial crawl attempt encountered error: {str(initial_error)}")
            # Continue with normal process even if initial crawl fails

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Check if the URL has already been crawled completely
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), source_url=top_level_source_url, user_id=user_id)
                
                links_count = cursor.fetchone()[0]
                
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url 
                    AND (IS_CRAWLED = 0 OR IS_CRAWLED IS NULL)
                    AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), source_url=top_level_source_url, user_id=user_id)
                
                uncrawled_count = cursor.fetchone()[0]
                
                print(f"Found {links_count} total links and {uncrawled_count} uncrawled links")
                
                if links_count > 0 and uncrawled_count == 0:
                    print(f"URL already completely crawled: {top_level_source_url}")
                    return jsonify({
                        'status': 'info',
                        'message': f'URL {top_level_source_url} has already been completely crawled.',
                        'stats': {
                            'total_links': links_count
                        },
                        'timestamp': datetime.now().isoformat()
                    })
                
                # Check if the URL exists in Links_to_scrap
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE LINK = :link AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), link=top_level_source_url, user_id=user_id)
                
                existing_link_count = cursor.fetchone()[0]
                print(f"Existing link found: {existing_link_count > 0}")
                
                # If the URL is not in Links_to_scrap, add it as a new starting point
                if existing_link_count == 0:
                    try:
                        print(f"Inserting initial URL to Links_to_scrap: {top_level_source_url}")
                        cursor.execute("""
                            INSERT INTO {0} (
                                LINK, ADDED_AT, IS_CRAWLED, IS_PROCESSED, 
                                DEPTH, SOURCE_URL, TOP_LEVEL_SOURCE, USER_ID
                            ) VALUES (
                                :link, CURRENT_TIMESTAMP, 0, 'false',
                                0, :source_url, :top_level_source, :user_id
                            )
                        """.format(LINKS_TO_SCRAP_TABLE),
                            link=top_level_source_url,
                            source_url=top_level_source_url,
                            top_level_source=top_level_source_url,
                            user_id=user_id
                        )
                        
                        connection.commit()
                        print(f"URL inserted successfully: {top_level_source_url}")
                        
                    except Exception as e:
                        print(f"Error inserting URL: {str(e)}")
                        traceback.print_exc()
                        raise
                
                # Add any initial links we found (immediately improves responsiveness)
                if initial_links:
                    inserted_count = 0
                    unique_links = list(set(initial_links))[:50]  # Limit to first 50 for quick start
                    print(f"Inserting {len(unique_links)} initial links found during quick crawl")
                    
                    link_values = []
                    for link in unique_links:
                        link_values.append({
                            'link': link,
                            'top_level_source': top_level_source_url,
                            'user_id': user_id,
                            'source_url': top_level_source_url,
                            'depth': 1,
                            'has_text': 1 if contains_text_in_url(link) else 0
                        })
                    
                    # Use batch insert for better performance
                    if link_values:
                        inserted_count = batch_insert_links(connection, link_values)
                        print(f"Initially inserted {inserted_count} links during quick crawl")
                
                # Save the source URL and timestamp in the Source_Urls collection
                try:
                    print(f"Inserting/updating source URL in {SOURCE_URLS_TABLE}: {top_level_source_url}")
                    
                    # First check if the document already exists
                    cursor.execute("""
                        SELECT COUNT(*) FROM {0}
                        WHERE SOURCE_URL = :source_url AND USER_ID = :user_id
                    """.format(SOURCE_URLS_TABLE), source_url=top_level_source_url, user_id=user_id)
                    
                    existing_source_count = cursor.fetchone()[0]
                    
                    if existing_source_count > 0:
                        # If exists, update the timestamp and page limit
                        cursor.execute("""
                            UPDATE {0} SET 
                                TIMESTAMP = CURRENT_TIMESTAMP, 
                                PAGE_LIMIT = :page_limit
                            WHERE SOURCE_URL = :source_url AND USER_ID = :user_id
                        """.format(SOURCE_URLS_TABLE),
                            page_limit=page_limit,
                            source_url=top_level_source_url,
                            user_id=user_id
                        )
                        print(f"Updated timestamp and page limit for existing source URL record")
                    else:
                        # If not exists, create new entry
                        cursor.execute("""
                            INSERT INTO {0} (
                                SOURCE_URL, USER_ID, TIMESTAMP, PAGE_LIMIT
                            ) VALUES (
                                :source_url, :user_id, CURRENT_TIMESTAMP, :page_limit
                            )
                        """.format(SOURCE_URLS_TABLE),
                            source_url=top_level_source_url,
                            user_id=user_id,
                            page_limit=page_limit
                        )
                        print(f"Created new source URL record")
                        
                    connection.commit()
                        
                except Exception as e:
                    print(f"Error inserting source URL: {str(e)}")
                    traceback.print_exc()
                    raise
        
        # Check if user has an active job
        if user_has_active_job(user_id):
            print(f"User {user_id} already has an active job. Adding URL to queue: {top_level_source_url}")
            
            # Add the URL to the queue with page limit
            queue_doc = {
                'page_limit': page_limit
            }
            queue_result = add_to_queue(user_id, top_level_source_url, queue_doc)
            
            if queue_result:
                return jsonify({
                    'status': 'queued',
                    'message': f'URL {top_level_source_url} has been added to the processing queue with a limit of {page_limit} pages.',
                    'initial_links_found': len(initial_links),
                    'timestamp': datetime.now().isoformat(),
                    'source_url': top_level_source_url,
                    'page_limit': page_limit
                })
            else:
                return standardize_error_response(f'Failed to add URL {top_level_source_url} to the queue.', 'QUEUE_ERROR', 500)
        else:
            print(f"No active job for user {user_id}. Starting crawling for: {top_level_source_url} with limit: {page_limit}")
            
            # Start crawling and processing with the specified page limit
            result = start_crawling_and_processing(user_id, top_level_source_url, page_limit)
            
            if result:
                return jsonify({
                    'status': 'success',
                    'message': f'Continuous crawling started for {top_level_source_url} with a limit of {page_limit} pages',
                    'initial_links_found': len(initial_links),
                    'timestamp': datetime.now().isoformat(),
                    'source_url': top_level_source_url,
                    'page_limit': page_limit
                })
            else:
                return standardize_error_response(f'Failed to start crawling for {top_level_source_url}', 'CRAWL_ERROR', 500)
    
    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error in crawling: {str(e)}\n{traceback_str}")
        return standardize_error_response(str(e), 'SERVER_ERROR', 500)
    
@file_api.route('/progress-bar', methods=['GET'])
def get_progress_bar():
    """
    Get the progress information for crawling and scraping operations
    to display in a progress bar in the frontend.
    """
    try:
        # Get source URL from query parameters
        source_url = request.args.get('source_url')

        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Get the last progress record for this source URL
                cursor.execute("""
                    SELECT 
                        CRAWL_PROGRESS, SCRAPE_PROGRESS, CRAWLED_COUNT, SCRAPED_COUNT, 
                        TOTAL_LINKS, OPERATION_STATUS, TO_CHAR(TIMESTAMP, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as TIMESTAMP
                    FROM {0}
                    WHERE SOURCE_URL = :source_url
                    ORDER BY TIMESTAMP DESC
                    FETCH FIRST 1 ROW ONLY
                """.format(PROGRESS_HISTORY_TABLE), source_url=source_url)
                
                last_progress_row = cursor.fetchone()
                last_progress = None
                if last_progress_row:
                    last_progress = {
                        'crawl_progress': last_progress_row[0],
                        'scrape_progress': last_progress_row[1],
                        'crawled_count': last_progress_row[2],
                        'scraped_count': last_progress_row[3],
                        'total_links': last_progress_row[4],
                        'operation_status': last_progress_row[5],
                        'timestamp': last_progress_row[6]
                    }

                # Get total links for this source URL
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url)
                
                total_links = cursor.fetchone()[0]

                # Skip calculation if no links found
                if total_links == 0:
                    current_time = datetime.datetime.now()
                    return jsonify({
                        'status': 'pending',
                        'message': f'No links found for {source_url}',
                        'crawl_progress': 0,
                        'scrape_progress': 0,
                        'crawled_count': 0,
                        'total_links': 0,
                        'scraped_count': 0,
                        'change_since_last': {
                            'crawl_progress_change': 0,
                            'scrape_progress_change': 0,
                            'links_per_minute': 0,
                            'scrape_per_minute': 0,
                            'time_since_last': 0,
                            'estimated_completion_time': None,
                            'estimated_completion_minutes': None,
                            'estimated_time_readable': 'Unknown'
                        },
                        'timestamp': current_time.isoformat()
                    })

                # Calculate crawling progress
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND IS_CRAWLED = 1
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url)
                
                crawled_count = cursor.fetchone()[0]
                crawl_progress = round((crawled_count / total_links) * 100, 1) if total_links > 0 else 0

                # Calculate scraping progress based on is_processed field
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND IS_PROCESSED = 'true'
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url)
                
                scraped_count = cursor.fetchone()[0]
                scrape_progress = round((scraped_count / total_links) * 100, 1) if total_links > 0 else 0

                # Extract user_id from the links collection if available
                cursor.execute("""
                    SELECT DISTINCT USER_ID FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url
                    FETCH FIRST 1 ROW ONLY
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url)
                
                user_row = cursor.fetchone()
                user_id = user_row[0] if user_row else None
                
                # Check if this URL is in the queue
                queue_status = None
                in_queue = False
                queue_position = None
                is_active = False
                
                if user_id:
                    # Check if it's in the active jobs list
                    if user_id in active_user_jobs and source_url in active_user_jobs[user_id]:
                        is_active = True
                    
                    # Check if it's in the queue
                    cursor.execute("""
                        SELECT ID, PROCESSED, PROCESSING_STARTED
                        FROM {0}
                        WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
                    """.format(PROCESSING_QUEUE_TABLE), user_id=user_id, source_url=source_url)
                    
                    queue_row = cursor.fetchone()
                    if queue_row:
                        queue_id, is_processed, is_processing = queue_row
                        in_queue = True
                        
                        # If it's not actively processing, get its position in the queue
                        if is_processed == 0 and is_processing is None:
                            cursor.execute("""
                                SELECT COUNT(*) FROM {0}
                                WHERE USER_ID = :user_id
                                AND PROCESSED = 0
                                AND PROCESSING_STARTED IS NULL
                                AND ADDED_AT < (
                                    SELECT ADDED_AT FROM {0}
                                    WHERE ID = :queue_id
                                )
                            """.format(PROCESSING_QUEUE_TABLE), user_id=user_id, queue_id=queue_id)
                            
                            ahead_count = cursor.fetchone()[0]
                            queue_position = ahead_count + 1

                # Determine the overall status
                if is_active:
                    if crawled_count < total_links:
                        status = 'crawling'
                    else:
                        status = 'processing'
                elif in_queue:
                    status = 'queued'
                elif crawl_progress >= 100 and scrape_progress >= 100:
                    status = 'completed'
                else:
                    status = 'pending'

                # Save current progress to history
                cursor.execute("""
                    INSERT INTO {0} (
                        SOURCE_URL, TIMESTAMP, CRAWL_PROGRESS, SCRAPE_PROGRESS,
                        CRAWLED_COUNT, SCRAPED_COUNT, TOTAL_LINKS, OPERATION_STATUS
                    ) VALUES (
                        :source_url, CURRENT_TIMESTAMP, :crawl_progress, :scrape_progress,
                        :crawled_count, :scraped_count, :total_links, :operation_status
                    )
                """.format(PROGRESS_HISTORY_TABLE),
                    source_url=source_url,
                    crawl_progress=crawl_progress,
                    scrape_progress=scrape_progress,
                    crawled_count=crawled_count,
                    scraped_count=scraped_count,
                    total_links=total_links,
                    operation_status=status
                )

                # Return the response
                return jsonify({
                    'status': 'success',
                    'operation_status': status,
                    'crawl_progress': crawl_progress,
                    'scrape_progress': scrape_progress,
                    'crawled_count': crawled_count,
                    'total_links': total_links,
                    'scraped_count': scraped_count,
                    'is_active': is_active,
                    'in_queue': in_queue,
                    'queue_position': queue_position,
                    'timestamp': datetime.now().isoformat()
                })

    except Exception as e:
        print(f"Error in progress_bar: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'error_details': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500

@file_api.route('/process-all-links', methods=['POST'])
@token_required
def process_all_links(user_id):
    """
    Start a background thread to continuously process all links in the Links_to_scrap collection
    until all links are processed (is_processed: true).
    Includes an option to delay the start of processing.
    If a job is already active for the user, the new URL will be queued.
    """
    try:
        print(f"Starting process_all_links for user ID: {user_id}")
        
        # Get the delay parameter (default: 60 seconds) and source_url
        data = request.get_json() or {}
        delay_seconds = data.get('delay', 5)  # Reduced from 60 to 5 seconds for better performance
        source_url = data.get('source_url')
        
        print(f"Request data: delay={delay_seconds}, source_url={source_url}")
        
        if not source_url:
            print("source_url is missing in request body")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Check how many unprocessed links exist for this user and source
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE (IS_PROCESSED = 'false' OR IS_PROCESSED IS NULL)
                    AND TOP_LEVEL_SOURCE = :source_url
                    AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url, user_id=user_id)
                
                unprocessed_count = cursor.fetchone()[0]
                
                print(f"Found {unprocessed_count} unprocessed links for source: {source_url}, user: {user_id}")
                
                if unprocessed_count == 0:
                    print(f"No unprocessed links found for {source_url}")
                    return jsonify({
                        'status': 'complete',
                        'message': f'No unprocessed links found for {source_url}',
                        'timestamp': datetime.now().isoformat()
                    })
        
        # Check if user has an active job
        if user_has_active_job(user_id):
            print(f"User {user_id} already has an active job. Adding URL to queue: {source_url}")
            
            # Add the URL to the queue
            queue_result = add_to_queue(user_id, source_url)
            
            if queue_result:
                return jsonify({
                    'status': 'queued',
                    'message': f'URL {source_url} has been added to the processing queue.',
                    'timestamp': datetime.now().isoformat(),
                    'source_url': source_url
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': f'Failed to add URL {source_url} to the queue.',
                    'timestamp': datetime.now().isoformat()
                }), 500
        else:
            print(f"No active job for user {user_id}. Starting processing for: {source_url} with delay: {delay_seconds}s")
            
            # Start processing with the specified delay
            result = start_crawling_and_processing(user_id, source_url)
            
            if result:
                return jsonify({
                    'status': 'success',
                    'message': f'Continuous processing started for {source_url} with a delay of {delay_seconds} seconds',
                    'timestamp': datetime.now().isoformat(),
                    'source_url': source_url
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': f'Failed to start processing for {source_url}',
                    'timestamp': datetime.now().isoformat()
                }), 500
    
    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error in processing: {str(e)}\n{traceback_str}")
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback_str,
            'timestamp': datetime.now().isoformat()
        }), 500
    
@file_api.route('/source-url-status', methods=['GET'])
@token_required
def get_source_url_status(user_id):
    """Get the status of a specific source URL for the authenticated user"""
    try:
        print(f"Getting source URL status for user: {user_id}")
        
        # Get source_url from request args
        source_url = request.args.get('source_url')
            
        if not source_url:
            print("source_url parameter is missing")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        print(f"Checking status for source URL: {source_url}")
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Count total URLs associated with this source for this user
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url, user_id=user_id)
                
                total_urls = cursor.fetchone()[0]
                
                # Count successfully processed URLs for this source
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url 
                    AND IS_PROCESSED = 'true'
                    AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url, user_id=user_id)
                
                successful_processed = cursor.fetchone()[0]
                
                # Count failed processed URLs for this source
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url 
                    AND IS_PROCESSED = 'Failed'
                    AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url, user_id=user_id)
                
                failed_processed = cursor.fetchone()[0]
                
                # Calculate total processed (successful + failed)
                total_processed = successful_processed + failed_processed
                
                # Count scraped URLs for this source
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                """.format(SCRAPPED_TEXT_TABLE), source_url=source_url, user_id=user_id)
                
                total_scrapped = cursor.fetchone()[0]
                
                print(f"Stats: Total URLs: {total_urls}, Successful: {successful_processed}, Failed: {failed_processed}, Total Processed: {total_processed}, Scrapped: {total_scrapped}")
                
                # Check if URL is in the queue
                cursor.execute("""
                    SELECT COUNT(*), MIN(PROCESSED), MIN(PROCESSING_STARTED)
                    FROM {0}
                    WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
                """.format(PROCESSING_QUEUE_TABLE), user_id=user_id, source_url=source_url)
                
                queue_count, is_processed, is_processing = cursor.fetchone()
                queue_item_exists = queue_count > 0
                
                # Determine the status
                if total_urls == 0:
                    # If no URLs are found for this source, it's pending
                    status = 'Pending'
                elif total_processed == total_urls:
                    # Only mark as "Completed" if all URLs are processed (either successfully or failed)
                    status = 'Completed'
                else:
                    # Check if it's actively being processed
                    is_active = False
                    if user_id in active_user_jobs and source_url in active_user_jobs[user_id]:
                        is_active = True
                        status = 'In Progress'
                    # Check if it's in the queue but not active
                    elif queue_item_exists and is_processed == 0 and is_processing is None:
                        status = 'Queued'
                    else:
                        # Otherwise, it's still pending
                        status = 'Pending'
                
                print(f"Source status determined as: {status}")
                
                # Get queue position if applicable
                queue_position = None
                if status == 'Queued' and queue_item_exists:
                    # Count how many unprocessed items are ahead in the queue
                    cursor.execute("""
                        SELECT COUNT(*) FROM {0} q1
                        WHERE q1.USER_ID = :user_id
                          AND q1.PROCESSED = 0
                          AND q1.PROCESSING_STARTED IS NULL
                          AND q1.ADDED_AT < (
                              SELECT q2.ADDED_AT FROM {0} q2
                              WHERE q2.USER_ID = :user_id 
                                AND q2.SOURCE_URL = :source_url
                                AND q2.PROCESSED = 0
                                AND q2.PROCESSING_STARTED IS NULL
                          )
                    """.format(PROCESSING_QUEUE_TABLE), user_id=user_id, source_url=source_url)
                    
                    ahead_count = cursor.fetchone()[0]
                    queue_position = ahead_count + 1  # Add 1 for human-readable position (1-based indexing)
                
                # Get page limit for this source
                cursor.execute("""
                    SELECT PAGE_LIMIT FROM {0}
                    WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
                """.format(SOURCE_URLS_TABLE), user_id=user_id, source_url=source_url)
                
                row = cursor.fetchone()
                page_limit = row[0] if row else None
                
        return jsonify({
            'status': 'success',
            'source_url': source_url,
            'data': {
                'status': status,
                'total_urls': total_urls,
                'successful_processed': successful_processed,
                'failed_processed': failed_processed,
                'total_processed': total_processed,
                'scraped_urls': total_scrapped,
                'queue_position': queue_position,
                'page_limit': page_limit
            },
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting source URL status: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500



@file_api.route('/get-scrapped-links', methods=['GET'])
@token_required
def realtime_scrapped_links(user_id):
    """Get the count of scraped links with optimized DB connection"""
    try:
        source_url = request.args.get('source_url')
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                query = """
                    SELECT COUNT(*) FROM {0}
                    WHERE USER_ID = :user_id
                """.format(SCRAPPED_TEXT_TABLE)
                
                params = {'user_id': user_id}
                
                if source_url:
                    query += " AND TOP_LEVEL_SOURCE = :source_url"
                    params['source_url'] = source_url
                    
                cursor.execute(query, params)
                scraped_count = cursor.fetchone()[0]
                
        return jsonify({
            'status': 'success',
            'scrapped_links': scraped_count,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting scraped links: {str(e)}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'DB_ERROR', 500)

@file_api.route('/get-pending-links', methods=['GET'])
@token_required
def realtime_pending_links(user_id):
    """Get the count of pending links with optimized DB connection"""
    try:
        source_url = request.args.get('source_url')
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                query = """
                    SELECT COUNT(*) FROM {0}
                    WHERE (IS_PROCESSED = 'false' OR IS_PROCESSED IS NULL)
                    AND USER_ID = :user_id
                """.format(LINKS_TO_SCRAP_TABLE)
                
                params = {'user_id': user_id}
                
                if source_url:
                    query += " AND TOP_LEVEL_SOURCE = :source_url"
                    params['source_url'] = source_url
                    
                cursor.execute(query, params)
                pending_count = cursor.fetchone()[0]
                
        return jsonify({
            'status': 'success',
            'pending_links': pending_count,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting pending links: {str(e)}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'DB_ERROR', 500)

@file_api.route('/get-total-words-scrapped', methods=['GET'])
@token_required
def realtime_total_words_scrapped(user_id):
    """Get the total word count with optimized DB query using the WORD_COUNT column"""
    try:
        source_url = request.args.get('source_url')
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Build the base query that directly uses the WORD_COUNT column
                query = """
                    SELECT SUM(WORD_COUNT) AS total_words
                    FROM {0}
                    WHERE USER_ID = :user_id
                """.format(SCRAPPED_TEXT_TABLE)
                
                params = {'user_id': user_id}
                
                if source_url:
                    query += " AND TOP_LEVEL_SOURCE = :source_url"
                    params['source_url'] = source_url
                    
                cursor.execute(query, params)
                row = cursor.fetchone()
                total_words = row[0] if row and row[0] else 0
                
                # Also get document count
                count_query = """
                    SELECT COUNT(*) 
                    FROM {0}
                    WHERE USER_ID = :user_id
                """.format(SCRAPPED_TEXT_TABLE)
                
                if source_url:
                    count_query += " AND TOP_LEVEL_SOURCE = :source_url"
                    
                cursor.execute(count_query, params)
                doc_count = cursor.fetchone()[0] or 0
                
        return jsonify({
            'status': 'success',
            'total_words': total_words,
            'document_count': doc_count,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting total words: {str(e)}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    
@file_api.route('/get-discovered-links', methods=['GET'])
@token_required
def get_discovered_links(user_id):
    """
    Get counts and statistics for discovered links associated with a parent source URL
    
    Query params:
    - source_url: The parent source URL to filter by
    """
    try:
        # Get source URL parameter
        source_url = request.args.get('source_url')
        
        # Validate required parameter
        if not source_url:
            return standardize_error_response('source_url parameter is required.', 'MISSING_PARAM', 400)
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Get comprehensive link statistics in a single query
                stats_query = f"""
                    SELECT 
                        COUNT(*) AS total_links,
                        SUM(CASE WHEN IS_CRAWLED = 1 THEN 1 ELSE 0 END) AS crawled_links,
                        SUM(CASE WHEN IS_PROCESSED = 'true' THEN 1 ELSE 0 END) AS processed_links,
                        SUM(CASE WHEN IS_PROCESSED = 'Failed' THEN 1 ELSE 0 END) AS failed_links,
                        SUM(CASE WHEN ERROR IS NOT NULL THEN 1 ELSE 0 END) AS error_count,
                        SUM(CASE WHEN HAS_TEXT_IN_URL = 1 THEN 1 ELSE 0 END) AS text_indicator_count
                    FROM {LINKS_TO_SCRAP_TABLE}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                """
                cursor.execute(stats_query, source_url=source_url, user_id=user_id)
                stats_row = cursor.fetchone()
                
                if not stats_row:
                    return jsonify({
                        'status': 'success',
                        'total_links': 0,
                        'source_url': source_url,
                        'timestamp': datetime.now().isoformat()
                    })
                
                # Get domain statistics with optimized query
                domain_query = """
                    SELECT 
                        CASE 
                            WHEN INSTR(LINK, '//') > 0 THEN 
                                LOWER(REGEXP_SUBSTR(SUBSTR(LINK, INSTR(LINK, '//') + 2), '[^/]+'))
                            ELSE 
                                LOWER(REGEXP_SUBSTR(LINK, '[^/]+'))
                        END AS DOMAIN,
                        COUNT(*) as COUNT
                    FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                    GROUP BY CASE 
                        WHEN INSTR(LINK, '//') > 0 THEN 
                            LOWER(REGEXP_SUBSTR(SUBSTR(LINK, INSTR(LINK, '//') + 2), '[^/]+'))
                        ELSE 
                            LOWER(REGEXP_SUBSTR(LINK, '[^/]+'))
                    END
                    ORDER BY COUNT DESC
                """.format(LINKS_TO_SCRAP_TABLE)
                
                cursor.execute(domain_query, source_url=source_url, user_id=user_id)
                domain_stats = [{'domain': row[0], 'count': row[1]} for row in cursor.fetchall()]
                
                # Build response with just the counts and statistics
                return jsonify({
                    'status': 'success',
                    'total_links': stats_row[0],
                    'source_url': source_url,
                    'stats': {
                        'total': stats_row[0],
                        'crawled': stats_row[1],
                        'processed': stats_row[2],
                        'failed': stats_row[3],
                        'error_count': stats_row[4],
                        'text_indicator_count': stats_row[5]
                    },
                    'crawl_progress': round((stats_row[1] / stats_row[0]) * 100, 1) if stats_row[0] > 0 else 0,
                    'processing_progress': round((stats_row[2] / stats_row[0]) * 100, 1) if stats_row[0] > 0 else 0,
                    'domain_stats': domain_stats,
                    'timestamp': datetime.now().isoformat()
                })
                
    except Exception as e:
        print(f"Error getting discovered links counts: {str(e)}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'SERVER_ERROR', 500)

@file_api.route('/scrapped-sub-links', methods=['POST'])
def scrapped_sub_links():
    """
    Fetch links related to a specific source URL with pagination
    Returns 10 URLs at a time with their processing status
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                'status': 'error',
                'message': 'Request body is required.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        source_url = data.get('source_url')
        page = data.get('page', 1)  # Default to page 1
        page_size = 10  # Fixed page size of 10 items
        
        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the request body.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Calculate pagination parameters
                offset = (page - 1) * page_size
                
                # Get total count for pagination
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :source_url
                """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url)
                
                total_links = cursor.fetchone()[0]
                
                # Fetch the paginated links
                cursor.execute(f"""
                    SELECT LINK, IS_PROCESSED 
                    FROM (
                        SELECT LINK, IS_PROCESSED, ROW_NUMBER() OVER (ORDER BY ID) as rn
                        FROM {LINKS_TO_SCRAP_TABLE}
                        WHERE TOP_LEVEL_SOURCE = :source_url
                    ) 
                    WHERE rn > :offset AND rn <= :end_row
                """, source_url=source_url, offset=offset, end_row=(offset + page_size))
                
                links_data = []
                
                for link_row in cursor.fetchall():
                    link, is_processed = link_row
                    
                    # Determine URL status
                    url_status = "Completed" if is_processed == 'true' else "Pending"
                    
                    # If is_processed is "Failed", mark as failed
                    if is_processed == 'Failed':
                        url_status = "Failed"
                        
                    links_data.append({
                        'url': link,
                        'url_status': url_status
                    })
                
                # Calculate pagination metadata
                total_pages = (total_links + page_size - 1) // page_size  # Ceiling division
                has_next = page < total_pages
                has_prev = page > 1
                
        return jsonify({
            'status': 'success',
            'data': links_data,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total_items': total_links,
                'total_pages': total_pages,
                'has_next': has_next,
                'has_prev': has_prev
            },
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        print(f"Error in scrapped_sub_links: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500

@file_api.route('/all-documents', methods=['GET'])
@token_required
def get_all_documents(user_id):
    """Get all document sources for a user with improved status determination"""
    try:
        print(f"Fetching all documents for user: {user_id}")

        # Ensure user_id is validated
        if isinstance(user_id, list):
            user_id = user_id[0]
        elif not isinstance(user_id, (int, str)):
            raise ValueError(f"Invalid user_id type: {type(user_id)}. Expected int or str.")

        # Get pagination parameters
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)

        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Fetch source URLs
                print(f"Querying {SOURCE_URLS_TABLE} for user: {user_id}")
                
                query = f"""
                    SELECT SOURCE_URL, 
                           TO_CHAR(TIMESTAMP, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as TIMESTAMP,
                           PAGE_LIMIT
                    FROM {SOURCE_URLS_TABLE}
                    WHERE USER_ID = :user_id
                    ORDER BY TIMESTAMP DESC
                    OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY
                """
                cursor.execute(query, {"user_id": user_id})
                source_urls = cursor.fetchall()
                
                print(f"Found {len(source_urls)} source URLs for user: {user_id}")

                if not source_urls:
                    return jsonify({
                        'status': 'success',
                        'documents': [],
                        'count': 0,
                        'timestamp': datetime.now().isoformat()
                    })

                # Extract source URLs for the IN clause
                source_url_list = [row[0] for row in source_urls]
                if not source_url_list:
                    return jsonify({
                        'status': 'success',
                        'documents': [],
                        'count': 0,
                        'timestamp': datetime.now().isoformat()
                    })

                # Construct a valid IN clause for Oracle
                url_in_clause = ", ".join(f"'{url}'" for url in source_url_list)

                # Query for link stats
                link_stats_query = f"""
                    SELECT 
                        TOP_LEVEL_SOURCE,
                        COUNT(*) as total_links,
                        SUM(CASE WHEN IS_CRAWLED = 1 THEN 1 ELSE 0 END) as crawled_links,
                        SUM(CASE WHEN IS_PROCESSED = 'true' THEN 1 ELSE 0 END) as processed_links,
                        SUM(CASE WHEN IS_PROCESSED = 'Failed' THEN 1 ELSE 0 END) as failed_links
                    FROM {LINKS_TO_SCRAP_TABLE}
                    WHERE USER_ID = :user_id
                    AND TOP_LEVEL_SOURCE IN ({url_in_clause})
                    GROUP BY TOP_LEVEL_SOURCE
                """
                cursor.execute(link_stats_query, {"user_id": user_id})
                link_stats = {row[0]: {'total': row[1], 'crawled': row[2], 'processed': row[3], 'failed': row[4]} for row in cursor.fetchall()}

                # Query for scrapped text stats
                scrapped_query = f"""
                    SELECT 
                        TOP_LEVEL_SOURCE,
                        COUNT(*) as scrapped_count
                    FROM {SCRAPPED_TEXT_TABLE}
                    WHERE USER_ID = :user_id
                    AND TOP_LEVEL_SOURCE IN ({url_in_clause})
                    GROUP BY TOP_LEVEL_SOURCE
                """
                cursor.execute(scrapped_query, {"user_id": user_id})
                scrapped_stats = {row[0]: row[1] for row in cursor.fetchall()}

                # Query for queue information
                queue_query = f"""
                    SELECT 
                        SOURCE_URL,
                        PROCESSED,
                        PROCESSING_STARTED
                    FROM {PROCESSING_QUEUE_TABLE}
                    WHERE USER_ID = :user_id
                    AND SOURCE_URL IN ({url_in_clause})
                """
                cursor.execute(queue_query, {"user_id": user_id})
                queue_info = {row[0]: {'processed': row[1], 'processing_started': row[2]} for row in cursor.fetchall()}

                # Construct documents array with more accurate status determination
                documents = []
                for source_url, timestamp, page_limit in source_urls:
                    # Default values if no stats found
                    total_links = 0
                    processed_count = 0
                    failed_count = 0
                    crawled_count = 0
                    scrapped_count = 0
                    
                    # Get stats if available
                    if source_url in link_stats:
                        stats = link_stats[source_url]
                        total_links = stats['total']
                        processed_count = stats['processed']
                        failed_count = stats['failed']
                        crawled_count = stats['crawled']
                    
                    if source_url in scrapped_stats:
                        scrapped_count = scrapped_stats[source_url]

                    # Calculate progress percentages
                    crawl_progress = round((crawled_count / total_links * 100), 1) if total_links > 0 else 0
                    scrape_progress = round((scrapped_count / total_links * 100), 1) if total_links > 0 else 0

                    # Check if URL is in queue
                    in_queue = source_url in queue_info
                    queue_item = queue_info.get(source_url, {})
                    is_processed = queue_item.get('processed', 0) == 1
                    is_processing = queue_item.get('processing_started') is not None

                    # Determine if URL is active job
                    is_active = user_id in active_user_jobs and source_url in active_user_jobs.get(user_id, set())

                    # New, more accurate status determination
                    if total_links == 0:
                        status = 'Pending'
                    # Only consider "Completed" if:
                    # 1. All links have been crawled (crawled_count == total_links)
                    # 2. All links have been either processed or failed (processed_count + failed_count == total_links)
                    # 3. We actually have some scraped content (scrapped_count > 0)
                    elif (crawled_count == total_links and 
                          processed_count + failed_count == total_links and 
                          scrapped_count > 0):
                        status = 'Completed'
                    elif is_active:
                        status = 'In Progress'
                    elif in_queue and not is_processed and not is_processing:
                        status = 'Queued'
                    else:
                        status = 'Processing'

                    # Create document object
                    document = {
                        'source_url': source_url,
                        'timestamp': timestamp,
                        'page_limit': page_limit,
                        'status': status,
                        'total_links': total_links,
                        'processed_links': processed_count,
                        'failed_links': failed_count,
                        'scrapped_links': scrapped_count,
                        'crawled_links': crawled_count,
                        'crawl_progress': crawl_progress,
                        'scrape_progress': scrape_progress,
                        'is_active': is_active,
                        'in_queue': in_queue,
                        'queue_position': None
                    }
                    documents.append(document)

                # Get the total count for pagination
                count_query = f"SELECT COUNT(*) FROM {SOURCE_URLS_TABLE} WHERE USER_ID = :user_id"
                cursor.execute(count_query, {"user_id": user_id})
                total_count = cursor.fetchone()[0]

        return jsonify({
            'status': 'success',
            'documents': documents,
            'count': len(documents),
            'total': total_count,
            'limit': limit,
            'offset': offset,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        print(f"Error getting all documents: {str(e)}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    
@file_api.route('/get-total-words', methods=['GET'])
@token_required
def get_total_words(user_id):
    """
    Get the total word count for a source URL or all user content
    
    Query parameters:
    - source_url: (Optional) Filter by parent source URL
    """
    try:
        # Get source_url parameter
        source_url = request.args.get('source_url')
        
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                # Build the base query
                base_query = f"""
                    SELECT 
                        COUNT(*) AS total_documents,
                        SUM(WORD_COUNT) AS total_words,
                        AVG(WORD_COUNT) AS avg_words_per_document,
                        MIN(WORD_COUNT) AS min_words,
                        MAX(WORD_COUNT) AS max_words
                    FROM {SCRAPPED_TEXT_TABLE}
                    WHERE USER_ID = :user_id
                """
                
                params = {'user_id': user_id}
                
                # Add source_url filter if provided
                if source_url:
                    base_query += " AND TOP_LEVEL_SOURCE = :source_url"
                    params['source_url'] = source_url
                
                # Execute the query
                cursor.execute(base_query, params)
                
                result = cursor.fetchone()
                if not result:
                    return jsonify({
                        'status': 'success',
                        'message': 'No documents found',
                        'total_documents': 0,
                        'total_words': 0,
                        'source_url': source_url if source_url else 'all',
                        'timestamp': datetime.now().isoformat()
                    })
                
                total_documents, total_words, avg_words, min_words, max_words = result
                
                # If source_url is provided, also get document-level word counts
                document_stats = []
                if source_url:
                    document_query = f"""
                        SELECT 
                            ID,
                            CONTENT_LINK,
                            TITLE,
                            WORD_COUNT,
                            TO_CHAR(SCRAPE_DATE, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as SCRAPE_DATE
                        FROM {SCRAPPED_TEXT_TABLE}
                        WHERE USER_ID = :user_id AND TOP_LEVEL_SOURCE = :source_url
                        ORDER BY WORD_COUNT DESC
                    """
                    
                    cursor.execute(document_query, user_id=user_id, source_url=source_url)
                    
                    for row in cursor.fetchall():
                        document_stats.append({
                            'id': row[0],
                            'url': row[1],
                            'title': row[2],
                            'word_count': row[3],
                            'scrape_date': row[4]
                        })
                
                return jsonify({
                    'status': 'success',
                    'total_documents': total_documents,
                    'total_words': total_words,
                    'average_words_per_document': round(avg_words, 1) if avg_words else 0,
                    'min_words': min_words,
                    'max_words': max_words,
                    'document_details': document_stats if source_url else [],
                    'source_url': source_url if source_url else 'all',
                    'timestamp': datetime.now().isoformat()
                })
                
    except Exception as e:
        print(f"Error getting total words: {str(e)}")
        traceback.print_exc()
        return standardize_error_response(str(e), 'SERVER_ERROR', 500)  
      
@file_api.route('/vectorization-status', methods=['GET'])
@token_required
def get_vectorization_status(user_id):
    """Get the vectorization status for a source URL"""
    try:
        source_url = request.args.get('source_url')
        
        if not source_url:
            return standardize_error_response('source_url parameter is required.', 'MISSING_PARAM', 400)
        
        # Connect to vector database to check if vectors exist
        from oracle_chatbot import connect_to_vectdb
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        from langchain_community.vectorstores import OracleVS
        from langchain_community.vectorstores.utils import DistanceStrategy
        import os
        
        # Get the VECTDB_TABLE_NAME and GOOGLE_API_KEY
        VECTDB_TABLE_NAME = os.getenv("VECTDB_TABLE_NAME", "vector_files_with_10000_chunk_new")
        GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
        
        vectdb_connection = connect_to_vectdb()
        if not vectdb_connection:
            return standardize_error_response('Failed to connect to vector database', 'DB_ERROR', 500)
        
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
            
            # Use a query to check if any vectors exist for this source_url
            # We'll use an SQL query directly on the vector table
            with vectdb_connection.cursor() as cursor:
                # Check the vector store metadata to see if any entries are for this source_url
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE METADATA LIKE :source_pattern
                """.format(VECTDB_TABLE_NAME), 
                   source_pattern=f'%"source":"{source_url}"%')
                
                vector_count = cursor.fetchone()[0]
                
                # Get the total documents scraped for this source URL
                with get_db_connection() as text_conn:
                    with text_conn.cursor() as text_cursor:
                        text_cursor.execute("""
                            SELECT COUNT(*) FROM {0}
                            WHERE TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                        """.format(SCRAPPED_TEXT_TABLE), 
                           source_url=source_url, user_id=user_id)
                        
                        document_count = text_cursor.fetchone()[0]
                
                # Determine status
                if vector_count > 0:
                    status = "Vectorized"
                    message = f"Source URL has been vectorized with {vector_count} vector entries from {document_count} documents."
                elif document_count == 0:
                    status = "No Data"
                    message = "No scrapped documents found for this source URL."
                else:
                    status = "Pending"
                    message = f"Source URL has {document_count} documents that need to be vectorized."
                
                # Get processing status as well
                with get_db_connection() as processing_conn:
                    with processing_conn.cursor() as processing_cursor:
                        processing_cursor.execute("""
                            SELECT PROCESSED, PROCESSING_COMPLETED 
                            FROM {0}
                            WHERE SOURCE_URL = :source_url AND USER_ID = :user_id
                        """.format(PROCESSING_QUEUE_TABLE), 
                           source_url=source_url, user_id=user_id)
                        
                        processing_row = processing_cursor.fetchone()
                        crawling_complete = False
                        
                        if processing_row and processing_row[0] == 1:
                            crawling_complete = True
                
                # Final status
                is_ready_for_chatbot = status == "Vectorized" and crawling_complete
                
                return jsonify({
                    'status': 'success',
                    'vectorization_status': status,
                    'message': message,
                    'vector_count': vector_count,
                    'document_count': document_count,
                    'crawling_complete': crawling_complete,
                    'ready_for_chatbot': is_ready_for_chatbot,
                    'source_url': source_url,
                    'timestamp': datetime.now().isoformat()
                })
                
        finally:
            if vectdb_connection:
                vectdb_connection.close()
    
    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error checking vectorization status: {str(e)}\n{traceback_str}")
        return standardize_error_response(str(e), 'SERVER_ERROR', 500)
    
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
        print(f"Server error: {error_message}\n{traceback_str}")
        response['error_details'] = traceback_str
        
    return jsonify(response), status_code            
                