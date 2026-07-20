#!/usr/bin/env python3
"""Fully read and validate all signal arrays in a rebuilt POD5 file."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pod5


def sha256_file(path: Path, chunk_size: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open every read and decompress every signal array in a POD5 file"
    )
    parser.add_argument("pod5", type=Path)
    parser.add_argument("--json", type=Path, help="Optional JSON report path")
    parser.add_argument("--expected-reads", type=int)
    args = parser.parse_args()

    path = args.pod5.resolve()
    report: dict[str, Any] = {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "reported_reads": 0,
        "decoded_reads": 0,
        "total_samples": 0,
        "run_info": {},
        "passed": False,
    }

    with pod5.Reader(path) as reader:
        report["reported_reads"] = reader.num_reads
        for read in reader.reads():
            signal = read.signal
            if len(signal) != read.sample_count:
                raise RuntimeError(
                    f"{read.read_id}: decoded signal length {len(signal)} "
                    f"!= sample_count {read.sample_count}"
                )
            report["decoded_reads"] += 1
            report["total_samples"] += len(signal)
            if not report["run_info"]:
                info = read.run_info
                for key in (
                    "acquisition_id",
                    "flow_cell_id",
                    "flow_cell_product_code",
                    "sequencing_kit",
                    "sample_rate",
                    "protocol_run_id",
                    "sample_id",
                ):
                    report["run_info"][key] = getattr(info, key, None)

    if report["decoded_reads"] != report["reported_reads"]:
        raise RuntimeError(
            f"decoded_reads={report['decoded_reads']} != "
            f"reported_reads={report['reported_reads']}"
        )
    if args.expected_reads is not None and report["decoded_reads"] != args.expected_reads:
        raise RuntimeError(
            f"decoded_reads={report['decoded_reads']} != expected_reads={args.expected_reads}"
        )

    report["passed"] = True
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
