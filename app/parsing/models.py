"""The contract between the parsers and the repository."""

from pydantic import BaseModel, Field, field_validator


class ParsedEntry(BaseModel):
    """One expense extracted from a message, not yet persisted."""

    amount: int = Field(gt=0, description="minor units -- rupiah, never a float")
    currency: str = "IDR"
    category: str = "Uncategorized"
    note: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value):
        """LLMs happily return 45000.0 or "45000". Money stays an int."""
        if isinstance(value, str):
            value = value.replace(",", "").replace("_", "").strip()
        return int(round(float(value)))

    @field_validator("note", "category", mode="before")
    @classmethod
    def _blank_if_none(cls, value):
        return "" if value is None else str(value).strip()


class ParseResult(BaseModel):
    entries: list[ParsedEntry] = []
    parser: str = "regex"

    @property
    def ok(self) -> bool:
        return bool(self.entries)

    @property
    def min_confidence(self) -> float:
        return min((e.confidence for e in self.entries), default=0.0)

    @property
    def total(self) -> int:
        return sum(e.amount for e in self.entries)
