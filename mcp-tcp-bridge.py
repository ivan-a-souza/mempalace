#!/usr/bin/env python3
"""
MemPalace MCP Bridge v8 — TCP + HTTP com pool de workers persistentes
=====================================================================
Port 5000: TCP (fork + dup2 → MCP stdio) — uma sessão dedicada por conexão
Port 5001: HTTP MCP (POST /mcp, GET /mcp SSE, GET /health)

Mudanças da v7 → v8
--------------------
A v7 spawnava um subprocesso MCP novo a CADA POST HTTP. Isso causava dois
problemas:

1. Deadlock. O fork() acontecia dentro de um handler do ThreadingHTTPServer.
   O fork só clona a thread chamadora; se outra thread segurasse o lock do
   alocador/import naquele instante, o filho travava antes do exec. O próprio
   Python avisa: "This process is multi-threaded, use of fork() may lead to
   deadlocks in the child". Como proc.stdout.readline() não tinha timeout, o
   handler pendurava indefinidamente (>180s observados, curl http=000).

2. Latência. Cada request recarregava o modelo ONNX de embedding. Mesmo
   ocioso um POST levava ~3,4s; 10 concorrentes levavam ~45s cada (o
   container é limitado a 0,5 CPU).

A v8 mantém um pool de workers MCP de longa vida, criados no startup — antes
de qualquer thread existir, então o fork nunca é concorrente. Cada worker é
usado por uma thread por vez (lock próprio) e responde em milissegundos,
porque o modelo já está carregado.

O servidor TCP roda em um processo filho separado, criado por fork antes de
qualquer thread. Assim o fork() por conexão da porta 5000 também acontece em
processo single-thread, sem risco de deadlock — e o SIGCHLD handler do TCP
fica isolado nele, sem interferir no reaping dos Popen do pool.

Variáveis de ambiente:
  MCP_HTTP_POOL_SIZE   nº de workers HTTP (default 2; ~200MB RSS cada)
  MCP_HTTP_TIMEOUT     timeout por request em segundos (default 180)
"""
import json
import os
import queue
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
TCP_PORT = 5000
HTTP_PORT = 5001
PALACE_PATH = "/data"
MCP_CMD = ["python3", "-m", "mempalace.mcp_server", "--palace", PALACE_PATH]

POOL_SIZE = max(1, int(os.environ.get("MCP_HTTP_POOL_SIZE", "2")))
INIT_TIMEOUT = 300          # carga do modelo ONNX pode ser lenta sob carga
REQUEST_TIMEOUT = float(os.environ.get("MCP_HTTP_TIMEOUT", "180"))
ACQUIRE_TIMEOUT = 120       # espera por um worker livre
STDERR_PATH = "/tmp/mcp-worker-{}.err"


# ── TCP bridge (fork per connection, roda em processo single-thread) ─────────

def _reap_zombies():
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid <= 0:
                break
    except (ChildProcessError, OSError):
        pass


def _sigchld_handler(_signum, _frame):
    _reap_zombies()


def run_tcp_server():
    signal.signal(signal.SIGCHLD, _sigchld_handler)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, TCP_PORT))
    server.listen(128)
    server.set_inheritable(True)
    print(f"[mcp-bridge] TCP listening on tcp://{HOST}:{TCP_PORT}", file=sys.stderr)
    while True:
        try:
            client, addr = server.accept()
        except InterruptedError:
            continue
        client.set_inheritable(True)
        pid = os.fork()
        if pid == 0:
            server.close()
            cli_fd = client.fileno()
            os.dup2(cli_fd, 0)
            os.dup2(cli_fd, 1)
            if cli_fd > 2:
                client.close()
            os.execvp(MCP_CMD[0], MCP_CMD)
            sys.exit(1)
        else:
            client.close()
            _reap_zombies()


# ── Pool de workers MCP persistentes ─────────────────────────────────────────

class MCPWorker:
    """Um processo MCP stdio de longa vida, usado por uma thread por vez.

    O lock é adquirido pelo handler HTTP e liberado no fim do request. Uma
    thread leitora dedicada consome o stdout e enfileira as respostas, para
    que a espera tenha timeout de verdade (queue.get) em vez de um
    readline() que bloqueia para sempre.
    """

    def __init__(self, idx):
        self.idx = idx
        self.lock = threading.Lock()
        self.proc = None
        self.init_response = None
        self.responses = None
        self.restarts = 0
        self._stderr = None

    @property
    def stderr_path(self):
        return STDERR_PATH.format(self.idx)

    def start(self):
        if self._stderr:
            try:
                self._stderr.close()
            except OSError:
                pass
        self._stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            MCP_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
        )
        # A thread leitora é a única a tocar em proc.stdout.
        q = queue.Queue()
        self.responses = q
        threading.Thread(
            target=self._read_loop, args=(self.proc, q), daemon=True
        ).start()
        self.init_response = self._handshake()

    def _read_loop(self, proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        except (ValueError, OSError):
            pass
        finally:
            q.put(None)  # sentinel de EOF

    def _read(self, timeout):
        """Devolve uma linha, b'' no EOF, ou None em timeout."""
        try:
            item = self.responses.get(timeout=timeout)
        except queue.Empty:
            return None
        return item if item is not None else b""

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def _handshake(self):
        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcp-http-bridge", "version": "8.0"},
            },
        })
        line = self._read(INIT_TIMEOUT)
        if not line:
            raise RuntimeError(
                f"worker {self.idx}: handshake falhou: {self.stderr_tail()}"
            )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return json.loads(line)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stderr_tail(self, n=400):
        try:
            with open(self.stderr_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - n * 4))
                return f.read()[-n:].decode(errors="replace").strip()
        except OSError:
            return ""

    def restart(self):
        """Mata e recria o worker. Chamado dentro do lock do próprio worker."""
        self.shutdown()
        try:
            self.start()
            self.restarts += 1
        except Exception as e:
            print(f"[mcp-bridge] worker {self.idx} restart falhou: {e}", file=sys.stderr)
            self.proc = None

    def shutdown(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None


class WorkerPool:
    """Conjunto fixo de workers; cada request pega um livre e devolve no fim."""

    def __init__(self, size):
        self.workers = [MCPWorker(i) for i in range(size)]
        self.init_template = None

    def start(self):
        alive = 0
        for w in self.workers:
            try:
                w.start()
                alive += 1
            except Exception as e:
                print(f"[mcp-bridge] worker {w.idx} não subiu: {e}", file=sys.stderr)
        if alive == 0:
            raise RuntimeError("nenhum worker MCP subiu")
        self.init_template = next(
            (w.init_response for w in self.workers if w.init_response), None
        )
        print(f"[mcp-bridge] pool pronto: {alive}/{len(self.workers)} workers",
              file=sys.stderr)

    def acquire(self, timeout=ACQUIRE_TIMEOUT):
        """Devolve um worker com o lock já adquirido, ou None."""
        deadline = time.monotonic() + timeout
        while True:
            for w in self.workers:
                if w.lock.acquire(blocking=False):
                    return w
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def healthy(self):
        return [w for w in self.workers if w.alive()]


# ── HTTP MCP ─────────────────────────────────────────────────────────────────

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class MCPHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    pool = None  # injetado por run_http_server

    def _get_session_id(self):
        _, query = self._parsed()
        return query.get('session_id', [None])[0] or self.headers.get('X-MCP-Session-Id')

    def _parsed(self):
        parsed = urlparse(self.path)
        return parsed, parse_qs(parsed.query)

    def _path_ok(self) -> bool:
        parsed = urlparse(self.path)
        return parsed.path in ('/mcp', '/mcp/message', '/oauth/authorize')

    def _send_json(self, status, obj, sid=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if sid:
            self.send_header('X-MCP-Session-Id', sid)
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, status, rpc_id, message, sid=None):
        self._send_json(status, {
            "jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": -32603 if status >= 500 else -32600, "message": message},
        }, sid)

    # ── GET /mcp : SSE keepalive | GET /health ──
    def do_GET(self):
        parsed, query = self._parsed()

        if parsed.path == '/health':
            workers = self.pool.healthy() if self.pool else []
            status = 200 if workers else 503
            self._send_json(status, {
                "status": "ok" if workers else "unhealthy",
                "workers_alive": len(workers),
                "workers_total": len(self.pool.workers) if self.pool else 0,
                "restarts": sum(w.restarts for w in self.pool.workers) if self.pool else 0,
            })
            return

        if not self._path_ok():
            self.send_error(404)
            return

        # OAuth2 authorization endpoint — auto-approve
        if parsed.path == '/oauth/authorize':
            self._oauth_authorize(query)
            return

        sid = self._get_session_id() or str(uuid.uuid4())

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('X-MCP-Session-Id', sid)
        self.end_headers()

        ev = json.dumps({"jsonrpc": "2.0", "method": "initialized"})
        self.wfile.write(f"event: initialized\ndata: {ev}\n\n".encode())
        self.wfile.flush()

        try:
            while True:
                time.sleep(30)
                self.wfile.write(b': keepalive\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ── POST /mcp : worker persistente → resposta inline ──
    def do_POST(self):
        if not self._path_ok():
            self.send_error(404)
            return

        sid = self._get_session_id() or str(uuid.uuid4())
        content_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_len)
        if not body:
            self.send_error(400, "Empty body")
            return

        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        if msg.get('id') is None:
            # Notificação: o worker já foi inicializado no startup. Mandar de
            # novo corromperia o stream de respostas, então só confirma.
            self._send_json(202, {}, sid)
            return

        rpc_id = msg.get('id')

        # initialize é respondido do cache — não precisa acordar um worker.
        if msg.get('method') == 'initialize' and self.pool.init_template:
            resp = dict(self.pool.init_template)
            resp['id'] = rpc_id
            self._send_json(200, resp, sid)
            return

        worker = self.pool.acquire()
        if worker is None:
            self._rpc_error(503, rpc_id, "Nenhum worker MCP livre (pool ocupado)", sid)
            return

        try:
            if not worker.alive():
                worker.restart()
            if not worker.alive():
                self._rpc_error(502, rpc_id,
                                f"Worker MCP indisponível: {worker.stderr_tail()}", sid)
                return

            try:
                worker._send(msg)
            except (BrokenPipeError, OSError) as e:
                worker.restart()
                self._rpc_error(502, rpc_id, f"Falha ao enviar ao worker: {e}", sid)
                return

            line = worker._read(REQUEST_TIMEOUT)

            if line is None:
                # Timeout: o worker pode estar travado — descarta para não
                # devolver um processo em estado desconhecido ao próximo request.
                err = worker.stderr_tail()
                worker.restart()
                self._rpc_error(504, rpc_id,
                                f"Timeout de {REQUEST_TIMEOUT:.0f}s no worker MCP. {err}", sid)
                return

            if not line:
                err = worker.stderr_tail()
                worker.restart()
                self._rpc_error(502, rpc_id,
                                f"Worker MCP encerrou a conexão. {err}", sid)
                return

            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                worker.restart()
                self._rpc_error(502, rpc_id, "Resposta inválida do worker MCP", sid)
                return

            self._send_json(200, result, sid)

        finally:
            worker.lock.release()

    # ── OAuth2 authorize (auto-approve for ChatGPT) ──
    def _oauth_authorize(self, query):
        import http.client as http_client

        client_id = query.get('client_id', [None])[0]
        redirect_uri = query.get('redirect_uri', [None])[0]
        state = query.get('state', [None])[0]

        if not client_id or not redirect_uri:
            self.send_error(400, "Missing client_id or redirect_uri")
            return

        api = http_client.HTTPConnection('kong', 8001)

        # 1. Get the OAuth2 credential ID
        api.request('GET', '/consumers/mempalace-service/oauth2')
        info_data = json.loads(api.getresponse().read())

        cred_id = None
        for cred in info_data.get('data', []):
            if cred.get('client_id') == client_id:
                cred_id = cred['id']
                break

        if not cred_id:
            self.send_error(400, f"Unknown client_id: {client_id}")
            return

        # 2. Dynamically add the ChatGPT redirect_uri to the credential
        current_uris = [cred.get('redirect_uris', [])][0]
        if isinstance(current_uris, str):
            current_uris = [current_uris]
        if redirect_uri not in current_uris:
            current_uris.append(redirect_uri)
            patch_body = json.dumps({"redirect_uris": current_uris})
            api.request('PATCH', f'/consumers/mempalace-service/oauth2/{cred_id}', patch_body, {'Content-Type': 'application/json'})
            patch_resp = api.getresponse()
            if patch_resp.status >= 400:
                self.send_error(502, f"Failed to update redirect_uri: {patch_resp.read().decode()}")
                return

        # 3. Call Kong proxy authorize (with X-Forwarded-Proto) to get authorization code
        # Need to call Kong proxy (8000), not admin API (8001)
        proxy = http_client.HTTPConnection('kong', 8000)
        auth_body = json.dumps({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "",
            "provision_key": "provision-key-2026",
            "authenticated_userid": "mempalace-service",
            "state": state or "",
        })
        proxy.request('POST', '/mcp/oauth2/authorize', auth_body,
            {'Content-Type': 'application/json', 'X-Forwarded-Proto': 'https', 'Host': 'mempalace.naesquina.com.br'})
        resp = proxy.getresponse()
        data = json.loads(resp.read())

        location = data.get('redirect_uri')
        if not location:
            self.send_error(502, f"Kong auth error: {json.dumps(data)}")
            return

        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()

    def log_message(self, fmt, *args):
        try:
            sys.stderr.write(f"[mcp-bridge:http] {args[0]} {args[1]} {args[2]}\n")
        except IndexError:
            pass


def run_http_server(pool):
    MCPHTTPHandler.pool = pool
    httpd = ThreadingHTTPServer((HOST, HTTP_PORT), MCPHTTPHandler)
    print(f"[mcp-bridge] HTTP MCP listening on http://{HOST}:{HTTP_PORT}/mcp", file=sys.stderr)
    httpd.serve_forever()


def _watch_tcp_child(pid):
    """Se o processo do TCP morrer, derruba o container inteiro."""
    _, status = os.waitpid(pid, 0)
    print(f"[mcp-bridge] TCP child {pid} saiu (status={status}) — encerrando",
          file=sys.stderr)
    os._exit(1)


def main():
    # Fork do TCP ANTES de existir qualquer thread: o filho nasce
    # single-thread, então os fork() por conexão são seguros.
    tcp_pid = os.fork()
    if tcp_pid == 0:
        run_tcp_server()
        os._exit(0)

    # Pai: pool criado ainda single-thread, depois o servidor HTTP.
    pool = WorkerPool(POOL_SIZE)
    pool.start()

    threading.Thread(target=_watch_tcp_child, args=(tcp_pid,), daemon=True).start()

    try:
        run_http_server(pool)
    finally:
        for w in pool.workers:
            w.shutdown()


if __name__ == "__main__":
    main()
