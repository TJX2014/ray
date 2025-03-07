import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import List, Optional
from unittest.mock import patch

import numpy as np
import pytest

import ray
from ray._private.test_utils import wait_for_condition
from ray.data._internal.dataset_logger import DatasetLogger
from ray.data._internal.stats import (
    DatasetStats,
    StatsManager,
    _get_or_create_stats_actor,
    _StatsActor,
)
from ray.data._internal.util import create_dataset_tag
from ray.data.block import BlockMetadata
from ray.data.context import DataContext
from ray.data.tests.util import column_udf
from ray.tests.conftest import *  # noqa


def gen_expected_metrics(
    is_map: bool,
    spilled: bool = False,
    extra_metrics: Optional[List[str]] = None,
):
    if is_map:
        metrics = [
            "'num_inputs_received': N",
            "'bytes_inputs_received': N",
            "'num_task_inputs_processed': N",
            "'bytes_task_inputs_processed': N",
            "'bytes_inputs_of_submitted_tasks': N",
            "'num_task_outputs_generated': N",
            "'bytes_task_outputs_generated': N",
            "'rows_task_outputs_generated': N",
            "'num_outputs_taken': N",
            "'bytes_outputs_taken': N",
            "'num_outputs_of_finished_tasks': N",
            "'bytes_outputs_of_finished_tasks': N",
            "'num_tasks_submitted': N",
            "'num_tasks_running': Z",
            "'num_tasks_have_outputs': N",
            "'num_tasks_finished': N",
            "'num_tasks_failed': Z",
            "'obj_store_mem_freed': N",
            "'obj_store_mem_cur': Z",
            "'obj_store_mem_peak': N",
            f"""'obj_store_mem_spilled': {"N" if spilled else "Z"}""",
            "'block_generation_time': N",
            "'cpu_usage': Z",
            "'gpu_usage': Z",
        ]
    else:
        metrics = [
            "'num_inputs_received': N",
            "'bytes_inputs_received': N",
            "'num_outputs_taken': N",
            "'bytes_outputs_taken': N",
            "'cpu_usage': Z",
            "'gpu_usage': Z",
        ]
    if extra_metrics:
        metrics.extend(extra_metrics)
    return "{" + ", ".join(metrics) + "}"


def gen_extra_metrics_str(metrics: str, verbose: bool):
    return f"* Extra metrics: {metrics}" + "\n" if verbose else ""


STANDARD_EXTRA_METRICS = gen_expected_metrics(
    is_map=True,
    spilled=False,
    extra_metrics=[
        "'ray_remote_args': {'num_cpus': N, 'scheduling_strategy': 'SPREAD'}"
    ],
)

LARGE_ARGS_EXTRA_METRICS = gen_expected_metrics(
    is_map=True,
    spilled=False,
    extra_metrics=[
        "'ray_remote_args': {'num_cpus': N, 'scheduling_strategy': 'DEFAULT'}"
    ],
)


MEM_SPILLED_EXTRA_METRICS = gen_expected_metrics(
    is_map=True,
    spilled=True,
    extra_metrics=[
        "'ray_remote_args': {'num_cpus': N, 'scheduling_strategy': 'SPREAD'}"
    ],
)


CLUSTER_MEMORY_STATS = """
Cluster memory:
* Spilled to disk: M
* Restored from disk: M
"""

DATASET_MEMORY_STATS = """
Dataset memory:
* Spilled to disk: M
"""

EXECUTION_STRING = "N tasks executed, N blocks produced in T"


def canonicalize(stats: str, filter_global_stats: bool = True) -> str:
    # Dataset UUID expression.
    s0 = re.sub("([a-f\d]{32})", "U", stats)
    # Time expressions.
    s1 = re.sub("[0-9\.]+(ms|us|s)", "T", s0)
    # Memory expressions.
    s2 = re.sub("[0-9\.]+(B|MB|GB)", "M", s1)
    # Handle floats in (0, 1)
    s3 = re.sub(" (0\.0*[1-9][0-9]*)", " N", s2)
    # Handle zero values specially so we can check for missing values.
    s4 = re.sub(" [0]+(\.[0])?", " Z", s3)
    # Other numerics.
    s5 = re.sub("[0-9]+(\.[0-9]+)?", "N", s4)
    # Replace tabs with spaces.
    s6 = re.sub("\t", "    ", s5)
    if filter_global_stats:
        s7 = s6.replace(CLUSTER_MEMORY_STATS, "")
        s8 = s7.replace(DATASET_MEMORY_STATS, "")
        return s8
    return s6


def dummy_map_batches(x):
    """Dummy function used in calls to map_batches below."""
    return x


def map_batches_sleep(x, n):
    """Dummy function used in calls to map_batches below, which
    simply sleeps for `n` seconds before returning the input batch."""
    time.sleep(n)
    return x


@contextmanager
def patch_update_stats_actor():
    with patch(
        "ray.data._internal.stats.StatsManager.update_execution_metrics"
    ) as update_fn:
        yield update_fn


@contextmanager
def patch_update_stats_actor_iter():
    with patch(
        "ray.data._internal.stats.StatsManager.update_iteration_metrics"
    ) as update_fn, patch(
        "ray.data._internal.stats.StatsManager.clear_iteration_metrics"
    ):
        yield update_fn


def test_streaming_split_stats(ray_start_regular_shared, restore_data_context):
    context = DataContext.get_current()
    context.verbose_stats_logs = True
    ds = ray.data.range(1000, parallelism=10)
    it = ds.map_batches(dummy_map_batches).streaming_split(1)[0]
    list(it.iter_batches())
    stats = it.stats()
    extra_metrics = gen_expected_metrics(
        is_map=False, extra_metrics=["'num_output_N': N"]
    )
    assert (
        canonicalize(stats)
        == f"""Operator N ReadRange->MapBatches(dummy_map_batches): {EXECUTION_STRING}
* Remote wall time: T min, T max, T mean, T total
* Remote cpu time: T min, T max, T mean, T total
* Peak heap memory usage (MiB): N min, N max, N mean
* Output num rows per block: N min, N max, N mean, N total
* Output size bytes per block: N min, N max, N mean, N total
* Output rows per task: N min, N max, N mean, N tasks used
* Tasks per node: N min, N max, N mean; N nodes used
* Extra metrics: {STANDARD_EXTRA_METRICS}

Operator N split(N, equal=False): \n"""
        # Workaround to preserve trailing whitespace in the above line without
        # causing linter failures.
        f"""* Extra metrics: {extra_metrics}\n"""
        """
Dataset iterator time breakdown:
* Total time overall: T
    * Total time in Ray Data iterator initialization code: T
    * Total time user thread is blocked by Ray Data iter_batches: T
    * Total execution time for user thread: T
* Batch iteration time breakdown (summed across prefetch threads):
    * In ray.get(): T min, T max, T avg, T total
    * In batch creation: T min, T max, T avg, T total
    * In batch formatting: T min, T max, T avg, T total
"""
    )


@pytest.mark.parametrize("verbose_stats_logs", [True, False])
def test_large_args_scheduling_strategy(
    ray_start_regular_shared, verbose_stats_logs, restore_data_context
):
    context = DataContext.get_current()
    context.verbose_stats_logs = verbose_stats_logs
    ds = ray.data.range_tensor(100, shape=(100000,), parallelism=1)
    ds = ds.map_batches(dummy_map_batches, num_cpus=0.9).materialize()
    stats = ds.stats()
    expected_stats = (
        f"Operator N ReadRange: {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        f"{gen_extra_metrics_str(STANDARD_EXTRA_METRICS, verbose_stats_logs)}\n"
        f"Operator N MapBatches(dummy_map_batches): {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        f"{gen_extra_metrics_str(LARGE_ARGS_EXTRA_METRICS, verbose_stats_logs)}"
    )
    assert canonicalize(stats) == expected_stats


@pytest.mark.parametrize("verbose_stats_logs", [True, False])
def test_dataset_stats_basic(
    ray_start_regular_shared,
    enable_auto_log_stats,
    verbose_stats_logs,
    restore_data_context,
):
    context = DataContext.get_current()
    context.verbose_stats_logs = verbose_stats_logs
    logger = DatasetLogger(
        "ray.data._internal.execution.streaming_executor"
    ).get_logger(
        log_to_stdout=enable_auto_log_stats,
    )

    with patch.object(logger, "info") as mock_logger:
        ds = ray.data.range(1000, parallelism=10)
        ds = ds.map_batches(dummy_map_batches).materialize()

        if enable_auto_log_stats:
            logger_args, logger_kwargs = mock_logger.call_args

            assert canonicalize(logger_args[0]) == (
                f"Operator N ReadRange->MapBatches(dummy_map_batches): "
                f"{EXECUTION_STRING}\n"
                f"* Remote wall time: T min, T max, T mean, T total\n"
                f"* Remote cpu time: T min, T max, T mean, T total\n"
                f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
                f"* Output num rows per block: N min, N max, N mean, N total\n"
                f"* Output size bytes per block: N min, N max, N mean, N total\n"
                f"* Output rows per task: N min, N max, N mean, N tasks used\n"
                f"* Tasks per node: N min, N max, N mean; N nodes used\n"
                f"{gen_extra_metrics_str(STANDARD_EXTRA_METRICS, verbose_stats_logs)}"  # noqa: E501
            )

        ds = ds.map(dummy_map_batches).materialize()
        if enable_auto_log_stats:
            logger_args, logger_kwargs = mock_logger.call_args

            assert canonicalize(logger_args[0]) == (
                f"Operator N Map(dummy_map_batches): {EXECUTION_STRING}\n"
                f"* Remote wall time: T min, T max, T mean, T total\n"
                f"* Remote cpu time: T min, T max, T mean, T total\n"
                f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
                f"* Output num rows per block: N min, N max, N mean, N total\n"
                f"* Output size bytes per block: N min, N max, N mean, N total\n"
                f"* Output rows per task: N min, N max, N mean, N tasks used\n"
                f"* Tasks per node: N min, N max, N mean; N nodes used\n"
                f"{gen_extra_metrics_str(STANDARD_EXTRA_METRICS, verbose_stats_logs)}"  # noqa: E501
            )

    for batch in ds.iter_batches():
        pass
    stats = canonicalize(ds.materialize().stats())

    assert stats == (
        f"Operator N ReadRange->MapBatches(dummy_map_batches): {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        f"{gen_extra_metrics_str(STANDARD_EXTRA_METRICS, verbose_stats_logs)}\n"
        f"Operator N Map(dummy_map_batches): {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        f"{gen_extra_metrics_str(STANDARD_EXTRA_METRICS, verbose_stats_logs)}\n"
        f"Dataset iterator time breakdown:\n"
        f"* Total time overall: T\n"
        f"    * Total time in Ray Data iterator initialization code: T\n"
        f"    * Total time user thread is blocked by Ray Data iter_batches: T\n"
        f"    * Total execution time for user thread: T\n"
        f"* Batch iteration time breakdown (summed across prefetch threads):\n"
        f"    * In ray.get(): T min, T max, T avg, T total\n"
        f"    * In batch creation: T min, T max, T avg, T total\n"
        f"    * In batch formatting: T min, T max, T avg, T total\n"
    )


def test_block_location_nums(ray_start_regular_shared, restore_data_context):
    context = DataContext.get_current()
    context.enable_get_object_locations_for_metrics = True
    ds = ray.data.range(1000, parallelism=10)
    ds = ds.map_batches(dummy_map_batches).materialize()

    for batch in ds.iter_batches():
        pass
    stats = canonicalize(ds.materialize().stats())

    assert stats == (
        f"Operator N ReadRange->MapBatches(dummy_map_batches): {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        f"\n"
        f"Dataset iterator time breakdown:\n"
        f"* Total time overall: T\n"
        f"    * Total time in Ray Data iterator initialization code: T\n"
        f"    * Total time user thread is blocked by Ray Data iter_batches: T\n"
        f"    * Total execution time for user thread: T\n"
        f"* Batch iteration time breakdown (summed across prefetch threads):\n"
        f"    * In ray.get(): T min, T max, T avg, T total\n"
        f"    * In batch creation: T min, T max, T avg, T total\n"
        f"    * In batch formatting: T min, T max, T avg, T total\n"
        f"Block locations:\n"
        f"    * Num blocks local: Z\n"
        f"    * Num blocks remote: Z\n"
        f"    * Num blocks unknown location: N\n"
    )


def test_dataset__repr__(ray_start_regular_shared, restore_data_context):
    context = DataContext.get_current()
    context.enable_get_object_locations_for_metrics = True
    n = 100
    ds = ray.data.range(n)
    assert len(ds.take_all()) == n
    ds = ds.materialize()

    expected_stats = (
        "DatasetStatsSummary(\n"
        "   dataset_uuid=N,\n"
        "   base_name=None,\n"
        "   number=N,\n"
        "   extra_metrics={},\n"
        "   operators_stats=[\n"
        "      OperatorStatsSummary(\n"
        "         operator_name='Read',\n"
        "         is_suboperator=False,\n"
        "         time_total_s=T,\n"
        f"         block_execution_summary_str={EXECUTION_STRING}\n"
        "         wall_time={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "         cpu_time={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "         memory={'min': 'T', 'max': 'T', 'mean': 'T'},\n"
        "         output_num_rows={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "         output_size_bytes={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"  # noqa: E501
        "         node_count={'min': 'T', 'max': 'T', 'mean': 'T', 'count': 'T'},\n"
        "      ),\n"
        "   ],\n"
        "   iter_stats=IterStatsSummary(\n"
        "      wait_time=T,\n"
        "      get_time=T,\n"
        "      iter_blocks_local=None,\n"
        "      iter_blocks_remote=None,\n"
        "      iter_unknown_location=None,\n"
        "      next_time=T,\n"
        "      format_time=T,\n"
        "      user_time=T,\n"
        "      total_time=T,\n"
        "   ),\n"
        "   global_bytes_spilled=M,\n"
        "   global_bytes_restored=M,\n"
        "   dataset_bytes_spilled=M,\n"
        "   parents=[],\n"
        ")"
    )

    def check_stats():
        stats = canonicalize(repr(ds._plan.stats().to_summary()))
        assert stats == expected_stats
        return True

    # TODO(hchen): The reason why `wait_for_condition` is needed here is because
    # `to_summary` depends on an external actor (_StatsActor) that records stats
    # asynchronously. This makes the behavior non-deterministic.
    # See the TODO in `to_summary`.
    # We should make it deterministic and refine this test.
    wait_for_condition(
        check_stats,
        timeout=10,
        retry_interval_ms=1000,
    )

    ds2 = ds.map_batches(lambda x: x).materialize()
    assert len(ds2.take_all()) == n
    expected_stats2 = (
        "DatasetStatsSummary(\n"
        "   dataset_uuid=N,\n"
        "   base_name=MapBatches(<lambda>),\n"
        "   number=N,\n"
        "   extra_metrics={\n"
        "      num_inputs_received: N,\n"
        "      bytes_inputs_received: N,\n"
        "      num_task_inputs_processed: N,\n"
        "      bytes_task_inputs_processed: N,\n"
        "      bytes_inputs_of_submitted_tasks: N,\n"
        "      num_task_outputs_generated: N,\n"
        "      bytes_task_outputs_generated: N,\n"
        "      rows_task_outputs_generated: N,\n"
        "      num_outputs_taken: N,\n"
        "      bytes_outputs_taken: N,\n"
        "      num_outputs_of_finished_tasks: N,\n"
        "      bytes_outputs_of_finished_tasks: N,\n"
        "      num_tasks_submitted: N,\n"
        "      num_tasks_running: Z,\n"
        "      num_tasks_have_outputs: N,\n"
        "      num_tasks_finished: N,\n"
        "      num_tasks_failed: Z,\n"
        "      obj_store_mem_freed: N,\n"
        "      obj_store_mem_cur: Z,\n"
        "      obj_store_mem_peak: N,\n"
        "      obj_store_mem_spilled: Z,\n"
        "      block_generation_time: N,\n"
        "      cpu_usage: Z,\n"
        "      gpu_usage: Z,\n"
        "      ray_remote_args: {'num_cpus': N, 'scheduling_strategy': 'SPREAD'},\n"
        "   },\n"
        "   operators_stats=[\n"
        "      OperatorStatsSummary(\n"
        "         operator_name='MapBatches(<lambda>)',\n"
        "         is_suboperator=False,\n"
        "         time_total_s=T,\n"
        f"         block_execution_summary_str={EXECUTION_STRING}\n"
        "         wall_time={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "         cpu_time={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "         memory={'min': 'T', 'max': 'T', 'mean': 'T'},\n"
        "         output_num_rows={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "         output_size_bytes={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"  # noqa: E501
        "         node_count={'min': 'T', 'max': 'T', 'mean': 'T', 'count': 'T'},\n"
        "      ),\n"
        "   ],\n"
        "   iter_stats=IterStatsSummary(\n"
        "      wait_time=T,\n"
        "      get_time=T,\n"
        "      iter_blocks_local=None,\n"
        "      iter_blocks_remote=None,\n"
        "      iter_unknown_location=N,\n"
        "      next_time=T,\n"
        "      format_time=T,\n"
        "      user_time=T,\n"
        "      total_time=T,\n"
        "   ),\n"
        "   global_bytes_spilled=M,\n"
        "   global_bytes_restored=M,\n"
        "   dataset_bytes_spilled=M,\n"
        "   parents=[\n"
        "      DatasetStatsSummary(\n"
        "         dataset_uuid=N,\n"
        "         base_name=None,\n"
        "         number=N,\n"
        "         extra_metrics={},\n"
        "         operators_stats=[\n"
        "            OperatorStatsSummary(\n"
        "               operator_name='Read',\n"
        "               is_suboperator=False,\n"
        "               time_total_s=T,\n"
        f"               block_execution_summary_str={EXECUTION_STRING}\n"
        "               wall_time={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "               cpu_time={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"
        "               memory={'min': 'T', 'max': 'T', 'mean': 'T'},\n"
        "               output_num_rows={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"  # noqa: E501
        "               output_size_bytes={'min': 'T', 'max': 'T', 'mean': 'T', 'sum': 'T'},\n"  # noqa: E501
        "               node_count={'min': 'T', 'max': 'T', 'mean': 'T', 'count': 'T'},\n"  # noqa: E501
        "            ),\n"
        "         ],\n"
        "         iter_stats=IterStatsSummary(\n"
        "            wait_time=T,\n"
        "            get_time=T,\n"
        "            iter_blocks_local=None,\n"
        "            iter_blocks_remote=None,\n"
        "            iter_unknown_location=None,\n"
        "            next_time=T,\n"
        "            format_time=T,\n"
        "            user_time=T,\n"
        "            total_time=T,\n"
        "         ),\n"
        "         global_bytes_spilled=M,\n"
        "         global_bytes_restored=M,\n"
        "         dataset_bytes_spilled=M,\n"
        "         parents=[],\n"
        "      ),\n"
        "   ],\n"
        ")"
    )

    def check_stats2():
        stats = canonicalize(repr(ds2._plan.stats().to_summary()))
        assert stats == expected_stats2
        return True

    wait_for_condition(
        check_stats2,
        timeout=10,
        retry_interval_ms=1000,
    )


def test_dataset_stats_shuffle(ray_start_regular_shared):
    ds = ray.data.range(1000, parallelism=10)
    ds = ds.random_shuffle().repartition(1, shuffle=True)
    stats = canonicalize(ds.materialize().stats())
    assert (
        stats
        == """Operator N ReadRange->RandomShuffle: executed in T

    Suboperator Z ReadRange->RandomShuffleMap: N tasks executed, N blocks produced
    * Remote wall time: T min, T max, T mean, T total
    * Remote cpu time: T min, T max, T mean, T total
    * Peak heap memory usage (MiB): N min, N max, N mean
    * Output num rows per block: N min, N max, N mean, N total
    * Output size bytes per block: N min, N max, N mean, N total
    * Output rows per task: N min, N max, N mean, N tasks used
    * Tasks per node: N min, N max, N mean; N nodes used

    Suboperator N RandomShuffleReduce: N tasks executed, N blocks produced
    * Remote wall time: T min, T max, T mean, T total
    * Remote cpu time: T min, T max, T mean, T total
    * Peak heap memory usage (MiB): N min, N max, N mean
    * Output num rows per block: N min, N max, N mean, N total
    * Output size bytes per block: N min, N max, N mean, N total
    * Output rows per task: N min, N max, N mean, N tasks used
    * Tasks per node: N min, N max, N mean; N nodes used

Operator N Repartition: executed in T

    Suboperator Z RepartitionMap: N tasks executed, N blocks produced
    * Remote wall time: T min, T max, T mean, T total
    * Remote cpu time: T min, T max, T mean, T total
    * Peak heap memory usage (MiB): N min, N max, N mean
    * Output num rows per block: N min, N max, N mean, N total
    * Output size bytes per block: N min, N max, N mean, N total
    * Output rows per task: N min, N max, N mean, N tasks used
    * Tasks per node: N min, N max, N mean; N nodes used

    Suboperator N RepartitionReduce: N tasks executed, N blocks produced
    * Remote wall time: T min, T max, T mean, T total
    * Remote cpu time: T min, T max, T mean, T total
    * Peak heap memory usage (MiB): N min, N max, N mean
    * Output num rows per block: N min, N max, N mean, N total
    * Output size bytes per block: N min, N max, N mean, N total
    * Output rows per task: N min, N max, N mean, N tasks used
    * Tasks per node: N min, N max, N mean; N nodes used
"""
    )


def test_dataset_stats_repartition(ray_start_regular_shared):
    ds = ray.data.range(1000, parallelism=10)
    ds = ds.repartition(1, shuffle=False)
    stats = ds.materialize().stats()
    assert "Repartition" in stats, stats


def test_dataset_stats_union(ray_start_regular_shared):
    ds = ray.data.range(1000, parallelism=10)
    ds = ds.union(ds)
    stats = ds.materialize().stats()
    assert "Union" in stats, stats


def test_dataset_stats_zip(ray_start_regular_shared):
    ds = ray.data.range(1000, parallelism=10)
    ds = ds.zip(ds)
    stats = ds.materialize().stats()
    assert "Zip" in stats, stats


def test_dataset_stats_sort(ray_start_regular_shared):
    ds = ray.data.range(1000, parallelism=10)
    ds = ds.sort("id")
    stats = ds.materialize().stats()
    assert "SortMap" in stats, stats
    assert "SortReduce" in stats, stats


def test_dataset_stats_from_items(ray_start_regular_shared):
    ds = ray.data.from_items(range(10))
    stats = ds.materialize().stats()
    assert "FromItems" in stats, stats


def test_dataset_stats_read_parquet(ray_start_regular_shared, tmp_path):
    ds = ray.data.range(1000, parallelism=10)
    ds.write_parquet(str(tmp_path))
    ds = ray.data.read_parquet(str(tmp_path)).map(lambda x: x)
    stats = canonicalize(ds.materialize().stats())
    assert stats == (
        f"Operator N ReadParquet->Map(<lambda>): {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
    )


def test_dataset_split_stats(ray_start_regular_shared, tmp_path):
    ds = ray.data.range(100, parallelism=10).map(column_udf("id", lambda x: x + 1))
    dses = ds.split_at_indices([49])
    dses = [ds.map(column_udf("id", lambda x: x + 1)) for ds in dses]
    for ds_ in dses:
        stats = canonicalize(ds_.materialize().stats())

        assert stats == (
            f"Operator N ReadRange->Map(<lambda>): {EXECUTION_STRING}\n"
            f"* Remote wall time: T min, T max, T mean, T total\n"
            f"* Remote cpu time: T min, T max, T mean, T total\n"
            f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
            f"* Output num rows per block: N min, N max, N mean, N total\n"
            f"* Output size bytes per block: N min, N max, N mean, N total\n"
            f"* Output rows per task: N min, N max, N mean, N tasks used\n"
            f"* Tasks per node: N min, N max, N mean; N nodes used\n"
            f"\n"
            f"Operator N Split: {EXECUTION_STRING}\n"
            f"* Remote wall time: T min, T max, T mean, T total\n"
            f"* Remote cpu time: T min, T max, T mean, T total\n"
            f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
            f"* Output num rows per block: N min, N max, N mean, N total\n"
            f"* Output size bytes per block: N min, N max, N mean, N total\n"
            f"* Output rows per task: N min, N max, N mean, N tasks used\n"
            f"* Tasks per node: N min, N max, N mean; N nodes used\n"
            f"\n"
            f"Operator N Map(<lambda>): {EXECUTION_STRING}\n"
            f"* Remote wall time: T min, T max, T mean, T total\n"
            f"* Remote cpu time: T min, T max, T mean, T total\n"
            f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
            f"* Output num rows per block: N min, N max, N mean, N total\n"
            f"* Output size bytes per block: N min, N max, N mean, N total\n"
            f"* Output rows per task: N min, N max, N mean, N tasks used\n"
            f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        )


def test_calculate_blocks_stats(ray_start_regular_shared, op_two_block):
    block_params, block_meta_list = op_two_block
    stats = DatasetStats(
        metadata={"Read": block_meta_list},
        parent=None,
    )
    calculated_stats = stats.to_summary().operators_stats[0]

    assert calculated_stats.output_num_rows == {
        "min": min(block_params["num_rows"]),
        "max": max(block_params["num_rows"]),
        "mean": np.mean(block_params["num_rows"]),
        "sum": sum(block_params["num_rows"]),
    }
    assert calculated_stats.output_size_bytes == {
        "min": min(block_params["size_bytes"]),
        "max": max(block_params["size_bytes"]),
        "mean": np.mean(block_params["size_bytes"]),
        "sum": sum(block_params["size_bytes"]),
    }
    assert calculated_stats.wall_time == {
        "min": min(block_params["wall_time"]),
        "max": max(block_params["wall_time"]),
        "mean": np.mean(block_params["wall_time"]),
        "sum": sum(block_params["wall_time"]),
    }
    assert calculated_stats.cpu_time == {
        "min": min(block_params["cpu_time"]),
        "max": max(block_params["cpu_time"]),
        "mean": np.mean(block_params["cpu_time"]),
        "sum": sum(block_params["cpu_time"]),
    }

    node_counts = Counter(block_params["node_id"])
    assert calculated_stats.node_count == {
        "min": min(node_counts.values()),
        "max": max(node_counts.values()),
        "mean": np.mean(list(node_counts.values())),
        "count": len(node_counts),
    }


def test_summarize_blocks(ray_start_regular_shared, op_two_block):
    block_params, block_meta_list = op_two_block
    stats = DatasetStats(
        metadata={"Read": block_meta_list},
        parent=None,
    )
    stats.dataset_uuid = "test-uuid"

    calculated_stats = stats.to_summary()
    summarized_lines = calculated_stats.to_string().split("\n")

    latest_end_time = max(m.exec_stats.end_time_s for m in block_meta_list)
    earliest_start_time = min(m.exec_stats.start_time_s for m in block_meta_list)
    assert (
        "Operator 0 Read: 2 tasks executed, 2 blocks produced in {}s".format(
            max(round(latest_end_time - earliest_start_time, 2), 0)
        )
        == summarized_lines[0]
    )
    assert (
        "* Remote wall time: {}s min, {}s max, {}s mean, {}s total".format(
            min(block_params["wall_time"]),
            max(block_params["wall_time"]),
            np.mean(block_params["wall_time"]),
            sum(block_params["wall_time"]),
        )
        == summarized_lines[1]
    )
    assert (
        "* Remote cpu time: {}s min, {}s max, {}s mean, {}s total".format(
            min(block_params["cpu_time"]),
            max(block_params["cpu_time"]),
            np.mean(block_params["cpu_time"]),
            sum(block_params["cpu_time"]),
        )
        == summarized_lines[2]
    )
    assert (
        "* Peak heap memory usage (MiB): {} min, {} max, {} mean".format(
            min(block_params["max_rss_bytes"]) / (1024 * 1024),
            max(block_params["max_rss_bytes"]) / (1024 * 1024),
            int(np.mean(block_params["max_rss_bytes"]) / (1024 * 1024)),
        )
        == summarized_lines[3]
    )
    assert (
        "* Output num rows per block: {} min, {} max, {} mean, {} total".format(
            min(block_params["num_rows"]),
            max(block_params["num_rows"]),
            int(np.mean(block_params["num_rows"])),
            sum(block_params["num_rows"]),
        )
        == summarized_lines[4]
    )
    assert (
        "* Output size bytes per block: {} min, {} max, {} mean, {} total".format(
            min(block_params["size_bytes"]),
            max(block_params["size_bytes"]),
            int(np.mean(block_params["size_bytes"])),
            sum(block_params["size_bytes"]),
        )
        == summarized_lines[5]
    )

    assert (
        "* Output rows per task: {} min, {} max, {} mean, {} tasks used".format(
            min(block_params["num_rows"]),
            max(block_params["num_rows"]),
            int(np.mean(list(block_params["num_rows"]))),
            len(set(block_params["task_idx"])),
        )
        == summarized_lines[6]
    )

    node_counts = Counter(block_params["node_id"])
    assert (
        "* Tasks per node: {} min, {} max, {} mean; {} nodes used".format(
            min(node_counts.values()),
            max(node_counts.values()),
            int(np.mean(list(node_counts.values()))),
            len(node_counts),
        )
        == summarized_lines[7]
    )


def test_get_total_stats(ray_start_regular_shared, op_two_block):
    """Tests a set of similar getter methods which pull aggregated
    statistics values after calculating operator-level stats:
    `DatasetStats.get_max_wall_time()`,
    `DatasetStats.get_total_cpu_time()`,
    `DatasetStats.get_max_heap_memory()`."""
    block_params, block_meta_list = op_two_block
    stats = DatasetStats(
        metadata={"Read": block_meta_list},
        parent=None,
    )

    dataset_stats_summary = stats.to_summary()
    op_stats = dataset_stats_summary.operators_stats[0]
    wall_time_stats = op_stats.wall_time
    assert dataset_stats_summary.get_total_wall_time() == wall_time_stats.get("max")

    cpu_time_stats = op_stats.cpu_time
    assert dataset_stats_summary.get_total_cpu_time() == cpu_time_stats.get("sum")

    peak_memory_stats = op_stats.memory
    assert dataset_stats_summary.get_max_heap_memory() == peak_memory_stats.get("max")


@pytest.mark.skip(
    reason="Temporarily disable to deflake rest of test suite. "
    "See: https://github.com/ray-project/ray/pull/40173"
)
def test_streaming_stats_full(ray_start_regular_shared, restore_data_context):
    ds = ray.data.range(5, parallelism=5).map(column_udf("id", lambda x: x + 1))
    ds.take_all()
    stats = canonicalize(ds.stats())
    assert (
        stats
        == f"""Operator N ReadRange->Map(<lambda>): {EXECUTION_STRING}
* Remote wall time: T min, T max, T mean, T total
* Remote cpu time: T min, T max, T mean, T total
* Peak heap memory usage (MiB): N min, N max, N mean
* Output num rows per block: N min, N max, N mean, N total
* Output size bytes per block: N min, N max, N mean, N total
* Output rows per task: N min, N max, N mean, N tasks used
* Tasks per node: N min, N max, N mean; N nodes used

Dataset iterator time breakdown:
* Total time overall: T
    * Total time in Ray Data iterator initialization code: T
    * Total time user thread is blocked by Ray Data iter_batches: T
    * Total execution time for user thread: T
* Batch iteration time breakdown (summed across prefetch threads):
    * In ray.get(): T min, T max, T avg, T total
    * In batch creation: T min, T max, T avg, T total
    * In batch formatting: T min, T max, T avg, T total
"""
    )


def test_write_ds_stats(ray_start_regular_shared, tmp_path):
    ds = ray.data.range(100, parallelism=100)
    ds.write_parquet(str(tmp_path))
    stats = ds.stats()

    assert (
        canonicalize(stats)
        == f"""Operator N ReadRange->Write: {EXECUTION_STRING}
* Remote wall time: T min, T max, T mean, T total
* Remote cpu time: T min, T max, T mean, T total
* Peak heap memory usage (MiB): N min, N max, N mean
* Output num rows per block: N min, N max, N mean, N total
* Output size bytes per block: N min, N max, N mean, N total
* Output rows per task: N min, N max, N mean, N tasks used
* Tasks per node: N min, N max, N mean; N nodes used
"""
    )

    assert stats == ds._write_ds.stats()

    ds = ray.data.range(100, parallelism=100).map_batches(lambda x: x).materialize()
    ds.write_parquet(str(tmp_path))
    stats = ds.stats()

    assert (
        canonicalize(stats)
        == f"""Operator N ReadRange->MapBatches(<lambda>): {EXECUTION_STRING}
* Remote wall time: T min, T max, T mean, T total
* Remote cpu time: T min, T max, T mean, T total
* Peak heap memory usage (MiB): N min, N max, N mean
* Output num rows per block: N min, N max, N mean, N total
* Output size bytes per block: N min, N max, N mean, N total
* Output rows per task: N min, N max, N mean, N tasks used
* Tasks per node: N min, N max, N mean; N nodes used

Operator N Write: {EXECUTION_STRING}
* Remote wall time: T min, T max, T mean, T total
* Remote cpu time: T min, T max, T mean, T total
* Peak heap memory usage (MiB): N min, N max, N mean
* Output num rows per block: N min, N max, N mean, N total
* Output size bytes per block: N min, N max, N mean, N total
* Output rows per task: N min, N max, N mean, N tasks used
* Tasks per node: N min, N max, N mean; N nodes used
"""
    )

    assert stats == ds._write_ds.stats()


# NOTE: All tests above share a Ray cluster, while the tests below do not. These
# tests should only be carefully reordered to retain this invariant!


def test_stats_actor_cap_num_stats(ray_start_cluster):
    actor = _StatsActor.remote(3)
    metadatas = []
    task_idx = 0
    for uuid in range(3):
        metadatas.append(
            BlockMetadata(
                num_rows=uuid,
                size_bytes=None,
                schema=None,
                input_files=None,
                exec_stats=None,
            )
        )
        num_stats = uuid + 1
        actor.record_start.remote(uuid)
        assert ray.get(actor._get_stats_dict_size.remote()) == (
            num_stats,
            num_stats - 1,
            num_stats - 1,
        )
        actor.record_task.remote(uuid, task_idx, [metadatas[-1]])
        assert ray.get(actor._get_stats_dict_size.remote()) == (
            num_stats,
            num_stats,
            num_stats,
        )
    for uuid in range(3):
        assert ray.get(actor.get.remote(uuid))[0][task_idx] == [metadatas[uuid]]
    # Add the fourth stats to exceed the limit.
    actor.record_start.remote(3)
    # The first stats (with uuid=0) should have been purged.
    assert ray.get(actor.get.remote(0))[0] == {}
    # The start_time has 3 entries because we just added it above with record_start().
    assert ray.get(actor._get_stats_dict_size.remote()) == (3, 2, 2)


@pytest.mark.parametrize("verbose_stats_logs", [True, False])
def test_spilled_stats(shutdown_only, verbose_stats_logs, restore_data_context):
    context = DataContext.get_current()
    context.verbose_stats_logs = verbose_stats_logs
    context.enable_get_object_locations_for_metrics = True
    # The object store is about 100MB.
    ray.init(object_store_memory=100e6)
    # The size of dataset is 1000*80*80*4*8B, about 200MB.
    ds = ray.data.range(1000 * 80 * 80 * 4).map_batches(lambda x: x).materialize()
    expected_stats = (
        f"Operator N ReadRange->MapBatches(<lambda>): {EXECUTION_STRING}\n"
        f"* Remote wall time: T min, T max, T mean, T total\n"
        f"* Remote cpu time: T min, T max, T mean, T total\n"
        f"* Peak heap memory usage (MiB): N min, N max, N mean\n"
        f"* Output num rows per block: N min, N max, N mean, N total\n"
        f"* Output size bytes per block: N min, N max, N mean, N total\n"
        f"* Output rows per task: N min, N max, N mean, N tasks used\n"
        f"* Tasks per node: N min, N max, N mean; N nodes used\n"
        f"{gen_extra_metrics_str(MEM_SPILLED_EXTRA_METRICS, verbose_stats_logs)}\n"
        f"Cluster memory:\n"
        f"* Spilled to disk: M\n"
        f"* Restored from disk: M\n"
        f"\n"
        f"Dataset memory:\n"
        f"* Spilled to disk: M\n"
    )

    assert canonicalize(ds.stats(), filter_global_stats=False) == expected_stats

    # Around 100MB should be spilled (200MB - 100MB)
    assert ds._plan.stats().dataset_bytes_spilled > 100e6

    ds = (
        ray.data.range(1000 * 80 * 80 * 4)
        .map_batches(lambda x: x)
        .materialize()
        .map_batches(lambda x: x)
        .materialize()
    )
    # two map_batches operators, twice the spillage
    assert ds._plan.stats().dataset_bytes_spilled > 200e6

    # The size of dataset is around 50MB, there should be no spillage
    ds = (
        ray.data.range(250 * 80 * 80 * 4, parallelism=1)
        .map_batches(lambda x: x)
        .materialize()
    )

    assert ds._plan.stats().dataset_bytes_spilled == 0


def test_stats_actor_metrics():
    ray.init(object_store_memory=100e6)
    with patch_update_stats_actor() as update_fn:
        ds = ray.data.range(1000 * 80 * 80 * 4).map_batches(lambda x: x).materialize()

    # last emitted metrics from map operator
    final_metric = update_fn.call_args_list[-1].args[1][-1]

    assert final_metric.obj_store_mem_spilled == ds._plan.stats().dataset_bytes_spilled
    assert (
        final_metric.obj_store_mem_freed
        == ds._plan.stats().extra_metrics["obj_store_mem_freed"]
    )
    assert (
        final_metric.bytes_task_outputs_generated == 1000 * 80 * 80 * 4 * 8
    )  # 8B per int
    assert final_metric.rows_task_outputs_generated == 1000 * 80 * 80 * 4
    # There should be nothing in object store at the end of execution.
    assert final_metric.obj_store_mem_cur == 0

    args = update_fn.call_args_list[-1].args
    assert args[0] == f"dataset_{ds._uuid}"
    assert args[2][0] == "Input0"
    assert args[2][1] == "ReadRange->MapBatches(<lambda>)1"

    def sleep_three(x):
        import time

        time.sleep(3)
        return x

    with patch_update_stats_actor() as update_fn:
        ds = ray.data.range(3).map_batches(sleep_three, batch_size=1).materialize()

    final_metric = update_fn.call_args_list[-1].args[1][-1]
    assert final_metric.block_generation_time >= 9


def test_stats_actor_iter_metrics():
    ds = ray.data.range(1e6).map_batches(lambda x: x)
    with patch_update_stats_actor_iter() as update_fn:
        ds.take_all()

    ds_stats = ds._plan.stats()
    final_stats = update_fn.call_args_list[-1].args[0]

    assert final_stats == ds_stats
    assert f"dataset_{ds._uuid}" == update_fn.call_args_list[-1].args[1]


def test_dataset_name():
    ds = ray.data.range(100, parallelism=20).map_batches(lambda x: x)
    ds._set_name("test_ds")
    assert ds._name == "test_ds"
    assert str(ds) == (
        "MapBatches(<lambda>)\n"
        "+- Dataset(name=test_ds, num_blocks=20, num_rows=100, schema={id: int64})"
    )
    with patch_update_stats_actor() as update_fn:
        mds = ds.materialize()

    assert update_fn.call_args_list[-1].args[0] == f"test_ds_{mds._uuid}"

    # Names persist after an execution
    ds = ds.random_shuffle()
    assert ds._name == "test_ds"
    with patch_update_stats_actor() as update_fn:
        mds = ds.materialize()

    assert update_fn.call_args_list[-1].args[0] == f"test_ds_{mds._uuid}"

    ds._set_name("test_ds_two")
    ds = ds.map_batches(lambda x: x)
    assert ds._name == "test_ds_two"
    with patch_update_stats_actor() as update_fn:
        mds = ds.materialize()

    assert update_fn.call_args_list[-1].args[0] == f"test_ds_two_{mds._uuid}"

    ds._set_name(None)
    ds = ds.map_batches(lambda x: x)
    assert ds._name is None
    with patch_update_stats_actor() as update_fn:
        mds = ds.materialize()

    assert update_fn.call_args_list[-1].args[0] == f"dataset_{mds._uuid}"

    ds = ray.data.range(100, parallelism=20)
    ds._set_name("very_loooooooong_name")
    assert (
        str(ds)
        == """Dataset(
   name=very_loooooooong_name,
   num_blocks=20,
   num_rows=100,
   schema={id: int64}
)"""
    )


def test_op_metrics_logging():
    logger = DatasetLogger(
        "ray.data._internal.execution.streaming_executor"
    ).get_logger()
    with patch.object(logger, "info") as mock_logger:
        ray.data.range(100).map_batches(lambda x: x).materialize()
        logs = [canonicalize(call.args[0]) for call in mock_logger.call_args_list]
        input_str = (
            "Operator InputDataBuffer[Input] completed. Operator Metrics:\n"
            + gen_expected_metrics(is_map=False)
        )
        map_str = (
            "Operator InputDataBuffer[Input] -> "
            "TaskPoolMapOperator[ReadRange->MapBatches(<lambda>)] completed. "
            "Operator Metrics:\n"
        ) + STANDARD_EXTRA_METRICS

        # Check that these strings are logged exactly once.
        assert sum([log == input_str for log in logs]) == 1
        assert sum([log == map_str for log in logs]) == 1


def test_op_state_logging():
    logger = DatasetLogger(
        "ray.data._internal.execution.streaming_executor"
    ).get_logger()
    with patch.object(logger, "info") as mock_logger:
        ray.data.range(100).map_batches(lambda x: x).materialize()
        logs = [canonicalize(call.args[0]) for call in mock_logger.call_args_list]

        times_asserted = 0
        for i, log in enumerate(logs):
            if log == "Execution Progress:":
                times_asserted += 1
                assert "Input" in logs[i + 1]
                assert "ReadRange->MapBatches(<lambda>)" in logs[i + 2]
        assert times_asserted > 0


def test_stats_actor_datasets(ray_start_cluster):
    ds = ray.data.range(100, parallelism=20).map_batches(lambda x: x)
    ds._set_name("test_stats_actor_datasets")
    ds.materialize()
    stats_actor = _get_or_create_stats_actor()

    datasets = ray.get(stats_actor.get_datasets.remote())
    dataset_name = list(filter(lambda x: x.startswith(ds._name), datasets))
    assert len(dataset_name) == 1
    dataset = datasets[dataset_name[0]]

    assert dataset["state"] == "FINISHED"
    assert dataset["progress"] == 20
    assert dataset["total"] == 20
    assert dataset["end_time"] is not None

    operators = dataset["operators"]
    assert len(operators) == 2
    assert "Input0" in operators
    assert "ReadRange->MapBatches(<lambda>)1" in operators
    for value in operators.values():
        assert value["progress"] == 20
        assert value["total"] == 20
        assert value["state"] == "FINISHED"


@patch.object(StatsManager, "STATS_ACTOR_UPDATE_INTERVAL_SECONDS", new=0.5)
@patch.object(StatsManager, "_stats_actor_handle")
@patch.object(StatsManager, "UPDATE_THREAD_INACTIVITY_LIMIT", new=1)
def test_stats_manager(shutdown_only):
    ray.init()
    num_threads = 10

    datasets = [None] * num_threads
    # Mock clear methods so that _last_execution_stats and _last_iteration_stats
    # are not cleared. We will assert on them afterwards.
    with patch.object(StatsManager, "clear_execution_metrics"), patch.object(
        StatsManager, "clear_iteration_metrics"
    ):

        def update_stats_manager(i):
            datasets[i] = ray.data.range(10).map_batches(lambda x: x)
            for _ in datasets[i].iter_batches(batch_size=1):
                pass

        threads = [
            threading.Thread(target=update_stats_manager, args=(i,), daemon=True)
            for i in range(num_threads)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(StatsManager._last_execution_stats) == num_threads
    assert len(StatsManager._last_iteration_stats) == num_threads

    # Clear dataset tags manually.
    for dataset in datasets:
        dataset_tag = create_dataset_tag(dataset._name, dataset._uuid)
        assert dataset_tag in StatsManager._last_execution_stats
        assert dataset_tag in StatsManager._last_iteration_stats
        StatsManager.clear_execution_metrics(
            dataset_tag, ["Input0", "ReadRange->MapBatches(<lambda>)1"]
        )
        StatsManager.clear_iteration_metrics(dataset_tag)

    wait_for_condition(lambda: not StatsManager._update_thread.is_alive())
    prev_thread = StatsManager._update_thread

    ray.data.range(100).map_batches(lambda x: x).materialize()
    # Check that a new different thread is spawned.
    assert StatsManager._update_thread != prev_thread
    wait_for_condition(lambda: not StatsManager._update_thread.is_alive())


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-vv", __file__]))
