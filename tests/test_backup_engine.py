import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

from backup_engine import (build_asset_name, choose_chunk_items, create_chunk_archive,
                           sanitize_album_name, sanitize_filename)
from download_manager import safe_extract_tar
from app import dashboard, google_login

class BackupEngineTests(unittest.TestCase):
    def test_names_are_safe(self):
        self.assertEqual(sanitize_filename('../../bad?.jpg'), 'bad_.jpg')
        self.assertEqual(sanitize_album_name('  My   album '), 'My album')
        self.assertIn('Part 001', build_asset_name('A', 1, 'x.jpg', 'y.jpg'))

    def test_uncompressed_tar_manifest_and_safe_restore(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root / 'photo.jpg'; source.write_bytes(b'unchanged bytes')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            info = create_chunk_archive([{'media_id':'local:1:x','filename':'photo.jpg','mime_type':'image/jpeg','size':15,'sha256':digest,'local_path':str(source)}], 'Archive', 1, root)
            with tarfile.open(info['path']) as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
                self.assertEqual(manifest['items'][0]['sha256'], digest)
                self.assertEqual(archive.extractfile('media/000001_photo.jpg').read(), b'unchanged bytes')
            extracted = root / 'restore'; safe_extract_tar(info['path'], extracted)
            self.assertEqual((extracted/'media/000001_photo.jpg').read_bytes(), b'unchanged bytes')

    def test_chunking_preserves_order(self):
        items = [{'filename':str(i), 'size':1, 'local_path':'x'} for i in range(3)]
        self.assertEqual([x['filename'] for x in choose_chunk_items(items)], ['0','1','2'])

    def test_dashboard_allows_anonymous_visitor_to_reach_sign_in_page(self):
        request = SimpleNamespace(session={})
        response = dashboard(request)
        self.assertEqual(response.template.name, 'landing.html')

    def test_oauth_start_normalizes_loopback_host_to_callback_host(self):
        request = SimpleNamespace(url=urlsplit('http://127.0.0.1:8000/auth/google'), session={})
        with patch('app.os.getenv', return_value='http://localhost:8000/auth/google/callback'):
            response = google_login(request)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers['location'], 'http://localhost:8000/auth/google')

if __name__ == '__main__': unittest.main()
