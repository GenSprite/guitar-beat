const { app, BrowserWindow } = require('electron');
const server = require('./server');   // starts the Express + WebSocket server

let win;

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 800,
    autoHideMenuBar: true,
    title: 'GUITAR-BEAT',
    webPreferences: { autoplayPolicy: 'no-user-gesture-required' }
  });
  win.loadURL('http://localhost:3000');
}

server.on('listening', () => app.whenReady().then(createWindow));

app.on('window-all-closed', () => {
  server.close();
  app.quit();
});
