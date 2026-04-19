from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Download, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright


FID_MONTH_NAMES = {
    1: "Jan",
    2: "Feb",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


@dataclass
class AttachmentInfo:
    cdp_url: str
    context_index: int
    page_index: int
    page_title: str
    page_url: str


class FidelityBrowser:
    """Attach to a visible Chromium browser and automate Fidelity pages.

    This is designed for a noVNC workflow where the human logs in manually,
    then the automation continues inside the same visible browser session.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.cdp_url: str | None = None

    # ---------- lifecycle ----------
    def attach(self, cdp_url: str = "http://127.0.0.1:9222", context_index: int = 0, page_index: int = 0) -> AttachmentInfo:
        self.close()
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        self.cdp_url = cdp_url

        if not self.browser.contexts:
            raise RuntimeError("Connected to Chromium, but there are no browser contexts yet.")

        if context_index < 0 or context_index >= len(self.browser.contexts):
            raise IndexError(f"context_index {context_index} is out of range; found {len(self.browser.contexts)} context(s).")

        self.context = self.browser.contexts[context_index]

        if not self.context.pages:
            self.page = self.context.new_page()
        else:
            if page_index < 0 or page_index >= len(self.context.pages):
                raise IndexError(f"page_index {page_index} is out of range; found {len(self.context.pages)} page(s).")
            self.page = self.context.pages[page_index]

        return AttachmentInfo(
            cdp_url=cdp_url,
            context_index=context_index,
            page_index=page_index,
            page_title=self.safe_title(self.page),
            page_url=self.page.url,
        )

    def close(self) -> None:
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp_url = None

    # ---------- helpers ----------
    def require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("No browser page is attached. Run attach_visible_browser first.")
        return self.page

    def safe_title(self, page: Page) -> str:
        try:
            return page.title()
        except Exception:
            return ""

    def list_pages(self) -> list[dict[str, Any]]:
        if self.browser is None:
            raise RuntimeError("No browser is attached yet.")
        out: list[dict[str, Any]] = []
        for c_idx, ctx in enumerate(self.browser.contexts):
            for p_idx, page in enumerate(ctx.pages):
                out.append({
                    "context_index": c_idx,
                    "page_index": p_idx,
                    "title": self.safe_title(page),
                    "url": page.url,
                    "active": ctx == self.context and page == self.page,
                })
        return out

    def use_page(self, context_index: int = 0, page_index: int = 0) -> dict[str, Any]:
        if self.browser is None:
            raise RuntimeError("No browser is attached yet.")
        if context_index < 0 or context_index >= len(self.browser.contexts):
            raise IndexError(f"context_index {context_index} is out of range.")
        context = self.browser.contexts[context_index]
        if page_index < 0 or page_index >= len(context.pages):
            raise IndexError(f"page_index {page_index} is out of range.")
        self.context = context
        self.page = context.pages[page_index]
        return {
            "context_index": context_index,
            "page_index": page_index,
            "title": self.safe_title(self.page),
            "url": self.page.url,
        }

    def wait_for_loading_signs(self, timeout_ms: int = 30000) -> None:
        page = self.require_page()
        selectors = [
            "div:nth-child(2) > .loading-spinner-mask-after",
            ".pvd-spinner__mask-inner",
            "pvd-loading-spinner",
            ".pvd3-spinner-root > .pvd-spinner__spinner > .pvd-spinner__visual > div > .pvd-spinner__mask-inner",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="hidden", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                # Some pages never render some of these spinner types.
                pass
            except Exception:
                pass

    def wait_for_manual_login(self, timeout_seconds: int = 600, poll_seconds: float = 2.0) -> dict[str, Any]:
        page = self.require_page()
        deadline = time.time() + timeout_seconds
        last_url = page.url
        while time.time() < deadline:
            last_url = page.url
            text_checks = [
                "Accounts & Trade",
                "Documents",
                "Portfolio",
                "Statement PDF",
            ]
            try:
                if "digital.fidelity.com" in page.url or "fidelity.com" in page.url:
                    for text in text_checks:
                        try:
                            if page.get_by_text(text).first.is_visible(timeout=750):
                                return {"logged_in": True, "url": page.url, "matched_text": text}
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(poll_seconds)
        return {"logged_in": False, "url": last_url}

    # ---------- simple navigation ----------
    def goto(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        page = self.require_page()
        page.goto(url, wait_until=wait_until)
        return {"title": self.safe_title(page), "url": page.url}

    def current_page(self) -> dict[str, Any]:
        page = self.require_page()
        return {"title": self.safe_title(page), "url": page.url}

    def open_documents_hub(self) -> dict[str, Any]:
        page = self.require_page()
        page.goto("https://digital.fidelity.com/ftgw/digital/portfolio/documents/dochub", wait_until="domcontentloaded")
        self.wait_for_loading_signs(timeout_ms=15000)
        return {"title": self.safe_title(page), "url": page.url}

    def open_positions_page(self) -> dict[str, Any]:
        page = self.require_page()
        page.goto("https://digital.fidelity.com/ftgw/digital/portfolio/positions", wait_until="domcontentloaded")
        self.wait_for_loading_signs(timeout_ms=15000)
        return {"title": self.safe_title(page), "url": page.url}

    # ---------- downloads ----------
    def _sanitize_name(self, value: str) -> str:
        value = value.strip().replace("/", "-")
        return re.sub(r"[^A-Za-z0-9._ -]+", "_", value)

    def _save_download(self, download: Download, out_dir: str, prefix: str | None = None) -> str:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        filename = download.suggested_filename
        if prefix:
            filename = f"{prefix} - {filename}"
        path = Path(out_dir) / self._sanitize_name(filename)
        download.save_as(str(path))
        return str(path.resolve())

    def download_positions_csv(self, out_dir: str = "./Statements") -> dict[str, Any]:
        page = self.require_page()
        self.open_positions_page()
        page.wait_for_timeout(1000)
        self.wait_for_loading_signs(timeout_ms=150000)

        download = None
        new_ui = True
        try:
            page.get_by_role("button", name="Available Actions").click(timeout=8000)
            with page.expect_download(timeout=20000) as download_info:
                page.get_by_role("menuitem", name="Download").click(timeout=8000)
            download = download_info.value
        except PlaywrightTimeoutError:
            new_ui = False

        if not new_ui:
            with page.expect_download(timeout=20000) as download_info:
                page.get_by_label("Download Positions").click(timeout=8000)
            download = download_info.value

        if download is None:
            raise RuntimeError("Fidelity did not produce a positions CSV download.")

        saved_path = self._save_download(download, out_dir, prefix="positions")
        return {"saved_path": saved_path, "url": page.url}

    def download_statements(self, date_yyyy_mm: str, out_dir: str = "./Statements", account_contains: str | None = None) -> dict[str, Any]:
        """Download Fidelity statements for the requested month.

        Adapted from fidelity-api's existing statement downloader, but runs
        against the currently attached visible Chromium page instead of a
        separate hidden Firefox browser.
        """
        page = self.require_page()

        if not re.fullmatch(r"\d{4}/\d{2}", date_yyyy_mm):
            raise ValueError("date_yyyy_mm must look like YYYY/MM, for example 2026/03")

        target_year = int(date_yyyy_mm[:4])
        target_month = int(date_yyyy_mm[-2:])
        month_name = FID_MONTH_NAMES[target_month]

        def beneficiary_popup_close() -> bool:
            try:
                page.get_by_role("button", name="Close dialog").click(timeout=1500)
            except Exception:
                pass
            return True

        try:
            page.add_locator_handler(
                page.locator(".pvd3-cim-modal-root > .pvd-modal__overlay"),
                beneficiary_popup_close,
            )
        except Exception:
            pass

        self.open_documents_hub()

        page.get_by_role("button", name="Changing").click(timeout=5000)
        page.get_by_role("menuitem", name=f"{target_year}").click(timeout=5000)

        try:
            page.locator("statements-loading-skeleton div").nth(1).wait_for(state="hidden", timeout=20000)
        except Exception:
            pass

        if page.get_by_text("There are no statements").is_visible():
            return {"saved_files": [], "matched_rows": 0, "reason": "There are no statements for that year."}

        try:
            if page.get_by_role("button", name="Load more results").is_visible():
                while page.get_by_role("button", name="Load more results").is_visible():
                    try:
                        page.get_by_role("button", name="Load more results").click(timeout=5000)
                        page.wait_for_timeout(500)
                    except PlaywrightTimeoutError:
                        break
        except Exception:
            pass

        page.wait_for_timeout(1000)
        rows = page.get_by_role("row").all()
        valid_rows: list[Any] = []

        for row in rows:
            try:
                text = row.inner_text()
            except Exception:
                continue
            if str(target_year) not in text:
                continue
            if account_contains and account_contains.lower() not in text.lower():
                continue
            if month_name in text:
                valid_rows.append(row)
                continue

            found_months: list[str] = []
            for candidate in FID_MONTH_NAMES.values():
                if candidate in text:
                    found_months.append(candidate)
                if len(found_months) >= 2:
                    break
            if len(found_months) != 2:
                continue
            start_month = next((m for m, name in FID_MONTH_NAMES.items() if name == found_months[0]), None)
            end_month = next((m for m, name in FID_MONTH_NAMES.items() if name == found_months[1]), None)
            if start_month is None or end_month is None:
                continue
            if start_month <= target_month <= end_month:
                valid_rows.append(row)

        saved_files: list[str] = []
        for idx, row in enumerate(valid_rows, start=1):
            popup_page = None
            with page.expect_download(timeout=30000) as download_info:
                with page.expect_popup(timeout=30000) as popup_info:
                    row.filter(has=page.get_by_role("link")).click(timeout=5000)
                popup_page = popup_info.value
            download = download_info.value
            saved_files.append(self._save_download(download, out_dir, prefix=str(idx)))
            if popup_page is not None:
                try:
                    popup_page.close()
                except Exception:
                    pass

        return {
            "saved_files": saved_files,
            "matched_rows": len(valid_rows),
            "requested_month": date_yyyy_mm,
            "account_contains": account_contains,
        }

    def dump_dom_snapshot(self, max_chars: int = 25000) -> dict[str, Any]:
        page = self.require_page()
        html = page.content()
        return {
            "title": self.safe_title(page),
            "url": page.url,
            "html_excerpt": html[:max_chars],
            "truncated": len(html) > max_chars,
        }
