"""
AnyFlip PDF Downloader

Downloads all pages from an AnyFlip flipbook and assembles them into a PDF.
Pages are stored as MD5-hashed JPEGs referenced in the book's config.js file.

Usage:
    python anyflip_downloader.py <url> [--output filename.pdf]

Example:
    python anyflip_downloader.py https://online.anyflip.com/wnplk/kbxl/mobile/index.html
"""

import argparse
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import img2pdf
import requests


ANYFLIP_CONFIG_URL_TEMPLATE = "https://online.anyflip.com/{book_group}/{book_id}/mobile/javascript/config.js"
ANYFLIP_LARGE_IMAGE_URL_TEMPLATE = "https://online.anyflip.com/{book_group}/{book_id}/files/large/{filename}"

REQUEST_HEADERS_TEMPLATE = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    # CloudFront requires a matching Referer or it returns 403
    "Referer": "https://online.anyflip.com/{book_group}/{book_id}/mobile/index.html",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

DELAY_BETWEEN_REQUESTS_SECONDS = 0.2


def parse_book_ids_from_url(url: str) -> tuple[str, str]:
    """
    Extract the book group and book ID from an AnyFlip URL.

    AnyFlip URLs follow the pattern:
        https://online.anyflip.com/{book_group}/{book_id}/...

    Returns:
        (book_group, book_id) tuple, e.g. ("wnplk", "kbxl")
    """
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        raise ValueError(f"Cannot parse book IDs from URL: {url}")
    return path_parts[0], path_parts[1]


def build_request_headers(book_group: str, book_id: str) -> dict[str, str]:
    """Build headers with the book-specific Referer required by CloudFront."""
    headers = dict(REQUEST_HEADERS_TEMPLATE)
    headers["Referer"] = REQUEST_HEADERS_TEMPLATE["Referer"].format(
        book_group=book_group,
        book_id=book_id,
    )
    return headers


def fetch_page_filenames(book_group: str, book_id: str, session: requests.Session) -> list[str]:
    """
    Fetch config.js and extract the ordered list of large-image filenames.

    AnyFlip stores page metadata in a JS object called fliphtml5_pages:
        {"n":["<md5>.jpg"], "t":"../files/thumb/<md5>.jpg"}, ...

    The "n" field contains the large image filename for each page.

    Returns:
        List of filenames like ["421c1b02f632736830c2285c7048afa4.jpg", ...]
    """
    config_url = ANYFLIP_CONFIG_URL_TEMPLATE.format(
        book_group=book_group,
        book_id=book_id,
    )
    print(f"Fetching config from: {config_url}")
    response = session.get(config_url, headers=build_request_headers(book_group, book_id), timeout=15)
    response.raise_for_status()

    # Extract all "n":["filename.jpg"] entries — one per page, in order
    filenames = re.findall(r'"n":\["([^"]+)"', response.text)
    if not filenames:
        raise RuntimeError(
            "Could not parse page filenames from config.js. "
            "The book may be private or use an unsupported format.\n"
            f"Config preview: {response.text[:300]}"
        )

    print(f"Found {len(filenames)} pages in config.")
    return filenames


def download_page_image(
    book_group: str,
    book_id: str,
    filename: str,
    headers: dict[str, str],
    session: requests.Session,
) -> bytes | None:
    """
    Download a single large page image by its MD5-hashed filename.
    Returns raw JPEG bytes or None if the page is not found (404).
    """
    url = ANYFLIP_LARGE_IMAGE_URL_TEMPLATE.format(
        book_group=book_group,
        book_id=book_id,
        filename=filename,
    )
    response = session.get(url, headers=headers, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content


def download_all_pages_to_dir(
    book_group: str,
    book_id: str,
    page_filenames: list[str],
    session: requests.Session,
    pages_dir: Path,
) -> list[Path]:
    """
    Download all page images to disk, one file per page.

    Saving to disk (rather than accumulating bytes in memory) keeps RAM usage
    flat — each page is written and released before the next is fetched.

    Returns an ordered list of saved file paths, skipping any 404s.
    """
    headers = build_request_headers(book_group, book_id)
    total = len(page_filenames)
    saved_paths = []

    for index, filename in enumerate(page_filenames, start=1):
        print(f"  Downloading page {index}/{total}...", end="\r", flush=True)
        image_bytes = download_page_image(book_group, book_id, filename, headers, session)
        if image_bytes is None:
            print(f"\n  Warning: page {index} ({filename}) returned 404, skipping.")
            continue
        page_path = pages_dir / f"{index:04d}.jpg"
        page_path.write_bytes(image_bytes)
        saved_paths.append(page_path)
        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

    print(f"\n  Downloaded {len(saved_paths)} of {total} pages.")
    return saved_paths


def assemble_pdf(page_paths: list[Path], output_path: Path) -> None:
    """
    Assemble saved JPEG files into a single PDF using img2pdf.

    img2pdf wraps the raw JPEG bytes directly into a PDF container without
    decoding them — so memory usage stays constant regardless of page count.
    No pixel data is ever loaded into RAM during assembly.
    """
    if not page_paths:
        raise ValueError("No page images to assemble into PDF.")

    with output_path.open("wb") as pdf_file:
        pdf_file.write(img2pdf.convert([str(path) for path in page_paths]))

    print(f"PDF saved to: {output_path}  ({output_path.stat().st_size // 1024 // 1024} MB)")


def build_default_output_filename(book_group: str, book_id: str) -> str:
    return f"anyflip_{book_group}_{book_id}.pdf"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download an AnyFlip flipbook as a PDF.",
    )
    parser.add_argument(
        "url",
        help="AnyFlip book URL (e.g. https://online.anyflip.com/wnplk/kbxl/mobile/index.html)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output PDF filename (default: anyflip_<group>_<id>.pdf)",
    )
    args = parser.parse_args()

    book_group, book_id = parse_book_ids_from_url(args.url)
    print(f"Book: group={book_group}, id={book_id}")

    output_path = Path(args.output or build_default_output_filename(book_group, book_id))

    with tempfile.TemporaryDirectory(prefix="anyflip_pages_") as pages_tmp_dir:
        pages_dir = Path(pages_tmp_dir)
        with requests.Session() as session:
            page_filenames = fetch_page_filenames(book_group, book_id, session)
            page_paths = download_all_pages_to_dir(
                book_group, book_id, page_filenames, session, pages_dir
            )

        print("Assembling PDF...")
        assemble_pdf(page_paths, output_path)

    print("Done.")


if __name__ == "__main__":
    main()
