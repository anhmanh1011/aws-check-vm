"""Command-line entry point.

Wires the pieces together in one obvious place::

    argparse -> load_tasks (input lines -> batches) / ProxyManager.from_file
             / load_handler -> AutomationEngine (+ Dashboard) -> write_results
             -> exit code

WHY the dashboard and the engine run under one ``asyncio.gather``: both are
coroutines on the same event loop. The engine sets an ``asyncio.Event`` when
it finishes so the dashboard knows to draw a final frame and stop.

Exit codes: 0 all tasks ok, 1 some task failed or the browser could not
launch, 2 bad input, 130 interrupted with Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from dashboard import Dashboard
from engine import AutomationEngine
from handlers import DEFAULT_HANDLER, load_handler
import kiotproxy
from kiotproxy import load_keys, load_kiot_proxies
from models import RunState, TaskResult
from proxy_manager import ProxyManager
from tasks import load_tasks
from utils import setup_logging, write_results, write_split_outputs

log = logging.getLogger(__name__)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _screen_size(value: str) -> tuple[int, int]:
    """Parse ``WxH`` (e.g. ``1920x1080``) into ``(width, height)``."""
    try:
        width, height = (int(part) for part in value.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("must be WxH, e.g. 1920x1080") from None
    if width < 1 or height < 1:
        raise argparse.ArgumentTypeError("width and height must be >= 1")
    return width, height


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI. Kept separate from ``main`` so tests can inspect defaults."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Run a browser-automation flow over every line of an input file, "
                    "concurrently, each batch in its own isolated, proxied browser context.",
    )
    parser.add_argument("--input", type=Path, default=Path("input.txt"),
                        help="text file, one data item per line; your handler decides what "
                             "each line means (default: %(default)s)")
    parser.add_argument("--batch-size", type=_non_negative_int, default=3,
                        help="lines per browser context: 1 = one context per line, N = fixed "
                             "batches of N, 0 = split evenly so each worker gets one batch "
                             "(default: %(default)s)")
    parser.add_argument("--proxies", type=Path, default=Path("proxies.txt"),
                        help="proxy list; missing or empty means connect directly (default: %(default)s)")
    parser.add_argument("--kiot-keys", type=Path, default=None,
                        help="KiotProxy key file, one key per line; when given it replaces "
                             "--proxies as the proxy source (default: %(default)s)")
    parser.add_argument("--kiot-region", default="random", choices=sorted(kiotproxy.VALID_REGIONS),
                        help="KiotProxy region for --kiot-keys (default: %(default)s)")
    parser.add_argument("--concurrency", type=_positive_int, default=3,
                        help="number of concurrent workers / live contexts (default: %(default)s)")
    parser.add_argument("--proxy-per-worker", action="store_true",
                        help="pin each worker to one fixed proxy for the whole run instead of "
                             "sharing proxies round-robin; auto-enabled by --kiot-keys")
    parser.add_argument("--split-output", action="store_true",
                        help="instead of the --output file, append each email to exists.txt / "
                             "not_found.txt / error.txt (in --output's folder) by its check_VM "
                             "status; error.txt holds 'email<TAB>reason'")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True,
                        help="run the browser headless (default: --headless)")
    parser.add_argument("--screen-size", type=_screen_size, default=None, metavar="WxH",
                        help="screen size for tiling headed windows, e.g. 1920x1080 "
                             "(default: auto-detect)")
    parser.add_argument("--output", type=Path, default=Path("results.json"),
                        help="results file; .json or .csv (default: %(default)s)")
    parser.add_argument("--handler", default=DEFAULT_HANDLER,
                        help="per-task coroutine as module:function (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds allowed per task (default: %(default)s)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="print log lines instead of the live table (useful in CI)")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    return parser


async def run_async(engine: AutomationEngine, dashboard: Dashboard | None) -> list[TaskResult]:
    """Run the engine, with the dashboard alongside it when requested."""
    if dashboard is None:
        return await engine.run()

    done = asyncio.Event()

    async def run_engine() -> list[TaskResult]:
        try:
            return await engine.run()
        finally:
            done.set()

    results, _ = await asyncio.gather(run_engine(), dashboard.run_until(done))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Parse args, run the engine, and write results.

    Exit codes: 0 all tasks ok, 1 some task failed (or the browser could not
    launch, or results could not be written), 2 bad input, 130 interrupted
    with Ctrl+C.
    """
    args = build_parser().parse_args(argv)
    use_dashboard = not args.no_dashboard
    setup_logging(verbose=args.verbose, dashboard_active=use_dashboard)

    try:
        # Checked up front, before the (possibly long) browser run, so a
        # typo'd --output extension fails fast instead of burning a whole
        # run only to crash on the final write.
        if args.output.suffix.lower() not in {".json", ".csv"}:
            raise ValueError(
                f"unsupported output extension {args.output.suffix!r} (use .json or .csv)"
            )
        concurrency = args.concurrency
        proxy_per_worker = args.proxy_per_worker
        if args.kiot_keys is not None:
            # --kiot-keys replaces proxies.txt: fetch one IP per key up front
            # and build the same pool the engine already expects. Done before
            # load_tasks so a bad key file fails fast with its own message
            # instead of being masked by an unrelated input error.
            keys = load_keys(args.kiot_keys)
            proxy_manager = ProxyManager(load_kiot_proxies(keys, args.kiot_region))
            # One KiotProxy key = one IP, so give each proxy its own worker and
            # pin it there: N proxies -> N workers, each browser lane on a fixed
            # IP. This overrides --concurrency. With no usable proxy, fall back
            # to a single direct worker.
            proxy_per_worker = True
            concurrency = len(proxy_manager) or 1
            log.info(
                "KiotProxy: %d proxy(ies) -> %d worker(s), one proxy per browser",
                len(proxy_manager), concurrency,
            )
        else:
            proxy_manager = ProxyManager.from_file(args.proxies)
        tasks = load_tasks(args.input, batch_size=args.batch_size, concurrency=concurrency)
        handler = load_handler(args.handler)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    state = RunState(total=len(tasks))
    engine = AutomationEngine(
        tasks,
        proxy_manager,
        handler,
        concurrency=concurrency,
        headless=args.headless,
        timeout_s=args.timeout,
        state=state,
        proxy_per_worker=proxy_per_worker,
        screen_size=args.screen_size,
    )
    dashboard = Dashboard(state) if use_dashboard else None

    exit_code = 0
    try:
        asyncio.run(run_async(engine, dashboard))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # KeyboardInterrupt is Ctrl+C reaching Python's signal handler
        # directly. asyncio.CancelledError is the same interrupt observed
        # from inside the event loop: asyncio.run() cancels the top-level
        # task on KeyboardInterrupt, and that cancellation can itself
        # surface here as a CancelledError instead of (or alongside) the
        # original KeyboardInterrupt, depending on exactly where the signal
        # lands. Both mean the same thing to the user, so both get the same
        # exit code and message.
        print("\nInterrupted; writing partial results", file=sys.stderr)
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - e.g. browser failed to launch
        log.exception("run aborted")
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = 1

    # Partial results are still valuable after an interrupt or a crash.
    results = state.results
    if args.split_output:
        # Replace results.json with the three append files, next to --output.
        out_dir = args.output.parent
        destination = f"{out_dir / 'exists.txt'}, not_found.txt, error.txt"
        try:
            write_split_outputs(results, out_dir)
        except OSError as exc:
            print(f"error: could not write split outputs to {out_dir}: {exc}", file=sys.stderr)
            if exit_code == 0:
                exit_code = 1
    else:
        destination = str(args.output)
        try:
            write_results(results, args.output)
        except (OSError, ValueError) as exc:
            print(f"error: could not write results to {args.output}: {exc}", file=sys.stderr)
            if exit_code == 0:
                exit_code = 1
    failed = sum(1 for result in results if result.status != "ok")
    print(f"{len(results)}/{len(tasks)} tasks finished, {failed} failed -> {destination}")

    if exit_code == 0 and (failed or len(results) != len(tasks)):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
