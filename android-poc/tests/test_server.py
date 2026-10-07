from __future__ import annotations

import asyncio
import builtins
import os
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

PYTHON_SOURCE = Path(__file__).resolve().parents[1] / "app" / "src" / "main" / "python"
sys.path.insert(0, str(PYTHON_SOURCE))

import server


class FastApiRuntimeTests(unittest.TestCase):
    def test_server_configures_private_paths_before_importing_existing_app(self):
        with TemporaryDirectory() as root:
            private_data = Path(root) / "files"
            private_cache = Path(root) / "cache"
            web_root = Path(root) / "web"
            (web_root / "templates").mkdir(parents=True)
            (web_root / "static").mkdir()
            app_module = types.ModuleType("app")
            app_module.app = object()
            uvicorn_module = types.ModuleType("uvicorn")
            uvicorn_module.run = Mock()
            original_import = builtins.__import__
            import_environment = {}
            old_cwd = Path.cwd()
            old_tempdir = tempfile.tempdir

            def observe_import(name, *args, **kwargs):
                if name == "app":
                    import_environment.update(os.environ)
                    import_environment["PYTHON_TEMP_DIR"] = tempfile.gettempdir()
                    return app_module
                if name == "uvicorn":
                    return uvicorn_module
                return original_import(name, *args, **kwargs)

            try:
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch("builtins.__import__", side_effect=observe_import),
                ):
                    tempfile.tempdir = None
                    server.start_server(
                        str(private_data),
                        str(private_cache),
                        str(web_root),
                    )
            finally:
                os.chdir(old_cwd)
                tempfile.tempdir = old_tempdir

            expected_upload_dir = private_cache / "uploads"
            expected_temp_dir = private_cache / "tmp"
            self.assertEqual(import_environment["DATABASE_URL"], f"sqlite:///{private_data / 'mygoodoldphotos.db'}")
            self.assertEqual(import_environment["UPLOAD_TEMP_DIR"], str(expected_upload_dir))
            self.assertEqual(import_environment["TMPDIR"], str(expected_temp_dir))
            self.assertEqual(Path(import_environment["TEMP"]), expected_temp_dir)
            self.assertEqual(import_environment["HOME"], str(private_data))
            self.assertEqual(Path(import_environment["PYTHON_TEMP_DIR"]), expected_temp_dir)
            uvicorn_module.run.assert_called_once_with(
                app_module.app,
                host="127.0.0.1",
                port=8765,
                log_level="info",
                access_log=False,
            )

    def test_android_project_embeds_python_and_requests_internet_permission(self):
        project_dir = Path(__file__).resolve().parents[1]
        app_dir = project_dir / "app"
        gradle_config = (project_dir / "app" / "build.gradle.kts").read_text()
        android_requirements = (app_dir / "requirements-android.txt").read_text()
        manifest = ET.parse(project_dir / "app" / "src" / "main" / "AndroidManifest.xml").getroot()
        android_namespace = "{http://schemas.android.com/apk/res/android}"

        self.assertIn('id("com.chaquo.python")', gradle_config)
        self.assertIn('version = "3.13"', gradle_config)
        self.assertIn('install("-r", "requirements-android.txt")', gradle_config)
        self.assertIn("fastapi==0.115.12", android_requirements)
        self.assertIn("uvicorn==0.34.0", android_requirements)
        self.assertIn("pydantic==1.10.24", android_requirements)
        self.assertIn("cryptography==42.0.8", android_requirements)
        self.assertNotIn("psycopg", android_requirements)
        self.assertIn('"arm64-v8a"', gradle_config)
        self.assertTrue(
            any(
                permission.get(android_namespace + "name") == "android.permission.INTERNET"
                for permission in manifest.findall("uses-permission")
            )
        )


if __name__ == "__main__":
    unittest.main()
