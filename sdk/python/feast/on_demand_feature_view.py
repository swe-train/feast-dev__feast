import copy
import functools
import inspect
import warnings
from datetime import datetime
from types import FunctionType
from typing import Any, Dict, List, Optional, Type, Union

import dill
import pandas as pd
from typeguard import typechecked

from feast.base_feature_view import BaseFeatureView
from feast.batch_feature_view import BatchFeatureView
from feast.data_source import RequestSource
from feast.errors import RegistryInferenceFailure, SpecifiedFeaturesNotPresentError
from feast.feature_view import FeatureView
from feast.feature_view_projection import FeatureViewProjection
from feast.field import Field, from_value_type
from feast.protos.feast.core.OnDemandFeatureView_pb2 import (
    OnDemandFeatureView as OnDemandFeatureViewProto,
)
from feast.protos.feast.core.OnDemandFeatureView_pb2 import (
    OnDemandFeatureViewMeta,
    OnDemandFeatureViewSpec,
    OnDemandSource,
)
from feast.protos.feast.core.Transformation_pb2 import (
    FeatureTransformationV2 as FeatureTransformationProto,
)
from feast.protos.feast.core.Transformation_pb2 import (
    UserDefinedFunctionV2 as UserDefinedFunctionProto,
)
from feast.transformation.pandas_transformation import PandasTransformation
from feast.transformation.substrait_transformation import SubstraitTransformation
from feast.type_map import (
    feast_value_type_to_pandas_type,
    python_type_to_feast_value_type,
)
from feast.usage import log_exceptions
from feast.value_type import ValueType

warnings.simplefilter("once", DeprecationWarning)


@typechecked
class OnDemandFeatureView(BaseFeatureView):
    """
    [Experimental] An OnDemandFeatureView defines a logical group of features that are
    generated by applying a transformation on a set of input sources, such as feature
    views and request data sources.

    Attributes:
        name: The unique name of the on demand feature view.
        features: The list of features in the output of the on demand feature view.
        source_feature_view_projections: A map from input source names to actual input
            sources with type FeatureViewProjection.
        source_request_sources: A map from input source names to the actual input
            sources with type RequestSource.
        transformation: The user defined transformation.
        description: A human-readable description.
        tags: A dictionary of key-value pairs to store arbitrary metadata.
        owner: The owner of the on demand feature view, typically the email of the primary
            maintainer.
    """

    name: str
    features: List[Field]
    source_feature_view_projections: Dict[str, FeatureViewProjection]
    source_request_sources: Dict[str, RequestSource]
    transformation: Union[PandasTransformation]
    feature_transformation: Union[PandasTransformation]
    description: str
    tags: Dict[str, str]
    owner: str

    @log_exceptions  # noqa: C901
    def __init__(  # noqa: C901
        self,
        *,
        name: str,
        schema: List[Field],
        sources: List[
            Union[
                FeatureView,
                RequestSource,
                FeatureViewProjection,
            ]
        ],
        udf: Optional[FunctionType] = None,
        udf_string: str = "",
        transformation: Optional[Union[PandasTransformation]] = None,
        feature_transformation: Optional[Union[PandasTransformation]] = None,
        description: str = "",
        tags: Optional[Dict[str, str]] = None,
        owner: str = "",
    ):
        """
        Creates an OnDemandFeatureView object.

        Args:
            name: The unique name of the on demand feature view.
            schema: The list of features in the output of the on demand feature view, after
                the transformation has been applied.
            sources: A map from input source names to the actual input sources, which may be
                feature views, or request data sources. These sources serve as inputs to the udf,
                which will refer to them by name.
            udf (deprecated): The user defined transformation function, which must take pandas
                dataframes as inputs.
            udf_string (deprecated): The source code version of the udf (for diffing and displaying in Web UI)
            transformation: The user defined transformation.
            feature_transformation: The user defined transformation.
            description (optional): A human-readable description.
            tags (optional): A dictionary of key-value pairs to store arbitrary metadata.
            owner (optional): The owner of the on demand feature view, typically the email
                of the primary maintainer.
        """
        super().__init__(
            name=name,
            features=schema,
            description=description,
            tags=tags,
            owner=owner,
        )

        if not transformation:
            if udf:
                warnings.warn(
                    "udf and udf_string parameters are deprecated. Please use transformation=OnDemandPandasTransformation(udf, udf_string) instead.",
                    DeprecationWarning,
                )
                transformation = PandasTransformation(udf, udf_string)
            else:
                raise Exception(
                    "OnDemandFeatureView needs to be initialized with either transformation or udf arguments"
                )

        self.source_feature_view_projections: Dict[str, FeatureViewProjection] = {}
        self.source_request_sources: Dict[str, RequestSource] = {}
        for odfv_source in sources:
            if isinstance(odfv_source, RequestSource):
                self.source_request_sources[odfv_source.name] = odfv_source
            elif isinstance(odfv_source, FeatureViewProjection):
                self.source_feature_view_projections[odfv_source.name] = odfv_source
            else:
                self.source_feature_view_projections[
                    odfv_source.name
                ] = odfv_source.projection

        self.transformation = transformation
        self.feature_transformation = self.transformation

    @property
    def proto_class(self) -> Type[OnDemandFeatureViewProto]:
        return OnDemandFeatureViewProto

    def __copy__(self):
        fv = OnDemandFeatureView(
            name=self.name,
            schema=self.features,
            sources=list(self.source_feature_view_projections.values())
            + list(self.source_request_sources.values()),
            transformation=self.transformation,
            feature_transformation=self.transformation,
            description=self.description,
            tags=self.tags,
            owner=self.owner,
        )
        fv.projection = copy.copy(self.projection)
        return fv

    def __eq__(self, other):
        if not isinstance(other, OnDemandFeatureView):
            raise TypeError(
                "Comparisons should only involve OnDemandFeatureView class objects."
            )

        if not super().__eq__(other):
            return False

        if (
            self.source_feature_view_projections
            != other.source_feature_view_projections
            or self.source_request_sources != other.source_request_sources
            or self.transformation != other.transformation
            or self.feature_transformation != other.feature_transformation
        ):
            return False

        return True

    def __hash__(self):
        return super().__hash__()

    def to_proto(self) -> OnDemandFeatureViewProto:
        """
        Converts an on demand feature view object to its protobuf representation.

        Returns:
            A OnDemandFeatureViewProto protobuf.
        """
        meta = OnDemandFeatureViewMeta()
        if self.created_timestamp:
            meta.created_timestamp.FromDatetime(self.created_timestamp)
        if self.last_updated_timestamp:
            meta.last_updated_timestamp.FromDatetime(self.last_updated_timestamp)
        sources = {}
        for source_name, fv_projection in self.source_feature_view_projections.items():
            sources[source_name] = OnDemandSource(
                feature_view_projection=fv_projection.to_proto()
            )
        for (
            source_name,
            request_sources,
        ) in self.source_request_sources.items():
            sources[source_name] = OnDemandSource(
                request_data_source=request_sources.to_proto()
            )

        feature_transformation = FeatureTransformationProto(
            user_defined_function=self.transformation.to_proto()
            if type(self.transformation) == PandasTransformation
            else None,
            substrait_transformation=self.transformation.to_proto()
            if type(self.transformation) == SubstraitTransformation
            else None,  # type: ignore
        )
        spec = OnDemandFeatureViewSpec(
            name=self.name,
            features=[feature.to_proto() for feature in self.features],
            sources=sources,
            feature_transformation=feature_transformation,
            description=self.description,
            tags=self.tags,
            owner=self.owner,
        )

        return OnDemandFeatureViewProto(spec=spec, meta=meta)

    @classmethod
    def from_proto(cls, on_demand_feature_view_proto: OnDemandFeatureViewProto):
        """
        Creates an on demand feature view from a protobuf representation.

        Args:
            on_demand_feature_view_proto: A protobuf representation of an on-demand feature view.

        Returns:
            A OnDemandFeatureView object based on the on-demand feature view protobuf.
        """
        sources = []
        for (
            _,
            on_demand_source,
        ) in on_demand_feature_view_proto.spec.sources.items():
            if on_demand_source.WhichOneof("source") == "feature_view":
                sources.append(
                    FeatureView.from_proto(on_demand_source.feature_view).projection
                )
            elif on_demand_source.WhichOneof("source") == "feature_view_projection":
                sources.append(
                    FeatureViewProjection.from_proto(
                        on_demand_source.feature_view_projection
                    )
                )
            else:
                sources.append(
                    RequestSource.from_proto(on_demand_source.request_data_source)
                )

        if (
            on_demand_feature_view_proto.spec.feature_transformation.WhichOneof(
                "transformation"
            )
            == "user_defined_function"
            and on_demand_feature_view_proto.spec.feature_transformation.user_defined_function.body_text
            != ""
        ):
            transformation = PandasTransformation.from_proto(
                on_demand_feature_view_proto.spec.feature_transformation.user_defined_function
            )
        elif (
            on_demand_feature_view_proto.spec.feature_transformation.WhichOneof(
                "transformation"
            )
            == "substrait_transformation"
        ):
            transformation = SubstraitTransformation.from_proto(
                on_demand_feature_view_proto.spec.feature_transformation.substrait_transformation
            )
        elif (
            hasattr(on_demand_feature_view_proto.spec, "user_defined_function")
            and on_demand_feature_view_proto.spec.feature_transformation.user_defined_function.body_text
            == ""
        ):
            backwards_compatible_udf = UserDefinedFunctionProto(
                name=on_demand_feature_view_proto.spec.user_defined_function.name,
                body=on_demand_feature_view_proto.spec.user_defined_function.body,
                body_text=on_demand_feature_view_proto.spec.user_defined_function.body_text,
            )
            transformation = PandasTransformation.from_proto(
                user_defined_function_proto=backwards_compatible_udf,
            )
        else:
            raise Exception("At least one transformation type needs to be provided")

        on_demand_feature_view_obj = cls(
            name=on_demand_feature_view_proto.spec.name,
            schema=[
                Field(
                    name=feature.name,
                    dtype=from_value_type(ValueType(feature.value_type)),
                )
                for feature in on_demand_feature_view_proto.spec.features
            ],
            sources=sources,
            transformation=transformation,
            description=on_demand_feature_view_proto.spec.description,
            tags=dict(on_demand_feature_view_proto.spec.tags),
            owner=on_demand_feature_view_proto.spec.owner,
        )

        # FeatureViewProjections are not saved in the OnDemandFeatureView proto.
        # Create the default projection.
        on_demand_feature_view_obj.projection = FeatureViewProjection.from_definition(
            on_demand_feature_view_obj
        )

        if on_demand_feature_view_proto.meta.HasField("created_timestamp"):
            on_demand_feature_view_obj.created_timestamp = (
                on_demand_feature_view_proto.meta.created_timestamp.ToDatetime()
            )
        if on_demand_feature_view_proto.meta.HasField("last_updated_timestamp"):
            on_demand_feature_view_obj.last_updated_timestamp = (
                on_demand_feature_view_proto.meta.last_updated_timestamp.ToDatetime()
            )

        return on_demand_feature_view_obj

    def get_request_data_schema(self) -> Dict[str, ValueType]:
        schema: Dict[str, ValueType] = {}
        for request_source in self.source_request_sources.values():
            if isinstance(request_source.schema, List):
                new_schema = {}
                for field in request_source.schema:
                    new_schema[field.name] = field.dtype.to_value_type()
                schema.update(new_schema)
            elif isinstance(request_source.schema, Dict):
                schema.update(request_source.schema)
            else:
                raise Exception(
                    f"Request source schema is not correct type: ${str(type(request_source.schema))}"
                )
        return schema

    def get_transformed_features_df(
        self,
        df_with_features: pd.DataFrame,
        full_feature_names: bool = False,
    ) -> pd.DataFrame:
        # Apply on demand transformations
        columns_to_cleanup = []
        for source_fv_projection in self.source_feature_view_projections.values():
            for feature in source_fv_projection.features:
                full_feature_ref = f"{source_fv_projection.name}__{feature.name}"
                if full_feature_ref in df_with_features.keys():
                    # Make sure the partial feature name is always present
                    df_with_features[feature.name] = df_with_features[full_feature_ref]
                    columns_to_cleanup.append(feature.name)
                elif feature.name in df_with_features.keys():
                    # Make sure the full feature name is always present
                    df_with_features[full_feature_ref] = df_with_features[feature.name]
                    columns_to_cleanup.append(full_feature_ref)

        # Compute transformed values and apply to each result row

        df_with_transformed_features = self.transformation.transform(df_with_features)

        # Work out whether the correct columns names are used.
        rename_columns: Dict[str, str] = {}
        for feature in self.features:
            short_name = feature.name
            long_name = f"{self.projection.name_to_use()}__{feature.name}"
            if (
                short_name in df_with_transformed_features.columns
                and full_feature_names
            ):
                rename_columns[short_name] = long_name
            elif not full_feature_names:
                # Long name must be in dataframe.
                rename_columns[long_name] = short_name

        # Cleanup extra columns used for transformation
        df_with_features.drop(columns=columns_to_cleanup, inplace=True)
        return df_with_transformed_features.rename(columns=rename_columns)

    def infer_features(self) -> None:
        """
        Infers the set of features associated to this feature view from the input source.

        Raises:
            RegistryInferenceFailure: The set of features could not be inferred.
        """
        rand_df_value: Dict[str, Any] = {
            "float": 1.0,
            "int": 1,
            "str": "hello world",
            "bytes": str.encode("hello world"),
            "bool": True,
            "datetime64[ns]": datetime.utcnow(),
        }

        df = pd.DataFrame()
        for feature_view_projection in self.source_feature_view_projections.values():
            for feature in feature_view_projection.features:
                dtype = feast_value_type_to_pandas_type(feature.dtype.to_value_type())
                df[f"{feature_view_projection.name}__{feature.name}"] = pd.Series(
                    dtype=dtype
                )
                sample_val = rand_df_value[dtype] if dtype in rand_df_value else None
                df[f"{feature.name}"] = pd.Series(data=sample_val, dtype=dtype)
        for request_data in self.source_request_sources.values():
            for field in request_data.schema:
                dtype = feast_value_type_to_pandas_type(field.dtype.to_value_type())
                sample_val = rand_df_value[dtype] if dtype in rand_df_value else None
                df[f"{field.name}"] = pd.Series(sample_val, dtype=dtype)
        output_df: pd.DataFrame = self.transformation.transform(df)
        inferred_features = []
        for f, dt in zip(output_df.columns, output_df.dtypes):
            inferred_features.append(
                Field(
                    name=f,
                    dtype=from_value_type(
                        python_type_to_feast_value_type(f, type_name=str(dt))
                    ),
                )
            )

        if self.features:
            missing_features = []
            for specified_features in self.features:
                if specified_features not in inferred_features:
                    missing_features.append(specified_features)
            if missing_features:
                raise SpecifiedFeaturesNotPresentError(
                    missing_features, inferred_features, self.name
                )
        else:
            self.features = inferred_features

        if not self.features:
            raise RegistryInferenceFailure(
                "OnDemandFeatureView",
                f"Could not infer Features for the feature view '{self.name}'.",
            )

    @staticmethod
    def get_requested_odfvs(
        feature_refs, project, registry
    ) -> List["OnDemandFeatureView"]:
        all_on_demand_feature_views = registry.list_on_demand_feature_views(
            project, allow_cache=True
        )
        requested_on_demand_feature_views: List[OnDemandFeatureView] = []
        for odfv in all_on_demand_feature_views:
            for feature in odfv.features:
                if f"{odfv.name}:{feature.name}" in feature_refs:
                    requested_on_demand_feature_views.append(odfv)
                    break
        return requested_on_demand_feature_views


def on_demand_feature_view(
    *,
    schema: List[Field],
    sources: List[
        Union[
            FeatureView,
            RequestSource,
            FeatureViewProjection,
        ]
    ],
    description: str = "",
    tags: Optional[Dict[str, str]] = None,
    owner: str = "",
):
    """
    Creates an OnDemandFeatureView object with the given user function as udf.

    Args:
        schema: The list of features in the output of the on demand feature view, after
            the transformation has been applied.
        sources: A map from input source names to the actual input sources, which may be
            feature views, or request data sources. These sources serve as inputs to the udf,
            which will refer to them by name.
        description (optional): A human-readable description.
        tags (optional): A dictionary of key-value pairs to store arbitrary metadata.
        owner (optional): The owner of the on demand feature view, typically the email
            of the primary maintainer.
    """

    def mainify(obj) -> None:
        # Needed to allow dill to properly serialize the udf. Otherwise, clients will need to have a file with the same
        # name as the original file defining the ODFV.
        if obj.__module__ != "__main__":
            obj.__module__ = "__main__"

    def decorator(user_function):
        return_annotation = inspect.signature(user_function).return_annotation
        if (
            return_annotation
            and return_annotation.__module__ == "ibis.expr.types.relations"
            and return_annotation.__name__ == "Table"
        ):
            import ibis
            import ibis.expr.datatypes as dt
            from ibis_substrait.compiler.core import SubstraitCompiler

            compiler = SubstraitCompiler()

            input_fields: Field = []

            for s in sources:
                if type(s) == FeatureView:
                    fields = s.projection.features
                else:
                    fields = s.features

                input_fields.extend(
                    [
                        (
                            f.name,
                            dt.dtype(
                                feast_value_type_to_pandas_type(f.dtype.to_value_type())
                            ),
                        )
                        for f in fields
                    ]
                )

            expr = user_function(ibis.table(input_fields, "t"))

            transformation = SubstraitTransformation(
                substrait_plan=compiler.compile(expr).SerializeToString()
            )
        else:
            udf_string = dill.source.getsource(user_function)
            mainify(user_function)
            transformation = PandasTransformation(user_function, udf_string)

        on_demand_feature_view_obj = OnDemandFeatureView(
            name=user_function.__name__,
            sources=sources,
            schema=schema,
            transformation=transformation,
            description=description,
            tags=tags,
            owner=owner,
        )
        functools.update_wrapper(
            wrapper=on_demand_feature_view_obj, wrapped=user_function
        )
        return on_demand_feature_view_obj

    return decorator


def feature_view_to_batch_feature_view(fv: FeatureView) -> BatchFeatureView:
    bfv = BatchFeatureView(
        name=fv.name,
        entities=fv.entities,
        ttl=fv.ttl,
        tags=fv.tags,
        online=fv.online,
        owner=fv.owner,
        schema=fv.schema,
        source=fv.batch_source,
    )

    bfv.features = copy.copy(fv.features)
    bfv.entities = copy.copy(fv.entities)
    return bfv
