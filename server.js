const express = require('express');
const http = require('http');
const path = require('path');
const { WebSocketServer } = require('ws');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Receives end-of-song stats (hook up MySQL / Python ML here later)
app.post('/api/results', (req, res) => {
  console.log('Round results:', req.body);
  res.json({ ok: true });
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

server.listen(3000, '0.0.0.0', () => console.log('Game: http://localhost:3000'));

module.exports = server;
