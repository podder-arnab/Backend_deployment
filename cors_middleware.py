import os

class CORSMiddleware:
    def __init__(self, app):
        self.app = app
        # Create list of allowed origins
        self.allowed_origins = [
            'http://localhost:3000',  # Local development
            'https://smart-crawler-fe.vercel.app',  # Your Vercel domain
            'https://smart-crawler-6ghm35ek8-aniruddha-mukherjees-projects-00946ecf.vercel.app'  # The other Vercel domain
        ]
        
        # Add any CORS_ORIGINS from environment variables
        cors_origins = os.getenv('CORS_ORIGINS', '')
        if cors_origins:
            additional_origins = [origin.strip() for origin in cors_origins.split(',') if origin.strip()]
            self.allowed_origins.extend(additional_origins)

    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
            # Get origin from request
            origin = environ.get('HTTP_ORIGIN', '')
            
            # Define CORS headers
            cors_headers = []
            
            # Check if origin is allowed
            if origin in self.allowed_origins:
                cors_headers = [
                    ('Access-Control-Allow-Origin', origin),
                    ('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS'),
                    ('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With'),
                    ('Access-Control-Allow-Credentials', 'true')
                ]
            elif not origin:  # API client without origin
                cors_headers = [
                    ('Access-Control-Allow-Origin', '*'),
                    ('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS'),
                    ('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With'),
                    ('Access-Control-Allow-Credentials', 'true')
                ]
            
            # Add CORS headers to the response
            headers_list = list(headers)
            for header in cors_headers:
                headers_list.append(header)
                
            return start_response(status, headers_list, exc_info)
            
        # Handle OPTIONS requests automatically
        if environ['REQUEST_METHOD'] == 'OPTIONS':
            # Get origin from request
            origin = environ.get('HTTP_ORIGIN', '')
            
            # Set CORS headers based on origin
            if origin in self.allowed_origins:
                allowed_origin = origin
            else:
                allowed_origin = '*'
                
            resp_headers = [
                ('Access-Control-Allow-Origin', allowed_origin),
                ('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS'),
                ('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With'),
                ('Access-Control-Allow-Credentials', 'true'),
                ('Content-Type', 'text/plain'),
                ('Content-Length', '0')
            ]
            start_response('200 OK', resp_headers)
            return [b'']
            
        return self.app(environ, custom_start_response)