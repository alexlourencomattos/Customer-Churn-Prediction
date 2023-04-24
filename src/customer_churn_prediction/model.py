from abc import ABC, abstractmethod
from contextlib import contextmanager
from copy import deepcopy
from catboost import CatBoostClassifier
import click
import git
import numpy as np
import lightgbm as lgb
import mlflow
from sklearn.ensemble import RandomForestClassifier, StackingClassifier
from sklearn.exceptions import NotFittedError
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import (
    cross_val_score,
    GridSearchCV,
    StratifiedKFold,
)
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from pytorch_tabnet.tab_model import TabNetClassifier


class MLflowLogging:
    def __init__(
        self,
        mlflow_tracking_uri="http://0.0.0.0:5000/",
        mlflow_experiment_name=None,
        logging_enabled=True,
    ) -> None:
        self.mlflow_tracking_uri = mlflow_tracking_uri
        self.mlflow_experiment_name = mlflow_experiment_name
        self.logging_enabled = logging_enabled

        self.last_run_id = None
        self.experiment_id = None

        if self.logging_enabled:
            mlflow.set_tracking_uri(self.mlflow_tracking_uri)

    def autolog(self):
        if self.logging_enabled:
            mlflow.sklearn.autolog()

    @contextmanager
    def start_run(self, run_name=None):
        if self.logging_enabled:
            repo = git.Repo(search_parent_directories=True)
            version = repo.head.object.hexsha

            mlflow.start_run(
                experiment_id=self.experiment_id,
                run_id=self.last_run_id,
                run_name=run_name,
                tags={"mlflow.source.git.commit": version},
            )
            run = mlflow.active_run()
            # store run_id and experiment_id for future calls
            self.last_run_id = run.info.run_id
            self.experiment_id = run.info.experiment_id
            try:
                yield run
            finally:
                mlflow.end_run()

    def log_metric(self, name, value):
        if self.logging_enabled:
            mlflow.log_metric(name, value)

    def log_model(self, model, artifact_path):
        if self.logging_enabled:
            mlflow.sklearn.log_model(model, artifact_path)


class MLflowModel(ABC):
    """
    A base class for creating machine learning models with MLflow support.

    Attributes:
        mlflow_tracking_uri: str
            The URI of the MLflow server to use for logging.
        mlflow_experiment_name: str
            The name of the MLflow experiment to log to.
        pipeline: Optional[Union[Pipeline, Callable[..., Pipeline]]]
            An optional pipeline for feature preprocessing.
            If provided, the model will be added to this pipeline during
            initialization.
        random_state: Optional[Union[int, RandomState]]
            An optional random state to use for reproducible results.

    Properties:
        estimator:
            The estimator used for training and prediction.
        param_grid: dict
            A dictionary specifying the hyperparameter grid for hyperparameter
            tuning.

    Methods:
        train_with_logging(X, y, run_name=None):
            Train a model with MLflow autologging enabled.
        evaluate(X_val, y_val):
            Evaluate the trained logistic regression model on a validation set.
    """

    def __init__(
        self,
        mlflow_tracking_uri="http://0.0.0.0:5000/",
        mlflow_experiment_name=None,
        pipeline=None,
        cat_features=None,
        random_state=0,
    ) -> None:
        self.mlflow_log = MLflowLogging(
            mlflow_tracking_uri,
            mlflow_experiment_name,
        )
        self.cat_features = cat_features
        self.random_state = random_state
        self.pipeline = pipeline if pipeline else Pipeline([])
        self.pipeline.steps.append(("model", self.estimator))

        self.best_estimator = None

    @property
    @abstractmethod
    def estimator(self):
        """
        An estimator object that will be used for training and evaluation.

        Returns:
            object: An instance of an estimator class.
        """
        pass

    def train_with_logging(self, X, y, run_name=None) -> "MLflowModel":
        """
        Trains the model on the given data with logging enabled.

        Parameters:
            X: numpy.ndarray or pandas.DataFrame
               Training data set features.
            y: numpy.ndarray or pandas.Series
               Training data set target variable.
            run_name: str
               Name of MLflow run
        """
        self.mlflow_log.autolog()

        with self.mlflow_log.start_run(run_name):
            # define the inner and outer cross-validation splits
            inner_cv = StratifiedKFold(
                n_splits=5, shuffle=True, random_state=self.random_state
            )
            outer_cv = StratifiedKFold(
                n_splits=5, shuffle=True, random_state=self.random_state
            )

            # inner loop
            grid_search = GridSearchCV(
                estimator=self.pipeline,
                param_grid=self.param_grid,
                scoring="roc_auc",
                cv=inner_cv,
            )

            # outer loop
            nested_score = cross_val_score(
                grid_search,
                X=X,
                y=y,
                scoring="roc_auc",
                cv=outer_cv,
                n_jobs=-1,
            )
            click.echo(
                f"Nested CV score: {nested_score.mean():.5f} "
                f"{nested_score.std():.5f}"
            )
            self.mlflow_log.log_metric(
                "nested_cv_roc_auc", nested_score.mean()
            )
            self.mlflow_log.log_metric("nested_cv_std", nested_score.std())

            # retrain model with the best params found during last outer loop
            self.best_estimator = grid_search.fit(X, y)

        return self

    @property
    @abstractmethod
    def param_grid(self) -> dict:
        """
        Defines the parameter grid for hyperparameter tuning.

        Returns:
            dict: A dictionary of hyperparameters and their values
            for hyperparameter tuning.
        """
        pass

    def evaluate(self, X_val, y_val) -> int:
        """
        Evaluate the performance of the trained model on a validation data set.

        Args:
            X_val: numpy.ndarray or pandas.DataFrame
                Validation data set features.
            y_val: numpy.ndarray or pandas.Series
                Validation data set target variable.

        Returns:
            int:
                R-squared score of the predictions.
        """
        if self.best_estimator is None:
            raise NotFittedError(
                "This model instance is not fitted yet. Call train with "
                "appropriate arguments before using this estimator."
            )
        with self.mlflow_log.start_run():
            roc_auc = roc_auc_score(y_val, self.best_estimator.predict(X_val))
            self.mlflow_log.log_metric("roc_auc", roc_auc)
            return roc_auc


class LogisticRegressionMLflow(MLflowModel):
    @property
    def estimator(self):
        return LogisticRegression(random_state=self.random_state)

    @property
    def param_grid(self) -> dict:
        params = {
            "penalty": ["l1", "l2"],
            "C": np.linspace(0.1, 2, 20),
            "fit_intercept": [True, False],
            "solver": ["saga"],
            "max_iter": [500, 1000],
        }
        return {"model__" + key: val for key, val in params.items()}


class RandomForestMLflow(MLflowModel):
    @property
    def estimator(self):
        return RandomForestClassifier(random_state=self.random_state)

    @property
    def param_grid(self) -> dict:
        params = {
            "min_samples_split": range(2, 200, 20),
            "min_samples_leaf": range(1, 200, 20),
            "n_estimators": [200],
        }
        return {"model__" + key: val for key, val in params.items()}


class KnnMLflow(MLflowModel):
    @property
    def estimator(self):
        return KNeighborsClassifier()

    @property
    def param_grid(self) -> dict:
        params = {
            "n_neighbors": range(1, 100, 10),
            "metric": [
                "cityblock",
                "cosine",
                "euclidean",
                "l1",
                "l2",
                "manhattan",
                "nan_euclidean",
            ],
        }
        return {"model__" + key: val for key, val in params.items()}


class CatBoostMLflow(MLflowModel):
    @property
    def estimator(self):
        return CatBoostClassifier(
            cat_features=self.cat_features,
            logging_level="Silent",
            eval_metric="AUC:hints=skip_train~false",
            grow_policy="Lossguide",
            metric_period=1000,
            random_seed=self.random_state,
        )

    @property
    def param_grid(self) -> dict:
        params = {
            "n_estimators": [
                5,
                10,
                20,
                30,
                40,
                50,
                70,
                100,
                150,
                200,
                250,
                300,
                500,
                1000,
            ],
            "learning_rate": [
                0.0001,
                0.0005,
                0.001,
                0.005,
                0.01,
                0.02,
                0.04,
                0.05,
                0.1,
                0.2,
                0.3,
                0.5,
            ],
            "max_depth": np.arange(4, 20, 1),
            "l2_leaf_reg": np.arange(0.1, 1, 0.05),
            "subsample": [3, 5, 7, 10],
            "random_strength": [1, 2, 5, 10, 20, 50, 100],
            "min_data_in_leaf": np.arange(10, 1001, 10),
        }
        return {"model__" + key: val for key, val in params.items()}


class LgbmMLflow(MLflowModel):
    @property
    def estimator(self):
        return lgb.LGBMClassifier(
            verbose=-1,
            boosting_type="gbdt",
            objective="binary",
            learning_rate=0.01,
            metric="auc",
            random_state=self.random_state,
        )

    @property
    def param_grid(self) -> dict:
        params = {
            "num_leaves": [5, 7, 9, 10],
            "max_depth": [4],
            "min_child_samples": range(200, 215),
            "reg_lambda": [0, 0.1, 0.2, 0.5, 0.7, 1, 1.2, 1.5, 2],
        }
        return {"model__" + key: val for key, val in params.items()}


class TabNetMLflow(MLflowModel):
    @property
    def estimator(self):
        return TabNetClassifier(
            device_name="cpu",
            verbose=0,
            seed=self.random_state,
        )

    @property
    def param_grid(self) -> dict:
        params = {
            "gamma": [0.9, 0.92, 0.95, 0.97, 0.98],
            "lambda_sparse": [
                0.001,
                0.002,
                0.003,
                0.004,
                0.005,
                0.006,
                0.007,
                0.008,
                0.009,
                0.01,
            ],
            "momentum": np.arange(0.1, 1, 0.1),
            "n_independent": [0, 1],
            "n_shared": [7, 9],
            "n_steps": [4, 5],
        }
        return {"model__" + key: val for key, val in params.items()}


class StackingMLflow(MLflowModel):
    ESTIMATORS = ["LogReg", "KNN", "RandomForest", "CatBoost"]
    META_MODEL = CatBoostClassifier(
        logging_level="Silent",
        eval_metric="AUC:hints=skip_train~false",
        metric_period=1000,
        random_seed=0,
    )

    @property
    def estimator(self):
        # inner loop
        inner_cv = StratifiedKFold(
            n_splits=5, shuffle=True, random_state=self.random_state
        )
        grid_search = GridSearchCV(
            estimator=self.META_MODEL,
            param_grid=self.param_grid,
            scoring="roc_auc",
            cv=inner_cv,
        )
        return StackingClassifier(
            estimators=[
                (estim, mlflow.sklearn.load_model(f"models:/{estim}/Staging"))
                for estim in self.ESTIMATORS
            ],
            final_estimator=grid_search,
            n_jobs=-1,
        )

    def train_with_logging(self, X, y, run_name=None) -> "MLflowModel":
        self.mlflow_log.autolog()

        with self.mlflow_log.start_run(run_name):
            # outer loop
            outer_cv = StratifiedKFold(
                n_splits=5, shuffle=True, random_state=self.random_state
            )
            model = self.estimator.fit(X, y)

            nested_score = cross_val_score(
                model,
                X=X,
                y=y,
                scoring="roc_auc",
                cv=outer_cv,
                n_jobs=-1,
            )

            click.echo(
                f"Nested CV score: {nested_score.mean():.5f} "
                f"{nested_score.std():.5f}"
            )
            self.mlflow_log.log_metric(
                "nested_cv_roc_auc", nested_score.mean()
            )
            self.mlflow_log.log_metric("nested_cv_std", nested_score.std())

            # retrain model with the best params found during last outer loop
            self.best_estimator = deepcopy(model)
            self.best_estimator.final_estimator_ = deepcopy(  # type: ignore
                model.final_estimator_.best_estimator_
            )
            self.mlflow_log.log_model(self.best_estimator, "stacking")

        return self

    @property
    def param_grid(self) -> dict:
        params = {
            "n_estimators": [10, 20],
            "max_depth": [5, 7, 10],
            "subsample": [0.1, 0.4, 0.5, 0.65],
            "l2_leaf_reg": [0, 1, 3, 4],
            "random_strength": [4, 9, 10, 11, 12, 13],
            "learning_rate": [0.04, 0.05, 0.06, 0.08, 0.1],
            "min_data_in_leaf": [10, 100],
            "grow_policy": ["SymmetricTree", "Depthwise", "Lossguide"],
        }
        return params
