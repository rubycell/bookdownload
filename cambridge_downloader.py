"""
Cambridge GO E-Reader PDF Downloader

Logs into Cambridge GO, navigates to a book, intercepts the page images
loaded by the reader, saves them to disk, and assembles a PDF.

Credentials are read from a .env file (never hardcoded):
    CAMBRIDGE_EMAIL=your@email.com
    CAMBRIDGE_PASSWORD=yourpassword

Usage:
    python cambridge_downloader.py <book_url> [--output filename.pdf] [--dpi 150]

Example:
    python cambridge_downloader.py "https://www.cambridge.org/go/ereader/read/9781108972611/?groupId=0&bookid=2242&root=anon#book/2242"
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import img2pdf
from dotenv import load_dotenv
from playwright.sync_api import Page, Response, sync_playwright

load_dotenv()

LOGIN_URL = "https://www.cambridge.org/go/login"
CAMBRIDGE_GO_DOMAIN = "cambridge.org"

# Image responses larger than this are considered page content (not icons/UI)
MIN_PAGE_IMAGE_BYTES = 20_000

# How long to wait for a page to fully render before capturing (ms)
PAGE_RENDER_WAIT_MS = 2000

# Selector for the "next page" button in the Cambridge GO reader
NEXT_PAGE_SELECTORS = [
    "[aria-label='Next page']",
    "[aria-label='next page']",
    ".next-page",
    "button.next",
    "[data-testid='next-page']",
]


def load_credentials() -> tuple[str, str]:
    """
    Load email and password from environment variables (set via .env file).
    Fails fast with a clear message if credentials are missing.
    """
    email = os.getenv("CAMBRIDGE_EMAIL")
    password = os.getenv("CAMBRIDGE_PASSWORD")
    if not email or not password:
        print(
            "Error: credentials not found.\n"
            "Create a .env file with:\n"
            "  CAMBRIDGE_EMAIL=your@email.com\n"
            "  CAMBRIDGE_PASSWORD=yourpassword",
            file=sys.stderr,
        )
        sys.exit(1)
    return email, password


def login(page: Page, email: str, password: str) -> None:
    """
    Perform the two-step Cambridge GO login (email → password).
    Cambridge GO uses a two-step form: first submits email, then password appears.
    """
    print("Logging in...")
    page.goto(LOGIN_URL)
    page.wait_for_load_state("networkidle")

    # Step 1: enter email and click Next
    page.fill("input[type='email'], input[name='email'], #email, [placeholder*='Email']", email)
    page.click("button:has-text('Next'), input[type='submit']")
    page.wait_for_load_state("networkidle")

    # Step 2: enter password and submit
    page.fill("input[type='password']", password)
    page.click("button[type='submit'], button:has-text('Log in'), button:has-text('Sign in')")
    page.wait_for_load_state("networkidle")

    if "login" in page.url.lower():
        raise RuntimeError(
            "Login failed — still on login page. Check credentials in .env file."
        )
    print("Login successful.")


def is_page_image_response(response: Response) -> bool:
    """
    Decide if a network response is a book page image (not a UI icon or asset).

    Criteria:
    - Content-Type is image/jpeg or image/png or image/webp
    - Response body is larger than MIN_PAGE_IMAGE_BYTES (filters out thumbnails/icons)
    - URL does not look like a UI asset (logo, icon, avatar)
    """
    content_type = response.headers.get("content-type", "")
    if not any(mime in content_type for mime in ("image/jpeg", "image/png", "image/webp")):
        return False

    url_lower = response.url.lower()
    if any(skip in url_lower for skip in ("logo", "icon", "avatar", "thumbnail", "sprite")):
        return False

    try:
        body = response.body()
        return len(body) >= MIN_PAGE_IMAGE_BYTES
    except Exception:
        return False


def navigate_to_book(page: Page, book_url: str) -> None:
    """Navigate to the book URL and wait for the reader to fully load."""
    print(f"Opening book: {book_url}")
    page.goto(book_url)
    page.wait_for_load_state("networkidle")
    time.sleep(2)  # Extra wait for JS reader to initialize


def find_next_page_button(page: Page) -> object | None:
    """Try each known selector to locate the 'next page' button."""
    for selector in NEXT_PAGE_SELECTORS:
        button = page.query_selector(selector)
        if button and button.is_visible():
            return button
    return None


def get_total_pages(page: Page) -> int | None:
    """
    Try to read the total page count from the reader UI.
    Returns None if it cannot be determined.
    """
    try:
        # Common patterns: "1 / 237", "Page 1 of 237"
        text = page.inner_text("body")
        match = re.search(r'(?:of|/)\s*(\d+)', text)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


def capture_all_pages(page: Page, output_dir: Path) -> list[Path]:
    """
    Navigate through every page of the book, intercepting image responses.

    Strategy:
    1. Attach a response listener that saves images as they load
    2. Click "next page" repeatedly until the button is disabled or missing
    3. Return the ordered list of saved image paths

    The interceptor approach is preferred over screenshots because:
    - Images are captured at their native resolution
    - No UI chrome (toolbars, overlays) is included
    - The result is lossless — the exact bytes the server sent
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    captured_images: list[tuple[int, Path]] = []  # (order_index, path)
    capture_counter = [0]  # mutable for closure

    def on_response(response: Response) -> None:
        if not is_page_image_response(response):
            return
        try:
            body = response.body()
            index = capture_counter[0]
            capture_counter[0] += 1
            ext = "jpg" if "jpeg" in response.headers.get("content-type", "") else "png"
            image_path = output_dir / f"{index:04d}.{ext}"
            image_path.write_bytes(body)
            captured_images.append((index, image_path))
        except Exception as error:
            print(f"  Warning: failed to save image: {error}")

    page.on("response", on_response)

    total_pages = get_total_pages(page)
    page_number = 1

    print(f"Total pages: {total_pages or 'unknown'}")
    print("Capturing pages...")

    while True:
        print(f"  Page {page_number}{f'/{total_pages}' if total_pages else ''}...", end="\r", flush=True)
        page.wait_for_load_state("networkidle")
        time.sleep(PAGE_RENDER_WAIT_MS / 1000)

        next_button = find_next_page_button(page)
        if next_button is None or not next_button.is_enabled():
            break

        next_button.click()
        page_number += 1

        if total_pages and page_number > total_pages:
            break

    print(f"\nCaptured {len(captured_images)} page images.")
    return [path for _, path in sorted(captured_images)]


def build_output_path(book_url: str, custom_output: str | None) -> Path:
    """Derive output PDF filename from the book URL if not explicitly provided."""
    if custom_output:
        return Path(custom_output)
    parsed = urlparse(book_url)
    # Use the ISBN or last path segment as filename
    segments = [s for s in parsed.path.split("/") if s]
    name = segments[-1] if segments else "cambridge_book"
    return Path(f"cambridge_{name}.pdf")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a Cambridge GO e-reader book as a PDF.",
    )
    parser.add_argument("url", help="Cambridge GO book URL")
    parser.add_argument("--output", "-o", default=None, help="Output PDF filename")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default: True)")
    parser.add_argument("--visible", action="store_true",
                        help="Show the browser window while downloading")
    args = parser.parse_args()

    email, password = load_credentials()
    output_path = build_output_path(args.url, args.output)
    headless = not args.visible

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        try:
            login(page, email, password)
            navigate_to_book(page, args.url)

            pages_dir = output_path.with_suffix("") / "pages"
            image_paths = capture_all_pages(page, pages_dir)

            if not image_paths:
                print("No page images captured. Try running with --visible to debug.", file=sys.stderr)
                sys.exit(1)

            print("Assembling PDF...")
            with output_path.open("wb") as pdf_file:
                pdf_file.write(img2pdf.convert([str(p) for p in image_paths]))

            size_mb = output_path.stat().st_size // 1024 // 1024
            print(f"PDF saved to: {output_path}  ({size_mb} MB)")
            print("Done.")

        finally:
            browser.close()


if __name__ == "__main__":
    main()
