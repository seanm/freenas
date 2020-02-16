#!/usr/bin/env python3

# Author: Eric Turgeon
# License: BSD

import sys
import os
apifolder = os.getcwd()
sys.path.append(apifolder)
from functions import GET


def test_01_get_certificateauthority_query():
    results = GET('/certificateauthority/')
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), list), results.text
