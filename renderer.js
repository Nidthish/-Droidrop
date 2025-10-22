// renderer.js - Clear Version 2.6
const { ipcRenderer } = require('electron');
const path = require('path');
const os = require('os');

// --- Global State ---
const PYTHON_API_URL = 'http://127.0.0.1:5000';
let currentPath = "/sdcard/";
let isBusy = false;
let destinationPath = path.join(os.homedir(), 'PhoneBackup');
let duplicateScanResults = null;
let currentUser = null; // New state to track logged-in user

// --- UI Element References ---
const ui = {
    modals: {
        // ... (all your other modals)
        admin: document.getElementById('modal-admin'),
        confirmOverwrite: document.getElementById('modal-confirm-overwrite'), // <-- ADD THIS LINE
    },
    socket: io(PYTHON_API_URL),
    statusEl: document.getElementById('status-bar'),
    treeViewEl: document.getElementById('tree-view'),
    logEl: document.getElementById('log-output'),
    progressEl: document.getElementById('progress-bar'),
    pathEl: document.getElementById('current-path'),
    destPathDisplay: document.getElementById('destination-path-display'),
    buttons: {
        refresh: document.getElementById('btn-refresh'),
        up: document.getElementById('btn-up'),
        handleFiles: document.getElementById('btn-handle-files'),
        chooseDest: document.getElementById('btn-choose-dest'),
        cancel: document.getElementById('btn-cancel'),
        cloudBackup: document.getElementById('btn-cloud-backup'),
        cloudRestore: document.getElementById('btn-cloud-restore'),
        darkModeToggle: document.getElementById('btn-dark-mode-toggle'),
    },
    modals: {
        backdrop: document.getElementById('modal-backdrop'),
        transferOptions: document.getElementById('modal-transfer-options'),
        duplicateResults: document.getElementById('modal-duplicate-results'),
        manualSelect: document.getElementById('modal-manual-select'),
        createAccount: document.getElementById('modal-create-account'),
        login: document.getElementById('modal-login'),
        admin: document.getElementById('modal-admin'),
    },
    manual_modal_buttons: {
        transfer: document.getElementById('manual-transfer-button'),
        preview: document.getElementById('manual-preview-button'),
        cancel: document.getElementById('manual-cancel-button'),
    },
    toastContainer: document.getElementById('toast-container'),
    userDisplay: document.getElementById('user-display'),
    loginBtn: document.getElementById('btn-login'),
    logoutBtn: document.getElementById('btn-logout'),
    createAccountBtn: document.getElementById('btn-create-account'),
    adminPanelBtn: document.getElementById('btn-admin-panel'),
    createAccountForm: document.getElementById('create-account-form'),
    loginForm: document.getElementById('login-form'),
    adminTableBody: document.getElementById('admin-table-body'),
    adminRefreshBtn: document.getElementById('admin-refresh'),
};

// --- Socket Event Handlers ---
ui.socket.on('connect', () => { updateStatus(); fetchAndDisplayFiles(currentPath); });
ui.socket.on('log_message', addLogLine);
ui.socket.on('progress_update', (data) => { ui.progressEl.max = data.total; ui.progressEl.value = data.current; });
ui.socket.on('operation_complete', (data) => {
    setBusyState(false);
    showToast(`${data.operation.charAt(0).toUpperCase() + data.operation.slice(1)} complete! Success: ${data.success}, Failed: ${data.failed}`, 'success');
    if (data.operation === 'moving') fetchAndDisplayFiles(currentPath);
});
ui.socket.on('scan_complete', (results) => {
    setBusyState(false);
    duplicateScanResults = results;
    document.getElementById('duplicate-summary-text').textContent = `Found ${results.uniques.length} unique files and ${results.duplicates.length} duplicate groups.`;
    showModal(ui.modals.duplicateResults);
});



// This handles the request from the backend to confirm an overwrite.
// The 'ack' function is the special callback we MUST call to send the response.
// This handles the request from the backend to confirm an overwrite.
// This handles the new, non-blocking overwrite workflow
ui.socket.on('ask_for_overwrite', (data) => {
    let conflicts = data.conflicts;
    let non_conflicts = data.non_conflicts;
    let is_move_op = data.is_move_op;
    let currentIndex = 0;
    
    let to_overwrite = [];
    let to_skip = [];

    const modal = ui.modals.resolveConflicts;
    const filenameEl = modal.querySelector('#conflict-file-name');
    const counterEl = modal.querySelector('#conflict-counter');
    
    const overwriteBtn = modal.querySelector('#btn-conflict-overwrite');
    const skipBtn = modal.querySelector('#btn-conflict-skip');
    const overwriteAllBtn = modal.querySelector('#btn-conflict-overwrite-all');
    const skipAllBtn = modal.querySelector('#btn-conflict-skip-all');

    const showNextConflict = () => {
        if (currentIndex >= conflicts.length) {
            finishResolution();
            return;
        }
        filenameEl.textContent = conflicts[currentIndex].split('/').pop();
        counterEl.textContent = `File ${currentIndex + 1} of ${conflicts.length}`;
        showModal(modal);
    };

    const finishResolution = () => {
        hideModals();
        log_message(`User decisions received. Continuing operation...`);
        ui.socket.emit('resolve_conflicts', {
            to_overwrite: to_overwrite,
            to_process_first: non_conflicts, // Send non-conflicts to be processed
            is_move_op: is_move_op,
            dest_folder: destinationPath
        });
        // Clear these out for the next run
        non_conflicts = []; 
    };

    const handleOverwrite = () => {
        to_overwrite.push(conflicts[currentIndex]);
        currentIndex++;
        showNextConflict();
    };

    const handleSkip = () => {
        to_skip.push(conflicts[currentIndex]);
        currentIndex++;
        showNextConflict();
    };

    const handleOverwriteAll = () => {
        // Add all remaining conflicts to the overwrite list
        const remaining = conflicts.slice(currentIndex);
        to_overwrite.push(...remaining);
        finishResolution();
    };

    const handleSkipAll = () => {
        // Add all remaining conflicts to the skip list
        const remaining = conflicts.slice(currentIndex);
        to_skip.push(...remaining);
        finishResolution();
    };
    
    // Use .cloneNode(true) to cleanly remove old listeners before adding new ones
    let cleanOverwriteBtn = overwriteBtn.cloneNode(true);
    overwriteBtn.parentNode.replaceChild(cleanOverwriteBtn, overwriteBtn);
    cleanOverwriteBtn.addEventListener('click', handleOverwrite);

    let cleanSkipBtn = skipBtn.cloneNode(true);
    skipBtn.parentNode.replaceChild(cleanSkipBtn, skipBtn);
    cleanSkipBtn.addEventListener('click', handleSkip);

    let cleanOverwriteAllBtn = overwriteAllBtn.cloneNode(true);
    overwriteAllBtn.parentNode.replaceChild(cleanOverwriteAllBtn, overwriteAllBtn);
    cleanOverwriteAllBtn.addEventListener('click', handleOverwriteAll);

    let cleanSkipAllBtn = skipAllBtn.cloneNode(true);
    skipAllBtn.parentNode.replaceChild(cleanSkipAllBtn, skipAllBtn);
    cleanSkipAllBtn.addEventListener('click', handleSkipAll);
    
    // Start the process
    showNextConflict();
});
// ADD THIS NEW EVENT HANDLER
ui.socket.on('operation_cancelled', () => {
    setBusyState(false); // Re-enables buttons and puts UI back in its initial state
    ui.progressEl.value = 0; // Resets the progress bar
    showToast('Operation has been cancelled.', 'info');
});


// --- UI & Application Logic ---
function addLogLine(msg) {
    const line = document.createElement('div');
    line.className = 'log-line';
    const messageText = msg.data;
    const messageType = msg.type || 'info'; // Use the 'type' from the server or default to 'info'
    line.classList.add(messageType);
    line.textContent = messageText;
    ui.logEl.appendChild(line);
    ui.logEl.scrollTop = ui.logEl.scrollHeight;
}

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    ui.toastContainer.appendChild(toast);
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 500);
    }, 4000);
}

function showModal(modalElement) { ui.modals.backdrop.classList.remove('hidden'); modalElement.classList.remove('hidden'); }
function hideModals() { Object.values(ui.modals).forEach(modal => modal.classList.add('hidden')); }
function setBusyState(busy) {
    isBusy = busy;
    Object.values(ui.buttons).forEach(btn => {
        if (btn.id !== 'btn-cloud-backup' && btn.id !== 'btn-cloud-restore') {
            btn.disabled = busy;
        }
    });
    ui.buttons.cloudBackup.disabled = busy || !currentUser;
    ui.buttons.cloudRestore.disabled = busy || !currentUser;
    ui.buttons.cancel.disabled = !busy;
    document.body.style.cursor = busy ? 'wait' : 'default';
}

async function updateStatus() {
    try {
        const r = await fetch(`${PYTHON_API_URL}/api/status`);
        const d = await r.json();
        ui.statusEl.textContent = d.message;
        ui.statusEl.className = `status-bar ${d.status}`;
        
        // --- THIS IS THE CRITICAL LOGIC ---
        if (d.status === 'success') {
            // If the backend confirms a good connection, fetch the files.
            fetchAndDisplayFiles(currentPath);
        } else {
            // If the status is 'warning' or 'error', clear the file display.
            ui.treeViewEl.innerHTML = '';
        }
        
    } catch (e) {
        ui.statusEl.textContent = 'Error connecting to mobile.';
        ui.statusEl.className = 'status-bar error';
        ui.treeViewEl.innerHTML = ''; // Also clear on a total connection failure
        showToast('Could not connect to the Python backend.', 'error');
    }
}

async function fetchAndDisplayFiles(path) { ui.treeViewEl.innerHTML = '<div class="loading">Loading...</div>'; ui.pathEl.value = path; try { const r = await fetch(`${PYTHON_API_URL}/api/list_path`, {method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: path }),}); const f = await r.json(); renderTreeView(f); } catch (e) { ui.treeViewEl.innerHTML = '<div class="error">Failed to fetch files.</div>';} }
function renderTreeView(files) { ui.treeViewEl.innerHTML = ''; files.forEach(file => { const item=document.createElement('div'); item.className='tree-item'; item.dataset.path = currentPath + file.name; item.dataset.isDir = file.is_dir; const icon=file.is_dir ? '&#128193;' : '&#128196;'; item.innerHTML=`<input type="checkbox" class="item-checkbox" style="display: none;"><span class="item-icon">${icon}</span><span class="item-name">${file.name}</span><span class="item-size">${file.size}</span>`; ui.treeViewEl.appendChild(item); });}
function updateDestinationPathUI(p) { destinationPath = p; ui.destPathDisplay.textContent = p; }

function getSelectedFilePaths() { return Array.from(document.querySelectorAll('.tree-item .item-checkbox:checked')).map(cb => cb.closest('.tree-item').dataset.path); }
function startOperation(opType, fileList) {
    if (!fileList || fileList.length === 0) { showToast('No files were specified for the operation.', 'error'); return; }
    setBusyState(true);
    ui.socket.emit('start_operation', { operation: opType, paths: fileList, dest_folder: destinationPath, user_id: currentUser });
}
function showManualSelectionUI() {
    if (!duplicateScanResults || duplicateScanResults.duplicates.length === 0) {
        showToast('No duplicates found to select manually.', 'info');
        return;
    }
    const listEl = document.getElementById('manual-select-list'); listEl.innerHTML = '';
    duplicateScanResults.duplicates.forEach((group, index) => {
        const groupEl = document.createElement('div'); groupEl.className = 'duplicate-group';
        groupEl.innerHTML = `<div class="duplicate-group-header">Duplicate Group ${index + 1} (${group.files.length} files)</div>`;
        group.files.forEach((file, fileIndex) => {
            const fileEl = document.createElement('div'); fileEl.className = 'duplicate-file-item';
            const isChecked = fileIndex > 0 ? 'checked' : '';
            const inputId = `dup-${index}-${fileIndex}`;
            fileEl.innerHTML = `<input type="checkbox" id="${inputId}" data-path="${file}" ${isChecked}><label for="${inputId}">${file}</label>`;
            groupEl.appendChild(fileEl);
        });
        listEl.appendChild(groupEl);
    });
    showModal(ui.modals.manualSelect);
}

function updateLoginState(user) {
    currentUser = user;
    if (user) {
        ui.userDisplay.textContent = `Hello, ${user}`;
        ui.loginBtn.classList.add('hidden');
        ui.logoutBtn.classList.remove('hidden');
        ui.buttons.cloudBackup.disabled = false;
        ui.buttons.cloudRestore.disabled = false;
    } else {
        ui.userDisplay.textContent = 'Logged Out';
        ui.loginBtn.classList.remove('hidden');
        ui.logoutBtn.classList.add('hidden');
        ui.buttons.cloudBackup.disabled = true;
        ui.buttons.cloudRestore.disabled = true;
    }
    setBusyState(isBusy);
}

function fetchAdminUsers() {
    fetch(`${PYTHON_API_URL}/api/admin_users`)
        .then(r => r.json())
        .then(users => {
            ui.adminTableBody.innerHTML = '';
            users.forEach(user => {
                const row = ui.adminTableBody.insertRow();
                const expiryDate = new Date(user.expiry);
                const expiryText = expiryDate.toLocaleDateString();
                row.innerHTML = `
                    <td>${user.user_id}</td>
                    <td>${user.plan}</td>
                    <td>${user.container}</td>
                    <td>${user.created.split('T')[0]}</td>
                    <td>${expiryText}</td>
                    <td><button class="btn-delete-user" data-user-id="${user.user_id}">Delete</button></td>
                `;
            });
            document.querySelectorAll('.btn-delete-user').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const userIdToDelete = e.target.dataset.userId;
                    if (confirm(`Are you sure you want to delete user ${userIdToDelete}?`)) {
                        fetch(`${PYTHON_API_URL}/api/admin_delete_user`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ user_id: userIdToDelete })
                        })
                        .then(r => r.json())
                        .then(d => {
                            if (d.success) {
                                showToast(d.message, 'success');
                                fetchAdminUsers();
                            } else {
                                showToast(d.message, 'error');
                            }
                        });
                    }
                });
            });
        })
        .catch(e => {
            showToast('Failed to load admin data.', 'error');
        });
}


// --- Event Listener Setup ---
function initializeEventListeners() {
    ui.buttons.refresh.addEventListener('click', updateStatus);
    ui.buttons.up.addEventListener('click', () => { 
        if (isBusy || currentPath === '/sdcard/') return; 
        let p = currentPath.split('/').filter(i => i); 
        p.pop(); 
        currentPath = '/' + p.join('/') + '/'; 
        fetchAndDisplayFiles(currentPath); 
    });
    ui.buttons.chooseDest.addEventListener('click', async () => { 
        const p = await ipcRenderer.invoke('open-folder-dialog'); 
        if (p) { 
            updateDestinationPathUI(p); 
            showToast(`Destination set!`); 
        } 
    });
    ui.buttons.cancel.addEventListener('click', () => ui.socket.emit('cancel_operation'));

    ui.treeViewEl.addEventListener('click', (e) => { 
        if (isBusy) return; 
        const i = e.target.closest('.tree-item'); 
        if (!i) return; 
        const c = i.querySelector('.item-checkbox'); 
        c.checked = !c.checked; 
        i.classList.toggle('selected', c.checked); 
    });
    ui.treeViewEl.addEventListener('dblclick', async (e) => {
        if (isBusy) return; 
        const item = e.target.closest('.tree-item'); 
        if (!item) return;
        if (item.dataset.isDir === 'true') { 
            item.querySelector('.item-checkbox').checked = false; 
            item.classList.remove('selected'); 
            currentPath = item.dataset.path; 
            fetchAndDisplayFiles(currentPath); 
        } else {
            setBusyState(true); 
            const remotePath = item.dataset.path;
            try {
                const response = await fetch(`${PYTHON_API_URL}/api/preview_file`, { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ path: remotePath })
                });
                const result = await response.json();
                if (result.success) { 
                    await ipcRenderer.invoke('open-local-file', result.local_path); 
                } else { 
                    showToast(`Preview failed: ${result.error}`, 'error'); 
                }
            } catch (err) { 
                showToast('Failed to request preview from backend.', 'error'); 
            } finally { 
                setBusyState(false); 
            }
        }
    });

    ui.buttons.handleFiles.addEventListener('click', () => { 
        const s = getSelectedFilePaths(); 
        if (s.length === 0) { 
            showToast('Please select at least one file or folder first.', 'error'); 
            return; 
        } 
        showModal(ui.modals.transferOptions); 
    });
    ui.buttons.cloudBackup.addEventListener('click', () => {
        const selectedFiles = getSelectedFilePaths();
        if (selectedFiles.length === 0) {
            showToast('Please select files to back up to the cloud.', 'error');
            return;
        }
        startOperation('cloud_backup', selectedFiles);
    });

    // Add the new event listener for the Cloud Restore button here
    ui.buttons.cloudRestore.addEventListener('click', () => {
        if (!destinationPath) {
            showToast('Please choose a local destination folder first.', 'error');
            return;
        }
        if (!currentUser) {
            showToast('Please log in to a cloud account first.', 'error');
            return;
        }
        if (!confirm('Are you sure you want to restore all files from the cloud?')) {
            return;
        }
        setBusyState(true);
        // The file list is empty because we are restoring ALL files from the cloud
        ui.socket.emit('start_operation', {
            operation: 'cloud_restore',
            paths: [], // Pass an empty list since we restore all files from the cloud
            dest_folder: destinationPath,
            user_id: currentUser
        });
    });
    
    ui.loginBtn.addEventListener('click', () => showModal(ui.modals.login));
    ui.logoutBtn.addEventListener('click', () => {
        updateLoginState(null);
        showToast('Logged out successfully.', 'success');
    });
    ui.createAccountBtn.addEventListener('click', () => showModal(ui.modals.createAccount));
    ui.adminPanelBtn.addEventListener('click', () => {
        fetchAdminUsers();
        showModal(ui.modals.admin);
    });
    ui.adminRefreshBtn.addEventListener('click', fetchAdminUsers);

    // Consolidated modal event handling
    // Consolidated modal event handling
Object.values(ui.modals).forEach(modal => {
    modal.addEventListener('click', (e) => {
        const action = e.target.dataset.action;
        if (!action) return;
        
        // Handle all cancel buttons first
        if (e.target.classList.contains('cancel-modal') || action === 'cancel') {
            hideModals();
            return;
        }

        // --- CORRECTED LOGIC FOR MODAL ACTIONS ---

        if (action === 'copy_all') {
            hideModals();
            // Send the 'copy' command that the backend expects
            startOperation('copy', getSelectedFilePaths());
        }
        
        if (action === 'move_all') {
            if (!confirm(`CONFIRM: This will PERMANENTLY DELETE the selected items from your phone. Are you sure?`)) {
                return;
            }
            hideModals();
            // Send the 'move' command that the backend expects
            startOperation('move', getSelectedFilePaths());
        }

        if (action === 'find_duplicates') {
            hideModals();
            startOperation('find_duplicates', getSelectedFilePaths());
        }
        
        if (action === 'transfer_uniques') {
            hideModals();
            startOperation('copy', duplicateScanResults.uniques);
        }

        if (action === 'transfer_all') {
            hideModals();
            startOperation('copy', duplicateScanResults.all_files);
        }

        if (action === 'select_manually') {
            hideModals();
            showManualSelectionUI();
        }
    });
});
    // New Event listeners for Manual selection modal buttons
    ui.manual_modal_buttons.transfer.addEventListener('click', () => {
        const selectedPaths = Array.from(document.querySelectorAll('#manual-select-list input:checked')).map(input => input.dataset.path);
        if (selectedPaths.length > 0) {
            hideModals();
            startOperation('copy', selectedPaths);
        } else {
            showToast('Please select at least one file to transfer.', 'error');
        }
    });

    ui.manual_modal_buttons.preview.addEventListener('click', async () => {
        const selectedCheckboxes = document.querySelectorAll('#manual-select-list input:checked');
        if (selectedCheckboxes.length !== 1) {
            showToast('Please select exactly one file to preview.', 'error');
            return;
        }

        const remotePath = selectedCheckboxes[0].dataset.path;
        setBusyState(true);

        try {
            const response = await fetch(`${PYTHON_API_URL}/api/preview_file`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: remotePath })
            });
            const result = await response.json();
            if (result.success) {
                await ipcRenderer.invoke('open-local-file', result.local_path);
            } else {
                showToast(`Preview failed: ${result.error}`, 'error');
            }
        } catch (err) {
            showToast('Failed to request preview from backend.', 'error');
        } finally {
            setBusyState(false);
        }
    });

    ui.manual_modal_buttons.cancel.addEventListener('click', () => {
        hideModals();
    });

    ui.createAccountForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const formData = new FormData(ui.createAccountForm);
        const data = Object.fromEntries(formData.entries());
        fetch(`${PYTHON_API_URL}/api/create_account`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        })
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                showToast(d.message, 'success');
                hideModals();
            } else {
                showToast(d.message, 'error');
            }
        });
    });

    ui.loginForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const userId = document.getElementById('login-user-id').value;
        fetch(`${PYTHON_API_URL}/api/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId })
        })
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                updateLoginState(d.user);
                showToast(`Logged in as ${d.user}!`, 'success');
                hideModals();
            } else {
                showToast(d.message, 'error');
            }
        });
    });

    document.querySelectorAll('.cancel-modal').forEach(btn => {
        btn.addEventListener('click', () => {
            hideModals();
        });
    });

    ui.buttons.darkModeToggle.addEventListener('click', () => {
    document.body.classList.toggle('dark-theme');
    });

    
}

// --- App Initialization ---
document.addEventListener('DOMContentLoaded', () => {
    updateDestinationPathUI(destinationPath);
    initializeEventListeners();
});