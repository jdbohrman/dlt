import posixpath
import os
from types import TracebackType
from typing import ClassVar, List, Type, Iterable, Set, Iterator, Optional
from fsspec import AbstractFileSystem
from contextlib import contextmanager
from dlt.common import json, pendulum
from dlt.common.typing import DictStrAny

import re

from dlt.common import logger
from dlt.common.schema import Schema, TSchemaTables, TTableSchema
from dlt.common.storages import FileStorage, ParsedLoadJobFileName, fsspec_from_config
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.reference import (
    NewLoadJob,
    TLoadJobState,
    LoadJob,
    JobClientBase,
    FollowupJob,
    WithStagingDataset,
    WithStateSync,
    StorageSchemaInfo,
    StateInfo,
    DoNothingJob,
)
from dlt.common.destination.exceptions import DestinationUndefinedEntity
from dlt.destinations.job_impl import EmptyLoadJob
from dlt.destinations.impl.filesystem import capabilities
from dlt.destinations.impl.filesystem.configuration import FilesystemDestinationClientConfiguration
from dlt.destinations.job_impl import NewReferenceJob
from dlt.destinations import path_utils


class LoadFilesystemJob(LoadJob):
    def __init__(
        self,
        local_path: str,
        dataset_path: str,
        *,
        config: FilesystemDestinationClientConfiguration,
        schema_name: str,
        load_id: str,
    ) -> None:
        file_name = FileStorage.get_file_name_from_file_path(local_path)
        self.config = config
        self.dataset_path = dataset_path
        self.destination_file_name = LoadFilesystemJob.make_destination_filename(
            config.layout, file_name, schema_name, load_id
        )

        super().__init__(file_name)
        fs_client, _ = fsspec_from_config(config)
        self.destination_file_name = LoadFilesystemJob.make_destination_filename(
            config.layout, file_name, schema_name, load_id
        )
        item = self.make_remote_path()
        fs_client.put_file(local_path, item)

    @staticmethod
    def make_destination_filename(
        layout: str, file_name: str, schema_name: str, load_id: str
    ) -> str:
        job_info = ParsedLoadJobFileName.parse(file_name)
        return path_utils.create_path(
            layout,
            schema_name=schema_name,
            table_name=job_info.table_name,
            load_id=load_id,
            file_id=job_info.file_id,
            ext=job_info.file_format,
        )

    def make_remote_path(self) -> str:
        return (
            f"{self.config.protocol}://{posixpath.join(self.dataset_path, self.destination_file_name)}"
        )

    def state(self) -> TLoadJobState:
        return "completed"

    def exception(self) -> str:
        raise NotImplementedError()


class FollowupFilesystemJob(FollowupJob, LoadFilesystemJob):
    def create_followup_jobs(self, final_state: TLoadJobState) -> List[NewLoadJob]:
        jobs = super().create_followup_jobs(final_state)
        if final_state == "completed":
            ref_job = NewReferenceJob(
                file_name=self.file_name(), status="running", remote_path=self.make_remote_path()
            )
            jobs.append(ref_job)
        return jobs


class FilesystemClient(JobClientBase, WithStagingDataset, WithStateSync):
    """filesystem client storing jobs in memory"""

    capabilities: ClassVar[DestinationCapabilitiesContext] = capabilities()
    fs_client: AbstractFileSystem
    fs_path: str

    def __init__(self, schema: Schema, config: FilesystemDestinationClientConfiguration) -> None:
        super().__init__(schema, config)
        self.fs_client, self.fs_path = fsspec_from_config(config)
        self.config: FilesystemDestinationClientConfiguration = config
        # verify files layout. we need {table_name} and only allow {schema_name} before it, otherwise tables
        # cannot be replaced and we cannot initialize folders consistently
        self.table_prefix_layout = path_utils.get_table_prefix_layout(config.layout)
        self._dataset_path = self.config.normalize_dataset_name(self.schema)

    def drop_storage(self) -> None:
        if self.is_storage_initialized():
            self.fs_client.rm(self.dataset_path, recursive=True)

    @property
    def dataset_path(self) -> str:
        return posixpath.join(self.fs_path, self._dataset_path)

    @contextmanager
    def with_staging_dataset(self) -> Iterator["FilesystemClient"]:
        current_dataset_path = self._dataset_path
        try:
            self._dataset_path = self.schema.naming.normalize_table_identifier(
                current_dataset_path + "_staging"
            )
            yield self
        finally:
            # restore previous dataset name
            self._dataset_path = current_dataset_path

    def initialize_storage(self, truncate_tables: Iterable[str] = None) -> None:
        # clean up existing files for tables selected for truncating
        if truncate_tables and self.fs_client.isdir(self.dataset_path):
            # get all dirs with table data to delete. the table data are guaranteed to be files in those folders
            # TODO: when we do partitioning it is no longer the case and we may remove folders below instead
            truncated_dirs = self._get_table_dirs(truncate_tables)
            # print(f"TRUNCATE {truncated_dirs}")
            truncate_prefixes: Set[str] = set()
            for table in truncate_tables:
                table_prefix = self.table_prefix_layout.format(
                    schema_name=self.schema.name, table_name=table
                )
                truncate_prefixes.add(posixpath.join(self.dataset_path, table_prefix))
            # print(f"TRUNCATE PREFIXES {truncate_prefixes} on {truncate_tables}")

            for truncate_dir in truncated_dirs:
                # get files in truncate dirs
                # NOTE: glob implementation in fsspec does not look thread safe, way better is to use ls and then filter
                # NOTE: without refresh you get random results here
                logger.info(f"Will truncate tables in {truncate_dir}")
                try:
                    all_files = self.fs_client.ls(truncate_dir, detail=False, refresh=True)
                    # logger.debug(f"Found {len(all_files)} CANDIDATE files in {truncate_dir}")
                    # print(f"in truncate dir {truncate_dir}: {all_files}")
                    for item in all_files:
                        # check every file against all the prefixes
                        for search_prefix in truncate_prefixes:
                            if item.startswith(search_prefix):
                                # NOTE: deleting in chunks on s3 does not raise on access denied, file non existing and probably other errors
                                # print(f"DEL {item}")
                                try:
                                    # NOTE: must use rm_file to get errors on delete
                                    self.fs_client.rm_file(item)
                                except NotImplementedError:
                                    # not all filesystem implement the above
                                    self.fs_client.rm(item)
                                    if self.fs_client.exists(item):
                                        raise FileExistsError(item)
                except FileNotFoundError:
                    logger.info(
                        f"Directory or path to truncate tables {truncate_dir} does not exist but it"
                        " should be created previously!"
                    )

    def update_stored_schema(
        self, only_tables: Iterable[str] = None, expected_update: TSchemaTables = None
    ) -> TSchemaTables:
        # create destination dirs for all tables
        table_names = only_tables or self.schema.tables.keys()
        dirs_to_create = self._get_table_dirs(table_names)
        for tables_name, directory in zip(table_names, dirs_to_create):
            self.fs_client.makedirs(directory, exist_ok=True)
            # we need to mark the folders of the data tables as initialized
            if tables_name in self.schema.dlt_table_names():
                self.fs_client.touch(f"{directory}/init")

        # write schema to destination
        self.store_current_schema()

        return expected_update

    def _get_table_dirs(self, table_names: Iterable[str]) -> Set[str]:
        """Gets unique directories where table data is stored."""
        table_dirs: Set[str] = set()
        for table_name in table_names:
            # dlt tables do not respect layout (for now)
            if table_name in self.schema.dlt_table_names():
                table_prefix = posixpath.join(table_name, "")
            else:
                table_prefix = self.table_prefix_layout.format(
                    schema_name=self.schema.name, table_name=table_name
                )
            destination_dir = posixpath.join(self.dataset_path, table_prefix)
            # extract the path component
            table_dirs.add(os.path.dirname(destination_dir))
        return table_dirs

    def is_storage_initialized(self) -> bool:
        return self.fs_client.isdir(self.dataset_path)  # type: ignore[no-any-return]

    def start_file_load(self, table: TTableSchema, file_path: str, load_id: str) -> LoadJob:
        # skip the state table, we create a jsonl file in the complete_load step
        if table["name"] == self.schema.state_table_name:
            return DoNothingJob(file_path)

        cls = FollowupFilesystemJob if self.config.as_staging else LoadFilesystemJob
        return cls(
            file_path,
            self.dataset_path,
            config=self.config,
            schema_name=self.schema.name,
            load_id=load_id,
        )

    def restore_file_load(self, file_path: str) -> LoadJob:
        return EmptyLoadJob.from_file_path(file_path, "completed")

    def __enter__(self) -> "FilesystemClient":
        return self

    def __exit__(
        self, exc_type: Type[BaseException], exc_val: BaseException, exc_tb: TracebackType
    ) -> None:
        pass

    def should_load_data_to_staging_dataset(self, table: TTableSchema) -> bool:
        return False

    #
    # state stuff
    #

    def _write_to_json_file(self, filepath: str, data: DictStrAny) -> None:
        dirname = os.path.dirname(filepath)
        if not self.fs_client.isdir(dirname):
            return
        self.fs_client.write_text(filepath, json.dumps(data), "utf-8")

    def complete_load(self, load_id: str) -> None:
        # store current state
        self.store_current_state()

        # write entry to load "table"
        # TODO: this is also duplicate across all destinations. DRY this.
        load_data = {
            "load_id": load_id,
            "schema_name": self.schema.name,
            "status": 0,
            "inserted_at": pendulum.now().isoformat(),
            "schema_version_hash": self.schema.version_hash,
        }
        filepath = (
            f"{self.dataset_path}/{self.schema.loads_table_name}/{self.schema.name}.{load_id}.jsonl"
        )

        self._write_to_json_file(filepath, load_data)

    #
    # state read/write
    #

    def _get_state_file_name(self, pipeline_name: str, version_hash: str) -> str:
        """gets full path for schema file for a given hash"""
        safe_hash = "".join(
            [c for c in version_hash if re.match(r"\w", c)]
        )  # remove all special chars from hash
        return (
            f"{self.dataset_path}/{self.schema.state_table_name}/{pipeline_name}__{safe_hash}.jsonl"
        )

    def store_current_state(self) -> None:
        # get state doc from current pipeline
        from dlt.common.configuration.container import Container
        from dlt.common.pipeline import PipelineContext
        from dlt.pipeline.state_sync import state_doc

        pipeline = Container()[PipelineContext].pipeline()
        state = pipeline.state
        doc = state_doc(state)

        # get paths
        current_path = self._get_state_file_name(pipeline.pipeline_name, "current")
        hash_path = self._get_state_file_name(
            pipeline.pipeline_name, self.schema.stored_version_hash
        )

        # write
        self._write_to_json_file(current_path, doc)
        self._write_to_json_file(hash_path, doc)

    def get_stored_state(self, pipeline_name: str) -> Optional[StateInfo]:
        # raise if dir not initialized
        filepath = self._get_state_file_name(pipeline_name, "current")
        dirname = os.path.dirname(filepath)
        if not self.fs_client.isdir(dirname):
            raise DestinationUndefinedEntity({"dir": dirname})

        """Loads compressed state from destination storage"""
        if self.fs_client.exists(filepath):
            state_json = json.loads(self.fs_client.read_text(filepath))
            state_json.pop("version_hash")
            return StateInfo(**state_json)

        return None

    #
    # Schema read/write
    #

    def _get_schema_file_name(self, version_hash: str) -> str:
        """gets full path for schema file for a given hash"""
        safe_hash = "".join(
            [c for c in version_hash if re.match(r"\w", c)]
        )  # remove all special chars from hash
        return f"{self.dataset_path}/{self.schema.version_table_name}/{self.schema.name}__{safe_hash}.jsonl"

    def get_stored_schema(self) -> Optional[StorageSchemaInfo]:
        """Retrieves newest schema from destination storage"""
        return self.get_stored_schema_by_hash("current")

    def get_stored_schema_by_hash(self, version_hash: str) -> Optional[StorageSchemaInfo]:
        """retrieves the stored schema by hash"""
        filepath = self._get_schema_file_name(version_hash)
        # raise if dir not initialized
        dirname = os.path.dirname(filepath)
        if not self.fs_client.isdir(dirname):
            raise DestinationUndefinedEntity({"dir": dirname})
        if self.fs_client.exists(filepath):
            return StorageSchemaInfo(**json.loads(self.fs_client.read_text(filepath)))

        return None

    def store_current_schema(self) -> None:
        # get paths
        current_path = self._get_schema_file_name("current")
        hash_path = self._get_schema_file_name(self.schema.stored_version_hash)

        # TODO: duplicate of weaviate implementation, should be abstracted out
        version_info = {
            "version_hash": self.schema.stored_version_hash,
            "schema_name": self.schema.name,
            "version": self.schema.version,
            "engine_version": self.schema.ENGINE_VERSION,
            "inserted_at": pendulum.now(),
            "schema": json.dumps(self.schema.to_dict()),
        }

        # we always keep tabs on what the current schema is
        self._write_to_json_file(current_path, version_info)
        self._write_to_json_file(hash_path, version_info)
