import argparse
import io
import logging
import re
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests
import tabula as tb

logger = logging.getLogger(__name__)

_SALARY_REPORTS = {
    2007: "http://s3.amazonaws.com/mlspa/2007-08-31-Salary-Information-Alphabetical.pdf?mtime=20190611125445",
    2008: "http://s3.amazonaws.com/mlspa/2008-10-07-Salary-Information-Alphabetical.pdf?mtime=20190611125423",
    2009: "http://s3.amazonaws.com/mlspa/2009-09-15-Salary-Information-Alphabetical.pdf?mtime=20190611125405",
    2010: "http://s3.amazonaws.com/mlspa/2010-08-12-Salary-Information-Alphabetical.pdf?mtime=20190611125347",
    2011: "http://s3.amazonaws.com/mlspa/2011-09-01-Salary-Information-Alphabetical.pdf?mtime=20190611125323",
    2012: "http://s3.amazonaws.com/mlspa/2012-10-01-Salary-Information-Alphabetical.pdf?mtime=20190611125245",
    2013: "http://s3.amazonaws.com/mlspa/September-15-2013-Salary-Information-Alphabetical.pdf?mtime=20180416202425",
    2014: "http://s3.amazonaws.com/mlspa/September-15-2014-Salary-Information-Alphabetical.pdf?mtime=20180416202413",
    2015: "http://s3.amazonaws.com/mlspa/September-15-2015-Salary-Information-Alphabetical.pdf?mtime=20180416202358",
    2016: "http://s3.amazonaws.com/mlspa/September-15-2016-Salary-Information-Alphabetical.pdf?mtime=20180416202344",
    2017: "http://s3.amazonaws.com/mlspa/September-15-2017-Salary-Information-Alphabetical.pdf?mtime=20180416202256",
    2018: "http://s3.amazonaws.com/mlspa/2018-09-15-Salary-Information-Alphabetical.pdf?mtime=20190611125547",
    2019: "http://s3.amazonaws.com/mlspa/Salary-List-Fall-Release-FINAL-Salary-List-Fall-Release-MLS.pdf?mtime=20190927175823",
    2020: "http://s3.amazonaws.com/mlspa/2020-Fall-Winter-Salary-List-alphabetical.pdf?mtime=20210513131818",
    2021: "http://s3.amazonaws.com/mlspa/2021-MLSPA-Fall-Salary-release.pdf?mtime=20211020145904",
    2022: "http://s3.amazonaws.com/mlspa/2022-Fall-Salary-Guide.pdf?mtime=20221017132843",
    2023: "http://s3.amazonaws.com/mlspa/2023-Salary-Report-as-of-Sept-15-2023.pdf?mtime=20231018173909",
    2024: "http://s3.amazonaws.com/mlspa/Salary-Release-FALL-2024_241024_164547.csv?mtime=20241024164547",
    2025: "http://s3.amazonaws.com/mlspa/MLS-Salary-List-10-2025-REVISED.csv?mtime=20251029164256",
    2026: "http://s3.amazonaws.com/mlspa/Spring-2026-MLSPA-Salary-Guide.csv?mtime=20260512175019",
}

_CURRENCY_RE = re.compile(r"^\$?[\d\s,]+\.\d{2}$")

type CleaningStep = Callable[[pd.DataFrame], pd.DataFrame]
type PageFixup = Callable[[list[pd.DataFrame]], list[pd.DataFrame]]
type PageMerger = Callable[[list[pd.DataFrame]], pd.DataFrame]

# Shared schema every per-year pipeline is expected to converge on before
# _normalize_columns runs.
_INTERMEDIATE_COLUMNS = ["Club", "Last Name", "First Name", "Pos", "Base Salary", "Compensation"]

_CANONICAL_COLUMNS = [
    "First Name",
    "Last Name",
    "Club",
    "Position",
    "Base Salary",
    "Guaranteed Compensation",
]

# Keyed by each intermediate-schema column name; values are every alternate
# header label seen across report years. Populated incrementally as we hit
# report years that label a column differently.
_HEADER_ALIASES: dict[str, list[str]] = {
    "Club": ["2020 Fall Salary List Club", "Team", "club", "Team Name", "Club Name"],
    "First Name": ["fname"],
    "Last Name": ["lname"],
    "Pos": ["Playing Position", "Position", "position"],
    "Base Salary": [
        "Salary",
        "CY Salary (Annual)",
        "2021 Base Salary",
        "2022 Base Salary",
        "2023 Base Salary",
        "CY Base Salary",
        "PA Base Salary",
    ],
    "Compensation": [
        "CY Guaranteed Comp (Annual)",
        "Base Guaranteed Comp.",
        "2021 Guaranteed Comp.",
        "2022 Guar. Comp.",
        "2023 Guaranteed Comp",
        "CY Guaranteed Comp",
        "Guaranteed Comp",
        "CY Guaranteed Comp (PA)",
    ],
}

_HEADER_ALIAS_LOOKUP = {
    alias: canonical for canonical, aliases in _HEADER_ALIASES.items() for alias in aliases
}

_CLUB_ALIASES: dict[str, list[str]] = {
    # Populated incrementally as we confirm each report year's club abbreviations.
    # Keyed by each franchise's current/final name (not the name in use at the time
    # of the report); values are every alternate spelling seen across report years
    # (abbreviations, pre-rebrand names, truncated text, etc.)
    "Atlanta United FC": ["ATL", "Atlanta United"],
    "CF Montréal": ["MTL", "Montreal", "Montreal Impact", "CF Montreal"],
    "Chicago Fire FC": ["CHI", "Chicago Fire"],
    "Chivas USA": ["CHV"],
    "Colorado Rapids": ["COL", "Colorado Rapi"],
    "Columbus Crew": ["CLB", "Columbus Cre"],
    "D.C. United": ["DC", "DC United"],
    "FC Dallas": ["DAL"],
    "Houston Dynamo FC": ["HOU", "Houston Dyna", "Houston Dynamo"],
    "Inter Miami CF": ["Inter Miami"],
    "LA Galaxy": ["LA"],
    "Los Angeles FC": ["LAFC"],
    "Minnesota United FC": ["MNUFC", "Minnesota Unit", "Minnesota United"],
    "New England Revolution": ["NE", "New England", "New England Revolutio"],
    "New York City FC": ["NYCFC", "New York City"],
    "New York Red Bulls": ["NY", "NYRB", "New York Red"],
    "Orlando City SC": ["ORL", "Orlando City S"],
    "Philadelphia Union": ["PHI", "Philadelphia U"],
    "Portland Timbers": ["POR", "Portland Timb"],
    "Real Salt Lake": ["RSL"],
    "San Jose Earthquakes": ["SJ", "San Jose Earth"],
    "Seattle Sounders FC": ["SEA", "Seattle Sounde"],
    "Sporting Kansas City": ["KC", "Sporting Kans"],
    "St. Louis City SC": ["St. Louis SC"],
    "Toronto FC": ["TFC", "TOR"],
    "Vancouver Whitecaps FC": ["VAN", "Vancouver Whi", "Vancouver Whitecaps"],
    # Non-franchise designations (unattached/allocation-pool players, retirees, etc.)
    # are consolidated under "Major League Soccer" as the canonical league-held status.
    "Major League Soccer": [
        "Major League",
        "Player Pool",
        "Pool",
        "POOL",
        "Retired",
        "MLS Pool",
        "Without a Club",
    ],
}

_CLUB_ALIAS_LOOKUP = {
    alias: canonical for canonical, aliases in _CLUB_ALIASES.items() for alias in aliases
}


class Client:
    def fetch(self, year: int) -> None:
        """Fetch PDF/CSV from MLSPA's S3 bucket, clean the data, and save to local repo as CSV.

        The MLSPA Client handles all idiosyncracies of the supported salary
        reports, extracting, cleaning, formatting, and normalizing the source data
        for immediate use in pandas. These idiosyncracies require both default
        and custom cleaning tasks, which the client maps to each year's needs.
        """

        try:
            response = requests.get(url=_SALARY_REPORTS[year])
            response.raise_for_status()

            output_path = f"{Path(__file__).resolve().parent}/reports/mlspa_salaries_{year}.csv"

            if year < 2024:
                # Reports prior to 2024 are stored as PDFs, requiring conversion to CSV first.
                # tabula extracts each page independently, so the title/column-header boilerplate
                # at the top of every page is re-extracted as literal rows on every page too.
                pdf_file = io.BytesIO(response.content)
                pages = tb.read_pdf(pdf_file, pages="all", pandas_options={"header": None})
                pages = _get_page_fixup(year)(pages)
                df = _get_page_merger(year)(pages)
            else:
                # Reports released 2024 and after are already well-formed CSVs with a real
                # header row, so no tabula extraction or page-merging is needed.
                df = pd.read_csv(io.BytesIO(response.content))

            for step in _get_pipeline(year):
                df = step(df)

            df = self._validate_intermediate_schema(df)
            df = self._normalize_columns(df)
            df.to_csv(output_path, index=False)
            logger.info(f"Salary report written to {output_path}")
        except KeyError:
            # KeyErrors should be non-fatal, logging the exception and returning nothing.
            logger.exception(f"No report for {year} supported.")

    @staticmethod
    def _fix_2017_merged_club_column(pages: list[pd.DataFrame]) -> list[pd.DataFrame]:
        """Split the merged Club/Last Name column on most pages of the 2017 report.

        Most pages collapse Club and Last Name into a single column 0 (e.g. "NYRB
        Abang"), leaving column 1 entirely blank -- but a few pages (e.g. the
        final, shorter one) already have them split normally. We only touch pages
        that exhibit the merge (column 1 is entirely blank), since blindly
        splitting column 0 on an already-correct page would clobber real Last
        Name data with NaN (a lone club code like "RSL" has no space to split
        on). Club abbreviations never contain a space, so splitting on the first
        space recovers both fields even for multi-word last names.
        """
        fixed_pages = []
        for page in pages:
            if page[1].isna().all():
                split = page[0].str.split(" ", n=1, expand=True)
                page = page.copy()
                page[0] = split[0]
                page[1] = split[1]
            fixed_pages.append(page)
        return fixed_pages

    @staticmethod
    def _fix_2022_pages(pages: list[pd.DataFrame]) -> list[pd.DataFrame]:
        """Fix two structural quirks in the 2022 report's page extraction.

        Every page is rendered twice consecutively -- confirmed by comparing
        rows 1+ of each page against the next, which are byte-for-byte
        identical -- so we keep only the first of each pair. Each kept page's
        row 0 is also a garbled duplicate of the entire page's content crushed
        into a single column (every other column NaN), rather than a real
        header or data row, so it's dropped directly.
        """
        deduped = pages[::2]
        return [page.iloc[1:].reset_index(drop=True) for page in deduped]

    @staticmethod
    def _fix_2016_last_page(pages: list[pd.DataFrame]) -> list[pd.DataFrame]:
        """Realign the 2016 report's final page, which tabula parsed into a
        different column layout than the other 11 pages.

        That page has only 5 rows (2 boilerplate + 3 data), and tabula guessed an
        8-column split instead of the standard 7, inserting an always-blank column
        and shifting the "First Name" header label one column right of where the
        actual name values land. Since its title/header rows are shaped
        differently, _strip_repeated_page_headers can't recognize them as the same
        boilerplate as every other page, so we drop them here directly and realign
        the remaining data rows to the standard 7-column layout.
        """
        fixed_pages = []
        for page in pages:
            if page.shape[1] == 8:
                page = page.iloc[2:].drop(columns=3)
                page.columns = range(page.shape[1])
            fixed_pages.append(page)
        return fixed_pages

    @staticmethod
    def _strip_repeated_page_headers(pages: list[pd.DataFrame]) -> pd.DataFrame:
        """Collapse tabula's per-page boilerplate into a single header row.

        Every page of the source PDF repeats a title row (e.g. `"",,,,,2008,2008
        Guaranteed`) and a column header row (`Club,Last Name,...`) above its data.
        tabula extracts each page independently, so both are re-extracted as literal
        rows on every page. The first page's first two rows are treated as the
        canonical title/header: the title row is dropped everywhere it recurs, and
        the header row is kept only on its first occurrence.
        """

        if not pages:
            return pd.DataFrame()

        combined = pd.concat(pages, ignore_index=True)
        row_tuples = combined.apply(tuple, axis=1)

        title_row, header_row = row_tuples.iloc[0], row_tuples.iloc[1]
        is_title = row_tuples == title_row
        is_header = row_tuples == header_row
        is_repeat_header = is_header & is_header.cumsum().gt(1)

        return combined[~is_title & ~is_repeat_header].reset_index(drop=True)

    @staticmethod
    def _strip_single_header_row(pages: list[pd.DataFrame]) -> pd.DataFrame:
        """Collapse per-page boilerplate for reports that repeat only a single
        header row (no separate title row) at the top of every page.

        Using _strip_repeated_page_headers here would misfire: with no title row,
        its "first row" is actually the real header and its "second row" is the
        first real data row, so it would promote a data row to canonical-header
        status and only drop the real header as if it were a title.
        """

        if not pages:
            return pd.DataFrame()

        combined = pd.concat(pages, ignore_index=True)
        row_tuples = combined.apply(tuple, axis=1)

        header_row = row_tuples.iloc[0]
        is_header = row_tuples == header_row
        is_repeat_header = is_header & is_header.cumsum().gt(1)

        return combined[~is_repeat_header].reset_index(drop=True)

    @staticmethod
    def _promote_first_row_as_header(df: pd.DataFrame) -> pd.DataFrame:
        """Turn the retained header row (still a data row after merging) into real column labels."""
        df = df.set_axis(df.iloc[0], axis=1).iloc[1:].reset_index(drop=True)
        df.columns.name = None
        return df

    @staticmethod
    def _drop_index_column(df: pd.DataFrame) -> pd.DataFrame:
        """Drop the "Num" column some reports include as a redundant row index."""
        return df.drop(columns="Num", errors="ignore")

    @staticmethod
    def _fix_2020_salary_column(df: pd.DataFrame) -> pd.DataFrame:
        """The 2020 report's "Base Salary" header label sits one column left of
        where the actual salary values land: position 4 holds only the header's
        own label text (NaN on every data row) while position 5 -- labeled NaN
        in the header -- holds the real dollar amounts. Drop the empty labeled
        column and relabel the real one. Runs on the raw positional dataframe,
        before the header row is promoted to column names.
        """
        df = df.drop(columns=4)
        df.loc[0, 5] = "Base Salary"
        df.columns = range(df.shape[1])
        return df

    @staticmethod
    def _strip_currency_whitespace(cell):
        """Check each string cell and clean cells matching currency regex.

        The PDF exports are consistently adding whitespace between the first numeric
        character and the remaining characters. We simply need to identify cells
        matching this pattern and strip the embedded whitespace.
        """

        if isinstance(cell, str) and _CURRENCY_RE.match(cell):
            return re.sub(r"(?<=\d)\s+(?=[\d,])", "", cell)
        return cell

    @staticmethod
    def _fix_2022_garbled_row(df: pd.DataFrame) -> pd.DataFrame:
        """Correct one row in the 2022 report with interleaved Club/Last Name text.

        A PDF text-extraction glitch renders Vancouver Whitecaps forward Brian
        White as Club="Vancouver Whi", Last Name="eWcahpites" -- the two fields'
        characters got jumbled together. Not recoverable generically, so this
        one known row is corrected directly.
        """
        mask = (df["Club"] == "Vancouver Whi") & (df["Last Name"] == "eWcahpites")
        df.loc[mask, "Club"] = "Vancouver Whitecaps"
        df.loc[mask, "Last Name"] = "White"
        return df

    @staticmethod
    def _normalize_null_placeholders(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize tabula's stringified nulls back to real missing values.

        tabula's subprocess/JSON extraction path (used when jpype isn't installed)
        renders some genuinely blank cells as the literal string "None" rather than
        an actual null, which silently defeats any .isna()/.fillna() based handling
        downstream.
        """
        return df.mask(df == "None")

    @staticmethod
    def _strip_currency_whitespace_df(df: pd.DataFrame) -> pd.DataFrame:
        """DataFrame-level wrapper around _strip_currency_whitespace for use as a pipeline step."""
        return df.map(Client._strip_currency_whitespace)

    @staticmethod
    def _rename_header_variants(df: pd.DataFrame) -> pd.DataFrame:
        """Rename known alternate labels for intermediate-schema columns.

        Different report years use different wording for the same field (e.g.
        "Salary" or "CY Salary (Annual)" instead of "Base Salary", "Playing
        Position" instead of "Pos"). Safe to run unconditionally: renaming a
        column that isn't present is a no-op.
        """
        return df.rename(columns=_HEADER_ALIAS_LOOKUP)

    @staticmethod
    def _drop_currency_symbol_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Drop columns that contain nothing but a bare '$' symbol.

        Some report layouts extract the currency symbol into its own column,
        separate from the amount itself (e.g. Pos, $, "12,900.00"). Once the
        amount values are cleaned, this column carries no information.
        """
        is_symbol_col = df.apply(lambda col: col.isin(["$", "", None]).all(), axis=0)
        return df.loc[:, ~is_symbol_col]

    @staticmethod
    def _validate_intermediate_schema(df: pd.DataFrame) -> pd.DataFrame:
        """Fail loudly if a pipeline didn't converge on the shared intermediate schema.

        Order-insensitive: some report years have a genuinely different raw column
        order, which is harmless since _normalize_columns always selects by name.
        Comparing sorted lists (not sets) still catches missing/extra/duplicate columns.
        """
        actual = list(df.columns)
        if sorted(actual) != sorted(_INTERMEDIATE_COLUMNS):
            raise ValueError(
                f"Expected intermediate columns {_INTERMEDIATE_COLUMNS}, got {actual}. "
                "A cleaning step for this report year needs adjusting."
            )
        return df

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Map the shared intermediate schema onto the canonical output schema."""
        df = df.rename(columns={"Pos": "Position", "Compensation": "Guaranteed Compensation"})
        # A blank Club cell in the source means the player was unattached at
        # publication time; treat that the same as the other non-franchise designations.
        club = df["Club"].str.strip()
        df["Club"] = club.map(_CLUB_ALIAS_LOOKUP).fillna(club).fillna("Major League Soccer")

        # Some years embed a "$" in these values and some don't (depending on whether
        # the currency symbol was split into its own column), so strip everything but
        # digits and the decimal point rather than relying on a consistent format.
        # A handful of rows report a placeholder like "-" for a salary that wasn't yet
        # finalized at publication time; those become NaN rather than failing the run.
        for column in ("Base Salary", "Guaranteed Compensation"):
            cleaned = df[column].str.replace(r"[^\d.]", "", regex=True)
            df[column] = pd.to_numeric(cleaned, errors="coerce")

        return df[_CANONICAL_COLUMNS]


_DEFAULT_STEPS: list[CleaningStep] = [
    Client._promote_first_row_as_header,
    Client._rename_header_variants,
    Client._normalize_null_placeholders,
    Client._strip_currency_whitespace_df,
    Client._drop_currency_symbol_columns,
]

# Reports from 2024 onward are already well-formed CSVs with a real header row,
# so no page merger ever ran a header-promotion step for them -- reuse the same
# steps minus that one.
_CSV_DEFAULT_STEPS: list[CleaningStep] = [
    Client._drop_index_column,
    Client._rename_header_variants,
    Client._normalize_null_placeholders,
    Client._strip_currency_whitespace_df,
    Client._drop_currency_symbol_columns,
]

_CLEANING_PIPELINES: dict[int, list[CleaningStep]] = {
    2020: [Client._fix_2020_salary_column, *_DEFAULT_STEPS],
    2022: [
        Client._promote_first_row_as_header,
        Client._rename_header_variants,
        Client._fix_2022_garbled_row,
        Client._normalize_null_placeholders,
        Client._strip_currency_whitespace_df,
        Client._drop_currency_symbol_columns,
    ],
    2024: _CSV_DEFAULT_STEPS,
    2025: _CSV_DEFAULT_STEPS,
    2026: _CSV_DEFAULT_STEPS,
}


def _get_pipeline(year: int) -> list[CleaningStep]:
    return _CLEANING_PIPELINES.get(year, _DEFAULT_STEPS)


_PAGE_FIXUPS: dict[int, PageFixup] = {
    2016: Client._fix_2016_last_page,
    2017: Client._fix_2017_merged_club_column,
    2022: Client._fix_2022_pages,
}


_PAGE_MERGERS: dict[int, PageMerger] = {
    2018: Client._strip_single_header_row,
    2019: Client._strip_single_header_row,
    2020: Client._strip_single_header_row,
    2022: Client._strip_single_header_row,
    2023: Client._strip_single_header_row,
}


def _get_page_merger(year: int) -> PageMerger:
    return _PAGE_MERGERS.get(year, Client._strip_repeated_page_headers)


def _get_page_fixup(year: int) -> PageFixup:
    return _PAGE_FIXUPS.get(year, lambda pages: pages)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and normalize MLSPA salary reports.")
    parser.add_argument(
        "years",
        type=int,
        nargs="*",
        default=[max(_SALARY_REPORTS)],
        metavar="YEAR",
        help=(
            "One or more report years to fetch, e.g. 2024 2025 "
            f"(available: {min(_SALARY_REPORTS)}-{max(_SALARY_REPORTS)}). "
            f"Defaults to the most recent report ({max(_SALARY_REPORTS)})."
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    client = Client()

    for year in args.years:
        client.fetch(year=year)
