"""
Reproducible performance benchmarks for RAM-Linux.
Measures startup latency, memory footprint (RSS), vault throughput, and instance tracking overhead.
"""

import time
import os
import tempfile
import sys
import resource
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from core.models import RobloxAccount
from core.storage import StorageManager
from core.instance_runner import InstanceRunner


def measure_benchmark():
    results = {}

    # 1. Startup Latency & Storage Init
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir) / "ram-bench"
        mocktail_data = Path(tmpdir) / "mocktail_data"
        mocktail_bin = Path(tmpdir) / "mocktail_bin"
        (mocktail_data / "payloads").mkdir(parents=True, exist_ok=True)
        (mocktail_data / "current.json").write_text('{"dummy": true}')
        mocktail_bin.write_text("#!/bin/sh\nexit 0\n")
        mocktail_bin.chmod(0o755)

        storage = StorageManager(
            base_dir=base_dir,
            mocktail_data=mocktail_data,
            mocktail_bin=mocktail_bin,
        )
        runner = InstanceRunner(storage)
        init_time_ms = (time.perf_counter() - t0) * 1000
        results["storage_init_latency_ms"] = round(init_time_ms, 2)

        # 2. Vault Encryption & Persistence Throughput (100 accounts)
        t_batch_start = time.perf_counter()
        for i in range(100):
            acc = RobloxAccount(
                account_id=f"acc_{i:04d}",
                username=f"BenchmarkPlayer{i}",
                display_name=f"Player {i}",
                user_id=100000 + i,
                cookie=f"_|WARNING:-DO-NOT-SHARE-THIS...cookie_payload_{i:04d}",
                robux_balance=i * 50,
                group="Benchmark",
                notes="Automated benchmark account",
            )
            storage.save_account(acc)
        batch_save_time_ms = (time.perf_counter() - t_batch_start) * 1000
        results["batch_100_save_latency_ms"] = round(batch_save_time_ms, 2)
        results["per_account_save_latency_ms"] = round(batch_save_time_ms / 100, 3)

        # 2b. Single-Transaction Batch Save Throughput (100 accounts)
        batch_accounts = [
            RobloxAccount(
                account_id=f"batch_acc_{i:04d}",
                username=f"BatchPlayer{i}",
                display_name=f"Batch {i}",
                user_id=200000 + i,
                cookie=f"_|WARNING:-DO-NOT-SHARE-THIS...cookie_payload_{i:04d}",
                robux_balance=i * 25,
                group="BatchBenchmark",
                notes="Batch test account",
            )
            for i in range(100)
        ]
        t_single_tx_start = time.perf_counter()
        storage.save_accounts_batch(batch_accounts)
        single_tx_time_ms = (time.perf_counter() - t_single_tx_start) * 1000
        results["single_tx_100_save_latency_ms"] = round(single_tx_time_ms, 2)
        results["per_account_batch_save_latency_ms"] = round(single_tx_time_ms / 100, 3)

        # 3. Read & Decrypt Throughput (200 accounts)
        t_read_start = time.perf_counter()
        accounts = storage.list_accounts()
        batch_read_time_ms = (time.perf_counter() - t_read_start) * 1000
        assert len(accounts) == 200
        results["batch_200_read_latency_ms"] = round(batch_read_time_ms, 2)
        results["per_account_read_latency_ms"] = round(batch_read_time_ms / len(accounts), 3)

        # 4. Instance Tracking Check Overhead (100 checks)
        t_track_start = time.perf_counter()
        for acc in accounts:
            _ = runner.is_instance_running(acc.account_id)
        track_time_ms = (time.perf_counter() - t_track_start) * 1000
        results["100_liveness_checks_latency_ms"] = round(track_time_ms, 3)

    # 5. Peak Memory Usage (RSS)
    # ru_maxrss is in KiB on Linux
    max_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    results["peak_rss_mb"] = round(max_rss_kb / 1024, 2)

    return results


if __name__ == "__main__":
    res = measure_benchmark()
    print("\n==================== PERFORMANCE BENCHMARK RESULTS ====================")
    for k, v in res.items():
        print(f"  • {k:<36}: {v}")
    print("========================================================================\n")
