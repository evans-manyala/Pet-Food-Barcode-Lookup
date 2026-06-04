#!/usr/bin/env python3
"""
main.py – Pet Food Barcode Lookup
==================================
CLI entry point.

Usage
-----
  python main.py
  python main.py --barcode 0023100031105
  python main.py --barcode 0023100031105 --force-refresh

Lookup order
------------
  1. Redis cache (fastest, TTL-based, only trusted if barcode_verified=true)
  2. Pinecone vector DB (exact barcode ID lookup, only trusted if verified)
  3. Gemini Google Search Grounding (live search, then saved only if verified)
"""

from __future__ import annotations

import argparse
import logging
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

console = Console()


def _is_cache_safe(product: ProductInfo) -> bool:
    """
    Old cached records do not have barcode_verified/evidence fields, so they
    deserialize with defaults: barcode_verified=False, identity_confidence=low.
    That is intentional: old/unsafe cache entries are ignored.
    """
    return (
        bool(product.barcode_verified)
        and product.identity_confidence in {"high", "medium"}
        and product.product_name
        and product.product_name != "Unknown Product"
    )


def _banner() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]🐾  Pet Food Barcode Lookup[/bold cyan]\n"
            "[dim]Gemini Google Search Grounding · Redis · Pinecone[/dim]",
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

    id_table.add_row("Barcode", product.barcode)
    id_table.add_row("Barcode Match", verification)
    id_table.add_row("Product Name", product.product_name)
    id_table.add_row("Brand", product.brand or "—")
    id_table.add_row("Target Animal", product.target_animal or "—")

    if product.barcode_evidence:
        id_table.add_row("Evidence", product.barcode_evidence[:180] + ("…" if len(product.barcode_evidence) > 180 else ""))

    if product.evidence_urls:
        id_table.add_row("Evidence URL", f"[link={product.evidence_urls[0]}]{product.evidence_urls[0]}[/link]")

    if product.manufacturer_url:
        id_table.add_row("Manufacturer URL", f"[link={product.manufacturer_url}]{product.manufacturer_url}[/link]")

    if product.image_url:
        id_table.add_row("Image URL", f"[link={product.image_url}]{product.image_url}[/link]")

    console.print(Panel(id_table, title="[bold]🏷  Product Information[/bold]", border_style="cyan"))

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

        _row("Crude Protein (min)", ni.crude_protein_min)
        _row("Crude Fat (min)", ni.crude_fat_min)
        _row("Crude Fiber (max)", ni.crude_fiber_max)
        _row("Moisture (max)", ni.moisture_max)
        _row("Ash (max)", ni.ash_max)
        _row("Calories", ni.calories)
        for name, value in (ni.other or {}).items():
            _row(name, value)

        if nut_table.row_count:
            console.print(
                Panel(nut_table, title="[bold]🧪  Nutritional / Guaranteed Analysis[/bold]", border_style="yellow")
            )

    # ── HK Retailers panel ────────────────────────────────────────
    if product.hk_retailers:
        ret_table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
        ret_table.add_column("#", justify="right", width=3)
        ret_table.add_column("Retailer", style="bold", min_width=20)
        ret_table.add_column("Price (HKD)", justify="right", style="bright_green", min_width=12)
        ret_table.add_column("In Stock", justify="center", width=10)
        ret_table.add_column("Product URL", style="cyan", min_width=35, overflow="fold")
        ret_table.add_column("Notes", style="dim", min_width=15, overflow="fold")

        for i, r in enumerate(product.hk_retailers, 1):
            stock = "✔" if r.in_stock is True else ("✘" if r.in_stock is False else "?")
            ret_table.add_row(
                str(i),
                r.retailer_name,
                r.price_hkd or "—",
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
                "[yellow]No verified Hong Kong retailers found for this product.[/yellow]",
                title="🛒  Hong Kong Online Retailers",
                border_style="yellow",
            )
        )

    console.print()


def lookup(barcode: str, force_refresh: bool = False) -> Optional[ProductInfo]:
    """
    Lookup pipeline:
      Redis → Pinecone → Gemini grounded live search.

    Only verified product identities are trusted from cache or persisted.
    """
    redis_cache = RedisCache()
    pinecone = PineconeStore()
    searcher = ProductSearcher()

    # ── 1. Redis ──────────────────────────────────────────────────
    if not force_refresh and redis_cache.is_available:
        product = redis_cache.get(barcode)
        if product:
            if _is_cache_safe(product):
                _print_product(product, source="⚡ Redis cache")
                return product
            console.print("[yellow]Ignored unsafe/unverified Redis cache entry; refreshing live data.[/yellow]")
            redis_cache.delete(barcode)

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
            if _is_cache_safe(product):
                if redis_cache.is_available:
                    redis_cache.set(product)
                    console.print("[dim]  ↺  Re-cached in Redis[/dim]")
                _print_product(product, source="📦 Pinecone vector DB")
                return product
            console.print("[yellow]Ignored unsafe/unverified Pinecone entry; refreshing live data.[/yellow]")

    # ── 3. Live web search ────────────────────────────────────────
    console.print("\n[bold yellow]🌐  Fetching live data from Gemini grounded search …[/bold yellow]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as prog:
        task = prog.add_task("Verifying exact barcode identity and nutrition …", total=None)
        product = searcher.search(barcode)
        prog.remove_task(task)

    if not product.product_name or product.product_name == "Unknown Product":
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

    # ── Save only verified results ────────────────────────────────
    if _is_cache_safe(product):
        if redis_cache.is_available:
            redis_cache.set(product)
            ttl_h = cfg.redis_ttl // 3600
            console.print(f"[dim]  ✔  Saved to Redis (TTL: {ttl_h}h)[/dim]")

        if pinecone.is_available:
            ok = pinecone.upsert(product)
            if ok:
                console.print("[dim]  ✔  Saved to Pinecone vector DB[/dim]")
    else:
        console.print("[yellow]Result was not verified; skipped Redis/Pinecone save.[/yellow]")

    _print_product(product, source="🌐 Live web search")
    return product


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

        console.print(f"\n[green]Barcode validated:[/green] {result}")
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
