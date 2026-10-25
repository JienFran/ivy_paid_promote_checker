import os
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Private settings (spreadsheet / document names) live in .env, which is not committed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env is optional: real environment variables still work

# Docs API reads/writes the document, Drive API finds it by title.
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

VALID_TABS = ("VAULT", "ARENA")


def get_credentials_path():
    """Path of credentials.json in the project root (shared with sheets.py)."""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_dir, "credentials.json")


def _doc_url(document_id, tab_id=None):
    url = f"https://docs.google.com/document/d/{document_id}/edit"
    return f"{url}?tab={tab_id}" if tab_id else url


def _get_services():
    cred_path = get_credentials_path()

    if not os.path.exists(cred_path):
        raise FileNotFoundError(f"credentials.json not found at {cred_path}")

    creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)

    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    return docs_service, drive_service


def _resolve_document_id(drive_service, doc_title_or_url):
    """Return the document ID from a Google Docs URL, or look it up by title."""
    if doc_title_or_url.startswith("https://"):
        marker = "/d/"

        if marker not in doc_title_or_url:
            raise ValueError(f"Cannot read a document ID from URL: {doc_title_or_url}")

        return doc_title_or_url.split(marker, 1)[1].split("/")[0]

    safe_title = doc_title_or_url.replace("'", "\\'")

    query = (
        f"name = '{safe_title}' "
        "and mimeType = 'application/vnd.google-apps.document' "
        "and trashed = false"
    )

    response = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=5
    ).execute()

    files = response.get("files", [])

    if not files:
        raise FileNotFoundError(
            f"Google Doc '{doc_title_or_url}' not found. Share it (Editor role) "
            "with the service account email in credentials.json."
        )

    if len(files) > 1:
        print(f"[WARN] {len(files)} documents titled '{doc_title_or_url}', using the first one")

    return files[0]["id"]


def _find_tab(tabs, title):
    """Find a tab by title (case-insensitive), searching child tabs too. Returns None if missing."""
    for tab in tabs or []:
        tab_title = tab.get("tabProperties", {}).get("title") or ""

        if tab_title.strip().upper() == title.upper():
            return tab

        found = _find_tab(tab.get("childTabs"), title)
        if found is not None:
            return found

    return None


def _find_tab_by_id(tabs, tab_id):
    for tab in tabs or []:
        if tab.get("tabProperties", {}).get("tabId") == tab_id:
            return tab

        found = _find_tab_by_id(tab.get("childTabs"), tab_id)
        if found is not None:
            return found

    return None


def _default_doc_name(doc_title_or_url=None):
    """The given title/URL, or DOC_NAME from .env. Raises if neither is set."""
    name = doc_title_or_url or os.getenv("DOC_NAME")

    if not name:
        raise ValueError("Document not set: pass a title/URL or set DOC_NAME in .env")

    return name


def connect_docs(doc_title_or_url=None, tab_name="ARENA"):
    """Connect to a tab (VAULT or ARENA) of a Google Doc.

    Without a title/URL, the document comes from DOC_NAME in .env.

    The Docs API cannot create tabs, so both tabs must already exist. Create them once
    in Google Docs: right-click in the left tab panel > Add tab > rename.

    Returns the context dict used by append_report_text() and append_violation_entry().
    """
    doc_title_or_url = _default_doc_name(doc_title_or_url)
    tab_name = tab_name.upper()

    if tab_name not in VALID_TABS:
        raise ValueError(f"tab_name must be 'VAULT' or 'ARENA', got '{tab_name}'")

    docs_service, drive_service = _get_services()
    document_id = _resolve_document_id(drive_service, doc_title_or_url)

    document = docs_service.documents().get(
        documentId=document_id,
        includeTabsContent=True
    ).execute()

    tab = _find_tab(document.get("tabs"), tab_name)

    if tab is None:
        raise ValueError(
            f"Tab '{tab_name}' not found in document '{document.get('title')}'. "
            "Create it manually in Google Docs first."
        )

    return {
        "docs_service": docs_service,
        "document_id": document_id,
        "document_title": document.get("title"),
        "tab_id": tab["tabProperties"]["tabId"],
        "tab_title": tab_name,
    }


def _tab_end_index(docs_service, document_id, tab_id):
    """Index just before the tab's closing newline, so new text is appended at the end.

    Fetched fresh on every call because the index changes with each insert.
    """
    document = docs_service.documents().get(
        documentId=document_id,
        includeTabsContent=True
    ).execute()

    tab = _find_tab_by_id(document.get("tabs"), tab_id)

    if tab is None:
        raise ValueError(f"Tab ID '{tab_id}' no longer exists (deleted or renamed?)")

    content = tab["documentTab"]["body"]["content"]
    return content[-1]["endIndex"] - 1


def append_report_text(docs_context, report_text):
    """Append a report block to the end of the tab, separated from earlier content by blank lines."""
    if not report_text:
        print("[INFO] Empty report, nothing sent to Google Docs")
        return

    docs_service = docs_context["docs_service"]
    document_id = docs_context["document_id"]
    tab_id = docs_context["tab_id"]

    insert_index = _tab_end_index(docs_service, document_id, tab_id)

    separator = "\n\n" if insert_index > 1 else ""
    text = f"{separator}\n{report_text}\n"

    requests = [{
        "insertText": {
            "location": {"index": insert_index, "tabId": tab_id},
            "text": text
        }
    }]

    try:
        docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests}
        ).execute()
    except HttpError as e:
        raise RuntimeError(
            f"Could not write to tab '{docs_context['tab_title']}': {e}"
        ) from e

    print(
        f"[ OK ] Sent to Google Docs | {docs_context['document_title']} | "
        f"tab {docs_context['tab_title']}"
    )
    print(f"       {_doc_url(document_id, tab_id)}")


def append_violation_entry(
    docs_context,
    name=None,
    jenis_pelanggaran=None,
    nominal_denda=None,
    jenis_penugasan=None,
    username=None
):
    """Append one violation line (date | name | violation | fine | assignment) to the tab.

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

    today = datetime.now().strftime("%Y-%m-%d")

    append_report_text(
        docs_context,
        f"{today} | {name} | {jenis_pelanggaran} | Rp{nominal_denda:,} | {jenis_penugasan}"
    )


def send_report_to_docs(report_text, division_type, doc_title_or_url=None):
    """Connect and append in one step. `division_type` ('vault' or 'arena') picks the tab."""
    tab_name = "VAULT" if "vault" in division_type.lower() else "ARENA"

    docs_context = connect_docs(doc_title_or_url=doc_title_or_url, tab_name=tab_name)
    append_report_text(docs_context, report_text)


if __name__ == "__main__":
    try:
        contexts = {tab: connect_docs(tab_name=tab) for tab in ("ARENA", "VAULT")}

        for tab, ctx in contexts.items():
            print(f"[ OK ] Connected to doc '{ctx['document_title']}', tab {tab}")

        append_report_text(contexts["ARENA"], "[TEST] core/docs.py connected and wrote text.")

    except Exception as e:
        print(f"[FAIL] {e}")