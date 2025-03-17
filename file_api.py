from flask import Blueprint, request, jsonify
from pymongo import MongoClient
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
import re
import validators
from urllib.parse import urljoin
import time
import random
import traceback
from bson.objectid import ObjectId
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import current_app
from threading import Lock, Thread, Event
import jwt

# Load environment variables from .env file
load_dotenv()

file_api = Blueprint('file_api', __name__)  # Using Blueprint instead of Flask

# MongoDB connection configuration
MONGO_URI = os.environ.get('MONGO_URI')
DB_NAME = 'scrapper'
SOURCE_COLLECTION = 'Content_Links'
LINKS_COLLECTION = 'Links_to_scrap'
CONTENT_COLLECTION = 'scrapped_text'
SECRET_KEY = os.environ.get('SECRET_KEY')

# Global variables to track processing state
processing_events = {}
crawling_events = {}

def get_mongo_client():
    """Establish connection to MongoDB"""
    try:
        print(f"Connecting to MongoDB with URI: {MONGO_URI[:10]}...{MONGO_URI[-5:]}")
        client = MongoClient(MONGO_URI)
        # Test the connection
        db_names = client.list_database_names()
        print(f"Connected to MongoDB. Available databases: {db_names}")
        return client
    except Exception as e:
        print(f"MongoDB connection error: {str(e)}")
        traceback.print_exc()
        raise

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

def extract_urls_from_page(url):
    """
    Extract all URLs from a page that likely contain text content
    """
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
        
        # Find all anchor tags
        all_links = soup.find_all('a', href=True)
        
        # Base URL for resolving relative URLs
        base_url = url
        
        # Check if the HTML has a base tag
        base_tag = soup.find('base', href=True)
        if base_tag:
            base_url = base_tag['href']
        
        # Extract URLs
        valid_urls = []
        
        for link in all_links:
            href = link['href'].strip()
            
            # Skip empty hrefs, javascript:, mailto:, tel: links
            if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                continue
                
            try:
                # Convert relative URLs to absolute URLs
                full_url = urljoin(base_url, href)
                
                # Skip invalid URLs and non-content URLs
                if not is_valid_url(full_url) or not is_valid_content_url(full_url):
                    continue
                    
                valid_urls.append(full_url)
            except Exception as e:
                print(f"Error processing URL {href}: {str(e)}")
                continue
        
        return {
            'status': 'success',
            'url': url,
            'links_found': len(valid_urls),
            'links': valid_urls
        }
    
    except requests.exceptions.RequestException as e:
        return {
            'status': 'error',
            'url': url,
            'error': f"Request error: {str(e)}"
        }
    except Exception as e:
        return {
            'status': 'error',
            'url': url,
            'error': f"Error: {str(e)}",
            'traceback': traceback.format_exc()
        }

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

def continuous_crawl_job(top_level_source_url, stop_event, user_id):
    """
    Worker function to continuously crawl pages until all links are processed
    or stop_event is set
    """
    client = None
    try:
        print(f"Starting continuous crawl job for {top_level_source_url} for user {user_id}")
        
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
        
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        # Stats counters
        links_crawled = 0
        links_added = 0
        errors_encountered = 0
        
        # Set to track consecutive empty runs (no new links added)
        consecutive_empty_runs = 0
        max_consecutive_empty_runs = 3  # After this many empty runs, terminate
        
        while not stop_event.is_set():
            # Find an uncrawled link for this specific user - without using lock
            links_remaining = links_collection.count_documents({
                'is_crawled': False,
                'top_level_source': top_level_source_url,
                'user_id': user_id
            })
            
            # If no uncrawled links remain, we're done
            if links_remaining == 0:
                print(f"No more uncrawled links for {top_level_source_url} for user {user_id}. Exiting crawl job.")
                break
            
            print(f"Found {links_remaining} uncrawled links to process")
            
            # Get one uncrawled link - Use findAndModify operation (find_one_and_update) which is atomic
            link_doc = links_collection.find_one_and_update(
                {'is_crawled': False, 'top_level_source': top_level_source_url, 'user_id': user_id},
                {'$set': {'crawling_started': datetime.now()}},
                return_document=True
            )
            
            if not link_doc:
                # Unlikely, but possible race condition
                print("No uncrawled link found despite count showing some exist. Retrying after delay.")
                time.sleep(1)
                continue
            
            # Process the URL
            url_to_crawl = link_doc['link']
            current_depth = link_doc.get('depth', 0)
            top_level_source = link_doc.get('top_level_source', top_level_source_url)
            
            print(f"Processing URL: {url_to_crawl} for user {user_id}")
            
            # Skip social media URLs
            if is_social_media_url(url_to_crawl):
                print(f"Skipping social media URL: {url_to_crawl}")
                # Update without using lock
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {
                        'is_crawled': True, 
                        'crawled_at': datetime.now(),
                        'skipped': True,
                        'skip_reason': 'social_media'
                    }}
                )
                links_crawled += 1
                continue
            
            try:
                # Add user agent to avoid being blocked
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                
                # Make request to the URL
                print(f"Making HTTP request to: {url_to_crawl}")
                response = requests.get(url_to_crawl, headers=headers, timeout=30)
                response.raise_for_status()  # Raise exception for 4XX/5XX responses
                
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
                valid_urls = []
                
                for link in all_links:
                    href = link['href'].strip()
                    
                    # Skip empty hrefs, javascript:, mailto:, tel: links
                    if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                        continue
                    
                    try:
                        # Convert relative URLs to absolute URLs
                        full_url = urljoin(base_url, href)
                        
                        # Check if the URL belongs to the same domain - DOMAIN RESTRICTION
                        link_domain = extract_domain(full_url)
                        if link_domain != original_domain:
                            # print(f"Skipping link from different domain: {full_url} (domain: {link_domain})")
                            continue
                        
                        # Skip invalid URLs, non-content URLs, and social media URLs
                        if not is_valid_url(full_url) or not is_valid_content_url(full_url) or is_social_media_url(full_url):
                            continue
                        
                        # Check if the URL already exists in Links_to_scrap for this user - without lock
                        existing_link = links_collection.find_one({
                            'link': full_url,
                            'user_id': user_id
                        })
                        if existing_link:
                            continue
                            
                        valid_urls.append(full_url)
                    except Exception as e:
                        print(f"Error processing URL {href}: {str(e)}")
                        continue
                
                # Remove duplicates
                unique_links = list(set(valid_urls))
                print(f"Found {len(unique_links)} valid URLs on {url_to_crawl} for user {user_id}")
                
                # If no valid links are found, mark the URL as crawled and continue
                if not unique_links:
                    print(f"No valid links found on {url_to_crawl}")
                    links_collection.update_one(
                        {'_id': link_doc['_id']},
                        {'$set': {
                            'is_crawled': True, 
                            'crawled_at': datetime.now(),
                            'links_found': 0,
                            'links_added': 0
                        }}
                    )
                    links_crawled += 1
                    consecutive_empty_runs += 1
                    continue
                
                # Store in Content_Links for reference - without lock
                source_collection = db[SOURCE_COLLECTION]
                source_document = {
                    'source_url': url_to_crawl,
                    'uniqueLinks': unique_links,
                    'crawled_at': datetime.now(),
                    'depth': current_depth,
                    'top_level_source': top_level_source,
                    'user_id': user_id  # Associate with specific user
                }
                
                print(f"Storing source document for {url_to_crawl}")
                source_result = source_collection.insert_one(source_document)
                print(f"Source document stored with ID: {source_result.inserted_id}")
                
                # Add all links to Links_to_scrap
                current_links_added = 0
                
                for link in unique_links:
                    # Add to Links_to_scrap for further crawling (only if not already exists) - without lock
                    print(f"Adding link to queue: {link}")
                    crawl_doc = {
                        'link': link,
                        'added_at': datetime.now(),
                        'is_crawled': False,
                        'is_processed': False,
                        'source_url': url_to_crawl,
                        'top_level_source': top_level_source,
                        'depth': current_depth + 1,
                        'has_text_in_url': contains_text_in_url(link),
                        'user_id': user_id
                    }
                    
                    try:
                        # Use upsert with a filter to prevent duplicates
                        result = links_collection.update_one(
                            {'link': link, 'user_id': user_id},
                            {'$setOnInsert': crawl_doc},
                            upsert=True
                        )
                        
                        if result.upserted_id:
                            current_links_added += 1
                            print(f"Added new URL to Links_to_scrap: {link} for user {user_id}")
                    except Exception as insert_error:
                        print(f"Error adding link {link} to queue: {str(insert_error)}")
                
                # Mark this link as crawled - without lock
                print(f"Marking URL as crawled: {url_to_crawl}")
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {
                        'is_crawled': True, 
                        'crawled_at': datetime.now(),
                        'links_found': len(unique_links),
                        'links_added': current_links_added
                    }}
                )
                print(f"Marked URL as crawled: {url_to_crawl} for user {user_id}")
                
                links_crawled += 1
                links_added += current_links_added
                
                # Check if we're making progress
                if current_links_added > 0:
                    consecutive_empty_runs = 0  # Reset counter because we found new links
                else:
                    consecutive_empty_runs += 1
                    
                # If we've had too many consecutive runs with no new links, terminate
                if consecutive_empty_runs >= max_consecutive_empty_runs:
                    print(f"No new links found for {max_consecutive_empty_runs} consecutive runs. Terminating crawl job.")
                    break
                
            except requests.exceptions.RequestException as e:
                error_msg = f"Request error: {str(e)}"
                print(f"Error processing URL {url_to_crawl}: {error_msg}")
                
                # Update without lock
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {
                        'is_crawled': True, 
                        'crawled_at': datetime.now(),
                        'error': error_msg
                    }}
                )
                errors_encountered += 1
                consecutive_empty_runs += 1
                
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                print(f"Error processing URL {url_to_crawl}: {error_msg}")
                traceback.print_exc()
                
                # Update without lock
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {
                        'is_crawled': True, 
                        'crawled_at': datetime.now(),
                        'error': error_msg
                    }}
                )
                errors_encountered += 1
                consecutive_empty_runs += 1
            
            # Optional: Sleep to prevent hammering the target server
            time.sleep(0.5)
        
        print(f"Crawl job completed: {links_crawled} links crawled, {links_added} links added, {errors_encountered} errors")
        
        # Return stats if we exit the loop
        return {
            'links_crawled': links_crawled,
            'links_added': links_added,
            'errors_encountered': errors_encountered
        }
            
    except Exception as e:
        print(f"Error in continuous crawl job: {str(e)}")
        traceback.print_exc()
        return {
            'error': str(e),
            'traceback': traceback.format_exc()
        }
    finally:
        if client:
            client.close()

def continuous_processing_job(top_level_source_url, stop_event, delay_seconds=180, user_id=None):
    """
    Worker function to continuously process all the scraped links
    until all links in Links_to_scrap are processed (either successfully or failed).
    Includes an initial delay before starting processing.
    """
    client = None
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
        
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        # Stats counters
        links_processed = 0
        success_count = 0
        error_count = 0
        
        # Batch size for processing
        batch_size = 50
        
        # Keep processing until explicitly stopped or all links are processed
        while not stop_event.is_set():
            # Find a batch of unprocessed links for this user without using lock
            query = {
                'is_processed': False,
                'top_level_source': top_level_source_url
            }
            
            # Add user_id filter if provided
            if user_id:
                query['user_id'] = user_id
                
            unprocessed_links = list(links_collection.find(query).limit(batch_size))
            
            # If no unprocessed links remain, check if we're truly done
            if not unprocessed_links:
                print(f"No unprocessed links found in this batch. Double-checking after delay.")
                # Double-check after a brief delay to ensure no race conditions
                time.sleep(3)
                
                unprocessed_count = links_collection.count_documents(query)
                
                if unprocessed_count == 0:
                    print(f"No more unprocessed links for {top_level_source_url} for user {user_id}. Exiting processing job.")
                    break
                else:
                    print(f"Found {unprocessed_count} unprocessed links after delay. Continuing processing.")
                    # If we found more unprocessed links after the delay, continue the loop
                    continue
            
            print(f"Processing batch of {len(unprocessed_links)} links")
            
            # Process each link in the batch
            for link_doc in unprocessed_links:
                if stop_event.is_set():
                    print(f"Stop event triggered. Exiting processing job for {top_level_source_url}.")
                    break
                
                try:
                    link_url = link_doc.get('link', 'unknown')
                    print(f"Processing link: {link_url}")
                    
                    # Process the link including user_id
                    result = scrape_single_link(db, link_doc, user_id)
                    
                    if result['status'] == 'success':
                        success_count += 1
                        print(f"Successfully processed link: {link_url}")
                    else:
                        # Mark the link as failed instead of processed
                        error_count += 1
                        print(f"Failed to process link: {link_url}. Error: {result.get('error', 'Unknown error')}")
                        
                        # Update without using lock - MongoDB handles concurrency
                        links_collection.update_one(
                            {'_id': link_doc['_id']},
                            {'$set': {
                                'is_processed': "Failed",  # Use "Failed" instead of True
                                'processed_at': datetime.now(),
                                'error': result.get('error', 'Unknown error'),
                                'traceback': result.get('traceback', '')
                            }}
                        )
                    
                    links_processed += 1
                    
                    # Log progress periodically
                    if links_processed % 20 == 0:
                        print(f"Processed {links_processed} links for {top_level_source_url} ({success_count} successful, {error_count} failed) for user {user_id}")
                    
                except Exception as e:
                    # Catch any unexpected errors during processing
                    error_msg = f"Unexpected error processing link {link_doc.get('link', 'unknown')}: {str(e)}"
                    print(error_msg)
                    traceback.print_exc()
                    
                    # Mark the link as failed without using lock
                    links_collection.update_one(
                        {'_id': link_doc['_id']},
                        {'$set': {
                            'is_processed': "Failed",
                            'processed_at': datetime.now(),
                            'error': error_msg,
                            'traceback': traceback.format_exc()
                        }}
                    )
                    error_count += 1
                
                # Optional: Sleep to prevent hammering the target server
                time.sleep(0.5)
        
        print(f"Processing job completed: {links_processed} links processed, {success_count} successful, {error_count} failed")
        
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
        if client:
            client.close()

def scrape_single_link(db, link_doc, user_id=None):
    """Helper function to scrape a single link"""
    print(f"Starting to scrape link: {link_doc.get('link')} for user: {user_id}")
    
    link = link_doc['link']
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
    
    # Get collections
    links_collection = db[LINKS_COLLECTION]
    content_collection = db[CONTENT_COLLECTION]
    
    print(f"Got collection references. Starting to scrape content from {link}")
    
    try:
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
        except Exception as req_error:
            print(f"Request error: {str(req_error)}")
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
        
        # Create document for scraped content
        print("Creating content document")
        content_document = {
            'scrapped_content': text,
            'content_link': link,
            'scrape_date': datetime.now(),
            'link_id': link_doc['_id'],
            'source_url': source_url,  # Immediate parent URL
            'top_level_source': top_level_source,  # Original source URL
            'depth': link_doc.get('depth', 0),
            'title': title_text,
            'user_id': link_doc_user_id  # Add user_id to associate with specific user
        }
        
        # Check if content already exists to avoid duplicates
        existing_content = content_collection.find_one({
            'content_link': link,
            'user_id': link_doc_user_id
        })
        
        if existing_content:
            print(f"Content already exists for {link}, skipping insertion")
            content_id = existing_content['_id']
        else:
            # Insert into content collection - don't use lock
            print(f"Inserting content into database for {link}")
            try:
                result = content_collection.insert_one(content_document)
                content_id = result.inserted_id
                print(f"Content inserted with ID: {content_id}")
                
                # Verify the document was inserted
                inserted_doc = content_collection.find_one({'_id': content_id})
                if inserted_doc:
                    print(f"Content document insertion verified")
                else:
                    print(f"Warning: Could not verify content document insertion")
            except Exception as db_error:
                print(f"Database error inserting content: {str(db_error)}")
                traceback.print_exc()
                raise
        
        # Update the link as processed - don't use lock
        print(f"Updating link status to processed for {link}")
        try:
            update_result = links_collection.update_one(
                {'_id': link_doc['_id']},
                {'$set': {
                    'is_processed': True,  # Successfully processed
                    'processed_at': datetime.now(),
                    'top_level_source': top_level_source
                }}
            )
            print(f"Link status update result: {update_result.modified_count} document(s) modified")
            
            # Verify update
            updated_link = links_collection.find_one({'_id': link_doc['_id']})
            if updated_link and updated_link.get('is_processed') is True:
                print(f"Link status update verified")
            else:
                print(f"Warning: Could not verify link status update")
        except Exception as update_error:
            print(f"Database error updating link status: {str(update_error)}")
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
        
        # Do not update is_processed here, let the calling function handle it
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
        
        # Do not update is_processed here, let the calling function handle it
        return {
            'status': 'error',
            'link': link,
            'error': error_msg,
            'traceback': tb,
            'top_level_source': top_level_source
        }

@file_api.route('/recursive-crawl', methods=['POST'])
@token_required
def recursive_crawl(user_id):
    """
    Start a background thread to continuously crawl pages from Links_to_scrap collection.
    This function will immediately return and the crawling will continue in the background.
    """
    client = None
    try:
        print(f"Starting recursive crawl for user ID: {user_id}")  # Debug print
        
        # Get MongoDB client
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get collections
        links_collection = db[LINKS_COLLECTION]
        source_urls_collection = db['Source_Urls']
        
        # Get the URL from the request body
        data = request.get_json()
        print(f"Request data: {data}")  # Debug print
        
        if not data or 'url' not in data:
            print("URL is missing in request body")  # Debug print
            return jsonify({
                'status': 'error',
                'message': 'URL is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Get the top-level source URL provided by the user
        top_level_source_url = data['url']
        print(f"Top level source URL: {top_level_source_url}")  # Debug print
        
        # Validate URL format
        if not is_valid_url(top_level_source_url):
            print(f"Invalid URL format: {top_level_source_url}")  # Debug print
            return jsonify({
                'status': 'error',
                'message': f'Invalid URL format: {top_level_source_url}',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Check if already crawling this URL for this user
        crawl_key = f"{user_id}:{top_level_source_url}"
        if crawl_key in crawling_events and not crawling_events[crawl_key].is_set():
            print(f"Crawling already in progress for: {crawl_key}")  # Debug print
            return jsonify({
                'status': 'info',
                'message': f'Crawling for {top_level_source_url} is already in progress.',
                'timestamp': datetime.now().isoformat()
            })
        
        # Check if the URL has already been crawled completely
        links_count = links_collection.count_documents({
            'top_level_source': top_level_source_url,
            'user_id': user_id
        })
        
        uncrawled_count = links_collection.count_documents({
            'top_level_source': top_level_source_url,
            'is_crawled': False,
            'user_id': user_id
        })
        
        print(f"Found {links_count} total links and {uncrawled_count} uncrawled links")  # Debug print
        
        if links_count > 0 and uncrawled_count == 0:
            print(f"URL already completely crawled: {top_level_source_url}")  # Debug print
            return jsonify({
                'status': 'info',
                'message': f'URL {top_level_source_url} has already been completely crawled.',
                'stats': {
                    'total_links': links_count
                },
                'timestamp': datetime.now().isoformat()
            })
        
        # Check if the URL exists in Links_to_scrap
        existing_link = links_collection.find_one({
            'link': top_level_source_url,
            'user_id': user_id
        })
        
        print(f"Existing link found: {existing_link is not None}")  # Debug print
        
        # If the URL is not in Links_to_scrap, add it as a new starting point
        if not existing_link:
            try:
                print(f"Inserting initial URL to Links_to_scrap: {top_level_source_url}")  # Debug print
                insert_doc = {
                    'link': top_level_source_url,
                    'added_at': datetime.now(),
                    'is_crawled': False,
                    'is_processed': False,
                    'depth': 0,
                    'source_url': top_level_source_url,
                    'top_level_source': top_level_source_url,
                    'user_id': user_id  # Associate with specific user
                }
                
                # Use update_one with upsert instead of insert_one to prevent duplicates
                insert_result = links_collection.update_one(
                    {'link': top_level_source_url, 'user_id': user_id},
                    {'$setOnInsert': insert_doc},
                    upsert=True
                )
                
                print(f"Insert result: {insert_result.upserted_id or 'Already exists'}")  # Debug print
                
                # Verify the document was inserted
                inserted_doc = links_collection.find_one({'link': top_level_source_url, 'user_id': user_id})
                print(f"Inserted document verification: {inserted_doc is not None}")
                if inserted_doc:
                    print(f"Document found with ID: {inserted_doc.get('_id')}")
            except Exception as e:
                print(f"Error inserting URL: {str(e)}")  # Debug print
                traceback.print_exc()
                raise
        
        # Save the source URL and timestamp in the Source_Urls collection
        try:
            print(f"Inserting source URL to Source_Urls collection: {top_level_source_url}")  # Debug print
            # Create source document without timestamp field
            source_doc = {
                'source_url': top_level_source_url,
                'user_id': user_id  # Associate with specific user
            }
            
            # First try to find if the document already exists
            existing_source = source_urls_collection.find_one({
                'source_url': top_level_source_url,
                'user_id': user_id
            })
            
            if existing_source:
                # If exists, just update the timestamp
                source_urls_collection.update_one(
                    {'_id': existing_source['_id']},
                    {'$set': {'timestamp': datetime.now()}}
                )
                print(f"Updated timestamp for existing source URL record")
            else:
                # If not exists, create new with timestamp
                source_doc['timestamp'] = datetime.now()
                source_result = source_urls_collection.insert_one(source_doc)
                print(f"Source URL insert result: {source_result.inserted_id}")
                
        except Exception as e:
            print(f"Error inserting source URL: {str(e)}")  # Debug print
            traceback.print_exc()
            raise
        
        # Create a new stop event for this crawl job
        stop_event = Event()
        crawling_events[crawl_key] = stop_event
        
        print(f"Starting crawler thread for {top_level_source_url}")  # Debug print
        
        # Define a wrapper function to start the crawling
        def start_crawl(url, stop_event, user_id):
            try:
                print(f"Starting crawl thread for {url}, user: {user_id}")
                result = continuous_crawl_job(url, stop_event, user_id)
                print(f"Crawling completed with result: {result}")
            except Exception as e:
                print(f"Error in crawl thread: {e}")
                traceback.print_exc()
        
        # Start the crawling in a background thread
        crawler_thread = Thread(
            target=start_crawl,
            args=(top_level_source_url, stop_event, user_id),
            daemon=True
        )
        crawler_thread.start()
        
        return jsonify({
            'status': 'success',
            'message': f'Continuous crawling started for {top_level_source_url}',
            'timestamp': datetime.now().isoformat(),
            'source_url': top_level_source_url
        })
    
    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error in crawling: {str(e)}\n{traceback_str}")
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback_str,
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()
            
@file_api.route('/process-all-links', methods=['POST'])
@token_required
def process_all_links(user_id):
    """
    Start a background thread to continuously process all links in the Links_to_scrap collection
    until all links are processed (is_processed: true).
    Includes an option to delay the start of processing.
    """
    client = None
    try:
        print(f"Starting process_all_links for user ID: {user_id}")
        
        # Get the delay parameter (default: 3 minutes = 180 seconds)
        data = request.get_json() or {}
        delay_seconds = data.get('delay', 180)
        source_url = data.get('source_url')
        
        print(f"Request data: delay={delay_seconds}, source_url={source_url}")
        
        if not source_url:
            print("source_url is missing in request body")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
            
        # Check if already processing this URL for this user
        process_key = f"{user_id}:{source_url}"
        if process_key in processing_events and not processing_events[process_key].is_set():
            print(f"Processing already in progress for: {process_key}")
            return jsonify({
                'status': 'info',
                'message': f'Processing for {source_url} is already in progress.',
                'timestamp': datetime.now().isoformat()
            })
            
        # Get MongoDB client
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get collections
        links_collection = db[LINKS_COLLECTION]
        
        # Check how many unprocessed links exist for this user
        unprocessed_count = links_collection.count_documents({
            'is_processed': False,
            'top_level_source': source_url,
            'user_id': user_id
        })
        
        print(f"Found {unprocessed_count} unprocessed links for source: {source_url}, user: {user_id}")
        
        if unprocessed_count == 0:
            print(f"No unprocessed links found for {source_url}")
            return jsonify({
                'status': 'complete',
                'message': f'No unprocessed links found for {source_url}',
                'timestamp': datetime.now().isoformat()
            })
        
        # Create a new stop event for this processing job
        stop_event = Event()
        processing_events[process_key] = stop_event
        
        # Define a wrapper function to start the processing
        def start_processing(source_url, stop_event, delay_seconds, user_id):
            try:
                print(f"Starting processing thread for {source_url}, user: {user_id}, delay: {delay_seconds}s")
                result = continuous_processing_job(source_url, stop_event, delay_seconds, user_id)
                print(f"Processing completed with result: {result}")
            except Exception as e:
                print(f"Error in processing thread: {e}")
                traceback.print_exc()
        
        # Start the processing in a background thread with the specified delay
        processor_thread = Thread(
            target=start_processing,
            args=(source_url, stop_event, delay_seconds, user_id),
            daemon=True
        )
        processor_thread.start()
        print(f"Processing thread started for {source_url}")
        
        return jsonify({
            'status': 'success',
            'message': f'Continuous processing started for {source_url} (with {delay_seconds}s initial delay)',
            'unprocessed_links': unprocessed_count,
            'timestamp': datetime.now().isoformat(),
            'source_url': source_url
        })
    
    except Exception as e:
        traceback_str = traceback.format_exc()
        print(f"Error in processing: {str(e)}\n{traceback_str}")
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback_str,
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/source-url-status', methods=['GET'])
@token_required
def get_source_url_status(user_id):
    """Get the status of a specific source URL for the authenticated user"""
    client = None
    try:
        print(f"Getting source URL status for user: {user_id}")
        
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        
        source_url = request.args.get('source_url')
        if not source_url:
            print("source_url parameter is missing")
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        print(f"Checking status for source URL: {source_url}")
        
        # Count total URLs associated with this source for this user
        total_urls = links_collection.count_documents({
            'top_level_source': source_url,
            'user_id': user_id
        })
        
        # Count processed URLs for this source (both successful and failed)
        successful_processed = links_collection.count_documents({
            'top_level_source': source_url, 
            'is_processed': True,
            'user_id': user_id
        })
        failed_processed = links_collection.count_documents({
            'top_level_source': source_url, 
            'is_processed': "Failed",
            'user_id': user_id
        })
        total_processed = successful_processed + failed_processed
        
        # Count scraped URLs for this source
        total_scrapped = content_collection.count_documents({
            'top_level_source': source_url,
            'user_id': user_id
        })
        
        print(f"Stats: Total URLs: {total_urls}, Successful: {successful_processed}, Failed: {failed_processed}, Total Processed: {total_processed}, Scrapped: {total_scrapped}")
        
        # Determine the status
        if total_urls == 0:
            # If no URLs are found for this source, it's pending
            status = 'Pending'
        elif total_processed == total_urls:
            # Only mark as "Completed" if all URLs are processed (either successfully or failed)
            status = 'Completed'
        else:
            # Otherwise, it's still pending
            status = 'Pending'
        
        print(f"Source status determined as: {status}")
        
        return jsonify({
            'status': 'success',
            'source_url': source_url,
            'data': {
                'status': status,
                'total_urls': total_urls,
                'successful_processed': successful_processed,
                'failed_processed': failed_processed,
                'total_processed': total_processed,
                'scraped_urls': total_scrapped
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
    finally:
        if client:
            client.close()

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
            return jsonify({
                'status': 'success',
                'message': f'Crawling for {source_url} has been stopped.',
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
            return jsonify({
                'status': 'success',
                'message': f'Processing for {source_url} has been stopped.',
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

@file_api.route('/realtime-stats/links-to-scrap', methods=['GET'])
@token_required
def get_links_to_scrap(user_id):
    """Get all links in the Links_to_scrap collection, filtered by source URL for the current user"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_to_scrap_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        query = {'top_level_source': source_url, 'user_id': user_id} if source_url else {'user_id': user_id}
        
        links_to_scrap = list(links_to_scrap_collection.find(query, {'link': 1, 'top_level_source': 1, '_id': 0}))
        
        return jsonify({
            'status': 'success',
            'links': links_to_scrap,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/realtime-stats/total-processed-links', methods=['GET'])
@token_required
def get_total_processed_links(user_id):
    """Get the total number of processed links, filtered by source URL for the current user"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        if source_url:
            query = {'top_level_source': source_url, 'is_processed': True, 'user_id': user_id}
        else:
            query = {'is_processed': True, 'user_id': user_id}
        
        total_processed_links = links_collection.count_documents(query)
        
        return jsonify({
            'status': 'success',
            'total_processed_links': total_processed_links,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/realtime-stats/scrapped-links', methods=['GET'])
@token_required
def get_scrapped_links(user_id):
    """Get the number of scrapped links, filtered by source URL for the current user"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        scrapped_text_collection = db[CONTENT_COLLECTION]
        
        source_url = request.args.get('source_url')
        if source_url:
            query = {'top_level_source': source_url, 'user_id': user_id}
        else:
            query = {'user_id': user_id}
        
        scrapped_links_count = scrapped_text_collection.count_documents(query)
        
        return jsonify({
            'status': 'success',
            'scrapped_links': scrapped_links_count,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/realtime-stats/pending-links', methods=['GET'])
@token_required
def get_pending_links(user_id):
    """Get the number of pending links, filtered by source URL for the current user"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        if source_url:
            query = {'top_level_source': source_url, 'is_processed': False, 'user_id': user_id}
        else:
            query = {'is_processed': False, 'user_id': user_id}
        
        pending_links = links_collection.count_documents(query)
        
        return jsonify({
            'status': 'success',
            'pending_links': pending_links,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/realtime-stats/total-words-scrapped', methods=['GET'])
@token_required
def get_total_words_scrapped(user_id):
    """Get the total number of words scraped, filtered by source URL for the current user"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        scrapped_text_collection = db[CONTENT_COLLECTION]
        
        source_url = request.args.get('source_url')
        pipeline = []
        
        # Build the match condition based on whether a source_url is provided
        match_condition = {"user_id": user_id}
        if source_url:
            match_condition["top_level_source"] = source_url
            
        pipeline.append({"$match": match_condition})
        
        pipeline.extend([
            {
                "$project": {
                    "word_count": { "$size": { "$split": ["$scrapped_content", " "] } }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total_words": { "$sum": "$word_count" }
                }
            }
        ])
        
        result = list(scrapped_text_collection.aggregate(pipeline))
        total_words_scrapped = result[0]['total_words'] if result else 0
        
        return jsonify({
            'status': 'success',
            'total_words_scrapped': total_words_scrapped,
            'source_url': source_url,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/scrapped-sub-links', methods=['POST'])
def scrapped_sub_links():
    """
    Fetch links related to a specific source URL with pagination
    Returns 10 URLs at a time with their processing status
    """
    client = None
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
        
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        
        # Calculate skip value for pagination
        skip = (page - 1) * page_size
        
        # Fetch links for this source
        query = {'top_level_source': source_url}
        total_links = links_collection.count_documents(query)
        
        # Get paginated links
        links_cursor = links_collection.find(
            query, 
            {'link': 1, 'is_processed': 1, '_id': 0}
        ).skip(skip).limit(page_size)
        
        links_data = []
        
        for link_doc in links_cursor:
            # Determine URL status
            url_status = "Completed" if link_doc.get('is_processed') is True else "Pending"
            
            # If is_processed is "Failed", mark as failed
            if link_doc.get('is_processed') == "Failed":
                url_status = "Failed"
                
            links_data.append({
                'url': link_doc['link'],
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
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/all-documents', methods=['GET'])
@token_required
def get_all_documents(user_id):
    """Get all documents with source_link and status for the authenticated user"""
    client = None
    try:
        print(f"Fetching all documents for user: {user_id}")
        
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get collections
        source_urls_collection = db['Source_Urls']
        links_collection = db[LINKS_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        
        # Fetch all source URLs for this user, sorted by timestamp in descending order (latest first)
        print(f"Querying Source_Urls collection for user: {user_id}")
        source_urls = list(source_urls_collection.find({'user_id': user_id}).sort('timestamp', -1))
        
        print(f"Found {len(source_urls)} source URLs for user: {user_id}")
        
        documents = []
        
        for source_url_doc in source_urls:
            source_url = source_url_doc['source_url']
            print(f"Processing source URL: {source_url}")
            
            # Count the number of processed links for this source URL
            processed_count = links_collection.count_documents({
                'top_level_source': source_url,
                'is_processed': True,
                'user_id': user_id
            })
            
            # Count the number of scrapped texts for this source URL
            scrapped_count = content_collection.count_documents({
                'top_level_source': source_url,
                'user_id': user_id
            })
            
            print(f"Source URL: {source_url}, Processed: {processed_count}, Scrapped: {scrapped_count}")
            
            # Determine the status
            status = 'Completed' if processed_count > 0 and processed_count == scrapped_count else 'Pending'
            
            # Append the document to the list
            documents.append({
                'source_link': source_url,
                'status': status,
                'processed_links': processed_count,
                'scrapped_texts': scrapped_count,
                'last_scrapped': source_url_doc['timestamp'].isoformat()
            })
        
        return jsonify({
            'status': 'success',
            'documents': documents,
            'timestamp': datetime.now().isoformat()
        })
    
    except Exception as e:
        print(f"Error fetching all documents: {str(e)}")
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

@file_api.route('/progress-bar', methods=['GET'])
def get_progress_bar():
    """
    Get the progress information for crawling and scraping operations
    to display in a progress bar in the frontend.

    Query Parameters:
    - source_url: The top-level source URL to track progress for

    Returns:
    - crawl_progress: Percentage of links that have been crawled
    - scrape_progress: Percentage of links that have been scraped
    - crawled_count: Total number of links crawled
    - total_links: Total number of links found
    - scraped_count: Total number of links scraped
    - status: Current status of the operation
    - change_since_last: Dictionary containing change in progress percentages
    """
    client = None
    try:
        # Get source URL from query parameters
        source_url = request.args.get('source_url')

        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400

        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        progress_history_collection = db['Progress_History']

        # Get the last progress record for this source URL
        last_progress = progress_history_collection.find_one(
            {'source_url': source_url},
            sort=[('timestamp', -1)]
        )

        # Get total links for this source URL
        total_links = links_collection.count_documents({'top_level_source': source_url})

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
        crawled_count = links_collection.count_documents({
            'top_level_source': source_url,
            'is_crawled': True
        })

        crawl_progress = round((crawled_count / total_links) * 100, 1) if total_links > 0 else 0

        # Calculate scraping progress based on is_processed field
        # Count successfully processed links (not Failed)
        scraped_count = links_collection.count_documents({
            'top_level_source': source_url,
            'is_processed': True
        })

        scrape_progress = round((scraped_count / total_links) * 100, 1) if total_links > 0 else 0

        # Determine the overall status
        status = 'completed'

        # If there are still links to be crawled
        if crawled_count < total_links:
            status = 'crawling'
        # If all links are crawled but not all are processed
        elif scraped_count < total_links:
            status = 'processing'
        # If the source URL is in the active crawling or processing lists
        elif source_url in crawling_events and not crawling_events[source_url].is_set():
            status = 'crawling'
        elif source_url in processing_events and not processing_events[source_url].is_set():
            status = 'processing'

        # Active crawling and processing status check
        is_crawling_active = source_url in crawling_events and not crawling_events[source_url].is_set()
        is_processing_active = source_url in processing_events and not processing_events[source_url].is_set()

        # Calculate change in percentages since last check
        current_timestamp = datetime.now()
        change_since_last = {
            'crawl_progress_change': 0,
            'scrape_progress_change': 0,
            'links_per_minute': 0,
            'scrape_per_minute': 0,
            'time_since_last': 0, # seconds
            'estimated_completion_time': None,
            'estimated_completion_minutes': None
        }

        if last_progress:
            last_timestamp = last_progress.get('timestamp')
            time_diff_seconds = (current_timestamp - last_timestamp).total_seconds()
            time_diff_minutes = time_diff_seconds / 60

            change_since_last['crawl_progress_change'] = round(crawl_progress - last_progress.get('crawl_progress', 0), 1)
            change_since_last['scrape_progress_change'] = round(scrape_progress - last_progress.get('scrape_progress', 0), 1)
            change_since_last['time_since_last'] = round(time_diff_seconds, 1)

            # Calculate rates (per minute)
            if time_diff_minutes > 0:
                links_diff = crawled_count - last_progress.get('crawled_count', 0)
                scrape_diff = scraped_count - last_progress.get('scraped_count', 0)

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
                estimated_completion_time = current_timestamp + datetime.timedelta(minutes=total_estimated_minutes)
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
        progress_history_collection.insert_one({
            'source_url': source_url,
            'timestamp': current_timestamp,
            'crawl_progress': crawl_progress,
            'scrape_progress': scrape_progress,
            'crawled_count': crawled_count,
            'scraped_count': scraped_count,
            'total_links': total_links,
            'operation_status': status
        })

        # Limit history records to prevent excessive storage
        # Keep only the 100 most recent records for each source URL
        progress_history_collection.create_index([('source_url', 1), ('timestamp', -1)])

        # Delete older records (keep the most recent 100)
        old_records = progress_history_collection.find(
            {'source_url': source_url},
            sort=[('timestamp', -1)],
            skip=100
        )

        old_record_ids = [record['_id'] for record in old_records]
        if old_record_ids:
            progress_history_collection.delete_many({'_id': {'$in': old_record_ids}})

        return jsonify({
            'status': 'success',
            'operation_status': status,
            'crawl_progress': crawl_progress,
            'scrape_progress': scrape_progress,
            'crawled_count': crawled_count,
            'total_links': total_links,
            'scraped_count': scraped_count,
            'is_crawling_active': is_crawling_active,
            'is_processing_active': is_processing_active,
            'change_since_last': change_since_last,
            'timestamp': current_timestamp.isoformat()
        })

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'error_details': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()   
                     
@file_api.route('/discovered-links', methods=['GET'])
@token_required
def get_discovered_links(user_id):
    """Get all unique links discovered for a user, optionally filtered by source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        
        # Build query with user_id
        query = {'user_id': user_id}
        if source_url:
            query['top_level_source'] = source_url
        
        # Get distinct links
        discovered_links = links_collection.distinct('link', query)
        
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
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()

# Helper function to verify collections exist
def verify_collections():
    """Verify all required collections exist"""
    client = None
    try:
        print("Verifying MongoDB collections...")
        client = get_mongo_client()
        db = client[DB_NAME]
        
        print(f"Checking collections in database: {DB_NAME}")
        collection_names = db.list_collection_names()
        print(f"Available collections: {collection_names}")
        
        # Check if our collections exist
        required_collections = [SOURCE_COLLECTION, LINKS_COLLECTION, CONTENT_COLLECTION, 'Source_Urls']
        for coll_name in required_collections:
            if coll_name not in collection_names:
                print(f"Warning: Collection {coll_name} does not exist in database")
            else:
                print(f"Collection {coll_name} exists")
                # Count documents in collection
                count = db[coll_name].count_documents({})
                print(f"Collection {coll_name} has {count} documents")
    except Exception as e:
        print(f"Error verifying collections: {str(e)}")
        traceback.print_exc()
    finally:
        if client:
            client.close()

# Test MongoDB connection and collection existence on module load
verify_collections()