from __future__ import annotations

from dataclasses import dataclass, field

Rect = tuple[float, float, float, float]  # points, top-left origin


@dataclass
class Table:
    rows: list[list[str]]
    bbox: Rect
    engine: str
    title: str = ""
    notes: str = ""  # "Sumber: ..." / "Catatan: ..." lines printed under the table
    merges: list[list[int]] = field(default_factory=list)  # [row, col, row_span, col_span]
    raw_rows: int = 0

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "bbox": list(self.bbox),
            "engine": self.engine,
            "title": self.title,
            "notes": self.notes,
            "merges": self.merges,
        }
