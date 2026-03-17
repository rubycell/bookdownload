"""
PDF Page Image Exporter

Exports a range of PDF pages as JPEG images using pdftoppm.

Usage:
    python pdf_export_images.py <pdf_file> [--from PAGE] [--to PAGE] [--dpi DPI] [--output DIR]

Examples:
    python pdf_export_images.py "Science 8.pdf" --from 18 --to 59
    python pdf_export_images.py "Math 8.pdf" --from 1 --to 10 --dpi 300 --output math_pages
"""

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_DPI = 150
DEFAULT_OUTPUT_SUFFIX = "_pages"


def build_output_dir(pdf_path: Path, custom_output: str | None) -> Path:
    """
    Derive the output directory from the PDF filename if not explicitly provided.

    e.g. "Science 8.pdf" → "Science_8_pages/"
    """
    if custom_output:
        return Path(custom_output)
    sanitized_stem = pdf_path.stem.replace(" ", "_")
    return pdf_path.parent / f"{sanitized_stem}{DEFAULT_OUTPUT_SUFFIX}"


def export_pages(
    pdf_path: Path,
    first_page: int,
    last_page: int,
    dpi: int,
    output_dir: Path,
) -> list[Path]:
    """
    Export PDF pages as JPEG images using pdftoppm.

    pdftoppm is chosen over PDF libraries like PyMuPDF because it handles
    complex PDFs (fonts, vector graphics, transparency) more reliably and
    is available system-wide without extra Python dependencies.

    Args:
        pdf_path:   Path to the source PDF.
        first_page: First page to export (1-based, inclusive).
        last_page:  Last page to export (1-based, inclusive).
        dpi:        Output image resolution.
        output_dir: Directory where images will be saved.

    Returns:
        Sorted list of exported image file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_prefix = str(output_dir / "page")

    result = subprocess.run(
        [
            "pdftoppm",
            "-r", str(dpi),
            "-jpeg",
            "-f", str(first_page),
            "-l", str(last_page),
            str(pdf_path),
            output_prefix,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"pdftoppm failed (exit {result.returncode}):\n{result.stderr}"
        )

    exported_files = sorted(output_dir.glob("page-*.jpg"))
    return exported_files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a range of PDF pages as JPEG images.",
    )
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--from", dest="first_page", type=int, default=1,
                        help="First page to export (default: 1)")
    parser.add_argument("--to", dest="last_page", type=int, default=None,
                        help="Last page to export (default: last page of PDF)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"Output resolution in DPI (default: {DEFAULT_DPI})")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: <pdf_name>_pages/)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Determine last page: if not specified, use a large number and let pdftoppm clip
    last_page = args.last_page or 9999

    output_dir = build_output_dir(pdf_path, args.output)

    print(f"PDF:    {pdf_path}")
    print(f"Pages:  {args.first_page} → {last_page if args.last_page else 'end'}")
    print(f"DPI:    {args.dpi}")
    print(f"Output: {output_dir}/")

    exported = export_pages(pdf_path, args.first_page, last_page, args.dpi, output_dir)

    print(f"Exported {len(exported)} images to {output_dir}/")


if __name__ == "__main__":
    main()
