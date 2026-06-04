#!/usr/bin/env python3
"""
main.py – Pet Food Barcode Lookup
==================================
CLI entry point.

Usage
-----
  python main.py                        # interactive prompt loop
  python main.py --barcode 0023100031105
  python main.py --barcode 0023100031105 --force-refresh

Lookup order
------------
  1. Redis cache (fastest, TTL-based)
  2. Pinecone vector DB (permanent, exact barcode ID lookup)
  3. Gemini web search (live search, then saved to Redis + Pinecone)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.rule import Rule
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.config import get_settings
from src.barcode_validator import validate_barcode
from src.models import ProductInfo
from src.redis_cache import RedisCache
from src.pinecone_store import PineconeStore
from src.llm_searcher import ProductSearcher

# ─── Logging ─────────────────────────────────────────────────────────────────

cfg = get_settings()
logging.basicConfig(
    level=getattr(logging, cfg.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

# ─── Rich console ─────────────────────────────────────────────────────────────

console = Console()

# ─── Display helpers ──────────────────────────────────────────────────────────

def _is_cache_safe(product: ProductInfo) -> bool:
    """
    Old cached records deserialize with default verification fields, so they are
    intentionally ignored rather than displayed or re-cached.
    """
    return (
        bool(product.barcode_verified)
        and product.identity_confidence in {"high", "medium"}
        and bool(product.product_name)
        and product.product_name != "Unknown Product"
    )


def _banner() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]🐾  Pet Food Barcode Lookup[/bold cyan]\n"
            "[dim]Powered by Gemini AI · Redis · Pinecone[/dim]",
            border_style="bright_blue",
            padding=(0, 4),
        )
    )


def _print_product(product: ProductInfo, source: str) -> None:
    console.print()
    console.print(Rule(f"[bold green]Result[/bold green]  [dim](source: {source})[/dim]"))

    # ── Identity panel ────────────────────────────────────────────
    id_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    id_table.add_column("Field", style="dim", no_wrap=True)
    id_table.add_column("Value", style="bold")

    verification = (
        f"[green]verified[/green] ({product.identity_confidence})"
        if product.barcode_verified
        else f"[red]not verified[/red] ({product.identity_confidence})"
    )

    id_table.add_row("Barcode",        product.barcode)
    id_table.add_row("Barcode Match",  verification)
    id_table.add_row("Product Name",   product.product_name)
    id_table.add_row("Brand",          product.brand or "—")
    id_table.add_row("Target Animal",  product.target_animal or "—")
    if product.barcode_evidence:
        evidence = product.barcode_evidence
        id_table.add_row("Evidence", evidence[:180] + ("…" if len(evidence) > 180 else ""))
    if product.evidence_urls:
        id_table.add_row("Evidence URL", f"[link={product.evidence_urls[0]}]{product.evidence_urls[0]}[/link]")
    if product.manufacturer_url:
        id_table.add_row("Manufacturer URL", f"[link={product.manufacturer_url}]{product.manufacturer_url}[/link]")
    if product.image_url:
        id_table.add_row("Image URL",  f"[link={product.image_url}]{product.image_url}[/link]")

    console.print(
        Panel(id_table, title="[bold]🏷  Product Information[/bold]", border_style="cyan")
    )

    if product.warnings:
        console.print(
            Panel(
                "\n".join(f"• {w}" for w in product.warnings),
                title="[bold yellow]⚠ Verification Warnings[/bold yellow]",
                border_style="yellow",
            )
        )

    # ── Nutritional panel ─────────────────────────────────────────
    ni = product.nutritional_info
    if ni:
        nut_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        nut_table.add_column("Nutrient", style="dim", no_wrap=True)
        nut_table.add_column("Value", style="bold yellow")

        def _row(label: str, val: Optional[str]) -> None:
            if val:
                nut_table.add_row(label, val)

        _row("Crude Protein (min)",  ni.crude_protein_min)
        _row("Crude Fat (min)",      ni.crude_fat_min)
        _row("Crude Fiber (max)",    ni.crude_fiber_max)
        _row("Moisture (max)",       ni.moisture_max)
        _row("Ash (max)",            ni.ash_max)
        _row("Calories",             ni.calories)
        for name, value in (ni.other or {}).items():
            _row(name, value)

        console.print(
            Panel(nut_table, title="[bold]🧪  Nutritional / Guaranteed Analysis[/bold]", border_style="yellow")
        )

    # ── HK Retailers panel ────────────────────────────────────────
    if product.hk_retailers:
        ret_table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
        ret_table.add_column("#",              justify="right",  width=3)
        ret_table.add_column("Retailer",       style="bold",     min_width=20)
        ret_table.add_column("Price (HKD)",    justify="right",  style="bright_green", min_width=12)
        ret_table.add_column("In Stock",       justify="center", width=10)
        ret_table.add_column("Product URL",    style="cyan",     min_width=35, overflow="fold")
        ret_table.add_column("Notes",          style="dim",      min_width=15, overflow="fold")

        for i, r in enumerate(product.hk_retailers, 1):
            stock = "✔" if r.in_stock is True else ("✘" if r.in_stock is False else "?")
            ret_table.add_row(
                str(i),
                r.retailer_name,
                r.price_hkd,
                stock,
                f"[link={r.url}]{r.url}[/link]",
                r.notes or "",
            )

        console.print(
            Panel(
                ret_table,
                title="[bold]🛒  Hong Kong Online Retailers[/bold]",
                subtitle="[dim]Prices in HK$ · Click URLs to open[/dim]",
                border_style="magenta",
            )
        )
    else:
        console.print(
            Panel(
                "[yellow]No Hong Kong retailers found for this product.[/yellow]",
                title="🛒  Hong Kong Online Retailers",
                border_style="yellow",
            )
        )

    console.print()


# ─── Core lookup logic ────────────────────────────────────────────────────────

def lookup(
    barcode: str,
    force_refresh: bool = False,
    display: bool = True,
) -> Optional[ProductInfo]:
    """
    Lookup pipeline:
      Redis → Pinecone → Gemini web search.

    Parameters
    ----------
    barcode        : validated barcode string
    force_refresh  : skip cache and force a fresh web search
    """
    redis_cache = RedisCache()
    pinecone    = PineconeStore()

    # ── 1. Redis ──────────────────────────────────────────────────
    if not force_refresh and redis_cache.is_available:
        product = redis_cache.get(barcode)
        if product:
            if _is_cache_safe(product):
                if display:
                    _print_product(product, source="⚡ Redis cache")
                return product
            if display:
                console.print("[yellow]Ignored unsafe/unverified Redis cache entry; refreshing live data.[/yellow]")
            redis_cache.delete(barcode)

    # ── 2. Pinecone ───────────────────────────────────────────────
    if not force_refresh and pinecone.is_available:
        if display:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
                transient=True,
            ) as prog:
                prog.add_task("Checking Pinecone vector store …", total=None)
                product = pinecone.fetch_by_barcode(barcode)
        else:
            product = pinecone.fetch_by_barcode(barcode)

        if product:
            if _is_cache_safe(product):
                # Re-populate Redis since it either missed or was skipped
                if redis_cache.is_available:
                    redis_cache.set(product)
                    if display:
                        console.print("[dim]  ↺  Re-cached in Redis[/dim]")
                if display:
                    _print_product(product, source="📦 Pinecone vector DB")
                return product
            if display:
                console.print("[yellow]Ignored unsafe/unverified Pinecone entry; refreshing live data.[/yellow]")

    # ── 3. Live web search ────────────────────────────────────────
    if display:
        console.print(
            f"\n[bold yellow]🌐  Fetching live data from the web …[/bold yellow]"
        )
    searcher = ProductSearcher()
    if display:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as prog:
            t1 = prog.add_task("Phase 1 – product details (manufacturer site) …", total=None)
            # We call searcher.search() which internally does two LLM calls
            product = searcher.search(barcode)
            prog.remove_task(t1)
    else:
        product = searcher.search(barcode)

    if not _is_cache_safe(product):
        if display:
            _print_product(product, source="🌐 Live web search — unverified")
            console.print(
                Panel(
                    "[bold red]Product not safely identified.[/bold red]\n"
                    "The search did not find strong source evidence connecting this exact barcode "
                    "to one product. Nothing was saved to Redis or Pinecone.",
                    border_style="red",
                )
            )
        return None

    # ── Save to Redis ──────────────────────────────────────────────
    if redis_cache.is_available:
        redis_cache.set(product)
        ttl_h = cfg.redis_ttl // 3600
        if display:
            console.print(f"[dim]  ✔  Saved to Redis (TTL: {ttl_h}h)[/dim]")

    # ── Save to Pinecone ───────────────────────────────────────────
    if pinecone.is_available:
        ok = pinecone.upsert(product)
        if ok and display:
            console.print("[dim]  ✔  Saved to Pinecone vector DB[/dim]")

    if display:
        _print_product(product, source="🌐 Live web search")
    return product


def _number_from_text(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalise_analysis_key(key: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", key or "").strip("_").lower()
    aliases = {
        "crude_protein_min": "protein",
        "crude_fat_min": "fat_content",
        "crude_fiber_max": "crude_fiber",
        "moisture_max": "moisture",
        "ash_max": "crude_ash",
        "crude_ash_max": "crude_ash",
    }
    return aliases.get(cleaned, cleaned)


def _api_payload(product: ProductInfo) -> dict:
    ni = product.nutritional_info
    guaranteed_analysis: dict[str, Optional[float]] = {}
    if ni:
        guaranteed_analysis = {
            "protein": _number_from_text(ni.crude_protein_min),
            "fat_content": _number_from_text(ni.crude_fat_min),
            "crude_fiber": _number_from_text(ni.crude_fiber_max),
            "moisture": _number_from_text(ni.moisture_max),
            "crude_ash": _number_from_text(ni.ash_max),
            "calories": _number_from_text(ni.calories),
        }
        for key, value in (ni.other or {}).items():
            guaranteed_analysis[_normalise_analysis_key(key)] = _number_from_text(value)

    prices = []
    for retailer in product.hk_retailers:
        price = _number_from_text(retailer.price_hkd)
        prices.append({
            "store": retailer.retailer_name,
            "retailer_name": retailer.retailer_name,
            "price": price,
            "price_display": retailer.price_hkd,
            "currency": "HKD",
            "url": retailer.url,
            "in_stock": retailer.in_stock,
            "region": "HK",
            "notes": retailer.notes,
        })
    prices.sort(key=lambda item: item["price"] if item["price"] is not None else float("inf"))

    return {
        "barcode": product.barcode,
        "product_name": product.product_name,
        "title_en": product.product_name,
        "brand": product.brand,
        "target_animal": product.target_animal,
        "pet_type": product.target_animal,
        "manufacturer_url": product.manufacturer_url,
        "image_url": product.image_url,
        "image_display": {
            "width": 240,
            "height": 240,
            "object_fit": "contain",
        },
        "guaranteed_analysis": guaranteed_analysis,
        "price_comparison": prices,
        "best_price": prices[0] if prices else None,
        "source_urls": product.evidence_urls,
        "barcode_verified": product.barcode_verified,
        "identity_confidence": product.identity_confidence,
        "barcode_evidence": product.barcode_evidence,
        "warnings": product.warnings,
    }


def _print_json_response(product: Optional[ProductInfo], error: str = "") -> None:
    payload = (
        {"success": True, "data": _api_payload(product)}
        if product
        else {"success": False, "error": error or "Product not found"}
    )
    print(json.dumps(payload, ensure_ascii=False))


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Look up pet food product info by barcode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py\n"
            "  python main.py --barcode 0023100031105\n"
            "  python main.py --barcode 0023100031105 --force-refresh\n"
        ),
    )
    p.add_argument("--barcode", "-b", help="Barcode to look up (skip interactive prompt)")
    p.add_argument(
        "--force-refresh", "-f",
        action="store_true",
        help="Ignore Redis/Pinecone cache and re-fetch from the web",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print API-friendly JSON instead of the Rich terminal UI",
    )
    return p.parse_args()


def _interactive_loop() -> None:
    """Ask for barcodes until the user types 'quit' or presses Ctrl-C."""
    console.print(
        "\n[dim]Type a barcode and press Enter. "
        "Type [bold]quit[/bold] or press [bold]Ctrl-C[/bold] to exit.[/dim]\n"
    )
    while True:
        try:
            raw = console.input("[bold cyan]Barcode ▶ [/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[bold]Goodbye! 🐾[/bold]")
            break

        if raw.lower() in {"quit", "exit", "q"}:
            console.print("[bold]Goodbye! 🐾[/bold]")
            break

        if not raw:
            continue

        result = validate_barcode(raw)
        if not result.is_valid:
            console.print(f"[bold red]Invalid barcode:[/bold red] {result.error}")
            continue

        console.print(
            f"\n[green]Barcode validated:[/green] {result} "
        )
        lookup(result.barcode)


def main() -> None:
    args = _parse_args()
    if not args.json:
        _banner()

    if args.barcode:
        result = validate_barcode(args.barcode)
        if not result.is_valid:
            if args.json:
                _print_json_response(None, result.error)
            else:
                console.print(f"[bold red]Invalid barcode:[/bold red] {result.error}")
            sys.exit(1)
        if not args.json:
            console.print(f"[green]Barcode validated:[/green] {result}")
        product = lookup(result.barcode, force_refresh=args.force_refresh, display=not args.json)
        if args.json:
            _print_json_response(product)
            if product is None:
                sys.exit(1)
    else:
        _interactive_loop()


if __name__ == "__main__":
    main()
