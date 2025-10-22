#!/usr/bin/env python3
"""
backend.py - Advance Duplicate Finder (Flask Backend)
VERSION: 2.5 (Removed hardcoded mapping; robust web scraping + caching)
"""
import os
import sys
import subprocess
import shlex
import threading
import time
import hashlib
import mimetypes
import shutil
import tempfile
from difflib import get_close_matches
import requests
from PIL import Image
from PIL.ExifTags import TAGS
from bs4 import BeautifulSoup
from collections import defaultdict
from datetime import datetime, timedelta, UTC
import json
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit
from azure.identity import ClientSecretCredential
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import AzureError, ServiceRequestError
from dotenv import load_dotenv
import base64

# --- Flask App Initialization ---
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- Configuration ---
ADB_EXECUTABLE = "adb"
HASH_FALLBACK_DIR = os.path.join(tempfile.gettempdir(), "adf_hash_temp")
ROOT_PHONE = "/sdcard"
MAX_HASH_PULL_SIZE = 500 * 1024 * 1024
CACHE_PATH = os.path.join(tempfile.gettempdir(), "adf_device_name_cache.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# --- Cloud Backup Configuration ---
load_dotenv()
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
TENANT_ID = os.getenv("AZURE_TENANT_ID")
STORAGE_ACCOUNT = os.getenv("STORAGE_ACCOUNT_NAME")
USER_FILE = "user.json"
ADMIN_CONTAINER = "admin-storage"

# Initialize Azure Blob Service Client
credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
account_url = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
blob_service_client = BlobServiceClient(account_url, credential=credential)

# --- Cache helpers ---
def load_cache():
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}
 
def save_cache(cache):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# --- Helper Functions (Most unchanged) ---

def get_marketable_name_from_scrape(model, brand=None):
    """
    Fetch the full marketing name of a device from GSMArena based on its model number.
    Tries to find the best match using fuzzy matching between model number and result names.
    """
    search_url = f"https://www.gsmarena.com/results.php3?sQuickSearch={model}"
    headers = {
        "User-Agent": USER_AGENT
    }

    try:
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # --- 1️⃣ Direct match: redirected page ---
        title_tag = soup.find("title")
        if title_tag and " - Full phone specifications" in title_tag.get_text():
            name = title_tag.get_text().split(" - ")[0].strip()
            return name

        # --- 2️⃣ Search results list ---
        result_tags = soup.select(".makers li a strong")
        results = [tag.get_text().strip() for tag in result_tags if tag.get_text().strip()]

        if not results:
            return None

        # --- 3️⃣ If brand provided, filter results containing brand name first ---
        if brand:
            brand_filtered = [r for r in results if brand.lower() in r.lower()]
            if brand_filtered:
                results = brand_filtered

        # --- 4️⃣ Fuzzy match between model number and results ---
        matches = get_close_matches(model.lower(), [r.lower() for r in results], n=1, cutoff=0.3)
        if matches:
            # Return the correctly capitalized version from results
            for r in results:
                if r.lower() == matches[0]:
                    return r

        # --- 5️⃣ If no fuzzy match, return the first result (most relevant) ---
        return results[0]

    except requests.exceptions.RequestException as e:
        print(f"Scraping failed: {e}")
        return None

def get_file_category(filename):
    mtype, _ = mimetypes.guess_type(filename)
    if not mtype: return "Others"
    main_type, sub_type = mtype.split('/', 1)
    if main_type == 'image': return "Photos"
    if main_type == 'video': return "Videos"
    if main_type == 'audio': return "Audio"
    doc_extensions = ('.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.odt', '.txt', '.rtf')
    if main_type == 'text' or filename.lower().endswith(doc_extensions) or 'document' in sub_type: return "Documents"
    archive_types = ('zip', 'rar', '7z', 'tar', 'g-zip')
    if 'application' in main_type and any(t in sub_type for t in archive_types): return "Archives"
    return "Others"


def run_adb_command(cmd_list, timeout=20):
    try:
        full_command = [ADB_EXECUTABLE] + cmd_list
        proc = subprocess.run(full_command, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='ignore')
        if proc.returncode != 0: return False, proc.stdout, proc.stderr
        return True, proc.stdout, proc.stderr
    except FileNotFoundError: return False, "", f"ADB executable not found at: {ADB_EXECUTABLE}."
    except subprocess.TimeoutExpired: return False, "", f"ADB command timed out: {' '.join(full_command)}"
    except Exception as e: return False, "", f"An unexpected error occurred: {e}"

def adb_get_file_mod_time(remote_path):
    cmd = ['shell', 'stat', '-c', '%Y', shlex.quote(remote_path)]
    success, stdout, _ = run_adb_command(cmd, timeout=10)
    if success and stdout.strip().isdigit(): return int(stdout.strip())
    return None

def get_date_folder_name(timestamp):
    if timestamp is None: return "Unknown_Date"
    try: return datetime.fromtimestamp(timestamp).strftime('%Y_%B')
    except (ValueError, OSError): return "Unknown_Date"

def adb_available():
    return run_adb_command(["version"], timeout=2)[0]

def get_connected_devices():
    success, stdout, _ = run_adb_command(["devices"], timeout=5)
    if not success: return []
    lines = stdout.strip().splitlines()[1:]
    return [line.split()[0] for line in lines if line.strip() and "device" in line]

def get_device_name():
    success, model_output, _ = run_adb_command(["shell", "getprop", "ro.product.model"], timeout=5)
    if not success or not model_output.strip():
        return "Unknown Device"
    
    model = model_output.strip()

    model_to_name_map = {
        "M2004J19C": "Xiaomi Redmi 9",
        "lancelot_in": "Xiaomi Redmi 9 Prime",
        "M2003J6A1G": "Xiaomi Redmi Note 9S",
        "M2004J19I": "Xiaomi Redmi 9 Prime",
        "M2006C3LG": "Xiaomi Redmi 9A",
        "M2007J20CG": "POCO X3 NFC",
        "M2101K7AG": "Xiaomi Redmi Note 10",
        "2201116TG": "Xiaomi 12 Pro",
        "23078PND5G": "Xiaomi Redmi Note 13",
        "SM-A125F": "Samsung Galaxy A12",
        "SM-A136B": "Samsung Galaxy A13 5G",
        "SM-A146P": "Samsung Galaxy A14 5G",
        "SM-A256B": "Samsung Galaxy A25 5G",
        "SM-A346B": "Samsung Galaxy A34 5G",
        "SM-A546B": "Samsung Galaxy A54 5G",
        "SM-F926B": "Samsung Galaxy Z Fold3 5G",
        "SM-F936B": "Samsung Galaxy Z Fold4",
        "SM-S901B": "Samsung Galaxy S22",
        "SM-S928B": "Samsung Galaxy S24 Ultra",
        "HD1913": "OnePlus 7T Pro",
        "CPH2603": "Oppo F25 Pro",
        "CPH2617": "Oppo A59",
        "LE2123": "OnePlus 9 Pro",
        "DN2103": "OnePlus Nord CE 5G",
        "A2645": "iPhone 13 Pro Max",
        "A2882": "iPhone 14",
        "A3090": "iPhone 15 Pro",
        "CPH2247": "OPPO A16",
        "CPH2263": "OPPO Reno5 4G",
        "CPH2239": "OPPO F19",
        "V2046": "Vivo Y20",
        "V2109": "Vivo X70 Pro+",
        "V2123A": "iQOO 8 Pro"
    }

    if model in model_to_name_map:
        return model_to_name_map[model]

    return model

def adb_ls(path):
    cmd = ["shell", "ls", "-l", shlex.quote(path)]
    success, stdout, stderr = run_adb_command(cmd, timeout=20)
    if not success: log_message(f" Failed to connect: {stderr}"); return []
    results = []
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    for line in lines:
        parts = line.split();
        if len(parts) < 6: continue
        filename = " ".join(parts[7:]) if parts[6].count(':') == 1 else " ".join(parts[8:])
        if not filename or filename in ('.', '..'): continue
        perms = parts[0]
        if perms.startswith('d'): results.append({'name': filename + '/', 'size': '-', 'is_dir': True})
        elif perms.startswith('-'):
            size_str = parts[4] if parts[4].isdigit() else '0'
            try:
                size_bytes = int(size_str)
                if size_bytes > 1024**3: size_formatted = f"{size_bytes / 1024**3:.2f} GB"
                elif size_bytes > 1024**2: size_formatted = f"{size_bytes / 1024**2:.2f} MB"
                elif size_bytes > 1024: size_formatted = f"{size_bytes / 1024:.1f} KB"
                else: size_formatted = f"{size_bytes} B"
                results.append({'name': filename, 'size': size_formatted, 'is_dir': False})
            except ValueError: results.append({'name': filename, 'size': 'N/A', 'is_dir': False})
    return results

def adb_get_file_size(remote_path):
    cmd = ["shell", "ls", "-l", shlex.quote(remote_path)]; success, stdout, _ = run_adb_command(cmd, timeout=10)
    if not success: return None
    parts = stdout.strip().split()
    if len(parts) > 4 and parts[4].isdigit(): return int(parts[4])
    return None

def adb_find_files(path):
    cmd = ["shell", "find", shlex.quote(path), "-type", "f", "-print"]; success, stdout, _ = run_adb_command(cmd, timeout=120)
    if not success: return []
    return [ln.strip() for ln in stdout.splitlines() if ln.strip()]

def adb_md5(remote_path):
    success_md5, out_md5, _ = run_adb_command(['shell', 'md5sum', remote_path], timeout=60)
    if success_md5 and out_md5.strip(): return out_md5.strip().split()[0]
    success_sha1, out_sha1, _ = run_adb_command(['shell', 'sha1sum', remote_path], timeout=60)
    if success_sha1 and out_sha1.strip(): return out_sha1.strip().split()[0]
    return None

def local_file_hash(path):
    try:
        h = hashlib.md5();
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""): h.update(chunk)
        return h.hexdigest()
    except Exception: return None

def adb_rm(remote_path):
    cmd = ['shell', 'rm', '-r' if remote_path.endswith('/') else '-f', shlex.quote(remote_path.strip())]
    return run_adb_command(cmd, timeout=60)

def adb_pull(remote_path, local_dir):
    local_filename = os.path.basename(remote_path)
    local_path = os.path.join(local_dir, local_filename)

    # Use a unique temporary path from the start
    safe_prefix = hashlib.sha256(remote_path.encode()).hexdigest()[:8]
    temp_path = os.path.join(local_dir, f"{safe_prefix}_{local_filename}.tmp")

    cmd = ['pull', remote_path, temp_path]
    success, _, stderr = run_adb_command(cmd, timeout=300)

    if success and os.path.exists(temp_path):
        # If the final file exists, remove it before renaming
        if os.path.exists(local_path):
            os.remove(local_path)
        
        # Now, rename the unique temporary file to the final destination
        os.rename(temp_path, local_path)
        return local_path
    else:
        # Cleanup the temporary file on failure
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return None
# --- Cloud Backup & User Management Functions (from workspace.py) ---
def load_users():
    if not os.path.exists(USER_FILE):
        return {}
    with open(USER_FILE, "r") as f:
        return json.load(f)

def save_users(users):
    with open(USER_FILE, "w") as f:
        json.dump(users, f, indent=4)

def sync_admin_backup():
    try:
        admin_blob = blob_service_client.get_blob_client(ADMIN_CONTAINER, "user.json")
        with open(USER_FILE, "rb") as data:
            admin_blob.upload_blob(data, overwrite=True)
    except Exception as e:
        log_message(f"Admin sync failed: {e}", type="error")

def plan_details(plan):
    return {
        "free": {"limit": 1, "duration": 1},
        "basic": {"limit": 10, "duration": 24},
        "pro": {"limit": 100, "duration": 72}
    }.get(plan, {})

def get_container_usage(container_name):
    try:
        container_client = blob_service_client.get_container_client(container_name)
        total_size = sum(blob.size for blob in container_client.list_blobs() if hasattr(blob, 'size'))
        return total_size / (1024**3)
    except Exception:
        return 0

def create_account_impl(user_id, plan):
    users = load_users()
    if user_id in users:
        return False, "User already exists."
    
    details = plan_details(plan)
    if not details:
        return False, "Invalid plan selected."
    
    created = datetime.now(UTC)
    expiry = created + timedelta(hours=details["duration"])
    container = f"user-{user_id}-{int(time.time())}"
    
    try:
        blob_service_client.create_container(container)
    except Exception as e:
        return False, f"Failed to create cloud container: {e}"
        
    users[user_id] = {
        "container": container,
        "plan": plan,
        "limit_gb": details["limit"],
        "created": created.isoformat(),
        "expiry": expiry.isoformat()
    }
    save_users(users)
    sync_admin_backup()
    return True, f"Account created for {user_id} with plan '{plan}'."

# --- Real-time Communication & Core Logic -

# And replace it with this updated version:
def log_message(msg, type="info"):
    """
    Emits a log message to the client with an optional type.
    """
    socketio.emit('log_message', {'data': f'{msg}', 'type': type})
    socketio.sleep(0.01)

def update_progress(current, total):
    socketio.emit('progress_update', {'current': current, 'total': total}); socketio.sleep(0.01)

stop_event = threading.Event()

def build_file_list_for_paths(paths):
    result = []
    for p in paths:
        if stop_event.is_set(): log_message("Operation cancelled."); return None
        if p.endswith("/"):
            log_message(f"Finding files in: {p}"); files = adb_find_files(p)
            log_message(f"  Found {len(files)} files."); result.extend(files)
        else: result.append(p)
    return sorted(set(result))


def compute_hashes_on_phone_impl(file_list):
    mapping = {}; total = len(file_list)
    for i, fp in enumerate(file_list, start=1):
        if stop_event.is_set(): log_message("  duplicate detection cancelled."); return None
        log_message(f" ({i}/{total}): {os.path.basename(fp)}"); update_progress(i, total)
        h = adb_md5(fp)
        if h: mapping[fp] = h; continue
        size = adb_get_file_size(fp)
        if size is not None and size > MAX_HASH_PULL_SIZE: log_message(f"  ! SKIPPING large file"); continue
        local = adb_pull(fp, HASH_FALLBACK_DIR)
        if not local: log_message(f"  ! Failed to pull for detection."); continue
        lh = local_file_hash(local)
        if lh: mapping[fp] = lh
        else: log_message(f"  ! failed.")
        try: os.remove(local)
        except Exception: pass
    return mapping


def group_by_hash(hmap):
    rev = defaultdict(list);
    for fp, h in hmap.items(): rev[h].append(fp)
    duplicates = [{"hash": h, "files": files} for h, files in rev.items() if len(files) > 1]
    uniques = [files[0] for h, files in rev.items() if h]
    return duplicates, uniques


def find_duplicates(file_list):
    log_message("Starting duplicate scan..."); hmap = compute_hashes_on_phone_impl(file_list)
    if hmap is None: update_progress(0, 0); return
    if not stop_event.is_set():
        duplicates, uniques = group_by_hash(hmap)
        log_message(f"Scan complete. Found {len(uniques)} unique files and {len(duplicates)} duplicate groups.")
        socketio.emit('scan_complete', {"all_files": file_list, "uniques": uniques, "duplicates": duplicates})
    update_progress(0, 0)

def get_exif_date(file_path):
    """
    Extracts the original creation date from a photo's EXIF data.
    Returns a datetime object if found, otherwise None.
    """
    try:
        with Image.open(file_path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return None

            # Tag ID 36867 is DateTimeOriginal
            date_str = exif_data.get(36867)
            if date_str:
                # Format is 'YYYY:MM:DD HH:MM:SS'
                return datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
    except Exception:
        # This can happen if the file is not an image or has no EXIF data
        return None
    return None

def transfer_or_move_files(file_list, dest_folder, is_move_op=False):
    total = len(file_list); op_name, op_past = ("Moving", "Moved") if is_move_op else ("Copying", "Copied")
    success_count, failed_count = 0, 0
    update_progress(0, total); log_message(f"Starting {op_name.lower()} of {total} files to {dest_folder}")
    
    for i, remote in enumerate(file_list, start=1):
        if stop_event.is_set(): log_message(f"{op_name} operation cancelled."); break
        
        basename = os.path.basename(remote)
        category = get_file_category(remote)
        date_folder = get_date_folder_name(adb_get_file_mod_time(remote))
        extension_folder = os.path.splitext(remote)[1].strip('.')
        if not extension_folder: extension_folder = "no_extension"
        
        target_dir = os.path.join(dest_folder, "My Album", category, extension_folder, date_folder)
        final_local_path = os.path.join(target_dir, basename)

        # --- NEW LOGIC: Check for existing file ---
        if os.path.exists(final_local_path):
            try:
                # Send an event to the frontend and wait for a response.
                response = socketio.call('confirm_overwrite', {'filename': basename}, timeout=600) # 10 minute timeout
                
                if response == 'skip':
                    log_message(f"  > User chose to skip '{basename}'.")
                    failed_count += 1
                    update_progress(i, total)
                    continue # Immediately move to the next file
                else: # response == 'overwrite'
                    log_message(f"  > User chose to overwrite '{basename}'.")
            except Exception as e:
                log_message(f" The file already exists '{basename}'.", type='error')
                failed_count += 1
                update_progress(i, total)
                continue
        # --- END OF NEW LOGIC ---
        
        os.makedirs(target_dir, exist_ok=True)
        log_message(f"[{i}/{total}] {op_name}: {basename}")
        local_path = adb_pull(remote, target_dir)
        
        if local_path:
            if is_move_op:
                log_message(f"  > Pull successful. Wiping from device...")
                delete_success, _, err_msg = adb_rm(remote)
                if delete_success: success_count += 1
                else: failed_count += 1; log_message(f"  ! WARNING: Failed to delete '{remote}'. Error: {err_msg}", type='error')
            else: success_count += 1
        else: failed_count += 1; log_message(f"  ! Failed to pull {remote}.", type='error')
        
        update_progress(i, total)
        
    log_message(f"{op_name} completed. {op_past}: {success_count}, Failed: {failed_count}")
    socketio.emit('operation_complete', {'success': success_count, 'failed': failed_count, 'operation': op_name.lower()})
    update_progress(0, 0)
    
def cloud_upload_task(file_list, user_id):
    users = load_users()
    user_info = users.get(user_id)
    if not user_info:
        log_message("Cloud upload failed: User not found.", type="error")
        return
    container_name = user_info['container']
    container_client = blob_service_client.get_container_client(container_name)
    
    total = len(file_list)
    success_count, failed_count = 0, 0
    
    for i, file_path in enumerate(file_list, start=1):
        if stop_event.is_set():
            log_message("Cloud upload cancelled.", type="warning")
            break
        
        local_path = adb_pull(file_path, HASH_FALLBACK_DIR)
        if not local_path:
            log_message(f"Failed to pull {os.path.basename(file_path)} for cloud upload.", type="error")
            failed_count += 1
            continue

        try:
            blob_path = os.path.relpath(local_path, HASH_FALLBACK_DIR).replace("\\", "/")
            blob_client = container_client.get_blob_client(blob_path)
            
            log_message(f"[↑] Uploading {os.path.basename(file_path)} ({i}/{total})...")
            with open(local_path, "rb") as data:
                blob_client.upload_blob(data, overwrite=True)
            success_count += 1
            log_message(f"[↑] Uploaded {os.path.basename(file_path)}.", type="info")
        except AzureError as e:
            log_message(f"Cloud upload failed for {os.path.basename(file_path)}: {e}", type="error")
            failed_count += 1
        finally:
            os.remove(local_path)

        update_progress(i, total)
        
    log_message(f"Cloud upload completed. Uploaded: {success_count}, Failed: {failed_count}.")
    socketio.emit('operation_complete', {'success': success_count, 'failed': failed_count, 'operation': 'cloud_backup'})

def cloud_download_task(user_id, dest_folder):
    users = load_users()
    user_info = users.get(user_id)
    if not user_info:
        log_message("Cloud restore failed: User not found.", type="error")
        socketio.emit('operation_complete', {'success': 0, 'failed': 0, 'operation': 'cloud_restore'})
        return

    container_name = user_info['container']
    container_client = blob_service_client.get_container_client(container_name)

    try:
        blobs = list(container_client.list_blobs())
    except Exception as e:
        log_message(f"Failed to list files in cloud container: {e}", type="error")
        socketio.emit('operation_complete', {'success': 0, 'failed': 0, 'operation': 'cloud_restore'})
        return

    total = len(blobs)
    success_count, failed_count = 0, 0

    log_message(f"Starting cloud restore of {total} files to '{dest_folder}'...", type="info")
    update_progress(0, total)

    for i, blob in enumerate(blobs, start=1):
        if stop_event.is_set():
            log_message("Cloud restore cancelled.", type="warning")
            break

        local_path = os.path.join(dest_folder, blob.name)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        log_message(f"[↓] Downloading {blob.name} ({i}/{total})...")
        update_progress(i, total)

        try:
            with open(local_path, "wb") as f:
                download_stream = container_client.download_blob(blob.name)
                download_stream.readinto(f)
            success_count += 1
            log_message(f"[↓] Downloaded {blob.name}.", type="info")
        except AzureError as e:
            log_message(f"Download failed for {blob.name}: {e}", type="error")
            failed_count += 1

    log_message(f"Cloud restore completed. Restored: {success_count}, Failed: {failed_count}.")
    socketio.emit('operation_complete', {'success': success_count, 'failed': failed_count, 'operation': 'cloud_restore'})
    update_progress(0, 0)

@app.route('/api/status')
def get_status():
    if not adb_available():
        return jsonify({'status': 'error', 'message': 'ADB executable not found. Please check installation.'})
    
    devices = get_connected_devices()
    if not devices:
        return jsonify({'status': 'warning', 'message': 'ADB is running, but no device is connected.'})

    # --- THIS IS THE CRITICAL LOGIC ---
    # It checks three things:
    # 1. The command runs successfully.
    # 2. It returns an actual list of files (not empty).
    # 3. It returns ZERO error messages.
    cmd = ["shell", "ls", "-A", shlex.quote(ROOT_PHONE)]
    success, stdout, stderr = run_adb_command(cmd, timeout=10)
    
    is_fully_accessible = success and stdout.strip() and not stderr.strip()
    
    if not is_fully_accessible:
        # If any check fails, we are not in "File transfer" mode.
        message = "Device connected, but files are inaccessible. Please select 'File transfer' on your phone."
        return jsonify({'status': 'warning', 'message': message})
        
    # Only if all checks pass, we declare a successful connection.
    device_model = get_device_name()
    return jsonify({'status': 'success', 'message': f'Connected to: {device_model}'})    
@app.route('/api/list_path', methods=['POST'])
def list_path():
    path = request.get_json().get('path', ROOT_PHONE)
    if not path: return jsonify({'error': 'Path is required'}), 400
    return jsonify(adb_ls(path))

@app.route('/api/preview_file', methods=['POST'])
def preview_file():
    remote_path = request.get_json().get('path')
    if not remote_path:
        return jsonify({'error': 'Remote path is required'}), 400
    
    log_message(f"Pulling for preview: {os.path.basename(remote_path)}")
    local_temp_path = adb_pull(remote_path, tempfile.gettempdir())
    
    if local_temp_path:
        return jsonify({'success': True, 'local_path': local_temp_path})
    else:
        return jsonify({'success': False, 'error': f'Failed to pull {remote_path} for preview.'}), 500

@app.route('/api/create_account', methods=['POST'])
def create_account_api():
    data = request.get_json()
    user_id = data.get('user_id')
    plan = data.get('plan')
    success, message = create_account_impl(user_id, plan)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'success': False, 'message': message}), 400

@app.route('/api/login', methods=['POST'])
def login_api():
    data = request.get_json()
    user_id = data.get('user_id')
    users = load_users()
    user_info = users.get(user_id)
    if user_info and datetime.fromisoformat(user_info['expiry']) > datetime.now(UTC):
        return jsonify({'success': True, 'user': user_id, 'info': user_info})
    else:
        return jsonify({'success': False, 'message': 'Invalid user ID or account expired.'}), 401

@app.route('/api/admin_users')
def get_admin_users():
    users = load_users()
    users_with_usage = []
    for uid, info in users.items():
        user_data = info.copy()
        user_data['user_id'] = uid
        user_data['usage_gb'] = get_container_usage(info['container'])
        users_with_usage.append(user_data)
    return jsonify(users_with_usage)

@app.route('/api/admin_delete_user', methods=['POST'])
def admin_delete_user():
    user_id = request.get_json().get('user_id')
    users = load_users()
    if user_id in users:
        container_name = users[user_id]["container"]
        try:
            blob_service_client.delete_container(container_name)
        except Exception:
            pass
        del users[user_id]
        save_users(users)
        sync_admin_backup()
        return jsonify({'success': True, 'message': f'User {user_id} deleted.'})
    return jsonify({'success': False, 'message': 'User not found.'}), 404

# --- Socket.IO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    print("Client connected"); log_message("connected to mobile successfully.")

@socketio.on('start_operation')
def handle_start_operation(data):
    op_type = data.get('operation')
    stop_event.clear()
    user_id = data.get('user_id')
    dest_folder = data.get('dest_folder')
    paths = data.get('paths')

    if not op_type:
        log_message("Error: Missing 'operation' parameter.", type="error")
        return

    if op_type == 'cloud_restore':
        if not user_id or not dest_folder:
            log_message("Error: Missing user ID or destination folder for cloud restore.", type="error")
            return
        socketio.start_background_task(cloud_download_task, user_id, dest_folder)
    elif op_type in ['copy', 'move', 'find_duplicates', 'cloud_backup']:
        if not paths:
            log_message(f"Error: No files selected for '{op_type}'.", type="error")
            return
        full_file_list = build_file_list_for_paths(paths)
        if full_file_list is None:
            return
        if op_type == 'copy':
            socketio.start_background_task(transfer_or_move_files, full_file_list, dest_folder, is_move_op=False)
        elif op_type == 'move':
            socketio.start_background_task(transfer_or_move_files, full_file_list, dest_folder, is_move_op=True)
        elif op_type == 'find_duplicates':
            socketio.start_background_task(find_duplicates, full_file_list)
        elif op_type == 'cloud_backup':
            if not user_id:
                log_message("Error: Missing user ID for cloud backup.", type="error")
                return
            socketio.start_background_task(cloud_upload_task, full_file_list, user_id)
    else:
        log_message(f"Error: Unknown operation type '{op_type}'.", type="error")

@socketio.on('cancel_operation')
def handle_cancel():
    log_message("Cancellation request received.")
    stop_event.set()
    # Immediately notify the frontend that the cancel was acknowledged.
    socketio.emit('operation_cancelled')

if __name__ == '__main__':
    if os.path.exists(HASH_FALLBACK_DIR):
        try: shutil.rmtree(HASH_FALLBACK_DIR)
        except Exception: pass
    os.makedirs(HASH_FALLBACK_DIR, exist_ok=True)
    
    if not os.path.exists(USER_FILE):
        with open(USER_FILE, "w") as f:
            json.dump({}, f)
    try:
        blob_service_client.get_container_client(ADMIN_CONTAINER).create_container()
    except Exception:
        pass
    
    print("Starting Python backend server for Electron...")
    socketio.run(app, port=5000, allow_unsafe_werkzeug=True)