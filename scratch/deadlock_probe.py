"""Deadlock probe — 20 sequential, then 5 concurrent ansible-runner invocations
through Rocannon's executor. Records timings; flags hangs."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from rocannon.executor import run_module  # noqa: E402

INV = [str(REPO / "scratch" / "inventory.yml")]


def one_call(i: int) -> dict:
    t0 = time.monotonic()
    res = run_module(
        module="ansible.builtin.ping",
        module_args={},
        inventory=INV,
        host_pattern="ubuntu",
        timeout=60,
        idle_timeout=20,
    )
    return {
        "i": i,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "status": res.get("status"),
        "ok": res.get("status") == "successful",
    }


async def aone(i: int) -> dict:
    return await asyncio.to_thread(one_call, i)


async def main() -> None:
    print("=== 20 sequential calls ===")
    seq_results = []
    seq_start = time.monotonic()
    for i in range(20):
        r = one_call(i)
        seq_results.append(r)
        print(f"  call {i}: {r['status']} {r['elapsed_ms']}ms")
    seq_total = time.monotonic() - seq_start

    print(f"\n=== 5 concurrent calls (asyncio.to_thread) ===")
    con_start = time.monotonic()
    con_results = await asyncio.gather(*(aone(i) for i in range(5)))
    con_total = time.monotonic() - con_start
    for r in con_results:
        print(f"  call {r['i']}: {r['status']} {r['elapsed_ms']}ms")

    summary = {
        "sequential": {
            "count": len(seq_results),
            "all_ok": all(r["ok"] for r in seq_results),
            "total_seconds": round(seq_total, 2),
            "avg_ms": sum(r["elapsed_ms"] for r in seq_results) // len(seq_results),
            "max_ms": max(r["elapsed_ms"] for r in seq_results),
        },
        "concurrent": {
            "count": len(con_results),
            "all_ok": all(r["ok"] for r in con_results),
            "total_seconds": round(con_total, 2),
            "max_ms": max(r["elapsed_ms"] for r in con_results),
        },
    }
    Path(REPO / "scratch" / "results" / "deadlock_probe.json").write_text(
        json.dumps(summary, indent=2)
    )
    print("\n", json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
