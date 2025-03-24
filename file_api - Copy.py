from flask import Blueprint, request, jsonify
from functools import wraps
import jwt
import datetime
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

# Load environment variables from .env file
load_dotenv()

file_api = Blueprint('file_api', __name__)

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

def get_oracle_connection():
    """Establish connection to Oracle Database"""
    try:
        connection = oracledb.connect(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN,
            config_dir="Wallet_jsondb",
            wallet_location="Wallet_jsondb",
            wallet_password=ORACLE_PASSWORD
        )
        return connection
    except Exception as e:
        print(f"Error connecting to Oracle DB: {e}")
        traceback.print_exc()
        raise

def initialize_tables():
    """Initialize all necessary tables if they don't exist"""
    connection = None
    cursor = None
    
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Map of table names to their creation SQL
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
                    ALTER TABLE LINKS_TO_SCRAP ADD PROCESSED_AT TIMESTAMP;
                    ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    LINK VARCHAR2(2000) NOT NULL,
                    ADDED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    IS_CRAWLED NUMBER(1) DEFAULT 0,
                    IS_PROCESSED VARCHAR2(20) DEFAULT 'false',
                    SOURCE_URL VARCHAR2(2000),
                    TOP_LEVEL_SOURCE VARCHAR2(2000),
                    DEPTH NUMBER DEFAULT 0,
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
                
                # Create necessary indexes
                if table_name == LINKS_TO_SCRAP_TABLE:
                    # Create index on common search fields
                    cursor.execute(f"""
                        CREATE INDEX IDX_{table_name}_TOP_LEVEL 
                        ON {table_name} (TOP_LEVEL_SOURCE, USER_ID, IS_CRAWLED)
                    """)
                    cursor.execute(f"""
                        CREATE INDEX IDX_{table_name}_PROCESSING 
                        ON {table_name} (TOP_LEVEL_SOURCE, USER_ID, IS_PROCESSED)
                    """)
                elif table_name == PROCESSING_QUEUE_TABLE:
                    # Create index for queue processing
                    cursor.execute(f"""
                        CREATE INDEX IDX_{table_name}_QUEUE 
                        ON {table_name} (USER_ID, PROCESSED, PROCESSING_STARTED, ADDED_AT)
                    """)
                
        connection.commit()
        print("Database tables initialized successfully")
    except Exception as e:
        print(f"Error initializing tables: {e}")
        traceback.print_exc()
        if connection:
            connection.rollback()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

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
def token_required(fn):
    def wrapper(*args, **kwargs):
        user_id = verify_token()
        if not user_id:
            return jsonify({
                'status': 'error',
                'message': 'Unauthorized access. Valid token required.',
                'timestamp': datetime.now().isoformat()
            }), 401
        return fn(user_id, *args, **kwargs)
    wrapper.__name__ = fn.__name__
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

# Queue Management Functions
def add_to_queue(user_id, source_url, additional_data=None):
    """Add a URL to the processing queue for a user with optional additional data"""
    connection = None
    cursor = None
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Check if it already exists in the queue
        cursor.execute("""
            SELECT COUNT(*) FROM {0}
            WHERE USER_ID = :user_id AND SOURCE_URL = :source_url AND PROCESSED = 0
        """.format(PROCESSING_QUEUE_TABLE), 
           user_id=user_id, source_url=source_url)
        
        existing_count = cursor.fetchone()[0]
        
        if existing_count > 0:
            print(f"URL already in queue: {source_url} for user {user_id}")
            return False
            
        # Set defaults
        page_limit = 10
        
        # Update with any additional data
        if additional_data:
            if 'page_limit' in additional_data:
                page_limit = additional_data['page_limit']
        
        # Insert queue item
        cursor.execute("""
            INSERT INTO {0} (USER_ID, SOURCE_URL, ADDED_AT, PROCESSED, PAGE_LIMIT)
            VALUES (:user_id, :source_url, CURRENT_TIMESTAMP, 0, :page_limit)
        """.format(PROCESSING_QUEUE_TABLE),
           user_id=user_id, source_url=source_url, page_limit=page_limit)
        
        connection.commit()
        print(f"Added URL to queue: {source_url} for user {user_id}, page_limit: {page_limit}")
        return True
    except oracledb.DatabaseError as e:
        error, = e.args
        print(f"Database error adding to queue: {str(error)}")
        if error.code == 1: # Constraint violation code
            print(f"URL already in queue (constraint error): {source_url}")
            return False
        traceback.print_exc()
        if connection:
            connection.rollback()
        return False
    except Exception as e:
        print(f"Error adding to queue: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def get_next_from_queue(user_id):
    """Get the next URL from the queue for a user"""
    connection = None
    cursor = None
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Find the oldest unprocessed URL for this user
        cursor.execute("""
            SELECT ID, SOURCE_URL, PAGE_LIMIT FROM {0}
            WHERE USER_ID = :user_id AND PROCESSED = 0 AND PROCESSING_STARTED IS NULL
            ORDER BY ADDED_AT ASC
            FETCH FIRST 1 ROW ONLY
        """.format(PROCESSING_QUEUE_TABLE), user_id=user_id)
        
        row = cursor.fetchone()
        
        if row:
            queue_id, source_url, page_limit = row
            
            # Mark as processing started
            cursor.execute("""
                UPDATE {0} SET PROCESSING_STARTED = CURRENT_TIMESTAMP
                WHERE ID = :id
            """.format(PROCESSING_QUEUE_TABLE), id=queue_id)
            
            connection.commit()
            
            print(f"Retrieved next URL from queue: {source_url} for user {user_id}")
            
            # Return both the URL and the page limit
            return {
                'source_url': source_url,
                'page_limit': page_limit or 10  # Default to 10 if None
            }
        else:
            print(f"No more URLs in queue for user {user_id}")
            return None
    except Exception as e:
        print(f"Error getting next from queue: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def mark_as_complete_and_process_next(user_id, source_url):
    """Mark a URL as complete in the queue and start processing the next one if available"""
    connection = None
    cursor = None
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Mark the current URL as complete
        cursor.execute("""
            UPDATE {0} SET PROCESSED = 1, PROCESSING_COMPLETED = CURRENT_TIMESTAMP
            WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
        """.format(PROCESSING_QUEUE_TABLE),
           user_id=user_id, source_url=source_url)
        
        connection.commit()
        print(f"Marked URL as complete: {source_url} for user {user_id}")
        
        # Update the active user jobs tracking
        if user_id in active_user_jobs:
            if source_url in active_user_jobs[user_id]:
                active_user_jobs[user_id].remove(source_url)
            if not active_user_jobs[user_id]:
                del active_user_jobs[user_id]
                
        # Clean up event objects - IMPORTANT: Use the same key format as in start_crawling_and_processing
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
        
        # Get the next URL from the queue
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
        if connection:
            connection.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def user_has_active_job(user_id):
    """Check if a user has an active job"""
    return user_id in active_user_jobs and len(active_user_jobs[user_id]) > 0

def add_active_job(user_id, source_url):
    """Add a job to the active jobs list for a user"""
    if user_id not in active_user_jobs:
        active_user_jobs[user_id] = set()
    active_user_jobs[user_id].add(source_url)
    print(f"Added active job: {source_url} for user {user_id}")

def scrape_link(url):
    """Scrape the content from a given URL"""
    try:
        # Add user agent to avoid being blocked
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Make request to the URL
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()  # Raise exception for 4XX/5XX responses
        
        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract all text content (removing script and style elements)
        for script in soup(["script", "style"]):
            script.extract()
            
        # Get text and clean it
        text = soup.get_text(separator=' ', strip=True)
        
        # Remove excessive whitespace
        text = ' '.join(text.split())
        
        return {
            'status': 'success',
            'content': text
        }
    
    except Exception as e:
        return {
            'status': 'error',
            'error': str(e)
        }

def start_crawling_and_processing(user_id, source_url, page_limit=10):
    """Start the crawling and processing for a URL simultaneously with a processing delay"""
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
            else:
                # Wait for processing to finish
                print(f"Crawling finished, waiting for processing to complete for {url}")
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
    
    # Define a function for the processing thread
    def start_processing(url, stop_event, user_id, completion_status):
        try:
            # Use 15 seconds delay
            delay_seconds = 15
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
    
    # Function to start the final 40-second cool-down period
    def start_final_cooldown(user_id, url, completion_status):
        connection = None
        cursor = None
        
        try:
            # Check if we're already in cooldown to prevent multiple cooldown processes
            if completion_status.get('in_final_cooldown', False):
                print(f"Already in final cooldown for {url}, user: {user_id}. Skipping duplicate cooldown.")
                return
                
            print(f"Starting final cooldown for {url}, user: {user_id}")
            completion_status['in_final_cooldown'] = True
            
            connection = get_oracle_connection()
            cursor = connection.cursor()
            
            # Count total links before cooldown
            cursor.execute("""
                SELECT COUNT(*) FROM {0}
                WHERE TOP_LEVEL_SOURCE = :url AND USER_ID = :user_id
            """.format(LINKS_TO_SCRAP_TABLE), url=url, user_id=user_id)
            
            completion_status['links_before_cooldown'] = cursor.fetchone()[0]
            
            print(f"Starting final 40-second cool-down for {url}, user {user_id}")
            print(f"Current link count before cool-down: {completion_status['links_before_cooldown']}")
            
            cooldown_duration = 40  # 40 seconds
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
                """.format(LINKS_TO_SCRAP_TABLE), url=url, user_id=user_id)
                
                current_link_count = cursor.fetchone()[0]
                
                # Check if any links still need processing
                cursor.execute("""
                    SELECT COUNT(*) FROM {0}
                    WHERE TOP_LEVEL_SOURCE = :url AND USER_ID = :user_id
                    AND (IS_PROCESSED = 'false' OR IS_PROCESSED IS NULL)
                """.format(LINKS_TO_SCRAP_TABLE), url=url, user_id=user_id)
                
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
                
                # Sleep for 5 seconds before checking again
                time.sleep(5)
            
            # Cool-down period has elapsed with no new links
            print(f"Final cool-down period of {cooldown_duration} seconds has elapsed. No new links found. Completing job for {url}")
            
            # Now mark as complete and process next in queue
            mark_as_complete_and_process_next(user_id, url)
            
        except Exception as e:
            print(f"Error in final cool-down: {str(e)}")
            traceback.print_exc()
            
            # Even if there's an error, try to move to the next URL
            mark_as_complete_and_process_next(user_id, url)
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()
    
    # Start the crawling in a background thread
    crawler_thread = Thread(
        target=start_crawl,
        args=(source_url, crawl_stop_event, user_id, page_limit, completion_status),
        daemon=True
    )
    crawler_thread.start()
    
    # Start the processing in a background thread (with 15 second delay)
    processor_thread = Thread(
        target=start_processing,
        args=(source_url, process_stop_event, user_id, completion_status),
        daemon=True
    )
    processor_thread.start()
    
    print(f"Started simultaneous crawling and processing for {source_url}, user {user_id}")
    return True

def continuous_crawl_job(top_level_source_url, stop_event, user_id, page_limit=10):
    """
    Worker function to continuously crawl pages until either:
    1. All links are processed
    2. stop_event is set
    3. page_limit is reached (number of URLs marked as is_crawled: true)
    """
    connection = None
    cursor = None
    try:
        # Ensure page_limit is an integer and valid
        try:
            page_limit = int(page_limit)
            if page_limit < 0:
                page_limit = 0  # No limit
            elif page_limit > 5000:
                page_limit = 5000  # Max allowed
        except (ValueError, TypeError):
            page_limit = 10  # Default if invalid
            
        print(f"Starting continuous crawl job for {top_level_source_url} for user {user_id} with STRICT limit of {page_limit} crawled pages")
        
        # Add domain extraction function
        def extract_domain(url):
            try:
                # Remove protocol and get domain
                if '//' in url:
                    domain = url.split('//', 1)[1].split('/', 1)[0]
                else:
                    domain = url.split('/', 1)[0]
                return domain.lower()
            except:
                return url
        
        # Extract the domain from the top-level source URL to restrict crawling
        original_domain = extract_domain(top_level_source_url)
        print(f"Original domain to restrict crawling to: {original_domain}")
        
        # Stats counters
        links_added = 0
        errors_encountered = 0
        
        # Set to track consecutive empty runs (no new links added)
        consecutive_empty_runs = 0
        max_consecutive_empty_runs = 3  # After this many empty runs, terminate
        
        # Check if we have a limit
        if page_limit == 0:
            print("Page limit is set to 0 (unlimited)")
        else:
            print(f"Page limit is set to {page_limit} crawled pages")
        
        while not stop_event.is_set():
            connection = get_oracle_connection()
            cursor = connection.cursor()
            
            # CRITICAL: Check how many URLs have already been crawled for this user and source
            cursor.execute("""
                SELECT COUNT(*) FROM {0}
                WHERE IS_CRAWLED = 1 AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
            """.format(LINKS_TO_SCRAP_TABLE), source_url=top_level_source_url, user_id=user_id)
            
            already_crawled_count = cursor.fetchone()[0]
            
            # Check if we've reached the page limit
            if page_limit > 0 and already_crawled_count >= page_limit:
                print(f"REACHED PAGE LIMIT: {already_crawled_count}/{page_limit} pages crawled. Enforcing limit.")
                break
                
            # Find out how many uncrawled links remain
            cursor.execute("""
                SELECT COUNT(*) FROM {0}
                WHERE (IS_CRAWLED = 0 OR IS_CRAWLED IS NULL) AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
            """.format(LINKS_TO_SCRAP_TABLE), source_url=top_level_source_url, user_id=user_id)
            
            links_remaining = cursor.fetchone()[0]
            
            # If no uncrawled links remain, we're done
            if links_remaining == 0:
                print(f"No more uncrawled links for {top_level_source_url} for user {user_id}. Exiting crawl job.")
                break

            # Find the next uncrawled link
            cursor.execute("""
                SELECT ID, LINK, DEPTH FROM {0}
                WHERE (IS_CRAWLED = 0 OR IS_CRAWLED IS NULL) AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
                ORDER BY DEPTH ASC, ADDED_AT ASC
                FETCH FIRST 1 ROW ONLY
            """.format(LINKS_TO_SCRAP_TABLE), source_url=top_level_source_url, user_id=user_id)
            
            link_row = cursor.fetchone()
            
            if not link_row:
                print(f"No more uncrawled links for {top_level_source_url} for user {user_id}. Exiting crawl job.")
                break
                
            link_id, url_to_crawl, current_depth = link_row
            
            # Mark the link as being crawled
            cursor.execute("""
                UPDATE {0} SET CRAWLING_STARTED = CURRENT_TIMESTAMP
                WHERE ID = :id
            """.format(LINKS_TO_SCRAP_TABLE), id=link_id)
            
            connection.commit()
            
            # Process the URL - request, parse, extract links
            print(f"Starting to crawl URL: {url_to_crawl} for user {user_id}")
            
            try:
                # Request and parse the URL
                # Extract valid links
                
                # After processing, update the database
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
                
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                tb = traceback.format_exc()
                print(f"Error processing URL {url_to_crawl}: {error_msg}")
                print(tb)
                
                # Update the link as failed
                cursor.execute("""
                    UPDATE {0} SET
                        IS_CRAWLED = 1,
                        CRAWLED_AT = CURRENT_TIMESTAMP,
                        ERROR = :error,
                        TRACEBACK = :traceback
                    WHERE ID = :id
                """.format(LINKS_TO_SCRAP_TABLE), 
                    id=link_id, 
                    error=error_msg[:4000],  # Limit to Oracle CLOB size
                    traceback=tb[:4000]      # Limit to Oracle CLOB size
                )
                
                connection.commit()
                errors_encountered += 1
                consecutive_empty_runs += 1
            
            # Close the connection after each link to prevent connection leaks
            if cursor:
                cursor.close()
                cursor = None
            if connection:
                connection.close()
                connection = None
            
            # Optional: Sleep to prevent hammering the target server
            time.sleep(0.5)
        
        # Get final count of crawled pages
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        cursor.execute("""
            SELECT COUNT(*) FROM {0}
            WHERE IS_CRAWLED = 1 AND TOP_LEVEL_SOURCE = :source_url AND USER_ID = :user_id
        """.format(LINKS_TO_SCRAP_TABLE), source_url=top_level_source_url, user_id=user_id)
        
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
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def continuous_processing_job(top_level_source_url, stop_event, delay_seconds=60, user_id=None):
    """
    Worker function to continuously process all the scraped links
    until all links in Links_to_scrap are processed (either successfully or failed).
    Includes an initial delay before starting processing.
    """
    connection = None
    cursor = None
    try:
        print(f"Starting processing job for {top_level_source_url} with {delay_seconds}s delay for user {user_id}")
        
        # Wait for the specified delay before starting processing
        print(f"Waiting {delay_seconds} seconds before starting processing...")
        
        # Wait either for the delay or until the stop event is set
        stop_event.wait(delay_seconds)
        
        # If stop event is set during the delay, exit early
        if stop_event.is_set():
            print(f"Processing job for {top_level_source_url} was stopped during delay")
            return
            
        print(f"Delay complete, beginning processing for {top_level_source_url} for user {user_id}")
        
        # Stats counters
        links_processed = 0
        success_count = 0
        error_count = 0
        consecutive_empty_cycles = 0
        max_consecutive_empty_cycles = 10  # Allow more empty cycles before exiting
        
        # Batch size for processing
        batch_size = 20  # Smaller batch size for more frequent checking
        
        # Keep processing until explicitly stopped
        while not stop_event.is_set():
            try:
                # Create a new DB connection for each iteration to prevent connection timeouts
                connection = get_oracle_connection()
                cursor = connection.cursor()
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
                
                print(f"Looking for unprocessed links with query: {query}")
                
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
                print(f"Found {unprocessed_count} unprocessed links")
                
                if unprocessed_count == 0:
                    # No unprocessed links found, wait and check again
                    consecutive_empty_cycles += 1
                    print(f"No unprocessed links found. Empty cycle #{consecutive_empty_cycles}/{max_consecutive_empty_cycles}")
                    
                    if consecutive_empty_cycles >= max_consecutive_empty_cycles:
                        print(f"Reached maximum consecutive empty cycles ({max_consecutive_empty_cycles}). Exiting processing job.")
                        break
                    
                    # Wait for a short time before checking again
                    sleep_time = 5  # 5 seconds
                    print(f"Waiting {sleep_time} seconds before checking for new links...")
                    
                    # Use stop_event.wait instead of time.sleep to respond to stop events
                    if stop_event.wait(sleep_time):
                        print("Stop event detected during wait. Exiting processing job.")
                        break
                    
                    # Close this client before continuing the loop
                    if cursor:
                        cursor.close()
                        cursor = None
                    if connection:
                        connection.close()
                        connection = None
                    
                    continue
                
                # Reset the consecutive empty cycles counter since we found links
                consecutive_empty_cycles = 0
                
                # Get a batch of unprocessed links
                cursor.execute(query, params)
                unprocessed_links = cursor.fetchall()
                print(f"Processing batch of {len(unprocessed_links)} links")
                
                batch_processed = 0
                
                # Process each link in the batch
                for link_row in unprocessed_links:
                    if stop_event.is_set():
                        print(f"Stop event triggered. Exiting processing job for {top_level_source_url}.")
                        break
                    
                    try:
                        link_id, link_url, depth, top_level, source, link_user_id = link_row
                        print(f"Processing link: {link_url}")
                        
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
                            print(f"Added missing user_id {user_id} to link document")
                        
                        # Create a link_doc dictionary to match the MongoDB interface
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
                            success_count += 1
                            print(f"Successfully processed link: {link_url}")
                        else:
                            # Mark the link as failed instead of processed
                            error_count += 1
                            print(f"Failed to process link: {link_url}. Error: {result.get('error', 'Unknown error')}")
                            
                            # Update with failure status
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
                                error=result.get('error', 'Unknown error')[:4000],  # Limit for Oracle CLOB
                                traceback=result.get('traceback', '')[:4000],      # Limit for Oracle CLOB
                                user_id=effective_user_id
                            )
                            
                            connection.commit()
                        
                        links_processed += 1
                        batch_processed += 1
                        
                        # Log progress periodically
                        if links_processed % 10 == 0:
                            print(f"Processed {links_processed} links for {top_level_source_url} ({success_count} successful, {error_count} failed) for user {effective_user_id}")
                        
                    except Exception as e:
                        # Catch any unexpected errors during processing
                        error_msg = f"Unexpected error processing link {link_url}: {str(e)}"
                        print(error_msg)
                        tb = traceback.format_exc()
                        traceback.print_exc()
                        
                        # Mark the link as failed
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
                            error=error_msg[:4000],  # Limit for Oracle CLOB
                            traceback=tb[:4000],     # Limit for Oracle CLOB
                            user_id=effective_user_id
                        )
                        
                        connection.commit()
                        error_count += 1
                    
                    # Short sleep between links to prevent hammering the server
                    time.sleep(0.5)
                
                print(f"Batch complete. Processed {batch_processed} links.")
                
                # Clean up DB connection after batch processing
                if cursor:
                    cursor.close()
                    cursor = None
                if connection:
                    connection.close()
                    connection = None
                
                # If the batch was smaller than the batch size, take a short break before checking again
                if batch_processed < batch_size:
                    sleep_time = 3  # 3 seconds
                    print(f"Processed less than batch size. Waiting {sleep_time} seconds before continuing...")
                    if stop_event.wait(sleep_time):
                        print("Stop event detected during wait. Exiting processing job.")
                        break
                
            except Exception as batch_error:
                print(f"Error processing batch: {str(batch_error)}")
                traceback.print_exc()
                
                # Sleep before retrying
                time.sleep(5)
                
                # Close client in case of error
                if cursor:
                    cursor.close()
                    cursor = None
                if connection:
                    connection.close()
                    connection = None
        
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
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def scrape_single_link(connection, link_doc, user_id=None):
    """Helper function to scrape a single link"""
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
            cursor.execute("""
                UPDATE {0} SET
                    IS_PROCESSED = 'Failed',
                    PROCESSED_AT = CURRENT_TIMESTAMP,
                    ERROR = :error,
                    USER_ID = :user_id,
                    TOP_LEVEL_SOURCE = :top_level_source
                WHERE ID = :id
            """.format(LINKS_TO_SCRAP_TABLE),
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
        print(f"Extracted text length: {len(text)} characters")
        
        # Check if content already exists to avoid duplicates
        cursor.execute("""
            SELECT COUNT(*) FROM {0}
            WHERE CONTENT_LINK = :link AND USER_ID = :user_id
        """.format(SCRAPPED_TEXT_TABLE), link=link, user_id=link_doc_user_id)
        
        if cursor.fetchone()[0] > 0:
            print(f"Content already exists for {link}, skipping insertion")
            
            # Get the content ID
            cursor.execute("""
                SELECT ID FROM {0}
                WHERE CONTENT_LINK = :link AND USER_ID = :user_id
            """.format(SCRAPPED_TEXT_TABLE), link=link, user_id=link_doc_user_id)
            
            content_id = cursor.fetchone()[0]
        else:
            # Insert into content collection
            print(f"Inserting content into database for {link}")
            try:
                # Create a variable to hold the returned ID
                content_id_var = cursor.var(int)
                
                cursor.execute("""
                    INSERT INTO {0} (
                        SCRAPPED_CONTENT, CONTENT_LINK, SCRAPE_DATE, LINK_ID,
                        SOURCE_URL, TOP_LEVEL_SOURCE, DEPTH, TITLE, USER_ID
                    ) VALUES (
                        :content, :link, CURRENT_TIMESTAMP, :link_id,
                        :source_url, :top_level_source, :depth, :title, :user_id
                    ) RETURNING ID INTO :content_id
                """.format(SCRAPPED_TEXT_TABLE),
                    content=text,
                    link=link,
                    link_id=link_doc['id'],
                    source_url=source_url,
                    top_level_source=top_level_source,
                    depth=link_doc.get('depth', 0),
                    title=title_text[:1000],  # Limit to column size
                    user_id=link_doc_user_id,
                    content_id=content_id_var
                )

                content_id = content_id_var.getvalue()
                connection.commit()
                print(f"Content inserted with ID: {content_id}")
                
            except oracledb.DatabaseError as db_error:
                error, = db_error.args
                print(f"Database error inserting content: {str(error)}")
                traceback.print_exc()
                raise
        
        # Update the link as processed
        print(f"Updating link status to processed for {link}")
        try:
            cursor.execute("""
                UPDATE {0} SET
                    IS_PROCESSED = 'true',
                    PROCESSED_AT = CURRENT_TIMESTAMP,
                    TOP_LEVEL_SOURCE = :top_level_source,
                    USER_ID = :user_id
                WHERE ID = :id
            """.format(LINKS_TO_SCRAP_TABLE),
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
                cursor.execute("""
                    UPDATE {0} SET
                        IS_PROCESSED = 'Failed',
                        PROCESSED_AT = CURRENT_TIMESTAMP,
                        ERROR = :error,
                        USER_ID = :user_id,
                        TOP_LEVEL_SOURCE = :top_level_source
                    WHERE ID = :id
                """.format(LINKS_TO_SCRAP_TABLE),
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
                cursor.execute("""
                    UPDATE {0} SET
                        IS_PROCESSED = 'Failed',
                        PROCESSED_AT = CURRENT_TIMESTAMP,
                        ERROR = :error,
                        TRACEBACK = :traceback,
                        USER_ID = :user_id,
                        TOP_LEVEL_SOURCE = :top_level_source
                    WHERE ID = :id
                """.format(LINKS_TO_SCRAP_TABLE),
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

@file_api.route('/recursive-crawl', methods=['POST'])
@token_required
def recursive_crawl(user_id):
    connection = None
    cursor = None
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
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
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
                connection.rollback()
                raise
        
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
            connection.rollback()
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
                    'timestamp': datetime.now().isoformat(),
                    'source_url': top_level_source_url,
                    'page_limit': page_limit
                })
            else:
                return standardize_error_response(f'Failed to start crawling for {top_level_source_url}', 'CRAWL_ERROR', 500)
    
    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error in crawling: {str(e)}\n{traceback_str}")
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
            
@file_api.route('/process-all-links', methods=['POST'])
@token_required
def process_all_links(user_id):
    """
    Start a background thread to continuously process all links in the Links_to_scrap collection
    until all links are processed (is_processed: true).
    Includes an option to delay the start of processing.
    If a job is already active for the user, the new URL will be queued.
    """
    connection = None
    cursor = None
    try:
        print(f"Starting process_all_links for user ID: {user_id}")
        
        # Get the delay parameter (default: 60 seconds) and source_url
        data = request.get_json() or {}
        delay_seconds = data.get('delay', 60)
        source_url = data.get('source_url')
        
        print(f"Request data: delay={delay_seconds}, source_url={source_url}")
        
        if not source_url:
            print("source_url is missing in request body")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400

        connection = get_oracle_connection()
        cursor = connection.cursor()
        
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
            result = start_crawling_and_processing(user_id, source_url, delay_seconds)
            
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
        if connection:
            connection.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback_str,
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/queue-status', methods=['GET'])
@token_required
def get_queue_status(user_id):
    """Get the status of the processing queue for the authenticated user"""
    connection = None
    cursor = None
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Get all queued items for this user
        cursor.execute("""
            SELECT SOURCE_URL, 
                   TO_CHAR(ADDED_AT, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as ADDED_AT, 
                   PROCESSED, 
                   TO_CHAR(PROCESSING_STARTED, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as PROCESSING_STARTED,
                   PAGE_LIMIT
            FROM {0}
            WHERE USER_ID = :user_id
            ORDER BY ADDED_AT ASC
        """.format(PROCESSING_QUEUE_TABLE), user_id=user_id)
        
        queue_items = []
        for row in cursor.fetchall():
            source_url, added_at, processed, processing_started, page_limit = row
            
            # Determine status field for each item
            status = None
            if processed == 1:
                status = 'Completed'
            elif processing_started is not None:
                status = 'In Progress'
            else:
                status = 'Queued'
                
            # Add to result list
            queue_items.append({
                'source_url': source_url,
                'added_at': added_at,
                'processed': processed == 1,  # Convert to boolean
                'processing_started': processing_started,
                'page_limit': page_limit,
                'status': status
            })
        
        # Check if there's an active job
        has_active_job = user_has_active_job(user_id)
        
        # If there's an active job, get its source URL
        active_job_url = None
        if has_active_job and user_id in active_user_jobs:
            active_job_url = list(active_user_jobs[user_id])[0] if active_user_jobs[user_id] else None
        
        return jsonify({
            'status': 'success',
            'queue': queue_items,
            'has_active_job': has_active_job,
            'active_job_url': active_job_url,
            'queue_length': len(queue_items),
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting queue status: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/source-url-status', methods=['GET'])
@token_required
def get_source_url_status(user_id):
    """Get the status of a specific source URL for the authenticated user"""
    connection = None
    cursor = None
    try:
        print(f"Getting source URL status for user: {user_id}")
        
        source_url = request.args.get('source_url')
        if not source_url:
            print("source_url parameter is missing")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        print(f"Checking status for source URL: {source_url}")
        
        connection = get_oracle_connection()
        cursor = connection.cursor()

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
        if connection:
            connection.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/stop-crawling', methods=['POST'])
@token_required
def stop_crawling(user_id):
    """Stop the continuous crawling for a specific source URL"""
    try:
        print(f"Request to stop crawling for user: {user_id}")
        
        data = request.get_json()
        if not data or 'source_url' not in data:
            print("source_url is missing in request body")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
            
        source_url = data['source_url']
        print(f"Stopping crawling for source URL: {source_url}")
        
        # Create user-specific key
        crawl_key = f"{user_id}:{source_url}"
        
        if crawl_key in crawling_events:
            print(f"Found crawling job for {crawl_key}, setting stop event")
            crawling_events[crawl_key].set()
            
            # Remove from active jobs list if it exists
            if user_id in active_user_jobs and source_url in active_user_jobs[user_id]:
                active_user_jobs[user_id].remove(source_url)
                if not active_user_jobs[user_id]:
                    del active_user_jobs[user_id]
            
            # Check if there are more URLs in the queue and start the next one
            start_next = mark_as_complete_and_process_next(user_id, source_url)
            
            return jsonify({
                'status': 'success',
                'message': f'Crawling for {source_url} has been stopped.',
                'next_started': start_next,
                'timestamp': datetime.now().isoformat()
            })
        else:
            print(f"No active crawling found for {crawl_key}")
            return jsonify({
                'status': 'error',
                'message': f'No active crawling found for {source_url}',
                'timestamp': datetime.now().isoformat()
            }), 404
            
    except Exception as e:
        print(f"Error stopping crawling: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500

@file_api.route('/progress-bar', methods=['GET'])
def get_progress_bar():
    """
    Get the progress information for crawling and scraping operations
    to display in a progress bar in the frontend.
    """
    connection = None
    cursor = None
    try:
        # Get source URL from query parameters
        source_url = request.args.get('source_url')

        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400

        connection = get_oracle_connection()
        cursor = connection.cursor()

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
            current_time = datetime.now()
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

        # Calculate change in percentages since last check
        current_timestamp = datetime.now()
        change_since_last = {
            'crawl_progress_change': 0,
            'scrape_progress_change': 0,
            'links_per_minute': 0,
            'scrape_per_minute': 0,
            'time_since_last': 0, # seconds
            'estimated_completion_time': None,
            'estimated_completion_minutes': None,
            'estimated_time_readable': 'Unknown'
        }

        if last_progress:
            last_timestamp = datetime.strptime(last_progress['timestamp'], '%Y-%m-%dT%H:%M:%SZ')
            time_diff_seconds = (current_timestamp - last_timestamp).total_seconds()
            time_diff_minutes = time_diff_seconds / 60

            change_since_last['crawl_progress_change'] = round(crawl_progress - last_progress['crawl_progress'], 1)
            change_since_last['scrape_progress_change'] = round(scrape_progress - last_progress['scrape_progress'], 1)
            change_since_last['time_since_last'] = round(time_diff_seconds, 1)

            # Calculate rates (per minute)
            if time_diff_minutes > 0:
                links_diff = crawled_count - last_progress['crawled_count']
                scrape_diff = scraped_count - last_progress['scraped_count']

                change_since_last['links_per_minute'] = round(links_diff / time_diff_minutes, 2)
                change_since_last['scrape_per_minute'] = round(scrape_diff / time_diff_minutes, 2)

            # Calculate estimated completion time
            links_remaining = total_links - crawled_count
            scrape_remaining = total_links - scraped_count

            # Estimate time for crawling (if not complete)
            estimated_minutes_crawl = 0
            if crawl_progress < 100 and change_since_last['links_per_minute'] > 0:
                estimated_minutes_crawl = links_remaining / change_since_last['links_per_minute']

            # Estimate time for scraping (if not complete)
            estimated_minutes_scrape = 0
            if scrape_progress < 100 and change_since_last['scrape_per_minute'] > 0:
                estimated_minutes_scrape = scrape_remaining / change_since_last['scrape_per_minute']

            # Total estimated time is the sum of remaining crawl and scrape time
            total_estimated_minutes = estimated_minutes_crawl + estimated_minutes_scrape

            if total_estimated_minutes > 0:
                # Format the estimated completion time
                change_since_last['estimated_completion_minutes'] = round(total_estimated_minutes, 1)

                # Calculate the absolute timestamp for estimated completion
                estimated_completion_time = current_timestamp + timedelta(minutes=total_estimated_minutes)
                change_since_last['estimated_completion_time'] = estimated_completion_time.isoformat()

                # Add human-readable estimate
                if total_estimated_minutes < 1:
                    change_since_last['estimated_time_readable'] = "Less than a minute"
                elif total_estimated_minutes < 60:
                    change_since_last['estimated_time_readable'] = f"~{round(total_estimated_minutes)} minutes"
                else:
                    hours = int(total_estimated_minutes // 60)
                    minutes = int(total_estimated_minutes % 60)
                    change_since_last['estimated_time_readable'] = f"~{hours}h {minutes}m"

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
        
        connection.commit()

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
            'change_since_last': change_since_last,
            'timestamp': current_timestamp.isoformat()
        })

    except Exception as e:
        print(f"Error in progress_bar: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'error_details': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/discovered-links', methods=['GET'])
@token_required
def get_discovered_links(user_id):
    connection = None
    cursor = None
    try:
        # Get the source_url parameter directly from the request
        source_url = request.args.get('source_url')
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Build query with user_id
        query = """
            SELECT LINK FROM {0}
            WHERE USER_ID = :user_id
        """.format(LINKS_TO_SCRAP_TABLE)
        
        params = {'user_id': user_id}
        
        if source_url:
            query += " AND TOP_LEVEL_SOURCE = :source_url"
            params['source_url'] = source_url
        
        # Execute query to get distinct links
        cursor.execute(f"""
            SELECT DISTINCT LINK FROM ({query})
        """, params)
        
        discovered_links = [row[0] for row in cursor.fetchall()]
        
        # Count by domain
        domains = {}
        for link in discovered_links:
            try:
                domain = link.split('//', 1)[1].split('/', 1)[0] if '//' in link else link.split('/', 1)[0]
                domains[domain] = domains.get(domain, 0) + 1
            except:
                continue
        
        # Convert to list of dictionaries for response
        domain_stats = [{'domain': domain, 'count': count} for domain, count in domains.items()]
        
        return jsonify({
            'status': 'success',
            'total_links': len(discovered_links),
            'domain_stats': domain_stats,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting discovered links: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@file_api.route('/remove-from-queue', methods=['POST'])
@token_required
def remove_from_queue(user_id):
    """Remove a URL from the processing queue"""
    connection = None
    cursor = None
    try:
        data = request.get_json()
        if not data or 'source_url' not in data:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the request body.',
                'timestamp': datetime.now().isoformat()
            }), 400
                
        source_url = data['source_url']
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Check if the URL is in the queue and not being processed
        cursor.execute("""
            SELECT ID FROM {0}
            WHERE USER_ID = :user_id 
            AND SOURCE_URL = :source_url
            AND PROCESSED = 0
            AND PROCESSING_STARTED IS NULL
        """.format(PROCESSING_QUEUE_TABLE), user_id=user_id, source_url=source_url)
        
        queue_row = cursor.fetchone()
        
        if not queue_row:
            return jsonify({
                'status': 'error',
                'message': f'URL {source_url} is not in the queue or is already being processed.',
                'timestamp': datetime.now().isoformat()
            }), 404
                
        # Remove the URL from the queue
        cursor.execute("""
            DELETE FROM {0}
            WHERE ID = :id
        """.format(PROCESSING_QUEUE_TABLE), id=queue_row[0])
        
        connection.commit()
        
        return jsonify({
            'status': 'success',
            'message': f'URL {source_url} has been removed from the queue.',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error removing from queue: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/scrapped-sub-links', methods=['POST'])
def scrapped_sub_links():
    """
    Fetch links related to a specific source URL with pagination
    Returns 10 URLs at a time with their processing status
    """
    connection = None
    cursor = None
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
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
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
        if connection:
            connection.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
@file_api.route('/get-pending-links', methods=['GET'])
@token_required
def get_pending_links(user_id):
    connection = None
    cursor = None
    try:
        source_url = request.args.get('source_url')
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
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
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/get-scrapped-links', methods=['GET'])
@token_required
def get_scrapped_links(user_id):
    connection = None
    cursor = None
    try:
        source_url = request.args.get('source_url')
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
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
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/get-total-words-scrapped', methods=['GET'])
@token_required
def get_total_words_scrapped(user_id):
    connection = None
    cursor = None
    try:
        source_url = request.args.get('source_url')
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Build the base query
        query = """
            SELECT SUM(LENGTH(REGEXP_REPLACE(SCRAPPED_CONTENT, ' +', ' ')) - 
                   LENGTH(REGEXP_REPLACE(SCRAPPED_CONTENT, ' ', '')) + 1) AS word_count
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
        
        return jsonify({
            'status': 'success',
            'total_words': total_words,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting total words: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/get-links-to-scrap', methods=['GET'])
@token_required
def get_links_to_scrap(user_id):
    connection = None
    cursor = None
    try:
        source_url = request.args.get('source_url')
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        query = """
            SELECT LINK FROM {0}
            WHERE USER_ID = :user_id
        """.format(LINKS_TO_SCRAP_TABLE)
        
        params = {'user_id': user_id}
        
        if source_url:
            query += " AND TOP_LEVEL_SOURCE = :source_url"
            params['source_url'] = source_url
            
        cursor.execute(query, params)
        
        links = [row[0] for row in cursor.fetchall()]
        
        return jsonify({
            'status': 'success',
            'links': links,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error getting links to scrap: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@file_api.route('/stop-processing', methods=['POST'])
@token_required
def stop_processing_job(user_id):  # Renamed to avoid conflicts
    """Stop the continuous processing for a specific source URL"""
    try:
        print(f"Request to stop processing for user: {user_id}")
        
        data = request.get_json()
        if not data or 'source_url' not in data:
            print("source_url is missing in request body")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
                
        source_url = data['source_url']
        print(f"Stopping processing for source URL: {source_url}")
        
        # Create user-specific key
        process_key = f"{user_id}:{source_url}"
        
        if process_key in processing_events:
            print(f"Found processing job for {process_key}, setting stop event")
            processing_events[process_key].set()
            
            # Remove from active jobs list if it exists
            if user_id in active_user_jobs and source_url in active_user_jobs[user_id]:
                active_user_jobs[user_id].remove(source_url)
                if not active_user_jobs[user_id]:
                    del active_user_jobs[user_id]
            
            # Check if there are more URLs in the queue and start the next one
            start_next = mark_as_complete_and_process_next(user_id, source_url)
            
            return jsonify({
                'status': 'success',
                'message': f'Processing for {source_url} has been stopped.',
                'next_started': start_next,
                'timestamp': datetime.now().isoformat()
            })
        else:
            print(f"No active processing found for {process_key}")
            return jsonify({
                'status': 'error',
                'message': f'No active processing found for {source_url}',
                'timestamp': datetime.now().isoformat()
            }), 404
                
    except Exception as e:
        print(f"Error stopping processing: {str(e)}")
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
    connection = None
    cursor = None
    try:
        print(f"Fetching all documents for user: {user_id}")
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Fetch all source URLs for this user, sorted by timestamp in descending order (latest first)
        print(f"Querying {SOURCE_URLS_TABLE} for user: {user_id}")
        cursor.execute("""
            SELECT SOURCE_URL, 
                   TO_CHAR(TIMESTAMP, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') as TIMESTAMP,
                   PAGE_LIMIT
            FROM {0}
            WHERE USER_ID = :user_id
            ORDER BY TIMESTAMP DESC
        """.format(SOURCE_URLS_TABLE), user_id=user_id)
        
        source_urls = cursor.fetchall()
        print(f"Found {len(source_urls)} source URLs for user: {user_id}")
        
        documents = []
        for source_url_row in source_urls:
            source_url, timestamp, page_limit = source_url_row
            print(f"Processing source URL: {source_url}")
            
            # Count the number of processed links for this source URL
            cursor.execute("""
                SELECT COUNT(*) FROM {0}
                WHERE TOP_LEVEL_SOURCE = :source_url
                AND IS_PROCESSED = 'true'
                AND USER_ID = :user_id
            """.format(LINKS_TO_SCRAP_TABLE), source_url=source_url, user_id=user_id)
            
            processed_count = cursor.fetchone()[0]
            
            # Count the number of scrapped texts for this source URL
            cursor.execute("""
                SELECT COUNT(*) FROM {0}
                WHERE TOP_LEVEL_SOURCE = :source_url
                AND USER_ID = :user_id
            """.format(SCRAPPED_TEXT_TABLE), source_url=source_url, user_id=user_id)
            
            scrapped_count = cursor.fetchone()[0]
            
            print(f"Source URL: {source_url}, Processed: {processed_count}, Scrapped: {scrapped_count}")
            
            # Check if this URL is in the queue
            cursor.execute("""
                SELECT COUNT(*), MIN(PROCESSED), MIN(PROCESSING_STARTED)
                FROM {0}
                WHERE USER_ID = :user_id AND SOURCE_URL = :source_url
            """.format(PROCESSING_QUEUE_TABLE), user_id=user_id, source_url=source_url)
            
            queue_count, is_processed, is_processing = cursor.fetchone()
            queue_item_exists = queue_count > 0
            
            # Determine if this source URL is active
            is_active = False
            if user_id in active_user_jobs and source_url in active_user_jobs[user_id]:
                is_active = True
            
            # Determine the status
            if is_active:
                status = 'In Progress'
            elif queue_item_exists and (is_processed == 0 and is_processing is None):
                # It's in the queue but not yet processing
                status = 'Queued'
            elif processed_count > 0 and processed_count == scrapped_count:
                status = 'Completed'
            else:
                status = 'Pending'
            
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
            
            # Append the document to the list
            documents.append({
                'source_link': source_url,
                'status': status,
                'processed_links': processed_count,
                'scrapped_texts': scrapped_count,
                'last_scrapped': timestamp,
                'queue_position': queue_position,
                'is_active': is_active,
                'page_limit': page_limit
            })
        
        return jsonify({
            'status': 'success',
            'documents': documents,
            'has_active_job': user_has_active_job(user_id),
            'timestamp': datetime.now().isoformat()
        })
    
    except Exception as e:
        print(f"Error fetching all documents: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(e), 'DB_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
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
        
    return jsonify(response), status_code
    
# Example usage in an endpoint:
# try:
#     # Some operation
#     pass
# except oracledb.DatabaseError as e:
#     error, = e.args
#     if error.code == 1:  # Specific Oracle error code
#         return standardize_error_response("Database constraint violation", "DB_CONSTRAINT", 400)
#     else:
#         return standardize_error_response(e, "DB_ERROR", 500)
# except Exception as e:
#     return standardize_error_response(e, "SERVER_ERROR", 500)
