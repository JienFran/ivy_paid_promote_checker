import os
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

# Private settings (spreadsheet / document names) live in .env, which is not committed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env is optional: real environment variables still work

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def get_credentials_path():
    """Path of credentials.json in the project root (shared with docs.py)."""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_dir, "credentials.json")


def connect_sheets(spreadsheet_title_or_url=None, worksheet_name="ARENA"):
    """Open a spreadsheet (by title or URL) and return its VAULT or ARENA worksheet.

    Without an argument, the spreadsheet comes from SPREADSHEET_NAME in .env.
    """
    spreadsheet_title_or_url = spreadsheet_title_or_url or os.getenv("SPREADSHEET_NAME")

    if not spreadsheet_title_or_url:
        raise ValueError("Spreadsheet not set: pass a title/URL or set SPREADSHEET_NAME in .env")

    cred_path = get_credentials_path()

    if not os.path.exists(cred_path):
        raise FileNotFoundError(f"credentials.json not found at {cred_path}")

    creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
    client = gspread.authorize(creds)

    if spreadsheet_title_or_url.startswith("https://"):
        sheet = client.open_by_url(spreadsheet_title_or_url)
    else:
        sheet = client.open(spreadsheet_title_or_url)

    worksheet_name = worksheet_name.upper()

    if worksheet_name not in ("VAULT", "ARENA"):
        raise ValueError(f"worksheet_name must be 'VAULT' or 'ARENA', got '{worksheet_name}'")

    try:
        return sheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        raise gspread.WorksheetNotFound(
            f"Tab '{worksheet_name}' not found in spreadsheet '{sheet.title}'"
        )


def append_violation_row(
    worksheet,
    name=None,
    jenis_pelanggaran=None,
    nominal_denda=None,
    jenis_penugasan=None,
    username=None
):
    """Write one violation to the first empty row of the worksheet.

    Columns: A No | B Name | C Fine date | D Violation | E Extra fine |
             F Evidence | G Fine amount | H Payment | I Assignment type.
    `username` is accepted as an alias of `name`.
    """
    name = name or username

    if not name:
        raise ValueError("name/username is required")
    if jenis_pelanggaran is None:
        raise ValueError("jenis_pelanggaran is required")
    if nominal_denda is None:
        raise ValueError("nominal_denda is required")
    if jenis_penugasan is None:
        raise ValueError("jenis_penugasan is required")

    names_column = worksheet.col_values(2)
    next_row = len(names_column) + 1
    row_number = len(names_column)

    worksheet.update(
        f"A{next_row}:I{next_row}",
        [[
            row_number,
            name,
            datetime.now().strftime("%Y-%m-%d"),
            jenis_pelanggaran,
            "Tdk ada",
            "",
            nominal_denda,
            "BELUM BAYAR",
            jenis_penugasan
        ]]
    )

    print(
        f"[ OK ] Sheet row {next_row} | {worksheet.title} | No {row_number} | "
        f"{name} | {jenis_pelanggaran} | Rp{nominal_denda:,}"
    )


if __name__ == "__main__":
    try:
        for tab in ("ARENA", "VAULT"):
            ws = connect_sheets(worksheet_name=tab)
            print(f"[ OK ] Connected to sheet tab: {ws.title}")
    except Exception as e:
        print(f"[FAIL] {e}")