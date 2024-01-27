# Copyright 2023 Aineko Authors
# SPDX-License-Identifier: Apache-2.0
"""Module for managing datasets.

Datasets are Kafka topics that are used to
communicate between nodes in the pipeline. The dataset module provides
a wrapper for the Kafka consumer and producer objects.

Note:
e.g. message:
{
    # Inferred metadata
    "timestamp": "2020-01-01 00:00:00"
    "dataset": "dataset_1",
    "source_node": "node_1",

    # User defined message
    "message": {...},
}
"""
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Union

from confluent_kafka import (  # type: ignore
    OFFSET_INVALID,
    Consumer,
    KafkaError,
    Message,
    Producer,
)

from aineko.config import AINEKO_CONFIG, DEFAULT_KAFKA_CONFIG
from aineko.models.base import MessageData
from aineko.models.config_schema import Dataset

logger = logging.getLogger(__name__)


# pylint: disable=too-few-public-methods
class DatasetConsumer:
    """Wrapper class for Kafka consumer object.

    DatasetConsumer objects are designed to consume messages from a single
    dataset and will consume the next unconsumed message in the queue.

    When accessing kafka topics, prefixes will automatically be added to the
    dataset name as part of namespacing. For datasets defined in the pipeline
    config, `has_pipeline_prefix` will be set to `True`, so a dataset named
    `my_dataset` will point to a topic named `my_pipeline.my_dataset`.

    Optionally, a custom prefix can be provided that will apply to all datasets.
    In the above example, if the prefix is set to `test`, the topic name will
    be `test.my_pipeline.my_dataset`.

    Args:
        dataset_name: name of the dataset
        node_name: name of the node that is consuming the dataset
        pipeline_name: name of the pipeline
        dataset_config: dataset config
        bootstrap_servers: bootstrap_servers to connect to (e.g. "1.2.3.4:9092")
        prefix: prefix for topic name (<prefix>.<dataset_name>)
        has_pipeline_prefix: whether the dataset name has pipeline name prefix

    Attributes:
        consumer: Kafka consumer object
        cached: if the high watermark offset has been cached (updated when
            message consumed)

    Methods:
        consume: reads a message from the dataset
    """

    def __init__(
        self,
        dataset_name: str,
        node_name: str,
        pipeline_name: str,
        dataset_config: Union[Dict[str, Any], Dataset],
        bootstrap_servers: Optional[str] = None,
        prefix: Optional[str] = None,
        has_pipeline_prefix: bool = False,
    ):
        """Initialize the consumer."""
        self.pipeline_name = pipeline_name
        self.kafka_config = DEFAULT_KAFKA_CONFIG
        self.prefix = prefix
        self.has_pipeline_prefix = has_pipeline_prefix
        self.cached = False

        if isinstance(dataset_config, dict):
            if "type" not in dataset_config:
                dataset_config["type"] = AINEKO_CONFIG.get("KAFKA_STREAM_TYPE")

            dataset_config = Dataset(**dataset_config)

        consumer_config = self.kafka_config.get("CONSUMER_CONFIG")
        # Overwrite bootstrap server with broker if provided
        if bootstrap_servers:
            consumer_config["bootstrap.servers"] = bootstrap_servers

        if dataset_config.params is None:
            dataset_config.params = {}

        # Override default config with dataset specific config
        for param, value in dataset_config.params.items():
            consumer_config[param] = value

        topic_name = dataset_name
        if has_pipeline_prefix:
            topic_name = f"{pipeline_name}.{dataset_name}"

        if self.prefix:
            self.name = f"{prefix}.{pipeline_name}.{node_name}"
            consumer_config["group.id"] = self.name
            self.consumer = Consumer(consumer_config)
            self.consumer.subscribe([f"{prefix}.{topic_name}"])

        else:
            self.name = f"{pipeline_name}.{node_name}"
            consumer_config["group.id"] = f"{pipeline_name}.{node_name}"
            self.consumer = Consumer(consumer_config)
            self.consumer.subscribe([topic_name])

        self.topic_name = topic_name

    def _validate_message(
        self,
        raw_message: Optional[Message] = None,
    ) -> Optional[MessageData]:
        """Checks if a message is valid and converts it to appropriate format.

        Args:
            message: message to check

        Returns:
            message if valid, None if not
        """
        # Check if message is valid
        if raw_message is None or raw_message.value() is None:
            return None

        # Check if message is an error
        if raw_message.error():
            logger.error(str(raw_message.error()))
            return None

        # Convert message to dict
        raw_message = raw_message.value()
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")

        if isinstance(raw_message, str):
            try:
                raw_message = json.loads(raw_message)
            except json.JSONDecodeError as err:
                logger.error(
                    "Error decoding message from dataset %s: %s",
                    self.topic_name,
                    err,
                )
                return None

        # Convert message to WrappedMessage
        wrapped_message = MessageData(**raw_message)

        return wrapped_message

    def _update_offset_to_latest(self) -> None:
        """Updates offset to latest.

        Note that the initial call, for this method might take
        a while due to consumer initialization.
        """
        partitions = self.consumer.assignment()
        # Initialize consumers if not already initialized by polling
        while not partitions:
            self.consumer.poll(timeout=0)
            partitions = self.consumer.assignment()

        for partition in partitions:
            high_offset = self.consumer.get_watermark_offsets(
                partition, cached=self.cached
            )[1]

            # Invalid high offset can be caused by various reasons,
            # including rebalancing and empty topic. Default to -1.
            if high_offset == OFFSET_INVALID:
                logger.error(
                    "Invalid offset received for consumer: %s", self.name
                )
                partition.offset = -1
            else:
                partition.offset = high_offset - 1

        self.consumer.assign(partitions)

    def consume(
        self,
        how: Literal["next", "last"] = "next",
        timeout: Optional[float] = None,
    ) -> Optional[MessageData]:
        """Polls a message from the dataset.

        If the consume method is last but the method encounters
        an error trying to udpdate the offset to latest, it will
        poll and return None.

        Args:
            how: how to read the message.
                "next": read the next message in the queue
                "last": read the last message in the queue
            timeout: seconds to poll for a response from kafka broker.
                If using how="last", set to bigger than 0.

        Returns:
            message from the dataset

        Raises:
            ValueError: if how is not "next" or "last"
        """
        if how not in ["next", "last"]:
            raise ValueError(f"Invalid how: {how}. Expected `next` or `last`.")

        timeout = timeout or self.kafka_config.get("CONSUMER_TIMEOUT")
        if how == "next":
            # next unread message from queue
            wrapped_message = self.consumer.poll(timeout=timeout)

        elif how == "last":
            # last message from queue
            try:
                self._update_offset_to_latest()
            except KafkaError as e:
                logger.error(
                    "Error updating offset to latest for consumer %s: %s",
                    self.name,
                    e,
                )
                return None
            wrapped_message = self.consumer.poll(timeout=timeout)

        self.cached = True

        return self._validate_message(wrapped_message)

    def _consume_message(
        self, how: Literal["next", "last"], timeout: Optional[float] = None
    ) -> MessageData:
        """Calls the consume method and blocks until a message is returned.

        Args:
            how: See `consume` method for available options.

        Returns:
            message from dataset
        """
        while True:
            try:
                message = self.consume(how=how, timeout=timeout)
                if message is not None:
                    return message
            except KafkaError as e:
                if e.code() == "_MAX_POLL_EXCEEDED":
                    continue
                raise e

    def next(self) -> MessageData:
        """Consumes the next message from the dataset.

        Wraps the `consume(how="next")` method. It implements a
        block that waits until a message is received before returning it.
        This method ensures that every message is consumed, but the consumed
        message may not be the most recent message if the consumer is slower
        than the producer.

        This is useful when the timeout is short and you expect the consumer
        to often return `None`.

        Returns:
            message from the dataset
        """
        return self._consume_message(how="next")

    def last(self, timeout: int = 1) -> MessageData:
        """Consumes the last message from the dataset.

        Wraps the `consume(how="last")` method. It implements a
        block that waits until a message is received before returning it.
        This method ensures that the consumed message is always the most
        recent message. If the consumer is slower than the producer, messages
        might be skipped. If the consumer is faster than the producer,
        messages might be repeated.

        This is useful when the timeout is short and you expect the consumer
        to often return `None`.

        Note: The timeout must be greater than 0 to prevent
        overwhelming the broker with requests to update the offset.

        Args:
            timeout: seconds to poll for a response from kafka broker.
                Must be >0.

        Returns:
            message from the dataset

        Raises:
            ValueError: if timeout is <= 0
        """
        if timeout <= 0:
            raise ValueError(
                "Timeout must be > 0 when consuming the last message."
            )
        return self._consume_message(how="last", timeout=timeout)

    def consume_all(self, end_message: Union[str, dict]) -> List[MessageData]:
        """Reads all messages from the dataset until a specific one is found.

        Args:
            end_message: Message to trigger the completion of consumption

        Returns:
            list of messages from the dataset
        """
        messages = []
        while True:
            wrapped_message = self.consume()
            if wrapped_message is None:
                continue
            if wrapped_message.message == end_message:
                break
            messages.append(wrapped_message)
        return messages


class DatasetProducer:
    """Wrapper class for Kafka producer object.

    See DatasetConsumer for prefix rules.

    Args:
        dataset_name: dataset name
        node_name: name of the node that is producing the message
        pipeline_name: name of the pipeline
        dataset_config: dataset config
        prefix: prefix for topic name (<prefix>.<dataset_name>)
        has_pipeline_prefix: whether the dataset name has pipeline name prefix

    Attributes:
        producer: Kafka producer object

    Methods:
        produce: produce a message to the dataset
    """

    def __init__(
        self,
        dataset_name: str,
        node_name: str,
        pipeline_name: str,
        dataset_config: Union[Dict, Dataset],
        prefix: Optional[str] = None,
        has_pipeline_prefix: bool = False,
    ):
        """Initialize the producer."""
        self.source_pipeline = pipeline_name
        self.dataset = dataset_name
        self.source_node = node_name
        self.prefix = prefix
        self.has_pipeline_prefix = has_pipeline_prefix

        # Create topic name based on prefix rules
        topic_name = dataset_name
        if has_pipeline_prefix:
            topic_name = f"{pipeline_name}.{topic_name}"
        if prefix:
            topic_name = f"{prefix}.{topic_name}"
        self.topic_name = topic_name

        # Assign kafka config
        self.kafka_config = DEFAULT_KAFKA_CONFIG

        # Set producer parameters
        producer_config = self.kafka_config.get("PRODUCER_CONFIG")

        if isinstance(dataset_config, dict):
            dataset_config = Dataset(**dataset_config)

        # Override default config with dataset specific config
        if dataset_config.params:
            for param in self.kafka_config.get("PRODUCER_OVERRIDABLES"):
                if param in dataset_config.params:
                    producer_config[param] = dataset_config.params[param]

        # Create producer
        self.producer = Producer(producer_config)

    @staticmethod
    def _delivery_report(err: Any, message: Message) -> None:
        """Called once for each message produced to indicate delivery result.

        Triggered by poll() or flush().

        Args:
            err: error message
            message: message object from Kafka
        """
        if err is not None:
            logger.error("Message %s delivery failed: %s", message, err)

    def produce(
        self, message: Union[dict, str], key: Optional[str] = None
    ) -> None:
        """Produce a message to the dataset.

        Args:
            message: message to produce to the dataset
            key: key to use for the message
        """
        wrapped_message = MessageData(
            dataset=self.dataset,
            message=message,
            source_node=self.source_node,
            source_pipeline=self.source_pipeline,
        ).model_dump(mode="json")

        self.producer.poll(0)

        key_bytes = str(key).encode("utf-8") if key is not None else None

        self.producer.produce(
            topic=self.topic_name,
            key=key_bytes,
            value=json.dumps(wrapped_message).encode("utf-8"),
            callback=self._delivery_report,
        )
        self.producer.flush()


# pylint: enable=too-few-public-methods
# pylint: disable=unused-argument
class FakeDatasetConsumer:
    """Fake dataset consumer for testing purposes.

    The class will store in its state the list of values to feed the node,
    and pop each value everytime the consume method is called.

    Args:
        dataset_name: name of the dataset
        node_name: name of the node that is consuming the dataset
        source_pipeline: name of the pipeline that is consuming the dataset
        values: list of values to feed the node

    Attributes:
        dataset_name (str): name of the dataset
        node_name (str): name of the node that is consuming the dataset
        values (list): list of mock data values to feed the node
        empty (bool): True if the list of values is empty, False otherwise
    """

    def __init__(
        self,
        dataset_name: str,
        node_name: str,
        source_pipeline: str,
        values: list,
    ):
        """Initialize the consumer."""
        self.dataset_name = dataset_name
        self.node_name = node_name
        self.source_pipeline = source_pipeline
        self.values = values
        self.empty = False

    def consume(
        self,
        how: Literal["next", "last"] = "next",
        timeout: Optional[float] = None,
    ) -> Optional[MessageData]:
        """Reads a message from the dataset.

        Args:
            how: how to read the message
                "next": read the next message in the queue
                ":last": read the last message in the queue

        Returns:
            next or last value in self.values

        Raises:
            ValueError: if how is not "next" or "last"
        """
        if how not in ["next", "last"]:
            raise ValueError(f"Invalid how: {how}. Expected 'next' or 'last'.")

        if how == "next":
            remaining = len(self.values)
            if remaining > 0:
                if remaining == 1:
                    self.empty = True
                return MessageData(
                    dataset=self.dataset_name,
                    message=self.values.pop(0),
                    source_node=self.node_name,
                    source_pipeline=self.source_pipeline,
                )
        if how == "last":
            if self.values:
                return self.values[-1]

        return None

    def next(self) -> Optional[MessageData]:
        """Wraps `consume(how="next")`, blocks until available.

        Returns:
            msg: message from the dataset
        """
        return self.consume(how="next")

    def last(self, timeout: float = 1) -> Optional[MessageData]:
        """Wraps `consume(how="last")`, blocks until available.

        Returns:
            msg: message from the dataset
        """
        return self.consume(how="last", timeout=timeout)


class FakeDatasetProducer:
    """Fake dataset producer for testing purposes.

    The class will store in its state the list of values produced
    by the node.

    Args:
        dataset_name: name of the dataset
        node_name: name of the node that is producing the dataset
        source_pipeline: name of the pipeline that is producing the dataset

    Attributes:
        dataset_name (str): name of the dataset
        node_name (str): name of the node that is producing the dataset
        values (list): list of mock data values produced by the node
    """

    def __init__(self, dataset_name: str, node_name: str, source_pipeline: str):
        """Initialize the producer."""
        self.dataset_name = dataset_name
        self.node_name = node_name
        self.source_pipeline = source_pipeline
        self.values = []  # type: ignore

    def produce(self, message: Union[dict, str]) -> None:
        """Stores message in self.values.

        Args:
            message: message to produce.
        """
        wrapped_message = MessageData(
            dataset=self.dataset_name,
            source_pipeline=self.source_pipeline,
            source_node=self.node_name,
            message=message,
        )
        self.values.append(wrapped_message)
