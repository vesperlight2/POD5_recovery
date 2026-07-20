#!/usr/bin/env python3
"""
Best-effort POD5 reconstruction from intact embedded Arrow IPC sections.

This script does NOT recover bytes that were overwritten or restored as zeros.
It scans a damaged POD5 for its 16-byte section marker, extracts complete Arrow
IPC files, identifies Reads / Signal / Run Info tables, and rewrites every read
whose metadata and all referenced signal chunks are still valid.

Requirements:
    python -m pip install pod5 pyarrow numpy

Example:
    python rebuild_pod5_from_sections.py damaged.pod5 rebuilt.pod5 \
        --work-dir damaged.sections

Optional same-run donor for Run Info only:
    python rebuild_pod5_from_sections.py damaged.pod5 rebuilt.pod5 \
        --work-dir damaged.sections --donor-pod5 intact_same_run.pod5
"""

from __future__ import annotations

import argparse
import bisect
import json
import mmap
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

import numpy as np
import pod5 as p5  # Import first so POD5 Arrow extension types are registered.
# ShiftScalePair exists in pod5.pod5_types but is not exported at the
# top-level `pod5` namespace in current POD5 Python releases.
from pod5.pod5_types import ShiftScalePair
import pyarrow as pa
import pyarrow.ipc as ipc
from pod5.signal_tools import vbz_decompress_signal


POD5_SIGNATURE = b"\x8bPOD\r\n\x1a\n"
ARROW_MAGIC = b"ARROW1"


class SalvageError(RuntimeError):
    pass


@dataclass
class ExtractedSection:
    index: int
    start: int
    end: int
    path: Path
    table_type: str
    rows: int
    batches: int
    columns: List[str]
    file_identifier: Optional[str]


def scalar_value(batch: pa.RecordBatch, name: str, row: int, default: Any = None) -> Any:
    index = batch.schema.get_field_index(name)
    if index < 0:
        return default
    scalar = batch.column(index)[row]
    if not scalar.is_valid:
        return default
    return scalar.as_py()


def dict_value(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    try:
        return {str(k): str(v) for k, v in value}
    except Exception as exc:
        raise SalvageError(f"Cannot convert map value to dict: {value!r}") from exc


def uuid_value(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != 16:
            raise ValueError(f"UUID byte value has length {len(raw)}, expected 16")
        return UUID(bytes=raw)
    return UUID(str(value))


def datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, np.datetime64):
        # Convert to UTC millisecond timestamp.
        millis = int(value.astype("datetime64[ms]").astype(np.int64))
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    if isinstance(value, (int, np.integer)):
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    raise ValueError(f"Unsupported timestamp value: {value!r}")


def classify_schema(schema: pa.Schema) -> str:
    names = set(schema.names)
    if {"read_id", "signal", "channel", "well", "run_info"}.issubset(names):
        return "reads"
    if {"read_id", "signal", "samples"}.issubset(names) and "channel" not in names:
        return "signal"
    if {"acquisition_id", "sample_rate", "flow_cell_id"}.issubset(names):
        return "run_info"
    return "other"


def schema_file_identifier(schema: pa.Schema) -> Optional[str]:
    metadata = schema.metadata or {}
    value = metadata.get(b"MINKNOW:file_identifier")
    return value.decode("utf-8", errors="replace") if value else None


def find_marker_positions(mm: mmap.mmap, marker: bytes) -> List[int]:
    positions: List[int] = []
    offset = 8
    while True:
        pos = mm.find(marker, offset)
        if pos < 0:
            break
        positions.append(pos)
        offset = pos + 1
    return positions


def strip_arrow_padding(mm: mmap.mmap, start: int, raw_end: int) -> Optional[int]:
    if raw_end <= start or mm[start : start + len(ARROW_MAGIC)] != ARROW_MAGIC:
        return None
    for padding in range(0, 8):
        end = raw_end - padding
        if end - start >= 12 and mm[end - len(ARROW_MAGIC) : end] == ARROW_MAGIC:
            return end
    return None


def last_nonzero_position(mm: mmap.mmap, chunk_size: int = 64 * 1024 * 1024) -> int:
    pos = len(mm)
    while pos > 0:
        start = max(0, pos - chunk_size)
        data = mm[start:pos]
        idx = len(data) - 1
        while idx >= 0 and data[idx] == 0:
            idx -= 1
        if idx >= 0:
            return start + idx
        pos = start
    return -1


def fallback_arrow_end(mm: mmap.mmap, start: int, maximum_end: int) -> Optional[int]:
    """Find a plausible final ARROW1 when the following POD5 marker was lost."""
    candidates: List[int] = []
    offset = start + len(ARROW_MAGIC)
    while True:
        pos = mm.find(ARROW_MAGIC, offset, maximum_end)
        if pos < 0:
            break
        candidates.append(pos + len(ARROW_MAGIC))
        offset = pos + 1
    return candidates[-1] if candidates else None


def copy_range(mm: mmap.mmap, start: int, end: int, output: Path, chunk: int = 64 * 1024 * 1024) -> None:
    with output.open("wb") as out:
        pos = start
        while pos < end:
            next_pos = min(end, pos + chunk)
            out.write(mm[pos:next_pos])
            pos = next_pos


def open_arrow_file(path: Path) -> ipc.RecordBatchFileReader:
    return ipc.open_file(str(path))


def extract_sections(input_path: Path, work_dir: Path) -> List[ExtractedSection]:
    work_dir.mkdir(parents=True, exist_ok=True)
    extracted: List[ExtractedSection] = []

    with input_path.open("rb") as handle:
        mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if len(mm) < 24 or mm[:8] != POD5_SIGNATURE:
                raise SalvageError("Input does not have a valid POD5 leading signature")

            marker = bytes(mm[8:24])
            positions = find_marker_positions(mm, marker)
            if not positions or positions[0] != 8:
                raise SalvageError("Could not locate the initial POD5 section marker")

            nonzero_end = last_nonzero_position(mm) + 1
            candidates: List[Tuple[int, int]] = []

            for left, right in zip(positions, positions[1:]):
                start = left + 16
                end = strip_arrow_padding(mm, start, right)
                if end is not None:
                    candidates.append((start, end))

            # If the marker after the final embedded Arrow file was zeroed, try the
            # final marker as a section start and use the last ARROW1 before zero tail.
            final_start = positions[-1] + 16
            if final_start < nonzero_end and mm[final_start : final_start + 6] == ARROW_MAGIC:
                final_end = fallback_arrow_end(mm, final_start, nonzero_end)
                if final_end is not None and (final_start, final_end) not in candidates:
                    candidates.append((final_start, final_end))

            seen: set[Tuple[int, int]] = set()
            for section_index, (start, end) in enumerate(candidates, start=1):
                if (start, end) in seen:
                    continue
                seen.add((start, end))
                section_path = work_dir / f"section_{section_index:02d}_{start}_{end}.arrow"
                copy_range(mm, start, end, section_path)

                try:
                    reader = open_arrow_file(section_path)
                    table_type = classify_schema(reader.schema)
                    rows = sum(reader.get_batch(i).num_rows for i in range(reader.num_record_batches))
                    section = ExtractedSection(
                        index=section_index,
                        start=start,
                        end=end,
                        path=section_path,
                        table_type=table_type,
                        rows=rows,
                        batches=reader.num_record_batches,
                        columns=list(reader.schema.names),
                        file_identifier=schema_file_identifier(reader.schema),
                    )
                    extracted.append(section)
                except Exception as exc:
                    bad_path = section_path.with_suffix(".invalid.arrow")
                    section_path.rename(bad_path)
                    print(f"[WARN] Section {section_index} is not a complete Arrow IPC file: {exc}", file=sys.stderr)

            report = {
                "input": str(input_path),
                "input_size": len(mm),
                "leading_signature_ok": mm[:8] == POD5_SIGNATURE,
                "trailing_signature_ok": mm[-8:] == POD5_SIGNATURE,
                "section_marker": marker.hex(),
                "section_marker_positions": positions,
                "last_nonzero_offset": nonzero_end - 1,
                "zero_tail_bytes": len(mm) - nonzero_end,
                "valid_sections": [
                    {
                        "index": s.index,
                        "start": s.start,
                        "end": s.end,
                        "bytes": s.end - s.start,
                        "path": str(s.path),
                        "table_type": s.table_type,
                        "rows": s.rows,
                        "batches": s.batches,
                        "columns": s.columns,
                        "file_identifier": s.file_identifier,
                    }
                    for s in extracted
                ],
            }
            (work_dir / "section_report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        finally:
            mm.close()

    return extracted


class BatchRowAccessor:
    def __init__(self, reader: ipc.RecordBatchFileReader):
        self.reader = reader
        self.starts: List[int] = []
        total = 0
        for index in range(reader.num_record_batches):
            self.starts.append(total)
            total += reader.get_batch(index).num_rows
        self.total_rows = total
        self._cached_index: Optional[int] = None
        self._cached_batch: Optional[pa.RecordBatch] = None

    def get(self, row_index: int) -> Tuple[pa.RecordBatch, int]:
        if row_index < 0 or row_index >= self.total_rows:
            raise IndexError(f"Signal row {row_index} outside 0..{self.total_rows - 1}")
        batch_index = bisect.bisect_right(self.starts, row_index) - 1
        if batch_index != self._cached_index:
            self._cached_batch = self.reader.get_batch(batch_index)
            self._cached_index = batch_index
        assert self._cached_batch is not None
        return self._cached_batch, row_index - self.starts[batch_index]


def run_info_from_batch(batch: pa.RecordBatch, row: int) -> p5.RunInfo:
    return p5.RunInfo(
        acquisition_id=str(scalar_value(batch, "acquisition_id", row, "")),
        acquisition_start_time=datetime_value(scalar_value(batch, "acquisition_start_time", row)),
        adc_max=int(scalar_value(batch, "adc_max", row, 0)),
        adc_min=int(scalar_value(batch, "adc_min", row, 0)),
        context_tags=dict_value(scalar_value(batch, "context_tags", row, {})),
        experiment_name=str(scalar_value(batch, "experiment_name", row, "")),
        flow_cell_id=str(scalar_value(batch, "flow_cell_id", row, "")),
        flow_cell_product_code=str(scalar_value(batch, "flow_cell_product_code", row, "")),
        protocol_name=str(scalar_value(batch, "protocol_name", row, "")),
        protocol_run_id=str(scalar_value(batch, "protocol_run_id", row, "")),
        protocol_start_time=datetime_value(scalar_value(batch, "protocol_start_time", row)),
        sample_id=str(scalar_value(batch, "sample_id", row, "")),
        sample_rate=int(scalar_value(batch, "sample_rate", row, 0)),
        sequencing_kit=str(scalar_value(batch, "sequencing_kit", row, "")),
        sequencer_position=str(scalar_value(batch, "sequencer_position", row, "")),
        sequencer_position_type=str(scalar_value(batch, "sequencer_position_type", row, "")),
        software=str(scalar_value(batch, "software", row, "")),
        system_name=str(scalar_value(batch, "system_name", row, "")),
        system_type=str(scalar_value(batch, "system_type", row, "")),
        tracking_id=dict_value(scalar_value(batch, "tracking_id", row, {})),
    )


def load_run_infos_from_arrow(path: Path) -> Dict[str, p5.RunInfo]:
    reader = open_arrow_file(path)
    result: Dict[str, p5.RunInfo] = {}
    for batch_index in range(reader.num_record_batches):
        batch = reader.get_batch(batch_index)
        for row in range(batch.num_rows):
            run_info = run_info_from_batch(batch, row)
            result[run_info.acquisition_id] = run_info
    return result


def load_run_infos_from_donor(path: Path) -> Dict[str, p5.RunInfo]:
    with p5.Reader(path) as reader:
        table = reader.run_info_table
        result: Dict[str, p5.RunInfo] = {}
        for batch_index in range(table.num_record_batches):
            batch = table.get_batch(batch_index)
            for row in range(batch.num_rows):
                run_info = run_info_from_batch(batch, row)
                result[run_info.acquisition_id] = run_info
        return result


def make_end_reason(name: str, forced: bool) -> p5.EndReason:
    key = str(name or "unknown").upper()
    reason_enum = p5.EndReasonEnum.__members__.get(key, p5.EndReasonEnum.UNKNOWN)
    return p5.EndReason(reason=reason_enum, forced=bool(forced))


def common_read_kwargs(batch: pa.RecordBatch, row: int, run_info: p5.RunInfo) -> Dict[str, Any]:
    return {
        "read_id": uuid_value(scalar_value(batch, "read_id", row)),
        "pore": p5.Pore(
            channel=int(scalar_value(batch, "channel", row, 0)),
            well=int(scalar_value(batch, "well", row, 0)),
            pore_type=str(scalar_value(batch, "pore_type", row, "")),
        ),
        "calibration": p5.Calibration(
            offset=float(scalar_value(batch, "calibration_offset", row, 0.0)),
            scale=float(scalar_value(batch, "calibration_scale", row, 1.0)),
        ),
        "read_number": int(scalar_value(batch, "read_number", row, 0)),
        "start_sample": int(scalar_value(batch, "start", row, 0)),
        "median_before": float(scalar_value(batch, "median_before", row, float("nan"))),
        "end_reason": make_end_reason(
            str(scalar_value(batch, "end_reason", row, "unknown")),
            bool(scalar_value(batch, "end_reason_forced", row, False)),
        ),
        "run_info": run_info,
        "num_minknow_events": int(scalar_value(batch, "num_minknow_events", row, 0)),
        "tracked_scaling": ShiftScalePair(
            shift=float(scalar_value(batch, "tracked_scaling_shift", row, float("nan"))),
            scale=float(scalar_value(batch, "tracked_scaling_scale", row, float("nan"))),
        ),
        "predicted_scaling": ShiftScalePair(
            shift=float(scalar_value(batch, "predicted_scaling_shift", row, float("nan"))),
            scale=float(scalar_value(batch, "predicted_scaling_scale", row, float("nan"))),
        ),
        "num_reads_since_mux_change": int(
            scalar_value(batch, "num_reads_since_mux_change", row, 0)
        ),
        "time_since_mux_change": float(
            scalar_value(batch, "time_since_mux_change", row, 0.0)
        ),
        "open_pore_level": float(
            scalar_value(batch, "open_pore_level", row, float("nan"))
        ),
    }


def as_compressed_chunk(value: Any) -> np.ndarray:
    if isinstance(value, np.ndarray):
        return np.asarray(value, dtype=np.uint8)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(value), dtype=np.uint8)
    raise TypeError(f"Signal value is not a compressed byte array: {type(value).__name__}")


def as_uncompressed_chunk(value: Any) -> np.ndarray:
    return np.asarray(value, dtype=np.int16)


def rebuild(
    reads_path: Path,
    signal_path: Path,
    run_infos: Dict[str, p5.RunInfo],
    output_path: Path,
    error_log: Path,
    max_reads: Optional[int],
    validate_signal: bool,
) -> Tuple[int, int]:
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output_path}")

    reads_reader = open_arrow_file(reads_path)
    signal_reader = open_arrow_file(signal_path)
    signal_rows = BatchRowAccessor(signal_reader)

    signal_field = signal_reader.schema.field("signal")
    extension_name = getattr(signal_field.type, "extension_name", None)
    storage_type = getattr(signal_field.type, "storage_type", signal_field.type)
    is_compressed = extension_name == "minknow.vbz" or pa.types.is_binary(storage_type) or pa.types.is_large_binary(storage_type)

    kept = 0
    skipped = 0
    with error_log.open("w", encoding="utf-8") as log, p5.Writer(
        output_path, software_name="pod5-section-salvage"
    ) as writer:
        log.write("read_id\treason\n")
        stop = False
        for batch_index in range(reads_reader.num_record_batches):
            batch = reads_reader.get_batch(batch_index)
            for row in range(batch.num_rows):
                read_id_for_log = "unknown"
                try:
                    read_id = uuid_value(scalar_value(batch, "read_id", row))
                    read_id_for_log = str(read_id)
                    acquisition_id = str(scalar_value(batch, "run_info", row, ""))
                    if acquisition_id not in run_infos:
                        raise KeyError(f"Run Info acquisition_id not found: {acquisition_id}")

                    references = scalar_value(batch, "signal", row, [])
                    if references is None:
                        references = []
                    references = [int(value) for value in references]
                    if not references:
                        raise ValueError("Read has no signal row references")

                    chunks: List[np.ndarray] = []
                    chunk_lengths: List[int] = []
                    for signal_index in references:
                        signal_batch, signal_row = signal_rows.get(signal_index)
                        signal_read_id = uuid_value(
                            scalar_value(signal_batch, "read_id", signal_row)
                        )
                        if signal_read_id != read_id:
                            raise ValueError(
                                f"Signal row {signal_index} belongs to {signal_read_id}, not {read_id}"
                            )
                        sample_count = int(
                            scalar_value(signal_batch, "samples", signal_row, 0)
                        )
                        signal_value = scalar_value(signal_batch, "signal", signal_row)
                        if is_compressed:
                            compressed_chunk = as_compressed_chunk(signal_value)
                            if validate_signal:
                                decoded = vbz_decompress_signal(compressed_chunk, sample_count)
                                if len(decoded) != sample_count:
                                    raise ValueError(
                                        f"Decoded chunk length {len(decoded)} != samples {sample_count}"
                                    )
                            chunks.append(compressed_chunk)
                        else:
                            chunk = as_uncompressed_chunk(signal_value)
                            if len(chunk) != sample_count:
                                raise ValueError(
                                    f"Uncompressed chunk length {len(chunk)} != samples {sample_count}"
                                )
                            chunks.append(chunk)
                        chunk_lengths.append(sample_count)

                    expected_samples = int(
                        scalar_value(batch, "num_samples", row, sum(chunk_lengths))
                    )
                    if sum(chunk_lengths) != expected_samples:
                        raise ValueError(
                            f"Signal sample total {sum(chunk_lengths)} != reads.num_samples {expected_samples}"
                        )

                    kwargs = common_read_kwargs(batch, row, run_infos[acquisition_id])
                    if is_compressed:
                        rebuilt_read = p5.CompressedRead(
                            **kwargs,
                            signal_chunks=chunks,
                            signal_chunk_lengths=chunk_lengths,
                        )
                    else:
                        rebuilt_read = p5.Read(
                            **kwargs,
                            signal=np.concatenate(chunks).astype(np.int16, copy=False),
                        )
                    writer.add_read(rebuilt_read)
                    kept += 1

                    if max_reads is not None and kept >= max_reads:
                        stop = True
                        break
                except Exception as exc:
                    skipped += 1
                    log.write(f"{read_id_for_log}\t{type(exc).__name__}: {exc}\n")
            if stop:
                break

    return kept, skipped


def choose_table(sections: Sequence[ExtractedSection], table_type: str) -> Optional[ExtractedSection]:
    matches = [section for section in sections if section.table_type == table_type]
    if not matches:
        return None
    # Prefer the candidate containing the largest number of rows.
    return max(matches, key=lambda section: (section.rows, section.end - section.start))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Best-effort POD5 reconstruction from intact embedded Arrow IPC sections"
    )
    parser.add_argument("input", type=Path, help="Damaged POD5 file; never use the source RAID device")
    parser.add_argument("output", type=Path, help="New POD5 file to create")
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Directory for extracted Arrow sections and reports",
    )
    parser.add_argument(
        "--donor-pod5",
        type=Path,
        help="Optional intact POD5 from the same acquisition, used only to supply missing Run Info rows",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Extract and classify sections but do not write a new POD5",
    )
    parser.add_argument(
        "--max-reads",
        type=int,
        help="Stop after writing this many valid reads; useful for a small test",
    )
    parser.add_argument(
        "--validate-signal",
        action="store_true",
        help="Decompress every VBZ signal chunk before accepting a read; slower but strongly recommended",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    work_dir = args.work_dir.resolve()

    sections = extract_sections(input_path, work_dir)
    for section in sections:
        print(
            f"section={section.index} type={section.table_type} rows={section.rows} "
            f"batches={section.batches} path={section.path}"
        )

    reads_section = choose_table(sections, "reads")
    signal_section = choose_table(sections, "signal")
    run_info_section = choose_table(sections, "run_info")

    print(f"reads_table={reads_section.path if reads_section else 'MISSING'}")
    print(f"signal_table={signal_section.path if signal_section else 'MISSING'}")
    print(f"run_info_table={run_info_section.path if run_info_section else 'MISSING'}")

    if args.inspect_only:
        return 0

    if reads_section is None or signal_section is None:
        raise SalvageError(
            "A complete Reads table and Signal table were not both found. "
            "This script cannot reconstruct truncated Arrow record batches."
        )

    run_infos: Dict[str, p5.RunInfo] = {}
    if run_info_section is not None:
        run_infos.update(load_run_infos_from_arrow(run_info_section.path))
    if args.donor_pod5 is not None:
        run_infos.update(load_run_infos_from_donor(args.donor_pod5.resolve()))
    if not run_infos:
        raise SalvageError(
            "No Run Info rows were recovered. Supply an intact same-acquisition POD5 with --donor-pod5."
        )

    error_log = work_dir / "skipped_reads.tsv"
    kept, skipped = rebuild(
        reads_path=reads_section.path,
        signal_path=signal_section.path,
        run_infos=run_infos,
        output_path=output_path,
        error_log=error_log,
        max_reads=args.max_reads,
        validate_signal=args.validate_signal,
    )
    print(f"recovered_reads={kept}")
    print(f"skipped_reads={skipped}")
    print(f"output={output_path}")
    print(f"error_log={error_log}")
    if kept == 0:
        output_path.unlink(missing_ok=True)
        raise SalvageError("No complete reads could be reconstructed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
