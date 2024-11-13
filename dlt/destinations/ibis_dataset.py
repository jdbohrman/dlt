"""TODO: merge with dataset.p and move ibis imports to libs/ibis after ibis helper from other PR is merged"""


from typing import Any, Generator, Sequence, Union, Tuple
from dlt.common.json import json

from contextlib import contextmanager
from dlt.common.destination.reference import (
    SupportsReadableRelation,
    SupportsReadableDataset,
    TDatasetType,
    TDestinationReferenceArg,
    Destination,
    JobClientBase,
    WithStateSync,
    DestinationClientDwhConfiguration,
)

from functools import partial

from dlt.common.schema.typing import TTableSchemaColumns
from dlt.destinations.sql_client import SqlClientBase, WithSqlClient
from dlt.common.schema import Schema
from dlt.common.exceptions import DltException

# TODO: move ibis dependencies to libs/ibis after ibis helper is merged
import ibis  # type: ignore
from ibis import Expr


# TODO: finish and validate dialect map
DIALECT_MAP = {
    "dlt.destinations.duckdb": "duckdb",
    "dlt.destinations.clickhouse": "clickhouse",
    "dlt.destinations.databricks": "databricks",
    "dlt.destinations.bigquery": "bigquery",
    "dlt.destinations.postgres": "postgres",
    "dlt.destinations.redshift": "redshift",
    "dlt.destinations.snowflake": "snowflake",
    "dlt.destinations.mssql": "tsql",
    "dlt.destinations.synapse": "tsql",
    "dlt.destinations.athena": "athena",
    "dlt.destinations.filesystem": "duckdb",
    # NOTE: this may or may not work
    "dlt.destinations.dremio": "postgres",
}

# TODO: make sure this is complete, not sure if precisions are needed here
# this is generated by anthropric for now
DATA_TYPE_MAP = {
    "text": "string",
    "double": "float64",
    "bool": "boolean",
    "timestamp": "timestamp",
    "bigint": "int64",
    "binary": "binary",
    "json": "string",  # Store JSON as string in ibis
    "decimal": "decimal",
    "wei": "int64",  # Wei is a large integer
    "date": "date",
    "time": "time",
}


class DatasetException(DltException):
    pass


class ReadableRelationHasQueryException(DatasetException):
    def __init__(self, attempted_change: str) -> None:
        msg = (
            "This readable relation was created with a provided sql query. You cannot change"
            f" {attempted_change}. Please change the orignal sql query."
        )
        super().__init__(msg)


class ReadableRelationUnknownColumnException(DatasetException):
    def __init__(self, column_name: str) -> None:
        msg = (
            f"The selected column {column_name} is not known in the dlt schema for this releation."
        )
        super().__init__(msg)


# TODO: provide ibis expression typing for the readable relation
class ReadableIbisRelation(SupportsReadableRelation):
    def __init__(
        self,
        *,
        readable_dataset: "ReadableIbisDataset",
        expression: Expr = None,
    ) -> None:
        """Create a lazy evaluated relation to for the dataset of a destination"""

        self._dataset = readable_dataset
        self._expression = expression

        # wire protocol functions
        self.df = self._wrap_func("df")  # type: ignore
        self.arrow = self._wrap_func("arrow")  # type: ignore
        self.fetchall = self._wrap_func("fetchall")  # type: ignore
        self.fetchmany = self._wrap_func("fetchmany")  # type: ignore
        self.fetchone = self._wrap_func("fetchone")  # type: ignore

        self.iter_df = self._wrap_iter("iter_df")  # type: ignore
        self.iter_arrow = self._wrap_iter("iter_arrow")  # type: ignore
        self.iter_fetch = self._wrap_iter("iter_fetch")  # type: ignore

    @property
    def sql_client(self) -> SqlClientBase[Any]:
        return self._dataset.sql_client

    @property
    def schema(self) -> Schema:
        return self._dataset.schema

    @property
    def query(self) -> Any:
        """build the query"""
        destination_type = self._dataset._destination.destination_type
        return ibis.to_sql(self._expression, dialect=DIALECT_MAP[destination_type])

    @property
    def columns_schema(self) -> TTableSchemaColumns:
        return self.compute_columns_schema()

    @columns_schema.setter
    def columns_schema(self, new_value: TTableSchemaColumns) -> None:
        raise NotImplementedError("columns schema in ReadableDBAPIRelation can only be computed")

    def compute_columns_schema(self) -> TTableSchemaColumns:
        """provide schema columns for the cursor, may be filtered by selected columns"""

        # TODO: enable column lineage tracing somehow
        return None

        from ibis.expr.operations import Field, UnboundTable  # type: ignore

        # get column names from expression schema
        column_names = self._expression.schema().names
        column_names = ["decimal_renamed"]

        # try to trace columns to original table columns
        def get_column_origin(column_name: str) -> Tuple[str, str]:
            column_expr = self._expression[column_name]
            # print(column_expr)

            return "Unknown origin", "Unknown column"

        for column_name in column_names:
            pass
            # print(column_name, get_column_origin(column_name))

    def _proxy_expression_method(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Proxy method calls to the underlying ibis expression, allowing to wrap the resulting expression in a new relation"""
        # Get the method from the expression
        method = getattr(self._expression, method_name)
        # if any of the args is a relation, we need to unwrap it
        unwrapped_args = [
            arg._expression if isinstance(arg, ReadableIbisRelation) else arg for arg in args
        ]
        # if any of the kwargs is a relation, we need to unwrap it
        unwrapped_kwargs = {
            k: v._expression if isinstance(v, ReadableIbisRelation) else v
            for k, v in kwargs.items()
        }
        # Call it with provided args
        result = method(*unwrapped_args, **unwrapped_kwargs)
        # If result is an ibis expression, wrap it in a new relation
        if isinstance(result, Expr):
            return self.__class__(readable_dataset=self._dataset, expression=result)
        # Otherwise return the raw result
        return result

    def __getattr__(self, name: str) -> Any:
        """Wrap all callable attributes of the expression"""
        if not hasattr(self._expression, name):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
        attr = getattr(self._expression, name)
        if not callable(attr):
            return attr
        return partial(self._proxy_expression_method, name)

    def __getitem__(self, columns: Union[str, Sequence[str]]) -> "SupportsReadableRelation":
        expr = self._expression[columns]
        return self.__class__(readable_dataset=self._dataset, expression=expr)

    @contextmanager
    def cursor(self) -> Generator[SupportsReadableRelation, Any, Any]:
        """Gets a DBApiCursor for the current relation"""
        with self.sql_client as client:
            # this hacky code is needed for mssql to disable autocommit, read iterators
            # will not work otherwise. in the future we should be able to create a readony
            # client which will do this automatically
            if hasattr(self.sql_client, "_conn") and hasattr(self.sql_client._conn, "autocommit"):
                self.sql_client._conn.autocommit = False
            with client.execute_query(self.query) as cursor:
                if columns_schema := self.columns_schema:
                    cursor.columns_schema = columns_schema
                yield cursor

    def _wrap_iter(self, func_name: str) -> Any:
        """wrap SupportsReadableRelation generators in cursor context"""

        def _wrap(*args: Any, **kwargs: Any) -> Any:
            with self.cursor() as cursor:
                yield from getattr(cursor, func_name)(*args, **kwargs)

        return _wrap

    def _wrap_func(self, func_name: str) -> Any:
        """wrap SupportsReadableRelation functions in cursor context"""

        def _wrap(*args: Any, **kwargs: Any) -> Any:
            with self.cursor() as cursor:
                return getattr(cursor, func_name)(*args, **kwargs)

        return _wrap

    # forward ibis methods defined on interface
    def limit(self, limit: int) -> "SupportsReadableRelation":
        """limit the result to 'limit' items"""
        return self._proxy_expression_method("limit", limit)  # type: ignore

    def head(self, limit: int = 5) -> "SupportsReadableRelation":
        """limit the result to 5 items by default"""
        return self._proxy_expression_method("head", limit)  # type: ignore

    def select(self, *columns: str) -> "SupportsReadableRelation":
        """set which columns will be selected"""
        return self._proxy_expression_method("select", *columns)  # type: ignore


class ReadableIbisDataset(SupportsReadableDataset):
    """Access to dataframes and arrowtables in the destination dataset via dbapi"""

    def __init__(
        self,
        destination: TDestinationReferenceArg,
        dataset_name: str,
        schema: Union[Schema, str, None] = None,
    ) -> None:
        self._destination = Destination.from_reference(destination)
        self._provided_schema = schema
        self._dataset_name = dataset_name
        self._sql_client: SqlClientBase[Any] = None
        self._schema: Schema = None  #

    @property
    def schema(self) -> Schema:
        self._ensure_client_and_schema()
        return self._schema

    @property
    def sql_client(self) -> SqlClientBase[Any]:
        self._ensure_client_and_schema()
        return self._sql_client

    def _destination_client(self, schema: Schema) -> JobClientBase:
        client_spec = self._destination.spec()
        if isinstance(client_spec, DestinationClientDwhConfiguration):
            client_spec._bind_dataset_name(
                dataset_name=self._dataset_name, default_schema_name=schema.name
            )
        return self._destination.client(schema, client_spec)

    def _ensure_client_and_schema(self) -> None:
        """Lazy load schema and client"""
        # full schema given, nothing to do
        if not self._schema and isinstance(self._provided_schema, Schema):
            self._schema = self._provided_schema

        # schema name given, resolve it from destination by name
        elif not self._schema and isinstance(self._provided_schema, str):
            with self._destination_client(Schema(self._provided_schema)) as client:
                if isinstance(client, WithStateSync):
                    stored_schema = client.get_stored_schema(self._provided_schema)
                    if stored_schema:
                        self._schema = Schema.from_stored_schema(json.loads(stored_schema.schema))

        # no schema name given, load newest schema from destination
        elif not self._schema:
            with self._destination_client(Schema(self._dataset_name)) as client:
                if isinstance(client, WithStateSync):
                    stored_schema = client.get_stored_schema()
                    if stored_schema:
                        self._schema = Schema.from_stored_schema(json.loads(stored_schema.schema))

        # default to empty schema with dataset name if nothing found
        if not self._schema:
            self._schema = Schema(self._dataset_name)

        # here we create the client bound to the resolved schema
        if not self._sql_client:
            destination_client = self._destination_client(self._schema)
            if isinstance(destination_client, WithSqlClient):
                self._sql_client = destination_client.sql_client
            else:
                raise Exception(
                    f"Destination {destination_client.config.destination_type} does not support"
                    " SqlClient."
                )

    def table(self, table_name: str) -> ReadableIbisRelation:
        # NOTE: to be able to create an unbound ibis table we need to access the schema
        # and if this is not present, this will not be fully lazy bc the dataset needs to be
        # queried to get the schema
        if table_name not in self.schema.tables:
            raise Exception(
                f"Table {table_name} not found in schema. Available tables:"
                f" {self.schema.tables.keys()}"
            )
        table_schema = self.schema.tables[table_name]

        # Convert dlt table schema columns to ibis schema
        ibis_schema = {
            col_name: DATA_TYPE_MAP[col_info.get("data_type", "string")]
            for col_name, col_info in table_schema.get("columns", {}).items()
        }

        # create unbound ibis table and return in dlt wrapper
        # NOTE: we can also add the dataset to the unbound table here, then the user could probably do cross
        # dataset joins with this on the same db
        unbound_table = ibis.table(schema=ibis_schema, name=table_name)
        return ReadableIbisRelation(readable_dataset=self, expression=unbound_table)  # type: ignore[abstract]

    def __getitem__(self, table_name: str) -> ReadableIbisRelation:
        """access of table via dict notation"""
        return self.table(table_name)

    def __getattr__(self, table_name: str) -> ReadableIbisRelation:
        """access of table via property notation"""
        return self.table(table_name)


def dataset(
    destination: TDestinationReferenceArg,
    dataset_name: str,
    schema: Union[Schema, str, None] = None,
    dataset_type: TDatasetType = "dbapi",
) -> ReadableIbisDataset:
    if dataset_type == "dbapi":
        return ReadableIbisDataset(destination, dataset_name, schema)  # type: ignore[abstract]
    raise NotImplementedError(f"Dataset of type {dataset_type} not implemented")
