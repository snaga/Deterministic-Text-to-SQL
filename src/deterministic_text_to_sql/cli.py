"""Typer and Rich powered Command Line Interface for Deterministic Text-to-SQL Engine.

Provides one-shot query execution, pure machine-readable JSON output for AI agents,
human-friendly interactive REPL mode with immediate HITL disambiguation prompts,
and schema inspection commands.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from deterministic_text_to_sql.engine import DeterministicTextToSQLEngine
from deterministic_text_to_sql.jev.client import JevClient
from deterministic_text_to_sql.models import EngineResult, EngineStatus

if sys.platform == "win32":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        with contextlib.suppress(Exception):
            sys.stderr.reconfigure(encoding="utf-8")

console = Console(safe_box=True)
err_console = Console(stderr=True, safe_box=True)


class DefaultCommandGroup(TyperGroup):
    """Custom TyperGroup that routes top-level arguments to the 'query' command.

    Allows running 'deterministic-text-to-sql "query"' directly without
    explicitly specifying the 'query' subcommand, while preserving subcommands
    such as 'interactive' and 'schema'.
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:  # type: ignore[override]
        """Intercept arguments to prepend 'query' command when applicable."""
        help_flags = {"--help", "-h", "--install-completion", "--show-completion"}
        should_route_to_query = not args or (
            args[0] not in self.commands and not any(arg in help_flags for arg in args)
        )
        if should_route_to_query:
            args.insert(0, "query")
        return super().parse_args(ctx, args)


app = typer.Typer(
    cls=DefaultCommandGroup,
    help="Deterministic Text-to-SQL Engine combining TypeSafe Jev and sqlglot.",
    no_args_is_help=False,
)


def resolve_schema_path(schema_arg: Path | None = None) -> Path:
    """Resolve schema file path from argument.

    Args:
        schema_arg: Optional user-supplied schema file path.

    Returns:
        Resolved absolute Path to schema file.

    Raises:
        FileNotFoundError: If schema file is not specified or does not exist.
    """
    if schema_arg is None:
        raise FileNotFoundError(
            "スキーマファイルが指定されていません。--schema <path> で "
            "Apache Ossie セマンティックモデル（YAML / JSON）を指定してください。"
            "（例: --schema examples/tpch/tpch_semantic_model.yaml）"
        )

    p = schema_arg.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Specified schema file does not exist: {schema_arg}")
    return p


def create_engine(
    schema_path: Path | None = None,
    api_key: str | None = None,
    jev_client: JevClient | None = None,
) -> DeterministicTextToSQLEngine:
    """Instantiate DeterministicTextToSQLEngine with resolved schema and client.

    Args:
        schema_path: Path to schema file (YAML or JSON).
        api_key: Optional TypeSafe API key.
        jev_client: Optional pre-configured JevClient instance.

    Returns:
        Configured DeterministicTextToSQLEngine instance.
    """
    resolved = resolve_schema_path(schema_path)
    return DeterministicTextToSQLEngine(
        schema=resolved,
        api_key=api_key,
        jev_client=jev_client,
    )


def _render_result_json(result: EngineResult) -> None:
    """Print pure machine-readable JSON output for AI agents without Rich markup.

    Args:
        result: EngineResult returned by the core engine.
    """
    data = result.model_dump(mode="json")
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _render_result_human(result: EngineResult, console_target: Console = console) -> None:
    """Render human-friendly Rich styled output for terminal users.

    Args:
        result: EngineResult returned by the core engine.
        console_target: Rich Console instance to print to.
    """
    if result.status == EngineStatus.SUCCESS:
        sql_content = result.sql or "-- No SQL generated"
        syntax = Syntax(sql_content, "sql", theme="monokai", line_numbers=False)
        sub_title = (
            f"Dialect: [cyan]{result.dialect}[/] | "
            f"Total Latency: [bold]{result.latency_ms.total:.1f}ms[/]"
        )
        console_target.print(
            Panel(
                syntax,
                title="[bold green]Generated Deterministic SQL[/]",
                subtitle=sub_title,
                border_style="green",
            )
        )
        latency_text = (
            f"[dim]Jev Inference: {result.latency_ms.jev_inference:.1f}ms | "
            f"AST Build: {result.latency_ms.ast_build:.1f}ms | "
            f"Total: {result.latency_ms.total:.1f}ms[/]"
        )
        console_target.print(latency_text)

    elif result.status == EngineStatus.HITL_REQUIRED:
        issue = result.hitl_issue
        options_table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold yellow",
        )
        options_table.add_column("Key", style="bold cyan", width=6)
        options_table.add_column("Choice", style="white")
        options_table.add_column("Context / Hint", style="dim")

        if issue:
            for opt in issue.options:
                options_table.add_row(opt.key, opt.label, opt.hint)

            question = issue.question
            rec_key = issue.options[0].key if issue.options else "A"
        else:
            question = "Clarification required."
            rec_key = "A"

        rerun_cmd1 = f'  [green]deterministic-text-to-sql "{result.query} / {rec_key}で"[/]\n'
        rerun_cmd2 = f'  [green]deterministic-text-to-sql "{result.query}" --clarify {rec_key}[/]'

        elements: list[Any] = [
            Text.from_markup(f"[bold white]Query:[/] [italic]{result.query}[/]\n"),
            Text.from_markup(f"[bold yellow]Question:[/] {question}\n"),
            options_table,
            Text.from_markup("\n[bold]To clarify and re-execute, use:[/]\n"),
            Text.from_markup(rerun_cmd1),
            Text.from_markup(rerun_cmd2),
        ]

        console_target.print(
            Panel(
                Group(*elements),
                title="[bold yellow][!] Human-in-the-Loop Disambiguation Required[/]",
                subtitle=f"Latency: {result.latency_ms.total:.1f}ms",
                border_style="yellow",
            )
        )

    else:
        err_msg = result.error_message or "Unknown engine error occurred."
        console_target.print(
            Panel(
                Text(f"Error: {err_msg}", style="bold red"),
                title="[bold red]Execution Failed[/]",
                border_style="red",
            )
        )


def _render_error_human(title: str, message: str, console_target: Console = err_console) -> None:
    """Render human-friendly Rich error panel for CLI exceptions.

    Args:
        title: Title string for the error panel.
        message: Error message body string.
        console_target: Target Rich console (defaults to err_console).
    """
    console_target.print(
        Panel(
            Text(message, style="bold red"),
            title=f"[bold red]{title}[/]",
            border_style="red",
        )
    )


@app.command(name="query")
def query_command(
    query: Annotated[
        str | None,
        typer.Argument(
            help="Natural language query string (supports slash syntax, e.g. 'Query / Aで').",
        ),
    ] = None,
    schema: Annotated[
        Path | None,
        typer.Option(
            "--schema",
            "-s",
            help="Path to semantic schema metadata file (YAML or JSON).",
        ),
    ] = None,
    dialect: Annotated[
        str,
        typer.Option(
            "--dialect",
            "-d",
            help="Target SQL dialect (postgres, duckdb, sqlite, snowflake, bigquery).",
        ),
    ] = "postgres",
    clarify: Annotated[
        str | None,
        typer.Option(
            "--clarify",
            "-c",
            help="Explicit HITL disambiguation key (e.g. 'A', 'B', 'lineitem').",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Bypass HITL ambiguity intervention and synthesize best-effort SQL.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json-output",
            "--json",
            help="Output pure structured JSON for AI agents (no terminal decoration).",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose diagnostics output.",
        ),
    ] = False,
) -> None:
    """Execute one-shot deterministic Text-to-SQL or launch interactive mode if query is omitted."""
    # If no query argument is passed, launch interactive REPL mode
    if query is None:
        interactive_command(schema=schema, dialect=dialect, verbose=verbose)
        return

    if verbose:
        console.print(f"[dim]Resolving schema and initializing engine (dialect={dialect})...[/]")

    try:
        engine = create_engine(schema_path=schema)
    except Exception as e:
        if json_output:
            err_dict = {
                "status": EngineStatus.ERROR.value,
                "query": query,
                "clarification": clarify,
                "dialect": dialect,
                "error_message": str(e),
            }
            sys.stdout.write(json.dumps(err_dict, indent=2, ensure_ascii=False) + "\n")
        else:
            _render_error_human("Failed to initialize engine", str(e))
        raise typer.Exit(code=1) from e

    try:
        result = engine.process_query(
            raw_query=query,
            dialect=dialect,
            clarify=clarify,
            force=force,
        )
    finally:
        engine.close()

    if json_output:
        _render_result_json(result)
    else:
        _render_result_human(result)

    if result.status == EngineStatus.SUCCESS:
        raise typer.Exit(code=0)
    if result.status == EngineStatus.HITL_REQUIRED:
        raise typer.Exit(code=2)
    raise typer.Exit(code=1)


@app.command(name="interactive")
def interactive_command(
    schema: Annotated[
        Path | None,
        typer.Option(
            "--schema",
            "-s",
            help="Path to semantic schema metadata file (YAML or JSON).",
        ),
    ] = None,
    dialect: Annotated[
        str,
        typer.Option(
            "--dialect",
            "-d",
            help="Target SQL dialect (postgres, duckdb, sqlite, snowflake, bigquery).",
        ),
    ] = "postgres",
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose diagnostics.",
        ),
    ] = False,
) -> None:
    """Start an interactive human-in-the-loop REPL session."""
    if verbose:
        console.print("[dim]Verbose diagnostics mode activated.[/]")

    try:
        engine = create_engine(schema_path=schema)
    except Exception as e:
        _render_error_human("Failed to initialize engine", str(e))
        raise typer.Exit(code=1) from e

    banner = (
        "[bold cyan]Deterministic Text-to-SQL Interactive REPL[/]\n"
        f"[dim]Dialect: [white]{dialect}[/] | Schema: [white]{engine.schema.title}[/] | "
        "Type 'exit', 'quit', or 'q' to quit.[/]"
    )
    console.print(Panel(banner, border_style="cyan"))

    last_query: str | None = None
    last_hitl_issue_present: bool = False

    try:
        while True:
            try:
                user_input = Prompt.ask("\n[bold cyan]SQL>[/]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Session closed. Goodbye![/]")
                break

            cleaned = user_input.strip()
            if not cleaned:
                continue

            if cleaned.lower() in ("exit", "quit", "q"):
                console.print("[dim]Goodbye![/]")
                break

            # Handle follow-up disambiguation if last query was ambiguous
            effective_query = cleaned
            if last_hitl_issue_present and last_query:
                # If user entered something like 'A', 'Aで', or '/ A', bind with previous query
                if not cleaned.startswith("/") and len(cleaned) <= 10:
                    effective_query = f"{last_query} / {cleaned}"
                elif cleaned.startswith("/"):
                    effective_query = f"{last_query} {cleaned}"

            result = engine.process_query(effective_query, dialect=dialect)

            if result.status == EngineStatus.SUCCESS:
                _render_result_human(result)
                last_query = None
                last_hitl_issue_present = False

            elif result.status == EngineStatus.HITL_REQUIRED:
                _render_result_human(result)
                last_query = result.query
                last_hitl_issue_present = True

                # Prompt immediately for choice to resolve ambiguity without re-entering query
                issue = result.hitl_issue
                if issue and issue.options:
                    valid_keys = [opt.key.upper() for opt in issue.options]
                    default_choice = valid_keys[0]
                    prompt_label = (
                        f"[bold yellow]曖昧性を解消する選択肢を入力してください "
                        f"({'/'.join(valid_keys)} / skip: Enter)[/]"
                    )
                    try:
                        choice = Prompt.ask(
                            prompt_label,
                            default="",
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        console.print("\n[dim]Disambiguation cancelled.[/]")
                        last_hitl_issue_present = False
                        continue

                    # If empty, fallback to recommended choice if user pressed Enter
                    # or leave for next prompt
                    if choice:
                        resolved_res = engine.process_query(
                            result.query, dialect=dialect, clarify=choice
                        )
                        _render_result_human(resolved_res)
                        if resolved_res.status == EngineStatus.SUCCESS:
                            last_query = None
                            last_hitl_issue_present = False
                    elif default_choice:
                        # User can type in subsequent turn or press enter to keep session
                        pass
            else:
                _render_result_human(result)
                last_query = None
                last_hitl_issue_present = False

    finally:
        engine.close()


@app.command(name="schema")
def schema_command(
    schema: Annotated[
        Path | None,
        typer.Option(
            "--schema",
            "-s",
            help="Path to semantic schema metadata file (YAML or JSON).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json-output",
            "--json",
            help="Output schema metadata in pure JSON format for AI agents.",
        ),
    ] = False,
) -> None:
    """Inspect database schema metadata, tables, columns, roles, and relationships."""
    try:
        engine = create_engine(schema_path=schema)
    except Exception as e:
        if json_output:
            err_dict = {
                "status": "error",
                "error_message": str(e),
                "error": str(e),
            }
            sys.stdout.write(json.dumps(err_dict, indent=2, ensure_ascii=False) + "\n")
        else:
            _render_error_human("Failed to load schema", str(e))
        raise typer.Exit(code=1) from e

    try:
        info = engine.get_schema_info()
    finally:
        engine.close()

    if json_output:
        sys.stdout.write(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        raise typer.Exit(code=0)

    header = f"[bold white]{info['title']}[/]\n[dim]{info.get('description', '')}[/]"
    console.print(
        Panel(header, title="[bold cyan]Database Schema Inspector[/]", border_style="cyan")
    )

    tables = info.get("tables", [])
    for tbl in tables:
        tbl_name = tbl["name"]
        tbl_desc = tbl.get("description", "")
        pk_cols = ", ".join(tbl.get("primary_key", []))
        fks = tbl.get("foreign_keys", [])

        table_view = Table(
            title=f"Table: [bold cyan]{tbl_name}[/] [dim]({tbl_desc})[/]",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold magenta",
        )
        table_view.add_column("Column", style="bold white")
        table_view.add_column("Type", style="dim cyan")
        table_view.add_column("Role", style="green")
        table_view.add_column("Description", style="white")

        for col in tbl.get("columns", []):
            role_display = col["role"]
            if col["name"] in pk_cols:
                role_display = f"[bold yellow]PK[/] | {role_display}"
            table_view.add_row(
                col["name"],
                col["type"],
                role_display,
                col.get("description", ""),
            )

        console.print(table_view)

        if fks:
            fk_lines: list[str] = []
            for fk in fks:
                cols = fk["column"] if isinstance(fk["column"], list) else [fk["column"]]
                ref_cols = (
                    fk["ref_column"]
                    if isinstance(fk["ref_column"], list)
                    else [fk["ref_column"]]
                )
                cols_str = ", ".join(cols)
                ref_cols_str = ", ".join(ref_cols)
                fk_lines.append(
                    f"  • {cols_str} → [bold cyan]{fk['ref_table']}[/]({ref_cols_str})"
                )
            console.print(
                Text.from_markup("[dim]Foreign Keys:\n" + "\n".join(fk_lines) + "[/]\n")
            )

    # Semantic Metrics
    metrics = info.get("metrics", [])
    if metrics:
        metrics_table = Table(
            title="[bold green]Semantic Metrics[/]",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold green",
        )
        metrics_table.add_column("Metric Name", style="bold white")
        metrics_table.add_column("Expression", style="cyan", overflow="fold")
        metrics_table.add_column("Description", style="white", overflow="fold")
        metrics_table.add_column("Synonyms", style="dim", overflow="fold")

        for m in metrics:
            syns = ", ".join(m.get("synonyms", []))
            metrics_table.add_row(
                m["name"],
                m["expression"],
                m.get("description", ""),
                syns,
            )
        console.print(metrics_table)

    # Semantic Dimensions
    dimensions = info.get("dimensions", [])
    if dimensions:
        dim_table = Table(
            title="[bold magenta]Semantic Dimensions[/]",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold magenta",
        )
        dim_table.add_column("Dimension Name", style="bold white")
        dim_table.add_column("Expression", style="cyan", overflow="fold")
        dim_table.add_column("Time Granularity", style="yellow")
        dim_table.add_column("Synonyms", style="dim", overflow="fold")
        dim_table.add_column("Description", style="white", overflow="fold")

        for d in dimensions:
            syns = ", ".join(d.get("synonyms", []))
            time_gran = d.get("time_granularity") or ""
            dim_table.add_row(
                d["name"],
                d.get("expression") or "",
                time_gran,
                syns,
                d.get("description", ""),
            )
        console.print(dim_table)

    # Ontology Hierarchies
    hierarchies = info.get("hierarchies", [])
    if hierarchies:
        hier_table = Table(
            title="[bold yellow]Ontology Hierarchies[/]",
            box=box.SIMPLE_HEAVY,
            show_header=True,
            header_style="bold yellow",
        )
        hier_table.add_column("Parent", style="bold white")
        hier_table.add_column("Child", style="bold white")
        hier_table.add_column("Mapping Count", style="dim")

        for h in hierarchies:
            parent_repr = f"{h['parent_table']}.{h['parent_column']}"
            child_repr = f"{h['child_table']}.{h['child_column']}"
            mapping_count = str(len(h.get("child_to_parent", {})))
            hier_table.add_row(parent_repr, child_repr, mapping_count)
        console.print(hier_table)

    raise typer.Exit(code=0)


def main() -> None:
    """CLI entrypoint callable."""
    app()


if __name__ == "__main__":
    main()
