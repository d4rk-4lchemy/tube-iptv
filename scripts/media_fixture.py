"""Local HTTP media fixture with byte ranges for FFmpeg seeking tests."""
from http.server import SimpleHTTPRequestHandler
import os
import re


class MediaHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()
        file = open(path, 'rb')
        size = os.fstat(file.fileno()).st_size
        start, end = 0, size - 1
        value = self.headers.get('Range')
        if value:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', value)
            if not match or int(match[1]) >= size:
                file.close()
                self.send_error(416)
                return None
            start = int(match[1])
            end = min(int(match[2]), end) if match[2] else end
        self.send_response(206 if value else 200)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if value:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        file.seek(start)
        self.remaining = end - start + 1
        return file

    def copyfile(self, source, outputfile):
        try:
            remaining = getattr(self, 'remaining', None)
            if remaining is None:
                return super().copyfile(source, outputfile)
            while remaining > 0:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
