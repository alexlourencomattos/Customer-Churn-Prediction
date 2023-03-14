from pathlib import Path

import click
import git
import mlflow
import mlflow.sklearn
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .data import FeaturePreprocessor
from .model import LogisticRegression

REPO = git.Repo(search_parent_directories=True)
VERSION = REPO.head.object.hexsha

NUM_COLS = [
    "ClientPeriod",
    "MonthlySpending",
    "TotalSpent",
]

CAT_COLS = [
    "Sex",
    "IsSeniorCitizen",
    "HasPartner",
    "HasChild",
    "HasPhoneService",
    "HasMultiplePhoneNumbers",
    "HasInternetService",
    "HasOnlineSecurityService",
    "HasOnlineBackup",
    "HasDeviceProtection",
    "HasTechSupportAccess",
    "HasOnlineTV",
    "HasMovieSubscription",
    "HasContractPhone",
    "IsBillingPaperless",
    "PaymentMethod",
]

FEATURE_COLS = NUM_COLS + CAT_COLS
TARGET_COL = "Churn"


@click.command()
@click.option(
    "-d",
    "--dataset-path",
    default="data/train.csv",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--random-state", default=42, type=int)
@click.option(
    "--test-split-ratio",
    default=0.2,
    type=click.FloatRange(0, 1, min_open=True, max_open=True),
)
def train(dataset_path: Path, random_state: int, test_split_ratio: float) -> None:
    dataset = pd.read_csv(dataset_path)
    click.echo(f"Dataset shape: {dataset.shape}.")
    features = dataset.drop(TARGET_COL, axis=1)
    target = dataset[TARGET_COL]
    features_train, features_val, target_train, target_val = train_test_split(
        features, target, test_size=test_split_ratio, random_state=random_state
    )

    mlflow.set_tracking_uri("http://0.0.0.0:5000/")
    mlflow.sklearn.autolog()

    with mlflow.start_run(tags={"mlflow.source.git.commit": VERSION}) as run:
        model = LogisticRegression(random_state=random_state)
        classifier = Pipeline(
            steps=[
                ("feature_preprocessor", FeaturePreprocessor()),
                (
                    "num_cat_preprocessor",
                    ColumnTransformer(
                        transformers=[
                            ("num", StandardScaler(), NUM_COLS),
                            ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_COLS),
                        ]
                    ),
                ),
                ("model", model.estimator),
            ]
        )

        grid_search = GridSearchCV(
            estimator=classifier,
            param_grid=model.params_grid,
            scoring="roc_auc",
            n_jobs=-1,
            cv=10,
            refit=True,
        )
        logreg = grid_search.fit(features_train, target_train)

        roc_auc = roc_auc_score(target_val, logreg.predict(features_val))
        mlflow.log_metric("roc_auc", roc_auc)
        click.echo(f"ROC AUC score: {roc_auc}.")
