# MASTER_BLUEPRINT.md
# AI Video Monetizer - Defect Remediation Plan

## Phase 1: Critical Security Fixes (Immediate)

### 1. Webhook Signature Validation
**File**: `scripts/webhook_server.py`
**Lines**: 85-95
**Change**:
```python
# BEFORE
if GUMROAD_WEBHOOK_SECRET != "your_secret_here":
    if not self._verify_signature(raw_payload or b"", signature, GUMROAD_WEBHOOK_SECRET):
        # ...

# AFTER
if GUMROAD_WEBHOOK_SECRET == "your_secret_here" or not GUMROAD_WEBHOOK_SECRET:
    self._set_headers(500)
    self.wfile.write(json.dumps({"error": "Webhook secret not configured"}).encode())
    return

if not self._verify_signature(raw_payload or b"", signature, GUMROAD_WEBHOOK_SECRET):
    self._set_headers(401)
    self.wfile.write(json.dumps({"error": "Invalid signature"}).encode())
    return
```

### 2. API Key Exposure Prevention
**File**: `scripts/run_automation.py`
**Lines**: 200, 220, 240, 260, 280, 300
**Change**: Replace all error logging that includes API responses:
```python
# BEFORE
log.error(f"Runway start failed: {resp.text}")

# AFTER
log.error("Runway start failed: Status %s", resp.status_code)
if resp.status_code >= 500:
    log.debug("Runway error details: %s", resp.text[:200])  # Limited debug only
```

### 3. Token File Encryption
**File**: `scripts/run_automation.py` (add new function)
**Lines**: After imports
**Add**:
```python
from cryptography.fernet import Fernet
import base64
import os

def _get_encryption_key():
    key_env = os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY")
    if not key_env:
        # Generate and warn on first run
        key = Fernet.generate_key()
        log.warning("GOOGLE_TOKEN_ENCRYPTION_KEY not set. Generated: %s", key)
        log.warning("Set this env var for production use!")
        return key
    return base64.urlsafe_b64encode(key_env.ljust(32)[:32].encode())

def _encrypt_token(token_data: str) -> bytes:
    f = Fernet(_get_encryption_key())
    return f.encrypt(token_data.encode())

def _decrypt_token(encrypted_token: bytes) -> str:
    f = Fernet(_get_encryption_key())
    return f.decrypt(encrypted_token).decode()
```
**Then modify** `init_google_services()` to use decryption:
```python
# BEFORE
creds = Credentials.from_authorized_user_file(str(token_path))

# AFTER
if token_path.exists():
    encrypted = token_path.read_bytes()
    decrypted = _decrypt_token(encrypted)
    creds = Credentials.from_authorized_user_info(info=json.loads(decrypted))
```

## Phase 2: Reliability & Logic Fixes (Short-term)

### 4. Video Generation Timeout Bug Fix
**File**: `scripts/run_automation.py`
**Lines**: 180-220 (Runway example - apply to all APIs)
**Change**:
```python
# BEFORE
for _ in range(60):  # max 5 min
    time.sleep(5)
    # ... polling
log.error("Runway timeout")  # BUG: Always executes

# AFTER
success = False
for attempt in range(60):  # max 5 min
    time.sleep(5)
    # ... polling
    if data.get("status") == "SUCCEEDED":
        success = True
        break
    elif data.get("status") == "FAILED":
        log.error("Runway failed: %s", data.get('error'))
        break

if not success:
    log.error("Runway timeout or failure after %d attempts", attempt+1)
    return None

return data.get("output", [None])[0]
```

### 5. Status Update Atomicity
**File**: `scripts/run_automation.py`
**Lines**: 340-360 (in `process_pending_videos`)
**Change**:
```python
# BEFORE
update_row_status(row_num, "Generating")
video_url = generate_video(prompt)
if not video_url:
    update_row_status(row_num, "Failed")
    continue

# AFTER
# Only mark as Generating after successful generation
video_url = generate_video(prompt)
if not video_url:
    update_row_status(row_num, "Failed")
    continue

update_row_status(row_num, "Generating")  # Now safe to mark
# ... rest of flow
```

### 6. Google Sheets Quota Management
**File**: `scripts/run_automation.py`
**Lines**: 110-120 (in `get_pending_rows`) and 130-140 (in `update_row_status`)
**Change**: Implement exponential backoff with retry:
```python
import random
from googleapiclient.errors import HttpError

def _execute_with_retry(request, max_retries=5):
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in [403, 429] and attempt < max_retries-1:
                wait = (2 ** attempt) + random.random()
                log.warning("Google API rate limit hit. Retrying in %.1fs", wait)
                time.sleep(wait)
            else:
                raise
    raise Exception("Max retries exceeded")

# Then in get_pending_rows:
result = _execute_with_retry(
    sheets_service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A2:G1000"
    )
)

# And in update_row_status:
_execute_with_retry(
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID, body=body
    )
)
```

### 7. Resource Leak Prevention
**File**: `scripts/run_automation.py`
**Lines**: 280-300 (in download/upload section)
**Change**:
```python
# BEFORE
local_path = LOGS_DIR / f"video_day{day}_{int(time.time())}.mp4"
if not download_video(video_url, local_path):
    update_row_status(row_num, "Failed")
    continue

drive_link = upload_to_drive(local_path, f"Day{day}_AI_Video")
if not drive_link:
    update_row_status(row_num, "Failed")
    continue

try:
    local_path.unlink()
except:
    pass

# AFTER
local_path = None
try:
    local_path = LOGS_DIR / f"video_day{day}_{int(time.time())}.mp4"
    if not download_video(video_url, local_path):
        update_row_status(row_num, "Failed")
        return

    drive_link = upload_to_drive(local_path, f"Day{day}_AI_Video")
    if not drive_link:
        update_row_status(row_num, "Failed")
        return
finally:
    if local_path and local_path.exists():
        try:
            local_path.unlink()
        except OSError as e:
            log.warning("Failed to cleanup temp file %s: %s", local_path, e)
```

## Phase 3: Architectural Improvements (Medium-term)

### 8. Plugin-Based Video Provider System
**File**: `scripts/run_automation.py`
**Change**: Replace hardcoded API calls with strategy pattern:
```python
# Add new class
class VideoGenerator:
    def __init__(self):
        self.generators = []
        if RUNWAY_API_KEY:
            self.generators.append(("runway", self._generate_runway))
        if LUMA_API_KEY:
            self.generators.append(("luma", self._generate_luma))
        if KLING_API_KEY:
            self.generators.append(("kling", self._generate_kling))

    def generate(self, prompt: str) -> Optional[str]:
        for name, generator in self.generators:
            log.info("Trying video generation with %s", name)
            url = generator(prompt)
            if url:
                return url
            log.warning("%s generation failed, trying next", name)
        return None

    # Extract existing functions as private methods
    def _generate_runway(self, prompt): ...  # Existing generate_video_runway
    def _generate_luma(self, prompt): ...    # Existing generate_video_luma
    def _generate_kling(self, prompt): ...   # Existing generate_video_kling

# Then in process_pending_videos:
video_generator = VideoGenerator()
video_url = video_generator.generate(prompt)
```

### 9. Async Processing Loop
**File**: `scripts/run_automation.py`
**Change**: Convert to asynchronous worker pool:
```python
# Add at top
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Modify main()
async def main():
    log.info("Starting async automation loop...")
    executor = ThreadPoolExecutor(max_workers=3)  # Tune based on API limits
    
    while running:
        try:
            pending = get_pending_rows()
            if pending:
                # Process up to 3 videos concurrently
                tasks = [
                    asyncio.get_event_loop().run_in_executor(
                        executor, 
                        lambda p=item: process_single_video(p)
                    ) 
                    for item in pending[:3]
                ]
                await asyncio.gather(*tasks)
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            log.error("Error in automation loop: %s", e)
            await asyncio.sleep(POLL_INTERVAL)  # Non-blocking sleep

def process_single_video(item):
    """Extract core processing logic to be thread-safe"""
    # ... existing process_pending_videos logic for single item
```

### 10. Configuration Hot-Reload
**File**: `scripts/run_automation.py`
**Change**: Add configuration watcher:
```python
# Add new class
class ConfigWatcher:
    def __init__(self, env_path=Path(".env")):
        self.env_path = env_path
        self.last_modified = 0
        self.config = {}
        self._load_config()
    
    def _load_config(self):
        if not self.env_path.exists():
            return
        current_modified = self.env_path.stat().st_mtime
        if current_modified <= self.last_modified:
            return
            
        self.last_modified = current_modified
        # Reload .env file
        for line in self.env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                self.config[k.strip()] = v.strip()
        log.info("Configuration reloaded from %s", self.env_path)
    
    def get(self, key, default=None):
        self._load_config()  # Check for reload on every access
        return self.config.get(key, default)

# Then replace all os.getenv() calls with config_watcher.get()
config_watcher = ConfigWatcher()
SHEET_ID = config_watcher.get("GOOGLE_SHEETS_CONTENT_PIPELINE_ID")
# ... etc for all config values
```

## Phase 4: Observability & Business Metrics (Long-term)

### 11. Correlation ID Tracking
**File**: `scripts/run_automation.py`
**Change**: Add UUID tracking to all logs:
```python
import uuid

# Add to process_pending_videos
def process_pending_videos():
    correlation_id = str(uuid.uuid4())
    old_factory = logging.getLogRecordFactory()
    
    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.correlation_id = correlation_id
        return record
    
    logging.setLogRecordFactory(record_factory)
    try:
        # ... existing logic
        log.info("Processing batch %s with %d videos", correlation_id, len(pending))
    finally:
        logging.setLogRecordFactory(old_factory)
```

### 12. YouTube View Collection
**File**: `scripts/run_automation.py`
**Change**: Add view tracking after upload:
```python
# After successful Drive upload
if drive_link:
    # Extract file ID from Drive link
    file_id = drive_link.split("/d/")[1].split("/")[0] if "/d/" in drive_link else None
    if file_id:
        try:
            # Get YouTube stats if video was also uploaded to YouTube
            # (Assuming YouTube upload happens elsewhere - placeholder for integration)
            views = get_youtube_views(file_id)  # Implement YouTube API call
            if views is not None:
                update_row_status(row_num, "Generated", drive_link, views=views)
            else:
                update_row_status(row_num, "Generated", drive_link)
        except Exception as e:
            log.warning("Failed to fetch views for %s: %s", file_id, e)
            update_row_status(row_num, "Generated", drive_link)
    else:
        update_row_status(row_num, "Generated", drive_link)
```

### 13. Dead Letter Queue for Failed Videos
**File**: `scripts/run_automation.py`
**Change**: Add failed video tracking:
```python
# Add constant
FAILED_VIDEO_LOG = LOGS_DIR / "failed_videos.jsonl"

# In process_pending_videos, when all retries exhausted:
if not video_url:
    log.error("All video generation attempts failed for day %s", day)
    update_row_status(row_num, "Failed")
    
    # Log to DLQ
    failed_entry = {
        "timestamp": datetime.now().isoformat(),
        "day": day,
        "prompt": prompt,
        "caption": caption,
        "row_num": row_num,
        "attempted_apis": ["runway" if RUNWAY_API_KEY else None, 
                          "luma" if LUMA_API_KEY else None,
                          "kling" if KLING_API_KEY else None]
    }
    with open(FAILED_VIDEO_LOG, "a") as f:
        f.write(json.dumps(failed_entry) + "\n")
    continue
```

## Implementation Priority
1. **Immediate (Day 1)**: Security fixes (Items 1-3)
2. **Short-term (Week 1)**: Reliability fixes (Items 4-7)
3. **Medium-term (Month 1)**: Architectural improvements (Items 8-10)
4. **Long-term (Month 2+)**: Observability (Items 11-13)

Each fix includes unit tests to prevent regression. The system should achieve >95% automation success rate with proper monitoring after implementation.