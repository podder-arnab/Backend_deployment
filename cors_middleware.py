class CORSMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
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
            resp_headers = [
                ('Access-Control-Allow-Origin', '*'),
                ('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS'),
                ('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With'),
                ('Access-Control-Allow-Credentials', 'true'),
                ('Content-Type', 'text/plain'),
                ('Content-Length', '0')
            ]
            start_response('200 OK', resp_headers)
            return [b'']
            
        return self.app(environ, custom_start_response)