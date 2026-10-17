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
        synonyms: Natural language aliases or synonyms for slot mapping.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: str
    description: str
    role: ColumnRole
    agg: list[str] | None = None
    values: list[str] | None = None
    synonyms: list[str] = Field(default_factory=list)


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
        aliases: Natural language aliases for slot mapping.
        synonyms: Natural language synonyms for slot mapping (Ossie semantic model).
        primary_key: List of primary key column names.
        foreign_keys: List of foreign key relationships.
        columns: List of column metadata definitions.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyMetadata] = Field(default_factory=list)
    columns: list[ColumnMetadata] = Field(default_factory=list)

    def find_column_by_synonym(self, synonym: str) -> ColumnMetadata | None:
        """Look up a column by synonym with case-insensitive fallback.

        Args:
            synonym: Synonym of the column to find.

        Returns:
            ColumnMetadata if found, None otherwise.
        """
        for col in self.columns:
            if synonym in col.synonyms:
                return col
        synonym_lower = synonym.lower()
        for col in self.columns:
            for s in col.synonyms:
                if s.lower() == synonym_lower:
                    return col
        return None

    def get_column(self, col_name: str) -> ColumnMetadata | None:
        """Look up a column by name or synonym with case-insensitive fallback.

        Args:
            col_name: Name or synonym of the column to find.

        Returns:
            ColumnMetadata if found, None otherwise.
        """
        # 1. Exact match on column name
        for col in self.columns:
            if col.name == col_name:
                return col
        # 2. Case-insensitive match on column name
        col_name_lower = col_name.lower()
        for col in self.columns:
            if col.name.lower() == col_name_lower:
                return col
        # 3. Fallback to synonym search
        return self.find_column_by_synonym(col_name)


class MetricDefinition(BaseModel):
    """Semantic metric definition with calculation expression and synonyms.

    Attributes:
        name: Unique identifier of the semantic metric.
        expression: ANSI SQL aggregation expression
            (e.g. 'SUM(l_extendedprice * (1 - l_discount))').
        description: Logical description of the metric.
        datatype: Return SQL data type of the metric (e.g. 'DOUBLE', 'BIGINT').
        synonyms: Natural language aliases or synonyms for slot mapping.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    expression: str
    description: str = ""
    datatype: str = "DOUBLE"
    synonyms: list[str] = Field(default_factory=list)


class HierarchyRule(BaseModel):
    """Hierarchical ontology rule defining parent-child concept relationships.

    Attributes:
        parent_table: Table name of parent concept (e.g. 'region').
        parent_column: Column name of parent concept (e.g. 'r_name').
        child_table: Table name of child concept (e.g. 'nation').
        child_column: Column name of child concept (e.g. 'n_name').
        child_to_parent: Value mapping from child entity to parent entity (e.g. {'JAPAN': 'ASIA'}).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    parent_table: str
    parent_column: str
    child_table: str
    child_column: str
    child_to_parent: dict[str, str] = Field(default_factory=dict)


class SchemaMetadata(BaseModel):
    """Top-level semantic schema metadata container.

    Attributes:
        title: Title of the schema definition.
        description: Description of the schema scope.
        tables: List of table metadata definitions.
        metrics: List of semantic metric definitions.
        hierarchies: List of hierarchical ontology rules.
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    description: str | None = None
    tables: list[TableMetadata] = Field(default_factory=list)
    metrics: list[MetricDefinition] = Field(default_factory=list)
    hierarchies: list[HierarchyRule] = Field(default_factory=list)

    def get_table(self, table_name: str) -> TableMetadata | None:
        """Look up a table by name, alias, or synonym with case-insensitive fallback.

        Args:
            table_name: Table name, alias, or synonym to find.

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
        # 3. Exact match on aliases or synonyms
        for tbl in self.tables:
            if table_name in tbl.aliases or table_name in tbl.synonyms:
                return tbl
        # 4. Case-insensitive match on aliases or synonyms
        for tbl in self.tables:
            for alias in tbl.aliases:
                if alias.lower() == tbl_name_lower:
                    return tbl
            for syn in tbl.synonyms:
                if syn.lower() == tbl_name_lower:
                    return tbl
        return None

    def get_metric(self, name_or_synonym: str) -> MetricDefinition | None:
        """Look up a metric by name or synonym with case-insensitive fallback.

        Args:
            name_or_synonym: Metric name or synonym to find.

        Returns:
            MetricDefinition if found, None otherwise.
        """
        # 1. Exact match on metric name
        for m in self.metrics:
            if m.name == name_or_synonym:
                return m
        # 2. Case-insensitive match on metric name
        target_lower = name_or_synonym.lower()
        for m in self.metrics:
            if m.name.lower() == target_lower:
                return m
        # 3. Exact match on synonyms
        for m in self.metrics:
            if name_or_synonym in m.synonyms:
                return m
        # 4. Case-insensitive match on synonyms
        for m in self.metrics:
            for syn in m.synonyms:
                if syn.lower() == target_lower:
                    return m
        return None
