#!/usr/bin/env python3
"""
download_bird_images.py — Bird Image Downloader for pi_birdie

Downloads one representative image per bird species from:
  1. eBird species media page (primary — Cornell Lab hosted images)
  2. Wikimedia Commons (fallback — searched by scientific name)

Images are saved as: bird_images/{Scientific_Name}.jpg
A manifest file is written to: bird_images/manifest.json

Usage:
    # Download all ~6000 species (takes 1-2 hours)
    python scripts/download_bird_images.py --api-key YOUR_EBIRD_KEY

    # Download species expected in a specific region first
    python scripts/download_bird_images.py --api-key YOUR_EBIRD_KEY --region US-TX

    # Quick test — first 50 species only
    python scripts/download_bird_images.py --api-key YOUR_EBIRD_KEY --limit 50

    # Specify output directory
    python scripts/download_bird_images.py --api-key YOUR_EBIRD_KEY --output ./bird_images

Requirements:
    pip install requests tqdm Pillow
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from PIL import Image
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
EBIRD_API_BASE      = "https://api.ebird.org/v2"
EBIRD_MEDIA_BASE    = "https://cdn.download.ams.birds.cornell.edu/api/v1/asset"
EBIRD_SPECIES_ASSET = "https://ebird.org/species/{species_code}"

WIKIMEDIA_API       = "https://en.wikipedia.org/w/api.php"
COMMONS_API         = "https://commons.wikimedia.org/w/api.php"

IMAGE_SIZE          = (300, 300)     # Target size for saved images
REQUEST_DELAY_S     = 1.0            # Seconds between requests (rate limiting)
REQUEST_TIMEOUT     = 15             # Seconds per request timeout
MAX_RETRIES         = 2


# ── Argument Parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download bird species images for pi_birdie",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--api-key", required=True,
        help="Your eBird API key (from https://ebird.org/api/keygen)"
    )
    parser.add_argument(
        "--output", default="./bird_images",
        help="Directory to save images"
    )
    parser.add_argument(
        "--region", default=None,
        help="eBird region code (e.g. US-TX). Species from this region are prioritised."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of species to process (useful for testing)"
    )
    parser.add_argument(
        "--image-size", type=int, default=300,
        help="Target image size in pixels (square)"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay between requests in seconds (rate limiting)"
    )
    parser.add_argument(
        "--skip-existing", action="store_true", default=True,
        help="Skip species that already have a downloaded image"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


# ── eBird API Helpers ─────────────────────────────────────────────────────────

def fetch_taxonomy(api_key: str, session: requests.Session) -> list[dict]:
    """Fetch full eBird taxonomy (all ~6000 species)."""
    logger.info("Fetching eBird taxonomy…")
    resp = session.get(
        f"{EBIRD_API_BASE}/ref/taxonomy/ebird",
        params={"fmt": "json", "cat": "species"},
        timeout=30,
    )
    resp.raise_for_status()
    taxonomy = resp.json()
    logger.info("Taxonomy fetched: %d species.", len(taxonomy))
    return taxonomy


def fetch_region_species(api_key: str, region: str, session: requests.Session) -> set[str]:
    """Fetch species codes recorded in a region (for prioritisation)."""
    logger.info("Fetching species list for region %s…", region)
    try:
        resp = session.get(
            f"{EBIRD_API_BASE}/product/spplist/{region}",
            timeout=15,
        )
        resp.raise_for_status()
        codes = set(resp.json())
        logger.info("Region %s: %d species.", region, len(codes))
        return codes
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch region species list: %s", exc)
        return set()


# ── Image Download Strategies ─────────────────────────────────────────────────

def try_ebird_image(
    species_code: str, scientific_name: str, session: requests.Session, delay: float
) -> Optional[bytes]:
    """
    Attempt to download a species image from eBird/Cornell Lab media.

    eBird does not have a public image API, but species have a predictable
    asset URL pattern. We fetch the species page HTML and extract the first
    media asset URL from the Open Graph image meta tag.
    """
    try:
        page_url = f"https://ebird.org/species/{species_code}"
        resp = session.get(
            page_url,
            headers={"User-Agent": "pi_birdie/1.0 (bird station; educational use)"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        time.sleep(delay)

        if resp.status_code != 200:
            return None

        # Extract og:image meta tag
        html = resp.text
        og_start = html.find('property="og:image"')
        if og_start == -1:
            og_start = html.find("og:image")
        if og_start == -1:
            return None

        # Find content="..." after the og:image property
        content_start = html.find('content="', og_start)
        if content_start == -1:
            return None
        content_start += len('content="')
        content_end = html.find('"', content_start)
        if content_end == -1:
            return None

        img_url = html[content_start:content_end]
        if not img_url.startswith("http"):
            return None

        img_resp = session.get(img_url, timeout=REQUEST_TIMEOUT)
        time.sleep(delay)
        if img_resp.status_code == 200 and img_resp.headers.get("content-type", "").startswith("image"):
            return img_resp.content

    except Exception as exc:  # noqa: BLE001
        logger.debug("eBird image fetch failed for %s: %s", species_code, exc)

    return None


def try_wikimedia_image(
    scientific_name: str, session: requests.Session, delay: float
) -> Optional[bytes]:
    """
    Fall back to Wikimedia Commons — search by scientific name and
    fetch the first suitable image.
    """
    try:
        # Search Wikipedia for the species article
        search_resp = session.get(
            WIKIMEDIA_API,
            params={
                "action": "query",
                "format": "json",
                "prop": "pageimages",
                "titles": scientific_name,
                "pithumbsize": 400,
                "redirects": 1,
            },
            timeout=REQUEST_TIMEOUT,
        )
        time.sleep(delay)
        search_resp.raise_for_status()

        data  = search_resp.json()
        pages = data.get("query", {}).get("pages", {})

        for page in pages.values():
            thumbnail = page.get("thumbnail", {})
            img_url   = thumbnail.get("source")
            if img_url:
                img_resp = session.get(img_url, timeout=REQUEST_TIMEOUT)
                time.sleep(delay)
                if (img_resp.status_code == 200 and
                        img_resp.headers.get("content-type", "").startswith("image")):
                    return img_resp.content

    except Exception as exc:  # noqa: BLE001
        logger.debug("Wikimedia image fetch failed for %s: %s", scientific_name, exc)

    return None


def save_image(
    image_bytes: bytes, output_path: Path, target_size: tuple[int, int]
) -> bool:
    """Decode, resize, and save an image. Returns True on success."""
    try:
        import io
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        img = img.resize(target_size, Image.LANCZOS)
        img.save(str(output_path), "JPEG", quality=90)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not save image to %s: %s", output_path, exc)
        return False


# ── Main Download Loop ────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    target_size = (args.image_size, args.image_size)

    session = requests.Session()
    session.headers.update({
        "x-ebirdapitoken": args.api_key,
        "User-Agent": "pi_birdie/1.0 (bird identification station; educational use)",
    })

    # Load existing manifest (for skip-existing and resume)
    manifest_path = output_dir / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            manifest = {}

    # Fetch taxonomy
    try:
        taxonomy = fetch_taxonomy(args.api_key, session)
    except Exception as exc:
        logger.critical("Could not fetch taxonomy: %s", exc)
        sys.exit(1)

    # Optionally prioritise species from a region
    region_codes: set[str] = set()
    if args.region:
        region_codes = fetch_region_species(args.api_key, args.region, session)

    # Sort: regional species first, then remainder
    if region_codes:
        regional  = [s for s in taxonomy if s.get("speciesCode") in region_codes]
        remainder = [s for s in taxonomy if s.get("speciesCode") not in region_codes]
        ordered   = regional + remainder
    else:
        ordered = taxonomy

    if args.limit:
        ordered = ordered[:args.limit]

    # ── Download loop ─────────────────────────────────────────────────────────
    stats = {"downloaded": 0, "skipped": 0, "failed": 0}

    with tqdm(total=len(ordered), unit="sp", desc="Downloading images") as pbar:
        for species in ordered:
            code     = species.get("speciesCode", "")
            sci_name = species.get("sciName",     "Unknown")
            com_name = species.get("comName",     "Unknown")

            # Safe filename (replace spaces and special chars)
            safe_name = sci_name.replace(" ", "_").replace("/", "_")[:80]
            img_path  = output_dir / f"{safe_name}.jpg"

            pbar.set_description(f"{com_name[:30]:<30}")
            pbar.update(1)

            # Skip if already downloaded
            if args.skip_existing and img_path.exists():
                manifest[sci_name] = {"status": "ok", "path": str(img_path), "source": "cached"}
                stats["skipped"] += 1
                continue

            image_bytes: Optional[bytes] = None
            source: str = "none"

            # Strategy 1: eBird
            image_bytes = try_ebird_image(code, sci_name, session, args.delay)
            if image_bytes:
                source = "ebird"

            # Strategy 2: Wikimedia Commons (fallback)
            if not image_bytes:
                image_bytes = try_wikimedia_image(sci_name, session, args.delay)
                if image_bytes:
                    source = "wikimedia"

            if image_bytes and save_image(image_bytes, img_path, target_size):
                manifest[sci_name] = {"status": "ok", "path": str(img_path), "source": source}
                stats["downloaded"] += 1
            else:
                manifest[sci_name] = {"status": "failed", "path": None, "source": "none"}
                stats["failed"] += 1
                logger.debug("No image found for %s (%s)", com_name, sci_name)

            # Persist manifest after every 10 species
            if (stats["downloaded"] + stats["failed"]) % 10 == 0:
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )

    # Final manifest save
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Summary
    print(f"\n{'='*55}")
    print(f"  Download complete!")
    print(f"  ✅ Downloaded:  {stats['downloaded']:>5}")
    print(f"  ⏭ Skipped:     {stats['skipped']:>5}  (already existed)")
    print(f"  ❌ Failed:      {stats['failed']:>5}  (no image found)")
    print(f"  📁 Output dir:  {output_dir.resolve()}")
    print(f"  📋 Manifest:    {manifest_path}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
