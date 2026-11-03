// MediaForge desktop: starts the bundled backend on a free local port and shows the app in a window.
// Files never leave this computer; the backend listens on 127.0.0.1 only.
const { app, BrowserWindow, shell, dialog } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const isWin = process.platform === "win32";
let server = null;
let win = null;
let quitting = false;

// Packaged: everything sits in Resources. Development (`npm start`): use the project folders.
function res(...parts) {
  return app.isPackaged ? path.join(process.resourcesPath, ...parts) : null;
}
const paths = {
  backend: res("backend") || path.join(__dirname, "dist-py", "mediaforge-server"),
  frontend: res("frontend") || path.join(__dirname, "..", "frontend", "dist"),
  samples: res("samples") || path.join(__dirname, "..", "samples"),
  ffmpeg: res("ffmpeg") || path.join(__dirname, "ffmpeg"),
};

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.unref();
    s.on("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function healthy(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/api/health", timeout: 1500 }, (r) => {
      r.resume();
      resolve(r.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function startBackend(port) {
  const data = app.getPath("userData");
  const exe = path.join(paths.backend, isWin ? "mediaforge-server.exe" : "mediaforge-server");
  // Bundled FFmpeg first; common system locations as a fallback.
  const extra = isWin ? [] : ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin"];
  const env = {
    ...process.env,
    PATH: [paths.ffmpeg, ...extra, process.env.PATH || ""].join(path.delimiter),
    MF_WORK_DIR: path.join(data, "work"),
    MF_CACHE_DIR: path.join(data, "cache"),
    MF_FRONTEND_DIST: paths.frontend,
    MF_SAMPLES_DIR: paths.samples,
  };
  fs.mkdirSync(path.join(data, "logs"), { recursive: true });
  const log = fs.openSync(path.join(data, "logs", "backend.log"), "a");
  server = spawn(exe, ["--port", String(port)], { env, stdio: ["ignore", log, log], windowsHide: true });
  server.on("exit", (code) => {
    server = null;
    if (!quitting) {
      dialog.showErrorBox("MediaForge stopped", `The conversion engine stopped unexpectedly (code ${code}). ` +
        `Details are in ${path.join(data, "logs", "backend.log")}. Please restart MediaForge.`);
      app.quit();
    }
  });
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 420, minHeight: 600, title: "MediaForge", show: false,
    backgroundColor: "#f5f2eb",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.once("ready-to-show", () => win.show());
  // Links that leave the app open in the normal browser.
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith("http://127.0.0.1:")) { e.preventDefault(); shell.openExternal(url); }
  });
  await win.loadFile(path.join(__dirname, "splash.html"));

  const port = await freePort();
  startBackend(port);
  for (let i = 0; i < 600 && server; i++) {          // up to ~5 minutes on a slow first launch
    if (await healthy(port)) { await win.loadURL(`http://127.0.0.1:${port}/`); return; }
    await new Promise((r) => setTimeout(r, 500));
  }
}

function stopBackend() {
  quitting = true;
  if (server) { try { server.kill(); } catch { /* already gone */ } }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(createWindow);
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", stopBackend);
  process.on("exit", stopBackend);
}
