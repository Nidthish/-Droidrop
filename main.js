// main.js - Electron Main Process

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron'); // Add shell
const path = require('path');
const { spawn } = require('child_process');

let mainWindow;
let pythonProcess;

// This listens for a request to open the folder chooser dialog
ipcMain.handle('open-folder-dialog', async () => {
    const result = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory'] });
    return result.canceled ? null : result.filePaths[0];
});

// --- NEW: This listens for a request to open a local file (for previews) ---
ipcMain.handle('open-local-file', async (event, localPath) => {
    try {
        await shell.openPath(localPath);
        return { success: true };
    } catch (error) {
        console.error('Failed to open path:', error);
        return { success: false, error: error.message };
    }
});
// --- END OF NEW SECTION ---

function createPythonProcess() {
    const scriptPath = path.join(__dirname, 'backend', 'backend.py');
    pythonProcess = spawn('python', [scriptPath]);
    pythonProcess.stdout.on('data', (data) => console.log(`Python stdout: ${data}`));
    pythonProcess.stderr.on('data', (data) => console.error(`Python stderr: ${data}`));
    pythonProcess.on('close', (code) => console.log(`Python process exited with code ${code}`));
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1200, height: 800,
        webPreferences: { nodeIntegration: true, contextIsolation: false },
    });
    mainWindow.loadFile('index.html');
}

app.whenReady().then(() => {
    createPythonProcess();
    createWindow();
    app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') { if (pythonProcess) pythonProcess.kill(); app.quit(); }
});

app.on('before-quit', () => { if (pythonProcess) pythonProcess.kill(); });