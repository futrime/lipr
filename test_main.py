import json
import unittest
from unittest.mock import Mock, patch

from main import download_manifest


class DownloadManifestTests(unittest.TestCase):
    def test_skip_migrate_when_manifest_is_already_valid(self) -> None:
        content = json.dumps(
            {
                "tooth": "0.0.1",
                "version": "1.2.3",
                "variants": [{"label": ""}],
            }
        ).encode()

        response = Mock()
        response.content = content
        response.raise_for_status = Mock()

        client = Mock()
        client.get.return_value = response

        with patch("main.subprocess.run") as run_mock:
            manifest = download_manifest("owner/repo", None, client=client)

        self.assertEqual(str(manifest.version), "1.2.3")
        run_mock.assert_not_called()

    def test_migrate_when_manifest_is_not_valid(self) -> None:
        migrated_content = json.dumps(
            {
                "tooth": "0.0.1",
                "version": "2.0.0",
                "variants": [{"label": ""}],
            }
        ).encode()

        response = Mock()
        response.content = b"{}"
        response.raise_for_status = Mock()

        client = Mock()
        client.get.return_value = response

        def run_side_effect(cmd: list[str], check: bool) -> None:
            self.assertTrue(check)
            output_path = cmd[3]
            with open(output_path, "wb") as output_file:
                output_file.write(migrated_content)

        with patch("main.subprocess.run", side_effect=run_side_effect) as run_mock:
            manifest = download_manifest("owner/repo", None, client=client)

        self.assertEqual(str(manifest.version), "2.0.0")
        run_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
