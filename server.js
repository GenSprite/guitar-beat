const express = require('express');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');
const { WebSocketServer } = require('ws');

const PY_PORT = Number(process.env.GUITAR_BEAT_PY_PORT || 5001);

const app = express();
app.use(express.json());
app.use(express.static('public'));

// Receives end-of-song stats (kept from the original project; the Python
// backend now does the real work through /api/game/attempts)
app.post('/api/results', (req, res) => {
  console.log('Round results:', req.body);
  res.json({ ok: true });
});

// Forward /api/game/* to the Python AI + SQLite backend (backend/app.py).
// If Python is not running the browser gets a clean 503 and the game falls
// back to classic mode instead of crashing.
app.use('/api/game', (req, res) => {
  const body = req.method === 'GET' || req.method === 'HEAD' ? null : JSON.stringify(req.body || {});
  const headers = { 'Content-Type': 'application/json' };
  if (body) headers['Content-Length'] = Buffer.byteLength(body);
  const upstream = http.request(
    { host: '127.0.0.1', port: PY_PORT, method: req.method, path: '/api' + req.url, headers },
    upstreamRes => {
      res.status(upstreamRes.statusCode || 502).set('Content-Type', 'application/json');
      upstreamRes.pipe(res);
    }
  );
  upstream.setTimeout(8000, () => upstream.destroy(new Error('Python backend timed out')));
  upstream.on('error', () => {
    if (!res.headersSent) {
      res.status(503).json({ ok: false, error: 'backend_unavailable', message: 'The Python backend is not running.' });
    } else {
      res.end();
    }
  });
  if (body) upstream.write(body);
  upstream.end();
});

const server = http.createServer(app);
const wss = new WebSocketServer({ server });

wss.on('connection', ws => {
  ws.on('message', msg => {
    // relay ESP32 input to every other client (the browser game)
    wss.clients.forEach(c => {
      if (c !== ws && c.readyState === 1) c.send(msg.toString());
    });
  });
});

// Start the Python backend alongside the game so one command runs everything.
// Set GUITAR_BEAT_NO_PYTHON=1 to start it yourself (python backend/app.py),
// or PYTHON=/path/to/python to choose the interpreter.
let pythonChild = null;
function startPythonBackend() {
  if (process.env.GUITAR_BEAT_NO_PYTHON === '1') return;
  const script = path.join(__dirname, 'backend', 'app.py');
  const candidates = process.env.PYTHON
    ? [process.env.PYTHON]
    : process.platform === 'win32' ? ['python', 'py', 'python3'] : ['python3', 'python'];
  const tried = [];
  const attempt = index => {
    if (index >= candidates.length) {
      console.warn(`[backend] Could not start the Python backend (tried: ${tried.join(', ')}).`);
      console.warn('[backend] Install Python 3.8+ or run "python backend/app.py" yourself. The game runs in classic mode until then.');
      return;
    }
    const command = candidates[index];
    tried.push(command);
    const startedAt = Date.now();
    const child = spawn(command, [script], {
      stdio: 'inherit',
      env: { ...process.env, GUITAR_BEAT_PY_PORT: String(PY_PORT) }
    });
    child.on('error', () => attempt(index + 1));           // interpreter not found
    child.on('spawn', () => { pythonChild = child; });
    child.on('exit', code => {
      if (code && Date.now() - startedAt < 4000) attempt(index + 1);   // e.g. Windows Store python stub
      else if (code) console.warn(`[backend] Python backend stopped (exit code ${code}).`);
    });
  };
  attempt(0);
}
process.on('exit', () => { if (pythonChild) pythonChild.kill(); });
process.on('SIGINT', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));

server.listen(3000, '0.0.0.0', () => {
  console.log('Game: http://localhost:3000');
  startPythonBackend();
});
