import os
import tempfile

import keyring

os.environ["COSMOS_DATA_DIR"] = tempfile.mkdtemp(prefix="cosmos-tests-")

# Tests must never write synthetic API keys into the developer's operating-
# system credential vault.  A process-local vault also prevents repeated test
# runs from exhausting Windows Credential Manager resources.
_TEST_VAULT: dict[tuple[str, str], str] = {}


def _get_test_password(service: str, username: str):
    return _TEST_VAULT.get((service, username))


def _set_test_password(service: str, username: str, value: str):
    _TEST_VAULT[(service, username)] = value


def _delete_test_password(service: str, username: str):
    _TEST_VAULT.pop((service, username), None)


keyring.get_password = _get_test_password
keyring.set_password = _set_test_password
keyring.delete_password = _delete_test_password
