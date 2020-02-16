#!/usr/bin/env python3

# License: BSD

import sys
import os
from time import sleep
apifolder = os.getcwd()
sys.path.append(apifolder)
from functions import DELETE, GET, POST, PUT
from auto_config import pool_name

dataset = f"{pool_name}/tftproot"
dataset_url = dataset.replace('/', '%2F')


def test_01_Creating_dataset_tftproot():
    # THIS IS UNDER THE ASSUMPTION THAT A TANK VOLUME ALREADY EXISTS WHEN THIS TEST RUNS

    result = POST(
        '/pool/dataset/', {
            'name': dataset
        }
    )

    assert result.status_code == 200, result.text


def test_02_Setting_permissions_for_TFTP_on_mnt_pool_name_tftproot():
    payload = {
        'acl': [],
        'mode': '777',
        'group': 'nobody',
        'user': 'nobody'
    }
    results = POST(f'/pool/dataset/id/{dataset_url}/permission/', payload)

    assert results.status_code == 200, results.text


def test_03_Configuring_TFTP_service():
    payload = {
        "directory": f"/mnt/{pool_name}/tftproot",
        "username": "nobody",
        "newfiles": True
    }
    results = PUT("/tftp/", payload)

    assert isinstance(results.json(), dict), results.text


def test_04_Enable_TFTP_service():
    results = PUT("/service/id/tftp/", {"enable": True})

    assert results.status_code == 200, results.text


def test_05_Start_TFTP_service():
    results = POST(
        '/service/start/', {
            'service': 'tftp',
            'service-control': {
                'onetime': True
            }
        }
    )

    assert results.status_code == 200, results.text
    sleep(1)


def test_06_Checking_to_see_if_TFTP_service_is_enabled():
    results = GET("/service/?service=tftp")

    assert results.json()[0]["state"] == "RUNNING", results.text


def test_07_delete_tftp_dataset():
    results = DELETE(f"/pool/dataset/id/{dataset_url}/")
    assert results.status_code == 200, results.text
