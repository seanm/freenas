#!/usr/bin/env python3

import os
import sys
apifolder = os.getcwd()
sys.path.append(apifolder)
from functions import GET, PUT


def test_01_get_alertclasses():
    results = GET("/alertclasses/")
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text


def test_02_add_classes_to_alertclasses():
    global payload
    payload = {
        "classes": {
            "VolumeStatus": {
                "level": "CRITICAL",
                "policy": "IMMEDIATELY"
            }
        }
    }
    results = PUT("/alertclasses/", payload)
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text


def test_03_verify_the_new_alertclasses():
    results = GET("/alertclasses/")
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text
    assert results.json()['classes'] == payload['classes'], results.text
