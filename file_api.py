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

# Load environment variables from .env file
load_dotenv()

file_api = Blueprint('file_api', __name__)  # Using Blueprint instead of Flask

# MongoDB connection configuration
MONGO_URI = os.environ.get('MONGO_URI')
DB_NAME = 'scrapper'
SOURCE_COLLECTION = 'Content_Links'
LINKS_COLLECTION = 'Links_to_scrap'
CONTENT_COLLECTION = 'scrapped_text'

# Global variables to track processing state
processing_events = {}
crawling_events = {}

def get_mongo_client():
    """Establish connection to MongoDB"""
    client = MongoClient(MONGO_URI)
    return client

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

# Global lock for thread-safe updates to MongoDB
mongo_lock = Lock()

def continuous_crawl_job(top_level_source_url, stop_event):
    """
    Worker function to continuously crawl pages until all links are processed
    or stop_event is set
    """
    client = None
    try:
        print(f"Starting continuous crawl job for {top_level_source_url}")
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
            # Find an uncrawled link
            with mongo_lock:
                links_remaining = links_collection.count_documents({
                    'is_crawled': False,
                    'top_level_source': top_level_source_url
                })
                
                # If no uncrawled links remain, we're done
                if links_remaining == 0:
                    print(f"No more uncrawled links for {top_level_source_url}. Exiting crawl job.")
                    break
                
                # Get one uncrawled link
                link_doc = links_collection.find_one_and_update(
                    {'is_crawled': False, 'top_level_source': top_level_source_url},
                    {'$set': {'crawling_started': datetime.now()}},
                    return_document=True
                )
                
                if not link_doc:
                    # Unlikely, but possible race condition
                    time.sleep(1)
                    continue
            
            # Process the URL
            url_to_crawl = link_doc['link']
            current_depth = link_doc.get('depth', 0)
            top_level_source = link_doc.get('top_level_source', top_level_source_url)
            
            print(f"Processing URL: {url_to_crawl}")
            
            # Skip social media URLs
            if is_social_media_url(url_to_crawl):
                print(f"Skipping social media URL: {url_to_crawl}")
                with mongo_lock:
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
                response = requests.get(url_to_crawl, headers=headers, timeout=30)
                response.raise_for_status()  # Raise exception for 4XX/5XX responses
                
                # Parse the HTML content
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
                        
                        # Skip invalid URLs, non-content URLs, and social media URLs
                        if not is_valid_url(full_url) or not is_valid_content_url(full_url) or is_social_media_url(full_url):
                            continue
                        
                        # Check if the URL already exists in Links_to_scrap
                        with mongo_lock:
                            existing_link = links_collection.find_one({'link': full_url})
                            if existing_link:
                                continue
                            
                        valid_urls.append(full_url)
                    except Exception as e:
                        print(f"Error processing URL {href}: {str(e)}")
                        continue
                
                # Remove duplicates
                unique_links = list(set(valid_urls))
                print(f"Found {len(unique_links)} valid URLs on {url_to_crawl}")
                
                # If no valid links are found, mark the URL as crawled and continue
                if not unique_links:
                    with mongo_lock:
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
                
                # Store in Content_Links for reference
                with mongo_lock:
                    source_collection = db[SOURCE_COLLECTION]
                    source_document = {
                        'source_url': url_to_crawl,
                        'uniqueLinks': unique_links,
                        'crawled_at': datetime.now(),
                        'depth': current_depth,
                        'top_level_source': top_level_source
                    }
                    source_collection.insert_one(source_document)
                
                # Add all links to Links_to_scrap
                current_links_added = 0
                
                for link in unique_links:
                    # Add to Links_to_scrap for further crawling (only if not already exists)
                    with mongo_lock:
                        crawl_doc = {
                            'link': link,
                            'added_at': datetime.now(),
                            'is_crawled': False,
                            'is_processed': False,  # New field to track processing status
                            'source_url': url_to_crawl,  # Immediate parent URL
                            'top_level_source': top_level_source,  # Original source URL
                            'depth': current_depth + 1,  # Increment depth for next level
                            'has_text_in_url': contains_text_in_url(link)  # New field to indicate if URL contains text
                        }
                        result = links_collection.insert_one(crawl_doc)
                        if result.inserted_id:
                            current_links_added += 1
                            print(f"Added new URL to Links_to_scrap: {link}")
                
                # Mark this link as crawled
                with mongo_lock:
                    links_collection.update_one(
                        {'_id': link_doc['_id']},
                        {'$set': {
                            'is_crawled': True, 
                            'crawled_at': datetime.now(),
                            'links_found': len(unique_links),
                            'links_added': current_links_added
                        }}
                    )
                    print(f"Marked URL as crawled: {url_to_crawl}")
                
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
                with mongo_lock:
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
                print(f"Error processing URL {url_to_crawl}: {error_msg}")
                
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                with mongo_lock:
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
                print(f"Error processing URL {url_to_crawl}: {error_msg}")
            
            # Optional: Sleep to prevent hammering the target server
            time.sleep(0.5)
        
        # Return stats if we exit the loop
        return {
            'links_crawled': links_crawled,
            'links_added': links_added,
            'errors_encountered': errors_encountered
        }
            
    except Exception as e:
        print(f"Error in continuous crawl job: {str(e)}")
        traceback.print_exc()
    finally:
        if client:
            client.close()

def continuous_processing_job(top_level_source_url, stop_event, delay_seconds=180):
    """
    Worker function to continuously process all the scraped links
    until all links in Links_to_scrap are processed (either successfully or failed).
    Includes an initial delay before starting processing.
    """
    client = None
    try:
        print(f"Starting processing job for {top_level_source_url} with {delay_seconds}s delay")
        
        # Wait for the specified delay before starting processing
        print(f"Waiting {delay_seconds} seconds before starting processing...")
        
        # Wait either for the delay or until the stop event is set
        stop_event.wait(delay_seconds)
        
        # If stop event is set during the delay, exit early
        if stop_event.is_set():
            print(f"Processing job for {top_level_source_url} was stopped during delay")
            return
            
        print(f"Delay complete, beginning processing for {top_level_source_url}")
        
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
            # Find a batch of unprocessed links
            with mongo_lock:
                unprocessed_links = list(links_collection.find({
                    'is_processed': False,
                    'top_level_source': top_level_source_url
                }).limit(batch_size))
            
            # If no unprocessed links remain, check if we're truly done
            if not unprocessed_links:
                # Double-check after a brief delay to ensure no race conditions
                time.sleep(3)
                with mongo_lock:
                    unprocessed_count = links_collection.count_documents({
                        'is_processed': False,
                        'top_level_source': top_level_source_url
                    })
                
                if unprocessed_count == 0:
                    print(f"No more unprocessed links for {top_level_source_url}. Exiting processing job.")
                    break
                else:
                    # If we found more unprocessed links after the delay, continue the loop
                    continue
            
            # Process each link in the batch
            for link_doc in unprocessed_links:
                if stop_event.is_set():
                    print(f"Stop event triggered. Exiting processing job for {top_level_source_url}.")
                    break
                
                try:
                    # Process the link
                    result = scrape_single_link(db, link_doc)
                    
                    if result['status'] == 'success':
                        success_count += 1
                    else:
                        # Mark the link as failed instead of processed
                        error_count += 1
                        with mongo_lock:
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
                        print(f"Processed {links_processed} links for {top_level_source_url} ({success_count} successful, {error_count} failed)")
                    
                except Exception as e:
                    # Catch any unexpected errors during processing
                    error_msg = f"Unexpected error processing link {link_doc.get('link', 'unknown')}: {str(e)}"
                    print(error_msg)
                    traceback.print_exc()
                    
                    # Mark the link as failed
                    with mongo_lock:
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
        
        # Return stats if we exit the loop
        return {
            'links_processed': links_processed,
            'success_count': success_count,
            'error_count': error_count
        }
            
    except Exception as e:
        print(f"Error in continuous processing job: {str(e)}")
        traceback.print_exc()
    finally:
        if client:
            client.close()
            
@file_api.route('/recursive-crawl', methods=['POST'])
def recursive_crawl():
    """
    Start a background thread to continuously crawl pages from Links_to_scrap collection.
    This function will immediately return and the crawling will continue in the background.
    """
    client = None
    try:
        # Get MongoDB client
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get collections
        links_collection = db[LINKS_COLLECTION]
        source_urls_collection = db['Source_Urls']
        
        # Get the URL from the request body
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                'status': 'error',
                'message': 'URL is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Get the top-level source URL provided by the user
        top_level_source_url = data['url']
        
        # Validate URL format
        if not is_valid_url(top_level_source_url):
            return jsonify({
                'status': 'error',
                'message': f'Invalid URL format: {top_level_source_url}',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Check if already crawling this URL
        if top_level_source_url in crawling_events and not crawling_events[top_level_source_url].is_set():
            return jsonify({
                'status': 'info',
                'message': f'Crawling for {top_level_source_url} is already in progress.',
                'timestamp': datetime.now().isoformat()
            })
        
        # Check if the URL has already been crawled completely
        links_count = links_collection.count_documents({
            'top_level_source': top_level_source_url
        })
        
        uncrawled_count = links_collection.count_documents({
            'top_level_source': top_level_source_url,
            'is_crawled': False
        })
        
        if links_count > 0 and uncrawled_count == 0:
            return jsonify({
                'status': 'info',
                'message': f'URL {top_level_source_url} has already been completely crawled.',
                'stats': {
                    'total_links': links_count
                },
                'timestamp': datetime.now().isoformat()
            })
        
        # Check if the URL exists in Links_to_scrap (i.e., crawling has started but not completed)
        existing_link = links_collection.find_one({'link': top_level_source_url})
        
        # If the URL is not in Links_to_scrap, add it as a new starting point
        if not existing_link:
            with mongo_lock:
                links_collection.insert_one({
                    'link': top_level_source_url,
                    'added_at': datetime.now(),
                    'is_crawled': False,
                    'is_processed': False,  # New field to track processing status
                    'depth': 0,  # Starting URL is depth 0
                    'source_url': top_level_source_url,  # Set the source URL as itself for the starting URL
                    'top_level_source': top_level_source_url  # Track the top-level source URL
                })
                print(f"Initial URL added to Links_to_scrap: {top_level_source_url}")
        
        # Save the source URL and timestamp in the new collection
        with mongo_lock:
            source_urls_collection.insert_one({
                'source_url': top_level_source_url,
                'timestamp': datetime.now()
            })
        
        # Create a new stop event for this crawl job
        stop_event = Event()
        crawling_events[top_level_source_url] = stop_event
        
        # Start the crawling in a background thread
        crawler_thread = Thread(
            target=continuous_crawl_job,
            args=(top_level_source_url, stop_event),
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
def process_all_links():
    """
    Start a background thread to continuously process all links in the Links_to_scrap collection
    until all links are processed (is_processed: true).
    Includes an option to delay the start of processing.
    """
    client = None
    try:
        # Get the delay parameter (default: 3 minutes = 180 seconds)
        data = request.get_json() or {}
        delay_seconds = data.get('delay', 180)
        source_url = data.get('source_url')
        
        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
            
        # Check if already processing this URL
        if source_url in processing_events and not processing_events[source_url].is_set():
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
        
        # Check how many unprocessed links exist
        unprocessed_count = links_collection.count_documents({
            'is_processed': False,
            'top_level_source': source_url
        })
        
        if unprocessed_count == 0:
            return jsonify({
                'status': 'complete',
                'message': f'No unprocessed links found for {source_url}',
                'timestamp': datetime.now().isoformat()
            })
        
        # Create a new stop event for this processing job
        stop_event = Event()
        processing_events[source_url] = stop_event
        
        # Start the processing in a background thread with the specified delay
        processor_thread = Thread(
            target=continuous_processing_job,
            args=(source_url, stop_event, delay_seconds),
            daemon=True
        )
        processor_thread.start()
        
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
def get_source_url_status():
    """Get the status of a specific source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        
        source_url = request.args.get('source_url')
        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        # Count total URLs associated with this source in Links_to_scrap collection
        total_urls = links_collection.count_documents({'top_level_source': source_url})
        
        # Count processed URLs for this source (both successful and failed)
        successful_processed = links_collection.count_documents({'top_level_source': source_url, 'is_processed': True})
        failed_processed = links_collection.count_documents({'top_level_source': source_url, 'is_processed': "Failed"})
        total_processed = successful_processed + failed_processed
        
        # Count scraped URLs for this source
        total_scrapped = content_collection.count_documents({'top_level_source': source_url})
        
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
        
        print(f"Source: {source_url}, Total URLs: {total_urls}, Successful: {successful_processed}, Failed: {failed_processed}, Total Processed: {total_processed}, Scrapped: {total_scrapped}, Status: {status}")
        
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
def stop_crawling():
    """Stop the continuous crawling for a specific source URL"""
    try:
        data = request.get_json()
        if not data or 'source_url' not in data:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
            
        source_url = data['source_url']
        
        if source_url in crawling_events:
            crawling_events[source_url].set()
            return jsonify({
                'status': 'success',
                'message': f'Crawling for {source_url} has been stopped.',
                'timestamp': datetime.now().isoformat()
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'No active crawling found for {source_url}',
                'timestamp': datetime.now().isoformat()
            }), 404
            
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500

@file_api.route('/stop-processing', methods=['POST'])
def stop_processing():
    """Stop the continuous processing for a specific source URL"""
    try:
        data = request.get_json()
        if not data or 'source_url' not in data:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the POST body.',
                'timestamp': datetime.now().isoformat()
            }), 400
            
        source_url = data['source_url']
        
        if source_url in processing_events:
            processing_events[source_url].set()
            return jsonify({
                'status': 'success',
                'message': f'Processing for {source_url} has been stopped.',
                'timestamp': datetime.now().isoformat()
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'No active processing found for {source_url}',
                'timestamp': datetime.now().isoformat()
            }), 404
            
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500

def scrape_single_link(db, link_doc):
    """Helper function to scrape a single link"""
    link = link_doc['link']
    is_wiki = 'wikipedia.org' in link or 'wiki' in link.lower()
    
    # Get the top-level source and immediate source URLs
    top_level_source = link_doc.get('top_level_source', link_doc.get('source_url', 'unknown'))
    source_url = link_doc.get('source_url', 'unknown')
    
    # Get collections
    links_collection = db[LINKS_COLLECTION]
    content_collection = db[CONTENT_COLLECTION]
    
    try:
        # Add user agent to avoid being blocked
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Make request to the URL with increased timeout
        response = requests.get(link, headers=headers, timeout=60)
        response.raise_for_status()
        
        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get title
        title = soup.find('title')
        title_text = title.get_text().strip() if title else "Unknown Title"
        
        # Extract text based on the site type
        if is_wiki:
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
                # Fallback to standard extraction
                for script in soup(["script", "style"]):
                    script.extract()
                text = soup.get_text(separator=' ', strip=True)
        else:
            # Standard extraction for non-Wikipedia sites
            for script in soup(["script", "style"]):
                script.extract()
            
            # Get text and clean it
            text = soup.get_text(separator=' ', strip=True)
            
            # Add title to the beginning
            text = f"# {title_text}\n\n{text}"
        
        # Remove excessive whitespace
        text = re.sub(r'\n\s*\n', '\n\n', text)
        
        # Create document for scraped content
        content_document = {
            'scrapped_content': text,
            'content_link': link,
            'scrape_date': datetime.now(),
            'link_id': link_doc['_id'],
            'source_url': source_url,  # Immediate parent URL
            'top_level_source': top_level_source,  # Original source URL
            'depth': link_doc.get('depth', 0),
            'title': title_text
        }
        
        # Insert into content collection
        result = content_collection.insert_one(content_document)
        
        # Update the link as processed
        links_collection.update_one(
            {'_id': link_doc['_id']},
            {'$set': {
                'is_processed': True,  # Successfully processed
                'processed_at': datetime.now(),
                'top_level_source': top_level_source
            }}
        )
        
        return {
            'status': 'success',
            'link': link,
            'content_length': len(text),
            'title': title_text,
            'content_id': str(result.inserted_id),
            'top_level_source': top_level_source,
            'source_url': source_url
        }
    
    except requests.exceptions.RequestException as e:
        error_msg = f"Request error: {str(e)}"
        print(f"Failed to scrape {link}: {error_msg}")
        
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
        
        # Do not update is_processed here, let the calling function handle it
        return {
            'status': 'error',
            'link': link,
            'error': error_msg,
            'traceback': tb,
            'top_level_source': top_level_source
        }
    
# Modified API endpoints to filter by source URL

@file_api.route('/realtime-stats/links-to-scrap', methods=['GET'])
def get_links_to_scrap():
    """Get all links in the Links_to_scrap collection, filtered by source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_to_scrap_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        query = {'top_level_source': source_url} if source_url else {}
        
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
def get_total_processed_links():
    """Get the total number of processed links, filtered by source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        query = {'top_level_source': source_url, 'is_processed': True} if source_url else {'is_processed': True}
        
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
def get_scrapped_links():
    """Get the number of scrapped links, filtered by source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        scrapped_text_collection = db[CONTENT_COLLECTION]
        
        source_url = request.args.get('source_url')
        query = {'top_level_source': source_url} if source_url else {}
        
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
def get_pending_links():
    """Get the number of pending links, filtered by source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        links_collection = db[LINKS_COLLECTION]
        
        source_url = request.args.get('source_url')
        query = {'top_level_source': source_url, 'is_processed': False} if source_url else {'is_processed': False}
        
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
def get_total_words_scrapped():
    """Get the total number of words scraped, filtered by source URL"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        scrapped_text_collection = db[CONTENT_COLLECTION]
        
        source_url = request.args.get('source_url')
        pipeline = []
        
        if source_url:
            pipeline.append({"$match": {"top_level_source": source_url}})
        
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
    """Fetch the content of links related to a specific source URL"""
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
        specific_url = data.get('url')
        
        if not source_url:
            return jsonify({
                'status': 'error',
                'message': 'source_url is required in the request body.',
                'timestamp': datetime.now().isoformat()
            }), 400
        
        client = get_mongo_client()
        db = client[DB_NAME]
        scrapped_text_collection = db[CONTENT_COLLECTION]
        
        if specific_url:
            query = {'content_link': specific_url, 'top_level_source': source_url}
            document = scrapped_text_collection.find_one(query, {'scrapped_content': 1, 'content_link': 1, 'title': 1, '_id': 0})
            
            if not document:
                return jsonify({
                    'status': 'error',
                    'message': f'No content found for the URL: {specific_url} with source: {source_url}',
                    'timestamp': datetime.now().isoformat()
                }), 404
                
            response = {
                'url': specific_url,
                'content': document['scrapped_content'],
                'title': document.get('title', 'Unknown Title')
            }
            
            return jsonify({
                'status': 'success',
                'data': response,
                'timestamp': datetime.now().isoformat()
            })
        else:
            query = {'top_level_source': source_url}
            documents = list(scrapped_text_collection.find(query, {'scrapped_content': 1, 'content_link': 1, 'title': 1, '_id': 0}))
            
            if not documents:
                return jsonify({
                    'status': 'error',
                    'message': f'No content found for source URL: {source_url}',
                    'timestamp': datetime.now().isoformat()
                }), 404
                
            response = [{
                'url': doc['content_link'],
                'title': doc.get('title', 'Unknown Title'),
                'content_preview': doc['scrapped_content'][:200] + '...' if len(doc['scrapped_content']) > 200 else doc['scrapped_content']
            } for doc in documents]
            
            return jsonify({
                'status': 'success',
                'data': response,
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
def get_all_documents():
    """Get all documents with source_link and status"""
    client = None
    try:
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get collections
        source_urls_collection = db['Source_Urls']
        links_collection = db[LINKS_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        
        # Fetch all source URLs, sorted by timestamp in descending order (latest first)
        source_urls = list(source_urls_collection.find().sort('timestamp', -1))
        
        documents = []
        
        for source_url_doc in source_urls:
            source_url = source_url_doc['source_url']
            
            # Count the number of processed links for this source URL
            processed_count = links_collection.count_documents({
                'top_level_source': source_url,
                'is_processed': True
            })
            
            # Count the number of scrapped texts for this source URL
            scrapped_count = content_collection.count_documents({
                'top_level_source': source_url
            })
            
            # Determine the status
            status = 'Completed' if processed_count == scrapped_count else 'Pending'
            
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
        return jsonify({
            'status': 'error',
            'message': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500
    finally:
        if client:
            client.close()