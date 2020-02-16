#!/usr/bin/env python3
# License: BSD

import sys
import os
import pytest
from time import sleep
apifolder = os.getcwd()
sys.path.append(apifolder)
from functions import PUT, GET, RC_TEST, DELETE, POST, SSH_TEST
from auto_config import ip, user


@pytest.fixture(scope='module')
def rsynctask_dict():
    return {}


def test_01_Configuring_rsyncd_service():
    results = PUT('/rsyncd/', {'port': 873})
    assert results.status_code == 200


def test_02_Checking_that_API_reports_rsyncd_service():
    results = GET("/rsyncd/")
    assert results.status_code == 200, results.text


def test_03_create_root_ssh_key():
    cmd = 'ssh-keygen -t rsa -f /root/.ssh/id_rsa -q -N ""'
    results = SSH_TEST(cmd, user, None, ip)
    assert results['result'] is True, results['output']


def test_04_Creating_rsync_task(rsynctask_dict):
    payload = {
        'user': 'root',
        'mode': 'SSH',
        'remotehost': 'foobar',
        'path': '/mnt/tank/share',
        "remotepath": "/share",
        "remoteport": 22
    }
    results = POST('/rsynctask/', payload)
    assert results.status_code == 200, results.text
    rsynctask_dict.update(results.json())
    assert isinstance(rsynctask_dict['id'], int) is True


def test_05_Enable_rsyncd_service():
    results = PUT('/service/id/rsync/', {'enable': True})
    assert results.status_code == 200, results.text


def test_06_Checking_to_see_if_rsyncd_service_is_enabled():
    results = GET(f'/service?service=rsync')
    assert results.json()[0]['enable'] is True, results


def test_07_Testing_rsync_access():
    RC_TEST(f'rsync -avn {ip}::testmod') is True


def test_08_Starting_rsyncd_service():
    results = POST("/service/start/",
                   {'service': 'rsyncd', 'service-control': {'onetime': True}}
                   )
    assert results.status_code == 200, results.text
    sleep(1)


def test_09_Checking_to_see_if_rsyncd_service_is_running():
    results = GET("/service?service=rsync")
    assert results.json()[0]['state'] == 'RUNNING', results.text


def test_10_Disable_rsync_task(rsynctask_dict):
    id = rsynctask_dict['id']
    results = PUT(f'/rsynctask/id/{id}/', {'enabled': False})
    assert results.status_code == 200, results.text


def test_11_Check_that_API_reports_the_rsync_task_as_disabled(rsynctask_dict):
    id = rsynctask_dict['id']
    results = GET(f'/rsynctask?id={id}')
    assert results.json()[0]['enabled'] is False


def test_12_Delete_rsync_task(rsynctask_dict):
    id = rsynctask_dict['id']
    results = DELETE(f'/rsynctask/id/{id}/')
    assert results.status_code == 200, results.text


def test_13_Check_that_the_API_reports_rsync_task_as_deleted(rsynctask_dict):
    id = rsynctask_dict['id']
    results = GET(f'/rsynctask?id={id}')
    assert results.json() == [], results.text


def test_14_remove_root_ssh_key():
    cmd = 'rm /root/.ssh/id_rsa*'
    results = SSH_TEST(cmd, user, None, ip)
    assert results['result'] is True, results['output']
