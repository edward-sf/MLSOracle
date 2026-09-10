# MLS Oracle

Personal DS/ML project analyzing performance, compensation, and outcomes in Major League Soccer.

## Getting Started

**Requirements:** Python 3.14+ and [`uv`](https://docs.astral.sh/uv/).

To begin development on this project, clone the repo from GitHub, start a task-appropriate branch, and run the following:

```bash
uv sync
```

Then install the git hooks so lint/format/type checks run automatically before each commit:

```bash
uv run pre-commit install
```

## Linting & Type Checking

We use [`ruff`](https://docs.astral.sh/ruff/) for linting/formatting and [`mypy`](https://mypy.readthedocs.io/en/stable/) for static type checking. These also run automatically on commit via [`pre-commit`](https://pre-commit.com/) and are enforced in CI.

```bash
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy src          # type check
```

## Testing

We use [`pytest`](https://docs.pytest.org/en/stable/) (with `pytest-cov`) for tests, mirroring the `src/` layout under `tests/`.

```bash
uv run pytest
```

## Dependencies

We leverage the following libraries. This is not an exhaustive list of dependencies, as each of the following carry their own. This represents a high-level understanding of the tooling deployed for MLS Oracle.

### Data Scraping & Ingestion

| **Library** | **Description** | **Use case(s)** |
| ----------- | --------------- | --------------- |
| [`BeautifulSoup4`](https://beautiful-soup-4.readthedocs.io/en/latest/) | HTML/XML parser | <ul><li>Parse and extract data from raw HTML from custom scraping.</li></ul> |
| [`requests`](https://requests.readthedocs.io/en/latest/) | Simple HTTP in Python | <ul><li>Make HTTP/HTTPS requests simple and easy.</li></ul> |
| [`soccerdata`](https://soccerdata.readthedocs.io/en/latest/) | Scraper collection optimized to open soccer datasets (FBref, Sofascore, etc.) | <ul><li>Our go-to event-level data scrapping library.</li></ul> |
| [`tabula-py`](https://tabula-py.readthedocs.io/en/latest/) | Converts PDFs to CSV/TSV/JSON. | <ul><li>Convert MLSPA salary reports to CSV for analysis.</li></ul> |

### Spatial Analysis & Pitch Visualization

| **Library** | **Description** | **Use case(s)** |
| ----------- | --------------- | --------------- |
| [`geopandas`](https://geopandas.org/en/stable/) | Extension of `pandas` for geo-spatial data. | <ul><li>Useful for treating spatial coordinates ($x, y$ locations on the pitch) as geometric shapes. Essential if you want to calculate spatial features like team compactness, convex hulls around defensive structures, or distances to goal/defenders.</li></ul> |
| [`mplsoccer`](https://mplsoccer.readthedocs.io/en/latest/) | Built on Matplotlib; tailored to soccer visualizations | <ul><li>Pitch layouts, heatmaps, pass maps, radar charts, shot maps, etc.</li></ul> |


### Feature Engineering & Machine Learning

| **Library** | **Description** | **Use case(s)** |
| ----------- | --------------- | --------------- |
| [`kloppy`](https://pypi.org/project/kloppy/0.6.2/) | Soccer-specific ETL normalizer | <ul><li>Standardizes spatial-temporal soccer tracking and event data across multiple providers (StatsBomb, Opta, Sportec) into a unified format.</ul></li>
| [`Optuna`](https://optuna.readthedocs.io/en/stable/) | Hyperparameter optimization framework | <ul><li>Simplifies tuning ML models on match data.</li></ul> |
| [`scikit-learn`](https://scikit-learn.org/stable/user_guide.html) | Open source Machine Learning library | <ul><li>Inference and prediction through (un)supervised ML.</li></ul> |
| [`XGBoost`](https://xgboost.readthedocs.io/en/stable/) | Optimized distributed gradient boosting library. | <ul><li>Tabular match and player features, building custom models for advanced statistics like $xG$ or $xA$.</li></ul> |
