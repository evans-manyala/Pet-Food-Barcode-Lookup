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
  3. Perplexity web search (live search, then saved to Redis + Pinecone)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
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

def _banner() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]🐾  Pet Food Barcode Lookup[/bold cyan]\n"
            "[dim]Powered by Perplexity AI · Redis · Pinecone[/dim]",
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

    id_table.add_row("Barcode",        product.barcode)
    id_table.add_row("Product Name",   product.product_name)
    id_table.add_row("Brand",          product.brand or "—")
    id_table.add_row("Target Animal",  product.target_animal or "—")
    if product.manufacturer_url:
        id_table.add_row("Manufacturer URL", f"[link={product.manufacturer_url}]{product.manufacturer_url}[/link]")
    if product.image_url:
        id_table.add_row("Image URL",  f"[link={product.image_url}]{product.image_url}[/link]")

    console.print(
        Panel(id_table, title="[bold]🏷  Product Information[/bold]", border_style="cyan")
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
) -> Optional[ProductInfo]:
    """
    Lookup pipeline:
      Redis → Pinecone → Perplexity web search.

    Parameters
    ----------
    barcode        : validated barcode string
    force_refresh  : skip cache and force a fresh web search
    """
    redis_cache = RedisCache()
    pinecone    = PineconeStore()
    searcher    = ProductSearcher()

    # ── 1. Redis ──────────────────────────────────────────────────
    if not force_refresh and redis_cache.is_available:
        product = redis_cache.get(barcode)
        if product:
            _print_product(product, source="⚡ Redis cache")
            return product

    # ── 2. Pinecone ───────────────────────────────────────────────
    if not force_refresh and pinecone.is_available:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as prog:
            prog.add_task("Checking Pinecone vector store …", total=None)
            product = pinecone.fetch_by_barcode(barcode)

        if product:
            # Re-populate Redis since it either missed or was skipped
            if redis_cache.is_available:
                redis_cache.set(product)
                console.print("[dim]  ↺  Re-cached in Redis[/dim]")
            _print_product(product, source="📦 Pinecone vector DB")
            return product

    # ── 3. Live web search ────────────────────────────────────────
    console.print(
        f"\n[bold yellow]🌐  Fetching live data from the web …[/bold yellow]"
    )
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

    if not product.product_name or product.product_name == "Unknown Product":
        console.print(
            Panel(
                "[bold red]Product not found.[/bold red]\n"
                "The barcode may not correspond to a cat or dog food product, "
                "or the product is not yet indexed online.",
                border_style="red",
            )
        )
        return None

    # ── Save to Redis ──────────────────────────────────────────────
    if redis_cache.is_available:
        redis_cache.set(product)
        ttl_h = cfg.redis_ttl // 3600
        console.print(f"[dim]  ✔  Saved to Redis (TTL: {ttl_h}h)[/dim]")

    # ── Save to Pinecone ───────────────────────────────────────────
    if pinecone.is_available:
        ok = pinecone.upsert(product)
        if ok:
            console.print("[dim]  ✔  Saved to Pinecone vector DB[/dim]")

    _print_product(product, source="🌐 Live web search")
    return product


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
    _banner()

    if args.barcode:
        result = validate_barcode(args.barcode)
        if not result.is_valid:
            console.print(f"[bold red]Invalid barcode:[/bold red] {result.error}")
            sys.exit(1)
        console.print(f"[green]Barcode validated:[/green] {result}")
        lookup(result.barcode, force_refresh=args.force_refresh)
    else:
        _interactive_loop()


if __name__ == "__main__":
    main()
