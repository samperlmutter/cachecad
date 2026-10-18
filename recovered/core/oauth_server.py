from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import threading

class OAuthHandler(BaseHTTPRequestHandler):
    server_version = 'OAuthHandler/1.0'
    def do_GET(self):
        query = urlparse(self.path).query
        params = parse_qs(query)
        if 'code' in params:
            self.server.auth_code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            html = '\n            <html>\n            <head>\n                <style>\n                    body {\n                        background-color: #191919;\n                        color: #ffffff;\n                        font-family: sans-serif;\n                        display: flex;\n                        align-items: center;\n                        justify-content: center;\n                        height: 100vh;\n                        margin: 0;\n                    }\n                    h1 {\n                        font-size: 1.5em;\n                    }\n                </style>\n            </head>\n            <body>\n                <h1>You are signed into CacheCAD. You may now close this window.</h1>\n            </body>\n            </html>\n            '
            self.wfile.write(html.encode('utf-8'))
            self.server.shutdown_event.set()
        else:
            self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            error_html = '\n            <html>\n            <body style="background-color:#191919; color:#ff5555; font-family:sans-serif; padding: 2em;">\n                <h1>Invalid response</h1>\n                <p>No authorization code was received.</p>\n            </body>\n            </html>\n            '
            self.wfile.write(error_html.encode('utf-8'))

    def log_message(self, *args):
        pass

class OAuthServer(HTTPServer):
    def __init__(self, port=8765):
        self.auth_code = None
        self.shutdown_event = threading.Event()
        super().__init__(('localhost', port), OAuthHandler)

    def wait_for_code(self, timeout=60):
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        success = self.shutdown_event.wait(timeout)
        self.shutdown()
        thread.join()
        return self.auth_code if success else None
