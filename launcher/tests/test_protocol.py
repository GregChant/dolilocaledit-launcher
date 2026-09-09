import unittest
from urllib.parse import urlencode

from dolilocaledit_launcher.errors import ProtocolError
from dolilocaledit_launcher.protocol import (
    inspect_check_uri,
    inspect_launch_uri,
    normalize_origin,
    parse_check_uri,
    protocol_operation,
    parse_launch_uri,
    validate_endpoint,
)


class ProtocolTest(unittest.TestCase):
    def test_dispatches_browser_normalized_check_uri(self) -> None:
        self.assertEqual(protocol_operation("dolilocaledit://check?redacted"), "check")
        self.assertEqual(protocol_operation("dolilocaledit://check/?redacted"), "check")
        self.assertEqual(protocol_operation("dolilocaledit://open/?redacted"), "open")

    def launch_uri(self, endpoint: str, **extra: str) -> str:
        query = {"endpoint": endpoint, "entity": "2", "ticket": "T" * 43, **extra}
        return "dolilocaledit://open?" + urlencode(query)

    def test_accepts_exact_trusted_https_endpoint(self) -> None:
        target = parse_launch_uri(
            self.launch_uri("https://erp.example/custom/dolilocaledit/public/api.php"),
            ("https://erp.example",),
        )
        self.assertEqual(target.origin, "https://erp.example")
        self.assertEqual(target.entity, 2)
        self.assertEqual(target.ticket, "T" * 43)

    def test_inspection_validates_without_silently_trusting(self) -> None:
        uri = self.launch_uri("https://new.example/custom/dolilocaledit/public/api.php")
        self.assertEqual(inspect_launch_uri(uri).origin, "https://new.example")
        with self.assertRaises(ProtocolError) as context:
            parse_launch_uri(uri, ())
        self.assertEqual(context.exception.code, "untrusted_origin")

    def test_launcher_check_uses_a_distinct_strict_operation(self) -> None:
        uri = self.launch_uri("https://erp.example/custom/dolilocaledit/public/api.php").replace(
            "dolilocaledit://open?", "dolilocaledit://check?"
        )
        inspected = inspect_check_uri(uri)
        self.assertEqual(inspected.origin, "https://erp.example")
        checked = parse_check_uri(uri, ("https://erp.example",))
        self.assertEqual(checked.ticket, "T" * 43)
        with self.assertRaises(ProtocolError):
            inspect_launch_uri(uri)
        with self.assertRaises(ProtocolError):
            inspect_check_uri(uri.replace("check", "unknown", 1))

    def test_accepts_http_only_on_loopback_and_when_trusted(self) -> None:
        target = parse_launch_uri(
            self.launch_uri("http://127.0.0.1:8080/custom/dolilocaledit/public/api.php"),
            ("http://127.0.0.1:8080",),
        )
        self.assertEqual(target.origin, "http://127.0.0.1:8080")
        with self.assertRaises(ProtocolError) as context:
            normalize_origin("http://erp.example")
        self.assertEqual(context.exception.code, "insecure_origin")

    def test_rejects_untrusted_or_credentialed_endpoint(self) -> None:
        with self.assertRaises(ProtocolError) as context:
            parse_launch_uri(self.launch_uri("https://evil.example/dolilocaledit/public/api.php"), ())
        self.assertEqual(context.exception.code, "untrusted_origin")
        with self.assertRaises(ProtocolError) as context:
            parse_launch_uri(
                self.launch_uri("https://user:pass@erp.example/dolilocaledit/public/api.php"),
                ("https://erp.example",),
            )
        self.assertEqual(context.exception.code, "invalid_endpoint")

    def test_rejects_duplicates_unknown_fields_and_durable_shapes(self) -> None:
        base = self.launch_uri("https://erp.example/dolilocaledit/public/api.php")
        for uri in (
            base + "&ticket=" + "U" * 43,
            base + "&command=calc",
            base.replace("T" * 43, "short"),
            base.replace("entity=2", "entity=0"),
            base.replace("api.php", "api.php%3Faction%3Dcontent"),
        ):
            with self.subTest(uri=uri[:80]):
                with self.assertRaises(ProtocolError):
                    parse_launch_uri(uri, ("https://erp.example",))

    def test_canonicalizes_unicode_dns_names_before_approval(self) -> None:
        self.assertEqual(normalize_origin("https://bücher.example"), "https://xn--bcher-kva.example")
        endpoint, origin = validate_endpoint(
            "https://bücher.example/custom/dolilocaledit/public/api.php"
        )
        self.assertEqual(origin, "https://xn--bcher-kva.example")
        self.assertEqual(endpoint, "https://xn--bcher-kva.example/custom/dolilocaledit/public/api.php")
        for hostile in ("evil\u202e.example", "%65xample.com", "[::1%25eth0]"):
            with self.subTest(hostile=hostile):
                with self.assertRaises(ProtocolError):
                    validate_endpoint(f"https://{hostile}/custom/dolilocaledit/public/api.php")

    def test_rejects_ambiguous_endpoint_paths(self) -> None:
        for path in (
            "/custom/%2e%2e/dolilocaledit/public/api.php",
            "/custom/../dolilocaledit/public/api.php",
            "/custom//dolilocaledit/public/api.php",
            "/custom\\proxy/dolilocaledit/public/api.php",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ProtocolError):
                    validate_endpoint("https://erp.example" + path)


if __name__ == "__main__":
    unittest.main()
