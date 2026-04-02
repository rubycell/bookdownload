"""
Cambridge GO E-Reader PDF Downloader

Opens a browser, logs into Cambridge GO, then waits for the user to navigate
to the e-reader. Once the e-reader is open, automatically downloads all page
images and assembles them into a PDF.

Why semi-automatic? Cambridge GO's Nuxt.js SPA requires real browser interaction
to set up e-reader cookies (EREADER_TOKEN, EREADER_SIGNATURE). The resource page
content doesn't reliably render when navigated programmatically. So the user
clicks one button, then the script takes over for the tedious 192-page download.

Usage:
    python cambridge_downloader.py [--output filename.pdf]

Example:
    python cambridge_downloader.py --output "Science 5.pdf"

Credentials are read from .env file (CAMBRIDGE_EMAIL, CAMBRIDGE_PASSWORD).
"""

import argparse
import base64
import os
import sys
import tempfile
import time
from pathlib import Path

import img2pdf
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Frame, BrowserContext

SCRIPT_DIR = Path(__file__).parent
CAMBRIDGE_LOGIN_URL = "https://www.cambridge.org/go/login"
CAMBRIDGE_RESOURCES_URL = "https://www.cambridge.org/go/resources"

LOGIN_TIMEOUT = 20
PAGE_DOWNLOAD_PAUSE = 0.3


def load_credentials() -> tuple[str, str]:
    """Load Cambridge GO credentials from .env file."""
    load_dotenv()
    email = os.getenv("CAMBRIDGE_EMAIL", "").strip()
    password = os.getenv("CAMBRIDGE_PASSWORD", "").strip()
    if not email or not password:
        print("Error: CAMBRIDGE_EMAIL and CAMBRIDGE_PASSWORD must be set in .env", file=sys.stderr)
        sys.exit(1)
    return email, password


def create_browser_context(playwright, profile_dir: str) -> BrowserContext:
    """Launch a persistent headed Chromium context."""
    return playwright.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def dismiss_cookie_banner(page: Page) -> None:
    """Dismiss the OneTrust cookie consent banner if present."""
    try:
        accept_button = page.locator("#onetrust-accept-btn-handler")
        if accept_button.is_visible(timeout=3000):
            accept_button.click()
            time.sleep(1)
            print("  Dismissed cookie consent banner.")
    except Exception:
        pass


def login_to_cambridge(page: Page, email: str, password: str) -> None:
    """Log in to Cambridge GO using keyboard-based input."""
    print("Navigating to login page...")
    page.goto(CAMBRIDGE_LOGIN_URL, timeout=60000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(5)

    # Check if already logged in
    if "/login" not in page.url:
        print("Already logged in (cached session).")
        return

    dismiss_cookie_banner(page)

    # Wait for login form
    for _ in range(30):
        time.sleep(1)
        if "/login" not in page.url:
            print("Already logged in (cached session).")
            return
        if page.get_by_text("Email address").is_visible():
            break
    else:
        raise RuntimeError("Login form did not appear.")

    time.sleep(1)
    print("Filling email...")
    page.get_by_text("Email address").click()
    page.keyboard.type(email, delay=50)
    page.get_by_role("button", name="Next").click()
    time.sleep(3)

    print("Filling password...")
    page.locator("[placeholder*='assword']").first.click(timeout=10000)
    time.sleep(0.5)
    page.keyboard.type(password, delay=50)
    page.get_by_role("button", name="Log in").click()
    time.sleep(5)

    # Wait for login to complete (may take a few extra seconds)
    if "/login" in page.url:
        try:
            page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        except Exception:
            raise RuntimeError("Login failed. Check credentials in .env file.")

    print("Login successful.")


def wait_for_ereader(page: Page) -> Page:
    """
    Navigate to the resources page, then wait for the user to click a book
    to open the e-reader. Returns the e-reader page (may be a new tab).

    The script polls all open tabs for the e-reader URL pattern, so it detects
    the e-reader regardless of whether it opens in the current or a new tab.
    """
    print("Navigating to resources...")
    page.goto(CAMBRIDGE_RESOURCES_URL, timeout=60000)
    time.sleep(3)

    print()
    print("=" * 60)
    print("  Please click on the book you want to download.")
    print("  The e-reader will open — the script will detect it")
    print("  and start downloading automatically.")
    print("=" * 60)
    print()
    print("Waiting for e-reader to open...", flush=True)

    # Poll all tabs for the e-reader URL pattern
    context = page.context
    for _ in range(300):  # Wait up to 10 minutes
        time.sleep(2)
        for tab_page in context.pages:
            if "/ereader/read/" in tab_page.url:
                print(f"E-reader detected: {tab_page.url}")
                # Wait for S3 content frames to load
                print("Waiting for e-reader content to load...")
                for _ in range(30):
                    time.sleep(2)
                    has_s3_frame = any(
                        "elevate-s3.cambridge.org" in f.url and "/OEBPS/" in f.url
                        for f in tab_page.frames
                    )
                    if has_s3_frame:
                        time.sleep(3)
                        return tab_page

                raise RuntimeError("E-reader opened but content frames did not load.")

    raise RuntimeError("Timed out waiting for e-reader (10 minutes).")


def find_s3_frame(ereader_page: Page) -> Frame:
    """Find an S3 content iframe with the Cloudflare signed cookie."""
    for frame in ereader_page.frames:
        if "elevate-s3.cambridge.org" in frame.url and "/OEBPS/" in frame.url:
            return frame
    raise RuntimeError("Could not find S3 content frame in e-reader.")


def discover_book_base_url(s3_frame: Frame) -> str:
    """Extract the book's base URL from the S3 frame's URL."""
    frame_url = s3_frame.url
    oebps_index = frame_url.index("/OEBPS/")
    return frame_url[:oebps_index]


def detect_total_pages(s3_frame: Frame, base_url: str) -> int:
    """Binary search for the last real page image (image/jpeg vs text/html)."""
    print("Detecting total page count...")

    def is_real_page(page_number: int) -> bool:
        padded = str(page_number).zfill(4)
        image_url = f"{base_url}/OEBPS/images/page{padded}.jpg"
        return s3_frame.evaluate(
            """async (url) => {
                const resp = await fetch(url, {credentials: 'include'});
                const blob = await resp.blob();
                return blob.type.startsWith('image/');
            }""",
            image_url,
        )

    low, high = 1, 200
    while is_real_page(high):
        low = high
        high *= 2

    while low < high - 1:
        mid = (low + high) // 2
        if is_real_page(mid):
            low = mid
        else:
            high = mid

    print(f"Found {low} pages.")
    return low


def download_page_from_browser(s3_frame: Frame, image_url: str) -> bytes:
    """Fetch a page image from the S3 iframe context as base64, decode to bytes."""
    base64_data = s3_frame.evaluate(
        """async (url) => {
            const resp = await fetch(url, {credentials: 'include'});
            const blob = await resp.blob();
            return new Promise(resolve => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                reader.readAsDataURL(blob);
            });
        }""",
        image_url,
    )
    return base64.b64decode(base64_data)


def download_all_pages(
    s3_frame: Frame,
    base_url: str,
    total_pages: int,
    output_dir: Path,
) -> list[Path]:
    """Download all page images via in-browser fetch, saving each to disk."""
    saved_paths = []
    for page_number in range(1, total_pages + 1):
        padded = str(page_number).zfill(4)
        image_url = f"{base_url}/OEBPS/images/page{padded}.jpg"

        print(f"  Downloading page {page_number}/{total_pages}...", end="\r", flush=True)
        image_bytes = download_page_from_browser(s3_frame, image_url)

        page_path = output_dir / f"page_{padded}.jpg"
        page_path.write_bytes(image_bytes)
        saved_paths.append(page_path)

        time.sleep(PAGE_DOWNLOAD_PAUSE)

    print(f"\n  Downloaded {len(saved_paths)} pages.")
    return saved_paths


def assemble_pdf(page_paths: list[Path], output_path: Path) -> None:
    """Assemble JPEG files into a PDF using img2pdf (lossless passthrough)."""
    if not page_paths:
        raise ValueError("No page images to assemble.")

    with output_path.open("wb") as pdf_file:
        pdf_file.write(img2pdf.convert([str(path) for path in page_paths]))

    size_mb = output_path.stat().st_size // 1024 // 1024
    print(f"PDF saved to: {output_path}  ({size_mb} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a Cambridge GO e-reader book as PDF.",
    )
    parser.add_argument("--output", "-o", default=None, help="Output PDF filename")
    parser.add_argument(
        "--profile",
        default=str(SCRIPT_DIR / ".browser_profile"),
        help="Browser profile directory for session persistence",
    )
    args = parser.parse_args()

    email, password = load_credentials()
    profile_dir = args.profile

    with sync_playwright() as playwright:
        browser = create_browser_context(playwright, profile_dir)
        page = browser.pages[0] if browser.pages else browser.new_page()

        try:
            login_to_cambridge(page, email, password)
            ereader_page = wait_for_ereader(page)

            s3_frame = find_s3_frame(ereader_page)
            base_url = discover_book_base_url(s3_frame)
            print(f"Book base URL: {base_url}")

            total_pages = detect_total_pages(s3_frame, base_url)

            # Derive output filename from the ISBN in the URL if not specified
            if args.output:
                output_path = Path(args.output)
            else:
                isbn = base_url.split("/extracted_books/")[1].split("-")[0]
                output_path = Path(f"cambridge_{isbn}.pdf")

            with tempfile.TemporaryDirectory(prefix="cambridge_pages_") as temp_dir:
                page_paths = download_all_pages(
                    s3_frame, base_url, total_pages, Path(temp_dir)
                )
                print("Assembling PDF...")
                assemble_pdf(page_paths, output_path)

        finally:
            browser.close()

    print("Done.")


if __name__ == "__main__":
    main()
