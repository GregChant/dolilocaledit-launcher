from pathlib import Path
import unittest
from unittest.mock import patch

from dolilocaledit_launcher import approval
from dolilocaledit_launcher.approval import (
    confirm_external_template_change,
    installation_error_message,
    installation_success_message,
    launcher_error_message,
)


class ApprovalTest(unittest.TestCase):
    def test_installation_result_messages_are_explicit(self) -> None:
        success = installation_success_message(Path(r"C:\Program Files\DoliLocalEdit\launcher.exe"), "1.0.2")
        self.assertIn("Installation réussie", success)
        self.assertIn("Version : 1.0.2", success)
        self.assertIn("dolilocaledit:// : enregistré", success)
        failure = installation_error_message("installation_failed", "Copie impossible\nréessayez")
        self.assertIn("Échec de l’installation", failure)
        self.assertIn("Code de diagnostic : installation_failed", failure)
        self.assertNotIn("impossible\nréessayez", failure)

    def test_changed_template_prompt_shows_sanitized_exact_target(self) -> None:
        messages: list[str] = []
        with patch.object(approval.sys, "platform", "win32"), patch.object(
            approval,
            "_windows_message",
            side_effect=lambda message, _flags: messages.append(message) or 6,
        ):
            accepted = confirm_external_template_change("file:///C:/Templates/local\n.dotx", "local")
        self.assertTrue(accepted)
        self.assertEqual(len(messages), 1)
        self.assertIn("file:///C:/Templates/local.dotx", messages[0])
        self.assertNotIn("local\n.dotx", messages[0])

    def test_error_message_identifies_document_recovery_and_next_action(self) -> None:
        message = launcher_error_message(
            "document_conflict",
            "Le document ou sa location a changé.",
            document_name="contrat.docx",
            server_origin="https://erp.example",
            recovery_directory=r"C:\Users\test\AppData\Local\DoliLocalEdit\recovery\1234",
            local_changes=True,
            server_cancelled=False,
        )

        self.assertIn("Document : contrat.docx", message)
        self.assertIn("Serveur Dolibarr : https://erp.example", message)
        self.assertIn("changements enregistrés sur disque", message)
        self.assertIn("Copie de reprise : conservée", message)
        self.assertIn(r"C:\Users\test\AppData\Local\DoliLocalEdit\recovery\1234", message)
        self.assertIn("comparez les deux versions", message)
        self.assertIn("libération non confirmée", message)
        self.assertIn("Code de diagnostic : document_conflict", message)

    def test_error_before_exchange_explains_that_document_is_unknown(self) -> None:
        message = launcher_error_message("invalid_credential", "Session refusée.")

        self.assertIn("Document : non déterminé", message)
        self.assertIn("relancez une nouvelle session", message)
        self.assertNotIn("Copie de reprise", message)

    def test_untrusted_values_are_sanitized_and_code_is_replaced(self) -> None:
        message = launcher_error_message(
            "BAD CODE",
            "Erreur\navec contrôle",
            document_name="rapport\r\n.docx",
        )

        self.assertNotIn("\navec", message)
        self.assertIn("Document : rapport.docx", message)
        self.assertIn("Code de diagnostic : launcher_error", message)


if __name__ == "__main__":
    unittest.main()
