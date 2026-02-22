import unittest

from pydantic import ValidationError

from entities import PackageManifestVariant


class PackageManifestVariantTests(unittest.TestCase):
    def test_label_accepts_underscore(self) -> None:
        variant = PackageManifestVariant(label="linux_x64")
        self.assertEqual(variant.label, "linux_x64")

    def test_label_accepts_underscore_in_two_part_label(self) -> None:
        variant = PackageManifestVariant(label="linux_x64/gnu_2")
        self.assertEqual(variant.label, "linux_x64/gnu_2")

    def test_label_rejects_uppercase(self) -> None:
        with self.assertRaises(ValidationError):
            PackageManifestVariant(label="Linux_x64")


if __name__ == "__main__":
    unittest.main()
