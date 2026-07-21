import os
import json
import yaml
import argparse
import logging
from io import BytesIO
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCOPES = ['https://www.googleapis.com/auth/drive']

def get_drive_service():
    # Expect credentials as string from environment variable
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        # Fallback to local file for testing if env not set
        local_key = "audiobook-uploader-503104-18226d210f54.json"
        if os.path.exists(local_key):
            with open(local_key, "r", encoding="utf-8") as f:
                creds_json = f.read()
        else:
            logging.error("GCP_CREDENTIALS environment variable not set and no local fallback found.")
            exit(1)
    
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    return service

def find_or_create_folder(service, folder_name, parent_id=None):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    
    response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if files:
        return files[0].get('id')
    
    # Create folder
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    if parent_id:
        file_metadata['parents'] = [parent_id]
        
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

def get_remote_file_map(service, parent_id):
    """Recursively build a map of {relative_path: file_metadata} for a folder"""
    file_map = {}
    
    def _traverse(current_folder_id, current_path=""):
        query = f"'{current_folder_id}' in parents and trashed = false"
        page_token = None
        while True:
            response = service.files().list(
                q=query, 
                spaces='drive', 
                fields='nextPageToken, files(id, name, mimeType, size)',
                pageToken=page_token,
                pageSize=1000
            ).execute()
            
            for f in response.get('files', []):
                rel_path = os.path.join(current_path, f.get('name')).replace("\\", "/")
                if f.get('mimeType') == 'application/vnd.google-apps.folder':
                    _traverse(f.get('id'), rel_path)
                else:
                    file_map[rel_path] = f
            
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
    
    _traverse(parent_id)
    return file_map

def download_file(service, file_id, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()

def sync_download(service, root_folder_id, local_workspace):
    logging.info(f"Downloading from Google Drive to {local_workspace}...")
    remote_files = get_remote_file_map(service, root_folder_id)
    
    downloaded = 0
    for rel_path, f in remote_files.items():
        local_path = os.path.join(local_workspace, rel_path)
        if not os.path.exists(local_path):
            logging.info(f"Downloading {rel_path}...")
            download_file(service, f.get('id'), local_path)
            downloaded += 1
        else:
            local_size = os.path.getsize(local_path)
            remote_size = int(f.get('size', 0))
            if local_size != remote_size:
                logging.info(f"Updating local {rel_path} (size mismatch)...")
                download_file(service, f.get('id'), local_path)
                downloaded += 1
    logging.info(f"Download complete. Fetched {downloaded} files.")

def get_mime_type(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.txt': return 'text/plain'
    if ext == '.wav': return 'audio/wav'
    if ext == '.mp4': return 'video/mp4'
    if ext == '.jpg' or ext == '.jpeg': return 'image/jpeg'
    return 'application/octet-stream'

def sync_upload(service, root_folder_id, local_workspace):
    logging.info(f"Uploading from {local_workspace} to Google Drive...")
    remote_files = get_remote_file_map(service, root_folder_id)
    folder_cache = {"": root_folder_id}
    
    def _get_or_create_folder_path(rel_path):
        parts = rel_path.replace("\\", "/").split("/")
        current_path = ""
        for part in parts:
            prev_path = current_path
            current_path = f"{current_path}/{part}" if current_path else part
            if current_path not in folder_cache:
                parent_id = folder_cache[prev_path]
                folder_id = find_or_create_folder(service, part, parent_id)
                folder_cache[current_path] = folder_id
        return folder_cache[current_path]

    uploaded = 0
    for root, dirs, files in os.walk(local_workspace):
        for file in files:
            local_path = os.path.join(root, file)
            rel_path = os.path.relpath(local_path, local_workspace).replace("\\", "/")
            
            # Find parent folder ID on Drive
            rel_dir = os.path.dirname(rel_path)
            parent_id = _get_or_create_folder_path(rel_dir) if rel_dir else root_folder_id
            
            file_metadata = {
                'name': file,
                'parents': [parent_id]
            }
            media = MediaFileUpload(local_path, mimetype=get_mime_type(local_path), resumable=True)
            
            if rel_path in remote_files:
                remote_f = remote_files[rel_path]
                remote_size = int(remote_f.get('size', 0))
                local_size = os.path.getsize(local_path)
                if remote_size == local_size:
                    continue # Skip identical size
                else:
                    logging.info(f"Updating remote {rel_path} (size diff)...")
                    service.files().update(fileId=remote_f['id'], media_body=media).execute()
                    uploaded += 1
            else:
                logging.info(f"Uploading {rel_path}...")
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                uploaded += 1
                remote_files[rel_path] = {'id': 'temp', 'size': os.path.getsize(local_path)}

    logging.info(f"Upload complete. Uploaded {uploaded} files.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="Download state from GDrive")
    parser.add_argument("--upload", action="store_true", help="Upload state to GDrive")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    if not args.download and not args.upload:
        logging.error("Must specify --download or --upload")
        return

    # Read config to get book title and gdrive_folder_id
    book_title = "Unknown_Book"
    gdrive_folder_id = None
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            if cfg and "book_title" in cfg:
                book_title = cfg["book_title"]
            if cfg and "gdrive_folder_id" in cfg and cfg["gdrive_folder_id"]:
                gdrive_folder_id = cfg["gdrive_folder_id"]
    
    local_workspace = os.path.join("Workspace", book_title)
    
    if args.upload and not os.path.exists(local_workspace):
        logging.info(f"Local workspace {local_workspace} not found. Nothing to upload.")
        return

    service = get_drive_service()
    
    # 1. Use user-provided folder ID or fallback to creating at root
    if gdrive_folder_id:
        cache_root_id = gdrive_folder_id
    else:
        cache_root_id = find_or_create_folder(service, "Audiobook_Cache")
    
    # 2. Find or create book folder inside cache
    book_folder_id = find_or_create_folder(service, book_title, parent_id=cache_root_id)

    if args.download:
        sync_download(service, book_folder_id, local_workspace)
    
    if args.upload:
        sync_upload(service, book_folder_id, local_workspace)

if __name__ == "__main__":
    main()
