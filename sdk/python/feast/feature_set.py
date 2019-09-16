# Copyright 2019 The Feast Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging

import pandas as pd
from typing import List
from collections import OrderedDict
from typing import Dict
from feast.source import Source, KafkaSource
from feast.type_map import dtype_to_value_type
from pandas.api.types import is_datetime64_ns_dtype
from feast.entity import Entity
from feast.feature import Feature, Field
from feast.core.FeatureSet_pb2 import FeatureSetSpec as FeatureSetSpecProto
from feast.core.Source_pb2 import Source as SourceProto
from feast.types import FeatureRow_pb2 as FeatureRow
from google.protobuf.timestamp_pb2 import Timestamp
from kafka import KafkaProducer
from tqdm import tqdm
from feast.type_map import pandas_dtype_to_feast_value_type
from feast.types import (
    Value_pb2 as ValueProto,
    FeatureRow_pb2 as FeatureRowProto,
    Field_pb2 as FieldProto,
)
from feast.type_map import pandas_value_to_proto_value

_logger = logging.getLogger(__name__)
DATETIME_COLUMN = "datetime"  # type: str


class FeatureSet:
    """
    Represents a collection of features.
    """

    def __init__(
        self,
        name: str,
        features: List[Feature] = None,
        entities: List[Entity] = None,
        source: Source = None,
        max_age: int = -1,
    ):
        self._name = name
        self._fields = OrderedDict()  # type: Dict[str, Field]
        if features is not None:
            self._add_fields(features)
        if entities is not None:
            self._add_fields(entities)
        if source is None:
            self._source = Source()
        self._max_age = max_age
        self._version = None
        self._client = None
        self._message_producer = None
        self._busy_ingesting = False
        self._is_dirty = True

    def __eq__(self, other):
        if not isinstance(other, FeatureSet):
            return NotImplemented

        for self_feature in self.features:
            for other_feature in other.features:
                if self_feature != other_feature:
                    return False

        for self_entity in self.entities:
            for other_entity in other.entities:
                if self_entity != other_entity:
                    return False

        if (
            self.name != other.name
            or self.version != other.version
            or self.max_age != other.max_age
            or self.source != other.source
        ):
            return False
        return True

    @property
    def features(self) -> List[Feature]:
        """
        Returns a list of features from this feature set
        """
        return [field for field in self._fields.values() if isinstance(field, Feature)]

    @property
    def entities(self) -> List[Entity]:
        """
        Returns list of entities from this feature set
        """
        return [field for field in self._fields.values() if isinstance(field, Entity)]

    @property
    def name(self):
        return self._name

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, source: Source):
        self._source = source

        # Create Kafka FeatureRow producer
        if self._message_producer is not None:
            self._message_producer = KafkaProducer(bootstrap_servers=source.brokers)

    @property
    def version(self):
        return self._version

    @property
    def max_age(self):
        return self._max_age

    @property
    def is_dirty(self):
        return self._is_dirty

    def add(self, resource):
        """
        Adds a resource (Feature, Entity) to this Feature Set.
        Does not register the updated Feature Set with Feast Core
        :param resource: A resource can be either a Feature or an Entity object
        :return:
        """
        if resource.name in self._fields.keys():
            raise ValueError(
                'could not add field "'
                + resource.name
                + '" since it already exists in feature set "'
                + self._name
                + '"'
            )

        if issubclass(type(resource), Field):
            return self._add_field(resource)

        raise ValueError("Could not identify the resource being added")

    def _add_field(self, field: Field):
        self._fields[field.name] = field
        return

    def drop(self, name: str):
        """
        Removes a Feature or Entity from a Feature Set
        :param name: Name of Feature or Entity to be removed
        """
        if name not in self._fields:
            raise ValueError("Could not find field " + name + ", no action taken")
        if name in self._fields:
            del self._fields[name]
            return

    def _add_fields(self, fields: List[Field]):
        """
        Adds multiple Fields to a Feature Set
        :param fields: List of Feature or Entity Objects
        """
        for field in fields:
            self.add(field)

    def update_from_dataset(self, df: pd.DataFrame, column_mapping=None):
        """
        Updates Feature Set values based on the data set. Only Pandas dataframes are supported.
        :param column_mapping: Dictionary of column names to resource (entity, feature) mapping. Forces the interpretation
        of a column as either an entity or feature. Example: {"driver_id": Entity(name="driver", dtype=ValueType.INT64)}
        :param df: Pandas dataframe containing datetime column, entity columns, and feature columns.
        """

        fields = OrderedDict()
        existing_entities = self._client.entities if self._client is not None else None

        # Validate whether the datetime column exists with the right name
        if DATETIME_COLUMN not in df:
            raise Exception("No column 'datetime'")

        # Validate the data type for the datetime column
        if not is_datetime64_ns_dtype(df.dtypes[DATETIME_COLUMN]):
            raise Exception(
                "Column 'datetime' does not have the correct type: datetime64[ns]"
            )

        # Iterate over all of the columns and detect their class (feature, entity) and type
        for column in df.columns:
            column = column.strip()

            # Skip datetime column
            if DATETIME_COLUMN in column:
                continue

            # Use entity or feature value if provided by the column mapping
            if column_mapping and column in column_mapping:
                if issubclass(type(column_mapping[column]), Field):
                    fields[column] = column_mapping[column]
                    continue
                raise ValueError(
                    "Invalid resource type specified at column name " + column
                )

            # Test whether this column is an existing entity (globally).
            if existing_entities and column in existing_entities:
                entity = existing_entities[column]

                # test whether registered entity type matches user provided type
                if entity.dtype == dtype_to_value_type(df[column].dtype):
                    # Store this field as an entity
                    fields[column] = entity
                    continue

            # Ignore fields that already exist
            if column in self._fields:
                continue

            # Store this field as a feature
            fields[column] = Feature(
                name=column, dtype=pandas_dtype_to_feast_value_type(df[column].dtype)
            )

        if len([field for field in fields.values() if type(field) == Entity]) == 0:
            raise Exception(
                "Could not detect entity column(s). Please provide entity column(s)."
            )
        if len([field for field in fields.values() if type(field) == Feature]) == 0:
            raise Exception(
                "Could not detect feature column(s). Please provide feature column(s)."
            )
        self._add_fields(list(fields.values()))

    def ingest(
        self, dataframe: pd.DataFrame, force_update: bool = False, timeout: int = 5
    ):
        # Update feature set from data set and re-register if changed
        if force_update:
            self.update_from_dataset(dataframe)
            self._client.apply(self)

        # Validate feature set version with Feast Core
        self._validate_feature_set()

        # Create Kafka FeatureRow producer
        if self._message_producer is None:
            self._message_producer = KafkaProducer(
                bootstrap_servers=self._get_kafka_source_brokers()
            )

        _logger.info(
            f"Publishing features to topic: '{self._get_kafka_source_topic()}' "
            f"on brokers: '{self._get_kafka_source_brokers()}'"
        )

        # Convert rows to FeatureRows and and push to stream
        for index, row in tqdm(
            dataframe.iterrows(), unit="rows", total=dataframe.shape[0]
        ):
            feature_row = self._pandas_row_to_feature_row(dataframe, row)
            self._message_producer.send(
                self._get_kafka_source_topic(), feature_row.SerializeToString()
            )

        # Wait for all messages to be completely sent
        self._message_producer.flush(timeout=timeout)

    def _pandas_row_to_feature_row(
        self, dataframe: pd.DataFrame, row
    ) -> FeatureRow.FeatureRow:
        if len(self._fields) != len(dataframe.columns) - 1:
            raise Exception(
                "Amount of entities and features in feature set do not match dataset columns"
            )

        event_timestamp = Timestamp()
        event_timestamp.FromNanoseconds(row[DATETIME_COLUMN].value)
        feature_row = FeatureRowProto.FeatureRow(
            event_timestamp=event_timestamp,
            feature_set=self.name + ":" + str(self.version),
        )

        for column in dataframe.columns:
            if column == DATETIME_COLUMN:
                continue
            proto_value = pandas_value_to_proto_value(
                pandas_dtype=dataframe[column].dtype, pandas_value=row[column]
            )
            feature_row.fields.extend(
                [FieldProto.Field(name=column, value=proto_value)]
            )
        return feature_row

    @classmethod
    def from_proto(cls, feature_set_proto: FeatureSetSpecProto):
        feature_set = cls(
            name=feature_set_proto.name,
            features=[
                Feature.from_proto(feature) for feature in feature_set_proto.features
            ],
            entities=[
                Entity.from_proto(entity) for entity in feature_set_proto.entities
            ],
        )
        feature_set._version = feature_set_proto.version
        feature_set._source = Source.from_proto(feature_set_proto.source)
        return feature_set

    def to_proto(self) -> FeatureSetSpecProto:
        return FeatureSetSpecProto(
            name=self.name,
            version=self.version,
            maxAge=self.max_age,
            source=self.source.to_proto(),
            features=[
                field.to_proto()
                for field in self._fields.values()
                if type(field) == Feature
            ],
            entities=[
                field.to_proto()
                for field in self._fields.values()
                if type(field) == Entity
            ],
        )

    def _validate_feature_set(self):
        # Validate whether the feature set has been modified and needs to be saved
        if self._is_dirty:
            raise Exception("Feature set has been modified and must be saved first")

    def _get_kafka_source_brokers(self) -> str:
        if self.source and self.source.source_type is "Kafka":
            return self.source.brokers
        raise Exception("Source type could not be identified")

    def _get_kafka_source_topic(self) -> str:
        if self.source and self.source.source_type == "Kafka":
            return self.source.topic
        raise Exception("Source type could not be identified")