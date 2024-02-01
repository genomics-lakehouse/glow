# Copyright 2019 The Glow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .ridge_udfs import *
from .ridge_reduction import RidgeReduction
from .model_functions import _prepare_labels_and_warn, _prepare_covariates, _check_model, _check_cv
from nptyping import Float, NDArray
import pandas as pd
from pyspark.sql import DataFrame, Row
import pyspark.sql.functions as f
from typeguard import typechecked
from typing import Any, Dict, List, Union
from glow.logging import record_hls_event
import warnings

# Ignore warning to use applyInPandas instead of apply
# TODO(hhd): Remove this and start using applyInPandas once we only support Spark 3.x.
warnings.filterwarnings('ignore', category=UserWarning, message='.*applyInPandas.*')

__all__ = ['RidgeRegression']


@typechecked
class RidgeRegression:
    """
    The RidgeRegression class is used to fit ridge models against one or more labels optimized over a provided list of
    ridge alpha parameters.  It is similar in function to RidgeReduction except that whereas RidgeReduction attempts to
    reduce a starting matrix X to a block matrix of smaller dimension, RidgeRegression is intended to find an optimal
    model of the form Y_hat ~ XB, where Y_hat is a matrix of one or more predicted labels and B is a matrix of
    coefficients.  The optimal ridge alpha value is chosen for each label by maximizing the average out of fold r2
    score.
    """
    def __init__(self,
                 reduced_block_df: DataFrame,
                 label_df: pd.DataFrame,
                 sample_blocks: Dict[str, List[str]],
                 cov_df: pd.DataFrame = pd.DataFrame({}),
                 add_intercept: bool = True,
                 alphas: List[float] = []) -> None:
        """
        Args:
            reduced_block_df : Spark DataFrame representing the reduced block matrix generated by
                RidgeReduction
            label_df : Pandas DataFrame containing the target labels used in fitting the ridge models
            sample_blocks : Dict containing a mapping of sample_block ID to a list of corresponding sample IDs
            cov_df : Pandas DataFrame containing covariates to be included in every model in the stacking
                ensemble (optional).
            add_intercept: If True, an intercept column (all ones) will be added to the covariates
                (as the first column)
            ridge_reduced: RidgeReduction object containing level 0 reduction data
            alphas : array_like of alpha values used in the ridge regression (optional).
        """
        self.reduced_block_df = reduced_block_df
        self.sample_blocks = sample_blocks
        self.set_label_df(label_df)
        self.set_cov_df(cov_df, add_intercept)
        self.set_alphas(alphas)
        self.model_df = None
        self.cv_df = None
        self.y_hat_df = None

    @classmethod
    def from_ridge_reduction(cls, ridge_reduced: RidgeReduction, alphas: List[float] = []):
        """
        Initializes an instance of RidgeRegression using a RidgeReduction object

        Args:
            ridge_reduced : A RidgeReduction instance based on which the RidgeRegression instance must be made
            alphas : array_like of alpha values used in the ridge regression (optional).
        """
        obj = cls.__new__(cls)
        obj.reduced_block_df = ridge_reduced.reduced_block_df
        obj.sample_blocks = ridge_reduced.sample_blocks
        obj._label_df = ridge_reduced.get_label_df()
        obj._std_label_df = ridge_reduced._std_label_df
        obj._cov_df = ridge_reduced.get_cov_df()
        obj._std_cov_df = ridge_reduced._std_cov_df
        obj.set_alphas(alphas)
        obj.model_df = None
        obj.cv_df = None
        obj.y_hat_df = None
        return obj

    def __getstate__(self):
        # Copy the object's state from self.__dict__ which contains
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        del state['reduced_block_df'], state['model_df'], state['cv_df']
        return state

    def set_label_df(self, label_df: pd.DataFrame) -> None:
        self._std_label_df = _prepare_labels_and_warn(label_df, False, 'quantitative')
        self._label_df = label_df

    def get_label_df(self) -> pd.DataFrame:
        return self._label_df

    def set_cov_df(self, cov_df: pd.DataFrame, add_intercept: bool) -> None:
        self._cov_df = cov_df
        self._std_cov_df = _prepare_covariates(cov_df, self._label_df, add_intercept)

    def get_cov_df(self) -> pd.DataFrame:
        return self._cov_df

    def set_alphas(self, alphas: List[float]) -> None:
        self._alphas = generate_alphas(
            self.reduced_block_df) if len(alphas) == 0 else create_alpha_dict(alphas)

    def get_alphas(self) -> Dict[str, Float]:
        return self._alphas

    def _cache_model_cv_df(self) -> None:
        _check_model(self.model_df)
        _check_cv(self.cv_df)
        self.model_df.cache()
        self.cv_df.cache()

    def _unpersist_model_cv_df(self) -> None:
        _check_model(self.model_df)
        _check_cv(self.cv_df)
        self.model_df.unpersist()
        self.cv_df.unpersist()

    def fit(self) -> (DataFrame, DataFrame):
        """
        Fits a ridge regression model, represented by a Spark DataFrame containing coefficients for each of the ridge
        alpha parameters, for each block in the starting reduced matrix, for each label in the target labels, as well as a
        Spark DataFrame containing the optimal ridge alpha value for each label.

        Returns:
            Two Spark DataFrames, one containing the model resulting from the fitting routine and one containing the
            results of the cross validation procedure.
        """

        map_key_pattern = ['sample_block', 'label']
        reduce_key_pattern = ['header_block', 'header', 'label']
        metric = 'r2'

        map_udf = lambda key, pdf: map_normal_eqn(key, map_key_pattern, pdf, self._std_label_df,
                                                  self.sample_blocks, self._std_cov_df)
        reduce_udf = lambda key, pdf: reduce_normal_eqn(key, reduce_key_pattern, pdf)
        model_udf = lambda key, pdf: solve_normal_eqn(key, map_key_pattern, pdf, self._std_label_df,
                                                      self._alphas, self._std_cov_df)
        score_udf = lambda key, pdf: score_models(key, map_key_pattern, pdf, self._std_label_df,
                                                  self.sample_blocks, self._alphas, self.
                                                  _std_cov_df, pd.DataFrame({}), metric)

        self.model_df = self.reduced_block_df.groupBy(map_key_pattern).applyInPandas(map_udf, normal_eqn_struct)\
            .groupBy(reduce_key_pattern).applyInPandas(reduce_udf, normal_eqn_struct)\
            .groupBy(map_key_pattern).applyInPandas(model_udf, model_struct)

        self.cv_df = cross_validation(self.reduced_block_df, self.model_df, score_udf,
                                      map_key_pattern, self._alphas, metric)

        record_hls_event('wgrRidgeRegressionFit')

        return self.model_df, self.cv_df

    def transform(self) -> pd.DataFrame:
        """
        Generates predictions for the target labels in the provided label DataFrame by applying the model resulting from
        the RidgeRegression fit method to the reduced block matrix.

        Returns:
            Pandas DataFrame containing prediction y_hat values. The shape and order match label_df such that the
            rows are indexed by sample ID and the columns by label. The column types are float64.
        """
        _check_model(self.model_df)
        _check_cv(self.cv_df)

        transform_key_pattern = ['sample_block', 'label']

        transform_udf = lambda key, pdf: apply_model(
            key, transform_key_pattern, pdf, self._std_label_df, self.sample_blocks, self._alphas,
            self._std_cov_df)

        blocked_prediction_df = apply_model_df(self.reduced_block_df, self.model_df, self.cv_df,
                                               transform_udf, reduced_matrix_struct,
                                               transform_key_pattern, 'right')

        self.y_hat_df = flatten_prediction_df(blocked_prediction_df, self.sample_blocks,
                                              self._std_label_df)

        record_hls_event('wgrRidgeRegressionTransform')

        return self.y_hat_df

    def transform_loco(self, chromosomes: List[str] = []) -> pd.DataFrame:
        """
        Generates predictions for the target labels in the provided label DataFrame by applying the model resulting from
        the RidgeRegression fit method to the starting reduced block matrix using a leave-one-chromosome-out (LOCO)
        approach (this method caches the model and cross-validation DataFrames in the process for better performance).

        Args:
            chromosomes : List of chromosomes for which to generate a prediction (optional). If not provided, the
                chromosomes will be inferred from the block matrix.

        Returns:
            Pandas DataFrame containing offset values (y_hat) per chromosome. The rows are indexed by sample ID and
            chromosome; the columns are indexed by label. The column types are float64. The DataFrame is sorted using
            chromosome as the primary sort key, and sample ID as the secondary sort key.
        """
        loco_chromosomes = chromosomes if chromosomes else infer_chromosomes(self.reduced_block_df)
        loco_chromosomes.sort()

        # Cache model and CV DataFrames to avoid re-computing for each chromosome
        self._cache_model_cv_df()

        y_hat_df = pd.DataFrame({})
        orig_model_df = self.model_df
        for chromosome in loco_chromosomes:
            print(f"Generating predictions for chromosome {chromosome}.")
            loco_model_df = self.model_df.filter(
                ~f.col('header').rlike(f'^chr_{chromosome}_(alpha|block)'))
            self.model_df = loco_model_df
            loco_y_hat_df = self.transform()
            loco_y_hat_df['contigName'] = chromosome
            y_hat_df = pd.concat([y_hat_df, loco_y_hat_df])
            self.model_df = orig_model_df

        self.y_hat_df = y_hat_df.set_index('contigName', append=True)
        self._unpersist_model_cv_df()
        return self.y_hat_df

    def fit_transform(self) -> pd.DataFrame:
        """
        Fits a ridge regression model, then transforms the matrix using the model.

        Returns:
            Pandas DataFrame containing prediction y_hat values. The shape and order match labeldf such that the
            rows are indexed by sample ID and the columns by label. The column types are float64.
        """
        self.fit()
        return self.transform()

    def fit_transform_loco(self, chromosomes: List[str] = []) -> pd.DataFrame:
        """
        Fits a ridge regression model and then generates predictions for the target labels in the provided label
        DataFrame by applying the model resulting from the RidgeRegression fit method to the starting reduced block
        matrix using a leave-one-chromosome-out (LOCO) approach ((this method caches the model and cross-validation
        DataFrames in the process for better performance).

        Args:
            chromosomes : List of chromosomes for which to generate a prediction (optional). If not provided, the
                chromosomes will be inferred from the block matrix.

        Returns:
            Pandas DataFrame containing offset values (y_hat) per chromosome. The rows are indexed by sample ID and
            chromosome; the columns are indexed by label. The column types are float64. The DataFrame is sorted using
            chromosome as the primary sort key, and sample ID as the secondary sort key.
        """
        self.fit()
        return self.transform_loco(chromosomes)
