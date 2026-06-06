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
import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.rule import Rule

from src.config import get_settings
from src.barcode_validator import validate_barcode
from src.models import ProductInfo
from src.serialization import product_to_api_payload
from src.service import lookup_barcode

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

SOURCE_LABELS = {
    "redis": "⚡ Redis cache",
    "pinecone": "📦 Pinecone vector DB",
    "live_search": "🌐 Live web search",
}


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
    """CLI wrapper around the shared lookup service."""
    if display:
        console.print("\n[dim]Searching Redis → Pinecone → live web …[/dim]")

    result = lookup_barcode(barcode, force_refresh=force_refresh)
    source_label = SOURCE_LABELS.get(result.source, result.source)

    if result.product and not result.error:
        if display:
            _print_product(result.product, source=source_label)
        return result.product

    if result.product and display:
        _print_product(result.product, source=f"{source_label} — unverified")
        console.print(
            Panel(
                "[bold red]Product not safely identified.[/bold red]\n"
                f"{result.error}",
                border_style="red",
            )
        )
    return None


def _print_json_response(product: Optional[ProductInfo], error: str = "", source: str = "") -> None:
    payload = (
        {"success": True, "data": product_to_api_payload(product, source=source)}
        if product and not error
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
        if args.json:
            lookup_result = lookup_barcode(result.barcode, force_refresh=args.force_refresh)
            _print_json_response(
                lookup_result.product,
                lookup_result.error,
                source=lookup_result.source,
            )
            if lookup_result.product is None or lookup_result.error:
                sys.exit(1)
        else:
            product = lookup(result.barcode, force_refresh=args.force_refresh, display=True)
            if product is None:
                sys.exit(1)
    else:
        _interactive_loop()


if __name__ == "__main__":
    main()
