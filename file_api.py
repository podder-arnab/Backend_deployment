from flask import Blueprint, request, jsonify
from pymongo import MongoClient
from datetime import datetime
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

# Load environment variables from .env file
load_dotenv()

file_api = Blueprint('file_api', __name__)  # Using Blueprint instead of Flask

# MongoDB connection configuration
MONGO_URI = os.environ.get('MONGO_URI')
DB_NAME = 'scrapper'
SOURCE_COLLECTION = 'Content_Links'
LINKS_COLLECTION = 'Links_to_scrap'
PROCESSED_COLLECTION = 'Processed_Links'
CONTENT_COLLECTION = 'scrapped_text'

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
        
    return True

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

@file_api.route('/recursive-crawl', methods=['POST'])
def recursive_crawl():
    """
    Crawl a page from the provided URL in the POST body only if the Links_to_scrap collection is empty.
    Otherwise, continue crawling existing links.
    """
    client = None
    try:
        # Get MongoDB client
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get the Links_to_scrap collection
        links_collection = db[LINKS_COLLECTION]
        
        # Check if the Links_to_scrap collection is empty
        is_collection_empty = links_collection.count_documents({}) == 0
        
        # Get the URL and depth from the request body (only if the collection is empty)
        data = request.get_json()
        url = None
        process_depth = 1  # Default depth
        
        if is_collection_empty:
            if data and 'url' in data:
                # Use URL directly from request
                url = data['url']
                
                # Check if depth parameter is provided
                if 'depth' in data:
                    try:
                        process_depth = int(data['depth'])
                    except:
                        process_depth = 1
                
                # Validate URL format
                if not is_valid_url(url):
                    return jsonify({
                        'status': 'error',
                        'message': f'Invalid URL format: {url}',
                        'timestamp': datetime.now().isoformat()
                    }), 400
                
                # Add the starting URL to Links_to_scrap
                links_collection.insert_one({
                    'link': url,
                    'added_at': datetime.now(),
                    'is_crawled': False,
                    'depth': 0  # Starting URL is depth 0
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': 'URL is required in the POST body for the first crawl.',
                    'timestamp': datetime.now().isoformat()
                }), 400
        
        # Process up to process_depth levels
        for current_depth in range(process_depth):
            # Find an uncrawled link at the current depth
            query = {'is_crawled': {'$ne': True}}
            if url and current_depth == 0:
                # For first iteration, use the provided URL if available
                query['link'] = url
            else:
                # For subsequent iterations, respect the depth level
                query['depth'] = current_depth
                
            link_doc = links_collection.find_one(query)
            
            if not link_doc:
                # If no link at current depth, try any uncrawled link
                link_doc = links_collection.find_one({'is_crawled': {'$ne': True}})
                
                if not link_doc:
                    return jsonify({
                        'status': 'info',
                        'message': f'No more links to crawl at depth {current_depth}',
                        'timestamp': datetime.now().isoformat()
                    })
            
            url_to_crawl = link_doc['link']
            current_depth = link_doc.get('depth', 0)
            processed_collection = db[PROCESSED_COLLECTION]
            
            # Check if this is Wikipedia or similar content site
            is_wiki = 'wikipedia.org' in url_to_crawl or 'wiki' in url_to_crawl.lower()
            
            # Extract URLs from the current page with enhanced extraction
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
                
                # For Wikipedia, focus on main content area
                if is_wiki:
                    # Try to find the main content div
                    content_div = soup.find('div', {'id': 'mw-content-text'})
                    if content_div:
                        all_links = content_div.find_all('a', href=True)
                
                for link in all_links:
                    href = link['href'].strip()
                    
                    # Skip empty hrefs, javascript:, mailto:, tel: links
                    if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                        continue
                    
                    try:
                        # Convert relative URLs to absolute URLs
                        full_url = urljoin(base_url, href)
                        
                        # Special handling for Wikipedia
                        if is_wiki:
                            # Keep only Wikipedia article links and filter out special pages
                            if 'wikipedia.org/wiki/' in full_url:
                                # Skip special pages
                                if any(special in full_url for special in [
                                    '/wiki/Special:', '/wiki/Talk:', '/wiki/Category:',
                                    '/wiki/Help:', '/wiki/Portal:', '/wiki/Wikipedia:',
                                    '/wiki/Template:', '/wiki/File:', '/wiki/MediaWiki:',
                                    'action=edit', 'oldid=', 'diff=', 'printable=yes'
                                ]):
                                    continue
                                valid_urls.append(full_url)
                        else:
                            # Default validation for non-Wikipedia sites
                            if not is_valid_url(full_url) or not is_valid_content_url(full_url):
                                continue
                            valid_urls.append(full_url)
                            
                    except Exception as e:
                        print(f"Error processing URL {href}: {str(e)}")
                        continue
                
                # Remove duplicates
                unique_links = list(set(valid_urls))
                
                # Store in Content_Links for reference
                source_collection = db[SOURCE_COLLECTION]
                source_document = {
                    'source_url': url_to_crawl,
                    'uniqueLinks': unique_links,
                    'crawled_at': datetime.now(),
                    'depth': current_depth
                }
                source_collection.insert_one(source_document)
                
                # Add all links to both Links_to_scrap and Processed_Links
                links_added_to_crawl = 0
                links_added_to_process = 0
                
                for link in unique_links:
                    # For Wikipedia, don't apply the text_in_url filter
                    should_process = is_wiki or contains_text_in_url(link)
                    
                    # Add to Links_to_scrap for further crawling
                    existing_crawl = links_collection.find_one({'link': link})
                    if not existing_crawl:
                        crawl_doc = {
                            'link': link,
                            'added_at': datetime.now(),
                            'is_crawled': False,
                            'source_url': url_to_crawl,
                            'depth': current_depth + 1  # Increment depth for next level
                        }
                        links_collection.insert_one(crawl_doc)
                        links_added_to_crawl += 1
                    
                    # Add to Processed_Links for content scraping if it's a content link
                    if should_process:
                        existing_process = processed_collection.find_one({'link': link})
                        if not existing_process:
                            process_doc = {
                                'link': link,
                                'created_at': datetime.now(),
                                'is_processed': False,
                                'source_url': url_to_crawl,
                                'has_text_in_url': True if is_wiki else contains_text_in_url(link),
                                'depth': current_depth + 1
                            }
                            processed_collection.insert_one(process_doc)
                            links_added_to_process += 1
                
                # Mark this link as crawled
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {'is_crawled': True, 'crawled_at': datetime.now()}}
                )
                
                # Add the URL to processed collection as well (if not already there)
                existing_process = processed_collection.find_one({'link': url_to_crawl})
                if not existing_process:
                    process_doc = {
                        'link': url_to_crawl,
                        'created_at': datetime.now(),
                        'is_processed': False,
                        'source_url': 'seed_url' if current_depth == 0 else link_doc.get('source_url', 'unknown'),
                        'has_text_in_url': True if is_wiki else contains_text_in_url(url_to_crawl),
                        'depth': current_depth
                    }
                    processed_collection.insert_one(process_doc)
            
            except requests.exceptions.RequestException as e:
                error_msg = f"Request error: {str(e)}"
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {
                        'is_crawled': True, 
                        'crawled_at': datetime.now(),
                        'error': error_msg
                    }}
                )
                continue  # Try the next depth level
                
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                links_collection.update_one(
                    {'_id': link_doc['_id']},
                    {'$set': {
                        'is_crawled': True, 
                        'crawled_at': datetime.now(),
                        'error': error_msg
                    }}
                )
                continue  # Try the next depth level
        
        # Return statistics after processing all depths
        stats = {
            'links_to_crawl': links_collection.count_documents({'is_crawled': {'$ne': True}}),
            'links_crawled': links_collection.count_documents({'is_crawled': True}),
            'links_to_scrape': processed_collection.count_documents({'is_processed': False}),
            'links_scraped': processed_collection.count_documents({'is_processed': True})
        }
        
        return jsonify({
            'status': 'success',
            'message': f'Crawling completed for {process_depth} depth levels',
            'stats': stats,
            'timestamp': datetime.now().isoformat()
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
    """Process all unprocessed links in the database at once."""
    client = None
    try:
        # Get MongoDB client
        client = get_mongo_client()
        db = client[DB_NAME]
        
        # Get collections
        processed_collection = db[PROCESSED_COLLECTION]
        content_collection = db[CONTENT_COLLECTION]
        
        # Check how many unprocessed links exist
        unprocessed_count = processed_collection.count_documents({'is_processed': False})
        
        if unprocessed_count == 0:
            return jsonify({
                'status': 'complete',
                'message': 'No unprocessed links found',
                'timestamp': datetime.now().isoformat()
            })
        
        # Get all unprocessed links
        unprocessed_links = list(processed_collection.find({'is_processed': False}))
        
        # Start processing all links
        results = {
            'total_unprocessed': unprocessed_count,
            'processed': 0,
            'success': 0,
            'failed': 0,
            'details': []
        }
        
        # Process each link
        for link_doc in unprocessed_links:
            link = link_doc['link']
            link_id = link_doc['_id']
            
            try:
                # Scrape the link
                result = scrape_single_link(db, link_doc)
                
                if result['status'] == 'success':
                    results['success'] += 1
                    results['details'].append({
                        'link': link,
                        'status': 'success',
                        'content_length': result['content_length'],
                        'title': result['title'],
                        'content_id': result['content_id']
                    })
                else:
                    results['failed'] += 1
                    results['details'].append({
                        'link': link,
                        'status': 'error',
                        'error': result['error']
                    })
                
            except Exception as e:
                results['failed'] += 1
                results['details'].append({
                    'link': link,
                    'status': 'error',
                    'error': str(e),
                    'traceback': traceback.format_exc()
                })
            
            results['processed'] += 1
        
        # Return results after processing all links
        return jsonify({
            'status': 'complete',
            'message': f'Processed {results["processed"]} links ({results["success"]} success, {results["failed"]} failed)',
            'results': results,
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

def scrape_single_link(db, link_doc):
    """Helper function to scrape a single link"""
    link = link_doc['link']
    is_wiki = 'wikipedia.org' in link or 'wiki' in link.lower()
    
    # Get collections
    processed_collection = db[PROCESSED_COLLECTION]
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
            'source_url': link_doc.get('source_url', 'unknown'),
            'depth': link_doc.get('depth', 0),
            'title': title_text
        }
        
        # Insert into content collection
        result = content_collection.insert_one(content_document)
        
        # Update the link as processed
        processed_collection.update_one(
            {'_id': link_doc['_id']},
            {'$set': {'is_processed': True, 'processed_at': datetime.now()}}
        )
        
        return {
            'status': 'success',
            'link': link,
            'content_length': len(text),
            'title': title_text,
            'content_id': str(result.inserted_id)
        }
    
    except requests.exceptions.RequestException as e:
        error_msg = f"Request error: {str(e)}"
        
        # Update the link with error info
        processed_collection.update_one(
            {'_id': link_doc['_id']},
            {
                '$set': {
                    'is_processed': True,
                    'processed_at': datetime.now(),
                    'error': error_msg
                }
            }
        )
        
        return {
            'status': 'error',
            'link': link,
            'error': error_msg
        }
    
    except Exception as e:
        error_msg = f"Processing error: {str(e)}"
        tb = traceback.format_exc()
        
        # Update the link with error info
        processed_collection.update_one(
            {'_id': link_doc['_id']},
            {
                '$set': {
                    'is_processed': True,
                    'processed_at': datetime.now(),
                    'error': error_msg,
                    'traceback': tb
                }
            }
        )
        
        return {
            'status': 'error',
            'link': link,
            'error': error_msg,
            'traceback': tb
        }

@file_api.route('/realtime-stats', methods=['GET'])
def realtime_stats():
    """Get real-time statistics for the frontend"""
    client = None
    try:
        # Get MongoDB client
        client = get_mongo_client()
        db = client[DB_NAME]

        # Get collections
        links_to_scrap_collection = db[LINKS_COLLECTION]
        processed_links_collection = db[PROCESSED_COLLECTION]
        scrapped_text_collection = db[CONTENT_COLLECTION]

        # Fetch all links in Links_to_scrap collection
        links_to_scrap = list(links_to_scrap_collection.find({}, {'link': 1, '_id': 0}))
        all_links = [link['link'] for link in links_to_scrap]

        # Fetch total number of links in Processed_Links collection
        total_number_of_links = processed_links_collection.count_documents({})

        # Fetch number of scrapped links (number of links in scrapped_text collection)
        scrapped_links_count = scrapped_text_collection.count_documents({})

        # Fetch pending links (links in Processed_Links collection that are not yet processed)
        pending_links = processed_links_collection.count_documents({'is_processed': False})

        # Calculate total words scrapped
        total_words_scrapped = 0
        scrapped_documents = scrapped_text_collection.find({}, {'scrapped_content': 1, '_id': 0})
        for doc in scrapped_documents:
            if 'scrapped_content' in doc:
                total_words_scrapped += len(doc['scrapped_content'].split())

        # Prepare response
        response = {
            'Links': all_links,
            'Total_Number_of_Links': total_number_of_links,
            'Scrapped_Links': scrapped_links_count,  # Number of links in scrapped_text collection
            'Pending_Links': pending_links,
            'Total_Words_Scrapped': total_words_scrapped
        }

        return jsonify({
            'status': 'success',
            'data': response,
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