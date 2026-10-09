"""Narrow structural test data for output previews."""

from dataclasses import dataclass


@dataclass
class CropConfig:
    exec_output_crop_after_lines: int = 300
    exec_output_crop_after_chars: int = 65536
    exec_output_crop_after_bytes: int = 65536
    exec_output_crop_head_lines: int = 25
    exec_output_crop_tail_lines: int = 25
    exec_output_crop_archive_max_bytes: int = 16777216
