#!/usr/bin/env python3
"""Serveur statique minimal avec support des requetes Range (necessaire pour
PMTiles, qui va chercher des morceaux precis du fichier par HTTP Range plutot
que de tout telecharger). http.server standard ne supporte pas Range."""
import http.server
import os
import re
import socketserver

PORT = 8938


class RangeHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()

        range_header = self.headers.get("Range")
        file_size = os.path.getsize(path)
        ctype = self.guess_type(path)

        if not range_header:
            self.send_response(200)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            f = open(path, "rb")
            return f

        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        f = open(path, "rb")
        f.seek(start)

        self.send_response(206)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        class LimitedReader:
            def __init__(self, fh, remaining):
                self.fh = fh
                self.remaining = remaining

            def read(self, n=-1):
                if self.remaining <= 0:
                    return b""
                n = self.remaining if n < 0 else min(n, self.remaining)
                data = self.fh.read(n)
                self.remaining -= len(data)
                return data

            def close(self):
                self.fh.close()

        return LimitedReader(f, length)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def copyfile(self, source, outputfile):
        try:
            while True:
                buf = source.read(65536)
                if not buf:
                    break
                outputfile.write(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            source.close()


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with socketserver.TCPServer(("", PORT), RangeHTTPRequestHandler) as httpd:
        print(f"Serving with Range support on :{PORT}")
        httpd.serve_forever()
