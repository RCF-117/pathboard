import re

import pytest

from pathboard.browser.session import BrowserConfig
from pathboard.testbed.state import LedgerConfig

LEDGER = LedgerConfig(seed=7, n_invoices=40, n_vendors=8, max_lines=4, first_invoice_number=1001)
BROWSER = BrowserConfig(
    headless=True,
    viewport_width=1400,
    viewport_height=900,
    settle_quiet_ms=100,
    settle_timeout_ms=8000,
    action_timeout_ms=5000,
)
SUITE_KW = dict(n_jobs=4, n_invoices=40, n_vendors=8, max_lines=4, first_invoice_number=1001)


@pytest.fixture(scope="session")
def browser_session():
    from pathboard.browser.session import BrowserSession

    with BrowserSession(BROWSER) as session:
        yield session


def recategorize_policy():
    """A tiny rule-based stand-in for the model that solves 'recategorize' goals."""
    state = {"item": 0}
    column = {"category": 0, "assignee": 1, "status": 2, "notes": 3}

    def policy(prompt: str) -> dict:
        goal, page = prompt.split("CURRENT PAGE")
        items = re.findall(r"Set the (\w+) of invoice (INV-\d+) to ([\w ]+)\.", goal)
        editor = re.search(r'\[(e\d+) select "Edit ', page)
        while state["item"] < len(items):
            field, iid, value = items[state["item"]]
            if editor:
                return {
                    "note": "choose",
                    "action": "select",
                    "id": editor.group(1),
                    "option": value,
                }
            row = next((ln for ln in page.splitlines() if f'link "{iid}"' in ln), None)
            if row is None:
                search = re.search(r'\[(e\d+) input "Search"', page).group(1)
                return {"note": "find", "action": "fill", "id": search, "text": iid}
            cells = re.findall(r'\[(e\d+) cell "([^"]*)"\]', row)
            cell_id, shown = cells[column[field]]
            if shown == value:
                state["item"] += 1
                continue
            return {"note": "edit", "action": "click", "id": cell_id}
        return {"note": "done", "action": "finish"}

    return policy
