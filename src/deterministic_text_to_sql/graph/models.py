"""Data models for schema graph paths and join steps."""

from pydantic import BaseModel, ConfigDict, Field


class JoinStep(BaseModel):
    """Represents a single step in a table join chain.

    Attributes:
        from_table: Source table name.
        to_table: Target table name being joined.
        join_pairs: List of (from_column, to_column) tuples defining the join equality condition.
        on_clause: Formatted SQL ON clause condition string
            (e.g. 'lineitem.l_orderkey = orders.o_orderkey').
    """

    model_config = ConfigDict(frozen=True)

    from_table: str
    to_table: str
    join_pairs: list[tuple[str, str]] = Field(default_factory=list)
    on_clause: str

    def __hash__(self) -> int:
        """Compute hash based on immutable tuple of join pairs and table names."""
        return hash((self.from_table, self.to_table, tuple(self.join_pairs), self.on_clause))
