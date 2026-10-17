"""Pydantic models for semantic schema metadata."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ColumnRole = Literal[
    "id",
    "dimension",
    "metric",
    "fk",
    "attribute",
    "filter",
    "date_filter",
]


class ColumnMetadata(BaseModel):
    """Metadata definition for a database column.

    Attributes:
        name: Physical column name.
        type: Data type string (e.g. 'INTEGER', 'VARCHAR(25)').
        description: Logical description of column meaning.
        role: Functional role of the column.
        agg: Supported aggregation functions if applicable (e.g. ['SUM', 'AVG']).
        values: Whitelist of allowed categorical values for filter binding.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: str
    description: str
    role: ColumnRole
    agg: list[str] | None = None
    values: list[str] | None = None


class ForeignKeyMetadata(BaseModel):
    """Foreign key reference metadata between tables.

    Attributes:
        column: Column name or list of column names in this table.
        ref_table: Referenced target table name.
        ref_column: Referenced column name or list of column names in target table.
    """

    model_config = ConfigDict(extra="ignore")

    column: str | list[str]
    ref_table: str
    ref_column: str | list[str]


class TableMetadata(BaseModel):
    """Metadata definition for a database table.

    Attributes:
        name: Physical table name.
        description: Logical description of the table.
        aliases: Natural language aliases or synonyms for slot mapping.
        primary_key: List of primary key column names.
        foreign_keys: List of foreign key relationships.
        columns: List of column metadata definitions.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyMetadata] = Field(default_factory=list)
    columns: list[ColumnMetadata] = Field(default_factory=list)

    def get_column(self, col_name: str) -> ColumnMetadata | None:
        """Look up a column by name with case-insensitive fallback.

        Args:
            col_name: Name of the column to find.

        Returns:
            ColumnMetadata if found, None otherwise.
        """
        for col in self.columns:
            if col.name == col_name:
                return col
        col_name_lower = col_name.lower()
        for col in self.columns:
            if col.name.lower() == col_name_lower:
                return col
        return None


class SchemaMetadata(BaseModel):
    """Top-level semantic schema metadata container.

    Attributes:
        title: Title of the schema definition.
        description: Description of the schema scope.
        tables: List of table metadata definitions.
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    description: str | None = None
    tables: list[TableMetadata] = Field(default_factory=list)

    def get_table(self, table_name: str) -> TableMetadata | None:
        """Look up a table by name or alias with case-insensitive fallback.

        Args:
            table_name: Table name or alias to find.

        Returns:
            TableMetadata if found, None otherwise.
        """
        # 1. Exact match on table name
        for tbl in self.tables:
            if tbl.name == table_name:
                return tbl
        # 2. Case-insensitive match on table name
        tbl_name_lower = table_name.lower()
        for tbl in self.tables:
            if tbl.name.lower() == tbl_name_lower:
                return tbl
        # 3. Exact match on aliases
        for tbl in self.tables:
            if table_name in tbl.aliases:
                return tbl
        # 4. Case-insensitive match on aliases
        for tbl in self.tables:
            for alias in tbl.aliases:
                if alias.lower() == tbl_name_lower:
                    return tbl
        return None
