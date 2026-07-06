from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.web_server import STATIC_DIR


class WebApprovalUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        cls.app_css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    def test_apply_toolbar_exposes_approval_status(self) -> None:
        self.assertIn('id="applyProposal"', self.index_html)
        self.assertIn('id="approvalStatus"', self.index_html)
        self.assertIn("No apply-ready file edits.", self.index_html)

    def test_proposal_rendering_exposes_per_file_approval_controls(self) -> None:
        self.assertIn("function renderProposals", self.app_js)
        self.assertIn("data-approval-path", self.app_js)
        self.assertIn("Approve this file for apply", self.app_js)
        self.assertIn("apply-ready", self.app_js)
        self.assertIn("No direct file edit was generated for this file.", self.app_js)

    def test_checkbox_changes_update_approved_paths_and_button_state(self) -> None:
        self.assertIn('document.addEventListener("change"', self.app_js)
        self.assertIn("target?.dataset?.approvalPath", self.app_js)
        self.assertIn("state.approvedPaths.add(target.dataset.approvalPath)", self.app_js)
        self.assertIn("state.approvedPaths.delete(target.dataset.approvalPath)", self.app_js)
        self.assertIn("updateApprovalState();", self.app_js)

    def test_apply_request_sends_only_selected_approved_paths(self) -> None:
        self.assertIn("const approvedPaths = approvedFilePaths();", self.app_js)
        self.assertIn("approved_paths: approvedPaths", self.app_js)
        self.assertIn("with ${approvedPaths.length} approved file edit(s)", self.app_js)

    def test_apply_button_is_disabled_without_selection_or_after_apply(self) -> None:
        self.assertIn("function updateApprovalState", self.app_js)
        self.assertIn("state.proposalApplied", self.app_js)
        self.assertIn("total === 0", self.app_js)
        self.assertIn("selected === 0", self.app_js)
        self.assertIn("setApprovalInputsDisabled(true)", self.app_js)
        self.assertIn("setApprovalInputsDisabled(false)", self.app_js)

    def test_default_approval_is_limited_to_visible_apply_ready_files(self) -> None:
        self.assertIn("function editableProposalPaths", self.app_js)
        self.assertIn("visiblePaths", self.app_js)
        self.assertIn(".filter((path) => !visiblePaths.size || visiblePaths.has(path));", self.app_js)
        self.assertIn("state.approvedPaths = new Set(editableProposalPaths(report.patch_proposal));", self.app_js)

    def test_approval_controls_have_dedicated_styles(self) -> None:
        self.assertIn(".approval-row", self.app_css)
        self.assertIn(".approval-row input", self.app_css)


if __name__ == "__main__":
    unittest.main()
