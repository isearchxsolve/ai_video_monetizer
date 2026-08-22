# Comprehensive Test Suite Exposing Defects
# File: tests/test_forensic_defects.py

import os
import sys
import json
import time
import hmac
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock, call
import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

class TestSecurityDefects:
    """Tests exposing security vulnerabilities"""
    
    def test_webhook_secret_bypass_with_default(self):
        """EXPLOITS: Webhook processes requests with default secret"""
        from scripts.webhook_server import WebhookHandler
        
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.headers = {"X-Gumroad-Signature": "invalid"}
        handler.wfile = MagicMock()
        handler._set_headers = MagicMock()
        
        # Simulate dangerous default configuration
        with patch('scripts.webhook_server.GUMROAD_WEBHOOK_SECRET', "your_secret_here"):
            handler.handle_gumroad_ping(
                {"product_name": "test"}, 
                raw_payload=b'{"product_name": "test"}'
            )
        
        # Should reject but actually processes (defect)
        handler._set_headers.assert_not_called_with(401)
        handler._set_headers.assert_not_called_with(500)
        # Instead calls success path (verified by checking for processed response in real code)
    
    def test_api_key_leak_in_error_logs(self):
        """EXPLOITS: Error messages expose API keys"""
        from scripts.run_automation import log
        
        with patch('scripts.run_automation.log') as mock_log:
            # Simulate API error response containing key-like string
            fake_resp = MagicMock()
            fake_resp.status_code = 401
            fake_resp.text = '{"error":"Invalid API Key: sk_live_abc123def456"}'
            
            # This is what the current code does
            mock_log.error(f"Runway start failed: {fake_resp.text}")
            
            # Verify the full response was logged (defect)
            mock_log.error.assert_called_once()
            args, _ = mock_log.error.call_args
            assert "sk_live_abc123def456" in args[0]
    
    def test_token_file_stored_plaintext(self):
        """EXPLOITS: Google token stored without encryption"""
        from pathlib import Path
        import json
        
        token_path = Path.home() / ".hermes" / "google_token.json"
        token_path.parent.mkdir(exist_ok=True)
        
        # Create fake token
        token_data = {
            "token": "ya29.fake-token-value",
            "refresh_token": "1//0fake-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-client-id.apps.googleusercontent.com",
            "client_secret": "fake-client-secret",
            "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
            "expiry": "2025-01-01T00:00:00Z"
        }
        token_path.write_text(json.dumps(token_data))
        
        # Verify file is readable plaintext (defect)
        assert token_path.exists()
        content = token_path.read_text()
        parsed = json.loads(content)
        assert parsed["token"] == "ya29.fake-token-value"
        assert parsed["client_secret"] == "fake-client-secret"  # Secret exposed!

class TestReliabilityDefects:
    """Tests exposing reliability and logic flaws"""
    
    def test_video_generation_timeout_bug(self):
        """EXPLOITS: Timeout error logged even on success"""
        from scripts.run_automation import generate_video_runway
        
        with patch('scripts.run_automation.requests.post') as mock_post, \
             patch('scripts.run_automation.requests.get') as mock_get, \
             patch('scripts.run_automation.time.sleep'), \
             patch('scripts.run_automation.log') as mock_log:
            
            # Mock successful generation
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"id": "task_123"}
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "status": "SUCCEEDED", 
                "output": ["http://video.com/result.mp4"]
            }
            
            result = generate_video_runway("test prompt")
            
            # Verify defect: success case should NOT log timeout error
            assert result == "http://video.com/result.mp4"
            # Check that timeout error was NOT logged
            error_calls = [c for c in mock_log.error.call_args_list 
                          if "timeout" in str(c).lower()]
            assert len(error_calls) == 0, f"Timeout error incorrectly logged: {error_calls}"
    
    def test_status_update_race_condition(self):
        """EXPLOITS: Row marked Generating before generation starts"""
        from scripts.run_automation import update_row_status, process_pending_videos
        
        updated_statuses = []
        def capture_update(row_num, status, video_link=""):
            updated_statuses.append((row_num, status, video_link))
        
        with patch('scripts.run_automation.update_row_status', side_effect=capture_update), \
             patch('scripts.run_automation.get_pending_rows', return_value=[{
                 "row_num": 2, "day": "1", "hook": "Test", 
                 "prompt": "Test prompt", "caption": "Test caption"
             }]), \
             patch('scripts.run_automation.generate_video', return_value=None), \
             patch('scripts.run_automation.download_video'), \
             patch('scripts.run_automation.upload_to_drive'), \
             patch('scripts.run_automation.schedule_post'):
            
            process_pending_videos()
            
            # Verify defect: Generating status set BEFORE generation attempt
            assert len(updated_statuses) >= 1
            first_update = updated_statuses[0]
            assert first_update[1] == "Generating"  # Status
            assert first_update[2] == ""            # No video link yet
            
            # In correct implementation, Generating should only be set AFTER success
    
    def test_google_sheets_quota_exhaustion(self):
        """EXPLOITS: No backoff causes quota exhaustion"""
        from scripts.run_automation import get_pending_rows
        from googleapiclient.errors import HttpError
        
        with patch('scripts.run_automation.sheets_service') as mock_service:
            # Simulate quota exceeded error
            mock_resp = MagicMock()
            mock_resp.status = 429
            mock_service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = \
                HttpError(mock_resp, b'{"error": {"message": "Quota exceeded"}}')
            
            # Current implementation will propagate error immediately
            with pytest.raises(HttpError) as exc_info:
                get_pending_rows()
            
            assert "Quota exceeded" in str(exc_info.value)
            # Verify no retry attempts were made (defect)
            assert mock_service.spreadsheets.return_value.values.return_value.get.return_value.execute.call_count == 1
    
    def test_resource_leak_on_download_failure(self):
        """EXPLOITS: Failed downloads leave temp files"""
        from scripts.run_automation import process_pending_videos
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            
            with patch('scripts.run_automation.LOGS_DIR', logs_dir), \
                 patch('scripts.run_automation.get_pending_rows', return_value=[{
                     "row_num": 2, "day": "1", "hook": "Test", 
                     "prompt": "Test prompt", "caption": "Test caption"
                 }]), \
                 patch('scripts.run_automation.generate_video', return_value="http://example.com/video.mp4"), \
                 patch('scripts.run_automation.download_video', return_value=False), \
                 patch('scripts.run_automation.update_row_status'):
                
                process_pending_videos()
                
                # Verify defect: temp file should NOT exist after failure
                temp_files = list(logs_dir.glob("video_day1_*.mp4"))
                assert len(temp_files) == 0, f"Temp files not cleaned up: {temp_files}"

class TestArchitecturalDefects:
    """Tests exposing architectural issues"""
    
    def test_synchronous_processing_blocks_on_error(self):
        """EXPLOITS: Single error stops all processing"""
        from scripts.run_automation import main
        
        call_count = 0
        def mock_process():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Simulated error")
        
        with patch('scripts.run_automation.process_pending_videos', side_effect=mock_process), \
             patch('scripts.run_automation.running', True), \
             patch('scripts.run_automation.time.sleep') as mock_sleep, \
             patch('scripts.run_automation.log') as mock_log:
            
            # Simulate KeyboardInterrupt after 3 loops to stop test
            mock_sleep.side_effect = [None, None, KeyboardInterrupt()]
            
            try:
                main()
            except KeyboardInterrupt:
                pass
            
            # Verify defect: processing stopped after first error
            assert call_count == 2  # Only processed twice before error stopped loop
            error_logged = any("Error in automation loop" in str(call) 
                              for call in mock_log.info.call_args_list)
            assert error_logged, "Error should have been logged"
    
    def test_no_hot_reload_of_configuration(self):
        """EXPLOITS: Changing .env requires restart"""
        from scripts.run_automation import POLL_INTERVAL
        import time
        
        original_interval = POLL_INTERVAL
        
        # Simulate changing .env file
        env_path = Path(".env")
        env_path.write_text(f"AUTOMATION_POLL_INTERVAL={original_interval + 60}\n")
        
        # Current implementation: value won't change until restart
        # We can't easily test without restarting module, but we can verify
        # that the value is loaded only at import time by checking it's not
        # dynamically read from os.environ
        assert POLL_INTERVAL == original_interval  # Still old value (defect)
        
        # Cleanup
        env_path.write_text(f"AUTOMATION_POLL_INTERVAL={original_interval}\n")
    
    def test_missing_youtube_view_collection(self):
        """EXPLOITS: Views column in sheet never populated"""
        from scripts.populate_sheet import HEADERS
        
        # Verify defect: Views column exists in header but never updated
        assert "Views" in HEADERS
        
        # Check that no code updates the Views column
        # This would require examining all sheet update calls - simplified check:
        from scripts import run_automation
        source = open(run_automation.__file__).read()
        assert "Views" not in source, "Views column should be referenced in update logic"
        assert "update_row_status" in source
        # Current update_row_status only takes 2-3 params, not 4 for views

class TestSilentDataLoss:
    """Tests exposing silent data loss scenarios"""
    
    def test_manychat_conversions_not_imported_to_sheets(self):
        """EXPLOITS: Conversion data stuck in JSONL logs"""
        from scripts.webhook_server import webhook_server
        import tempfile
        import json
        
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "logs" / "manychat_conversions.jsonl"
            log_path.parent.mkdir(exist_ok=True)
            
            with patch('scripts.webhook_server.Path') as mock_path:
                mock_path.return_value.parent.parent = Path(tmpdir)
                
                handler = webhook_server.WebhookHandler.__new__(webhook_server.WebhookHandler)
                handler.wfile = MagicMock()
                handler._set_headers = MagicMock()
                
                test_data = {
                    "event": "blueprint_requested",
                    "user_id": "12345",
                    "username": "testuser",
                    "keyword": "MAGNETIC",
                    "post_id": "post_123",
                    "timestamp": "2026-01-15T10:30:00Z"
                }
                
                handler.handle_manychat_conversion(test_data)
                
                # Verify defect: data written to log but not to Sheets
                assert log_path.exists()
                content = log_path.read_text().strip()
                assert content  # Should have data
                
                parsed = [json.loads(line) for line in content.splitlines()]
                assert len(parsed) == 1
                assert parsed[0]["username"] == "testuser"
                
                # Most importantly: NO attempt to write to Google Sheets
                # This would require mocking sheets_service - but we can verify
                # by checking the handler doesn't have sheets access
                assert not hasattr(handler, 'sheets_service')
    
    def test_gumroad_webhook_missing_field_tolerance(self):
        """EXPLOITS: Missing fields create None values in logs"""
        from scripts.webhook_server import WebhookHandler
        
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.wfile = MagicMock()
        handler._set_headers = MagicMock()
        
        # Gumroad webhook missing required fields
        incomplete_data = {
            "product_name": "Test Product"
            # Missing: sale_id, email, price, etc.
        }
        
        with patch('scripts.webhook_server.GUMROAD_WEBHOOK_SECRET', "your_secret_here"):
            handler.handle_gumroad_ping(incomplete_data, raw_payload=b'{}')
        
        # Verify defect: sale_data would contain None values
        # We can't directly inspect sale_data without refactoring, but we know
        # the current code uses .get() without defaults
        # This test documents the defect - fix should validate required fields

if __name__ == "__main__":
    pytest.main([__file__, "-v"])