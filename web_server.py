"""Portable local UI and safe public-hosting mode for background video jobs."""
import argparse
import json
import os
import re
import secrets
import sys
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from config import DEFAULT_DOWNLOAD_DIR, MAX_BODY, prepare_directory
from core.jobs import JobManager

HOSTED_MODE = os.getenv('HOSTED_MODE', '').strip().lower() in ('1', 'true', 'yes')
JOBS = JobManager()
SETTINGS = BASE_DIR / 'settings.json'
COOKIE_NAME = 'video_workspace_session'


def last_directory():
    if HOSTED_MODE:
        return str(DEFAULT_DOWNLOAD_DIR)
    try:
        value = json.loads(SETTINGS.read_text(encoding='utf-8')).get('save_dir')
        return value if isinstance(value, str) and value else str(DEFAULT_DOWNLOAD_DIR)
    except (OSError, ValueError):
        return str(DEFAULT_DOWNLOAD_DIR)


def persist_directory(path):
    if HOSTED_MODE:
        return
    temp = SETTINGS.with_suffix('.tmp')
    temp.write_text(json.dumps({'save_dir': str(path)}, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, SETTINGS)


class Handler(BaseHTTPRequestHandler):
    server_version = 'VideoWorkspace/2.2'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass

    def allowed_host(self):
        if HOSTED_MODE:
            return bool(self.headers.get('Host'))
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def session(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookies.get(COOKIE_NAME)
            return value.value if value and re.fullmatch(r'[A-Za-z0-9_-]{24,128}', value.value) else ''
        except (KeyError, ValueError):
            return ''

    def reply(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def serve_static(self, filename, new_session=''):
        data = (BASE_DIR / 'static' / filename).read_bytes()
        self.send_response(200)
        types = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}
        self.send_header('Content-Type', types[filename])
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if new_session:
            self.send_header('Set-Cookie', f'{COOKIE_NAME}={new_session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def require_session(self):
        owner = self.session()
        if not owner:
            self.reply({'error': '页面会话已失效，请刷新'}, 403)
            return ''
        return owner

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if not self.allowed_host():
                return self.reply({'error': '不允许的访问来源'}, 403)
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path in ('/', '/app.js', '/style.css'):
                filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[parsed.path]
                new_session = secrets.token_urlsafe(32) if filename == 'index.html' and not self.session() else ''
                return self.serve_static(filename, new_session)
            owner = self.require_session()
            if not owner:
                return
            if parsed.path == '/api/config':
                return self.reply({'save_dir': last_directory(), 'max_links': 100, 'hosted': HOSTED_MODE})
            if parsed.path == '/api/job':
                return self.reply(JOBS.snapshot(owner, query.get('id', [''])[0]))
            if parsed.path == '/api/file':
                path = JOBS.resolve_file(owner, query.get('id', [''])[0])
                if not path or not path.is_file():
                    return self.reply({'error': '文件不存在或已过期，请重新解析并下载'}, 404)
                return self.serve_file(path, query.get('download') == ['1'])
            self.reply({'error': '没有此接口'}, 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            self.reply({'error': str(e)}, 400)

    def serve_file(self, path, attachment):
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        raw = self.headers.get('Range')
        if raw:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', raw)
            if match and any(match.groups()):
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), end) if match[2] else end
                else:
                    start = max(0, size - int(match[2]))
                partial = True
            if not partial or start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if attachment:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(path.name))
        self.end_headers()
        if self.command == 'HEAD':
            return
        with path.open('rb') as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        try:
            if not self.allowed_host():
                return self.reply({'error': '不允许的访问来源'}, 403)
            owner = self.require_session()
            if not owner:
                return
            origin = self.headers.get('Origin')
            if origin:
                parsed_origin = urllib.parse.urlsplit(origin)
                if parsed_origin.netloc != self.headers.get('Host'):
                    return self.reply({'error': '不允许跨站操作'}, 403)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                return self.reply({'error': '请求过大或为空'}, 413)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('请求必须是 JSON 对象')
            if self.path == '/api/parse':
                return self.reply({'id': JOBS.parse(owner, data.get('text', ''))}, 202)
            if self.path == '/api/download':
                save_dir = str(DEFAULT_DOWNLOAD_DIR) if HOSTED_MODE else data.get('save_dir', '')
                jid = JOBS.download(owner, data.get('parse_id'), data.get('choices'), save_dir)
                persist_directory(JOBS.snapshot(owner, jid)['save_dir'])
                return self.reply({'id': jid}, 202)
            if self.path == '/api/cancel':
                JOBS.cancel(owner, data.get('id'))
                return self.reply({'ok': True})
            if self.path == '/api/path':
                path = DEFAULT_DOWNLOAD_DIR if HOSTED_MODE else prepare_directory(data.get('path', ''))
                persist_directory(path)
                return self.reply({'path': str(path)})
            self.reply({'error': '没有此接口'}, 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            self.reply({'error': str(e)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.getenv('HOST', '0.0.0.0' if HOSTED_MODE else '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '7860')))
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = None
    for port in range(args.port, args.port + (1 if HOSTED_MODE else 21)):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit('没有可用端口')
    url = f'http://{args.host}:{server.server_port}'
    print(f'视频批量下载工作台 v2.2 已启动：{url}', flush=True)
    print(f'运行模式：{"公网临时下载" if HOSTED_MODE else "本机文件保存"}', flush=True)
    if not args.no_browser and not HOSTED_MODE:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
