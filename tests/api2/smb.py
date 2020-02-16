#!/usr/bin/env python3

# Author: Eric Turgeon
# License: BSD
# Location for tests into REST API of FreeNAS

import pytest
import sys
import os
from time import sleep
apifolder = os.getcwd()
sys.path.append(apifolder)
from functions import PUT, POST, GET, DELETE, SSH_TEST
from auto_config import ip, pool_name, password, user

MOUNTPOINT = "/tmp/smb-cifs"
dataset = f"{pool_name}/smb-cifs"
dataset_url = dataset.replace('/', '%2F')
SMB_NAME = "TestCifsSMB"
smb_path = "/mnt/" + dataset
VOL_GROUP = "wheel"
BSDReason = 'BSD host configuration is missing in ixautomation.conf'
OSXReason = 'OSX host configuration is missing in ixautomation.conf'

try:
    from config import BSD_HOST, BSD_USERNAME, BSD_PASSWORD
    bsd_host_cfg = pytest.mark.skipif(False, reason=BSDReason)
except ImportError:
    bsd_host_cfg = pytest.mark.skipif(True, reason=BSDReason)


try:
    from config import OSX_HOST, OSX_USERNAME, OSX_PASSWORD
    osx_host_cfg = pytest.mark.skipif(False, reason=OSXReason)
except ImportError:
    osx_host_cfg = pytest.mark.skipif(True, reason=OSXReason)

smb_acl = [
    {
        "tag": 'USER',
        "id": 1001,
        "type": "ALLOW",
        "perms": {"BASIC": "FULL_CONTROL"},
        "flags": {"BASIC": "INHERIT"}
    },
    {
        "tag": "owner@",
        "id": None,
        "type": "ALLOW",
        "perms": {"BASIC": "FULL_CONTROL"},
        "flags": {"BASIC": "INHERIT"}
    },
    {
        "tag": "group@",
        "id": None,
        "type": "ALLOW",
        "perms": {"BASIC": "FULL_CONTROL"},
        "flags": {"BASIC": "INHERIT"}
    }
]

guest_path_verification = {
    "user": "shareuser",
    "group": "wheel",
    "acl": True
}


root_path_verification = {
    "user": "root",
    "group": "wheel",
    "acl": False
}


# Create tests
def test_001_setting_auxilary_parameters_for_mount_smbfs():
    toload = "lanman auth = yes\nntlm auth = yes \nraw NTLMv2 auth = yes"
    payload = {
        "smb_options": toload,
        "enable_smb1": True,
        "guest": "shareuser"
    }
    results = PUT("/smb/", payload)
    assert results.status_code == 200, results.text


def test_002_creating_smb_dataset():
    payload = {
        "name": dataset,
        "share_type": "SMB"
    }
    results = POST("/pool/dataset/", payload)
    assert results.status_code == 200, results.text


def test_003_changing_dataset_permissions_of_smb_dataset():
    payload = {
        "acl": smb_acl,
        "user": "shareuser",
        "group": "wheel",
    }
    results = POST(f"/pool/dataset/id/{dataset_url}/permission/", payload)
    assert results.status_code == 200, results.text


def test_004_get_filesystem_stat_from_smb_path_and_verify_acl_is_true():
    results = POST('/filesystem/stat/', smb_path)
    assert results.status_code == 200, results.text
    assert results.json()['acl'] is True, results.text


def test_005_starting_cifs_service_at_boot():
    results = PUT("/service/id/cifs/", {"enable": True})
    assert results.status_code == 200, results.text


def test_006_checking_to_see_if_clif_service_is_enabled_at_boot():
    results = GET("/service?service=cifs")
    assert results.json()[0]["enable"] is True, results.text


def test_007_creating_a_smb_share_path():
    global payload, results, smb_id
    payload = {
        "comment": "My Test SMB Share",
        "path": smb_path,
        "home": False,
        "name": SMB_NAME,
        "guestok": True,
    }
    results = POST("/sharing/smb/", payload)
    assert results.status_code == 200, results.text
    smb_id = results.json()['id']


def test_008_verify_if_smb_getparm_path_homes_is_null():
    cmd = 'midclt call smb.getparm path homes'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert results['output'].strip() == 'null'


def test_009_starting_cifs_service():
    payload = {"service": "cifs", "service-control": {"onetime": True}}
    results = POST("/service/start/", payload)
    assert results.status_code == 200, results.text
    sleep(1)


def test_010_checking_to_see_if_nfs_service_is_running():
    results = GET("/service?service=cifs")
    assert results.json()[0]["state"] == "RUNNING", results.text


@bsd_host_cfg
def test_011_creating_smb_mountpoint_on_bsd():
    cmd = f'mkdir -p "{MOUNTPOINT}" && sync'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_012_mounting_smb_on_bsd():
    cmd = f'mount_smbfs -N -I {ip} ' \
        f'"//guest@testnas/{SMB_NAME}" "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_013_creating_testfile_on_bsd():
    cmd = f"touch {MOUNTPOINT}/testfile.txt"
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_014_verify_testfile_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_015_get_filesystem_stat_from_testfilet_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_016_moving_smb_file_on_bsd():
    cmd = f'mv {MOUNTPOINT}/testfile.txt {MOUNTPOINT}/testfile2.txt'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_017_verify_testfile_does_not_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is False, results['output']


@bsd_host_cfg
def test_018_get_filesystem_stat_from_testfile_verify_it_is_not_there():
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 422, results.text
    message = f"Path {smb_path}/testfile.txt not found"
    assert results.json()['message'] == message, results.text


@bsd_host_cfg
def test_019_verify_testfile2_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile2.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_020_get_filesystem_stat_from_testfilet2_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile2.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_021_copying_smb_file_on_bsd():
    cmd = f'cp {MOUNTPOINT}/testfile2.txt {MOUNTPOINT}/testfile.txt'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_022_verify_testfile_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_023_get_filesystem_stat_from_testfilet_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_024_verify_testfile2_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile2.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_025_get_filesystem_stat_from_testfilet2_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile2.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_026_deleting_smb_testfile_on_bsd():
    cmd = f'rm "{MOUNTPOINT}/testfile.txt"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_027_verify_testfile_is_deleted_on_freenas():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is False, results['output']


@bsd_host_cfg
def test_028_get_filesystem_stat_from_testfile_verify_it_is_not_there():
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 422, results.text
    message = f"Path {smb_path}/testfile.txt not found"
    assert results.json()['message'] == message, results.text


# testing unmount with a testfile2 in smb
@bsd_host_cfg
def test_029_unmounting_smb_on_bsd():
    cmd = f'umount -f {MOUNTPOINT}'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_030_verify_testfile2_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile2.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_031_get_filesystem_stat_from_testfilet2_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile2.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_032_remounting_smb_on_bsd():
    cmd = f'mount_smbfs -N -I {ip} "//guest@testnas/{SMB_NAME}" "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_033_verify_testfile2_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile2.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_034_get_filesystem_stat_from_testfilet2_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile2.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_035_verify_testfile2_exist_on_bsd():
    cmd = f'test -f "{MOUNTPOINT}/testfile2.txt"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_036_create_tmp_directory_on_bsd():
    cmd = f'mkdir "{MOUNTPOINT}/tmp"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_037_verify__the_tmp_directory_exist_on_freenas():
    cmd = f'test -d {smb_path}/tmp'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_038_get_filesystem_stat_from_tmp_directory_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/tmp')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_039_moving_testfile2_into_the_tmp_directory_on_bsd():
    cmd = f'mv "{MOUNTPOINT}/testfile2.txt" "{MOUNTPOINT}/tmp/testfile2.txt"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_040_verify_testfile2_is_in_tmp_directory_on_freenas():
    cmd = f'test -f {smb_path}/tmp/testfile2.txt'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_041_get_filesystem_stat_from_testfile2_in_tmp_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/tmp/testfile2.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_042_deleting_testfile2_on_bsd_smb():
    cmd = f'rm "{MOUNTPOINT}/tmp/testfile2.txt"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_043_verify_testfile2_is_erased_from_freenas():
    cmd = f'test -f {smb_path}/tmp/testfile2.txt'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is False, results['output']


@bsd_host_cfg
def test_044_get_filesystem_stat_from_testfile_verify_it_is_not_there():
    results = POST('/filesystem/stat/', f'{smb_path}/tmp/testfile.txt')
    assert results.status_code == 422, results.text
    message = f"Path {smb_path}/tmp/testfile.txt not found"
    assert results.json()['message'] == message, results.text


@bsd_host_cfg
def test_045_remove_tmp_directory_on_bsd_smb():
    cmd = f'rmdir "{MOUNTPOINT}/tmp"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_046_verify_the_tmp_directory_exist_on_freenas():
    cmd = f'test -d {smb_path}/tmp'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is False, results['output']


@bsd_host_cfg
def test_047_get_filesystem_stat_from_testfile_verify_it_is_not_there():
    results = POST('/filesystem/stat/', f'{smb_path}/tmp')
    assert results.status_code == 422, results.text
    message = f"Path {smb_path}/tmp not found"
    assert results.json()['message'] == message, results.text


@bsd_host_cfg
def test_048_verify_the_mount_directory_is_empty_on_bsd():
    cmd = f'find -- "{MOUNTPOINT}/" -prune -type d -empty | grep -q .'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_049_verify_the_mount_directory_is_empty_on_freenas():
    cmd = f'find -- "{smb_path}/" -prune -type d -empty | grep -q .'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_050_creating_smb_file_on_bsd():
    cmd = f'touch {MOUNTPOINT}/testfile.txt'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_051_verify_testfile_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_052_get_filesystem_stat_from_testfile_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@bsd_host_cfg
def test_053_unmounting_smb_on_bsd():
    cmd = f'umount -f {MOUNTPOINT}'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_054_removing_smb_mountpoint_on_bsd():
    cmd = f'rm -r "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_055_verify_testfile_exist_on_freenas_after_unmout():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_056_get_filesystem_stat_from_testfile_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


def test_057_setting_enable_smb1_to_false():
    payload = {
        "enable_smb1": False
    }
    results = PUT("/smb/", payload)
    assert results.status_code == 200, results.text


def test_058_change_sharing_smd_home_to_true():
    payload = {
        'home': True
    }
    results = PUT(f"/sharing/smb/id/{smb_id}", payload)
    assert results.status_code == 200, results.text


def test_059_verify_smb_getparm_path_homes():
    cmd = 'midclt call smb.getparm path homes'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert results['output'].strip() == f'{smb_path}/%U'


def test_060_stoping_clif_service():
    payload = {"service": "cifs", "service-control": {"onetime": True}}
    results = POST("/service/stop/", payload)
    assert results.status_code == 200, results.text
    sleep(1)


def test_061_checking_if_cifs_is_stop():
    results = GET("/service?service=cifs")
    assert results.json()[0]['state'] == "STOPPED", results.text


# Create tests
def test_062_update_smb():
    payload = {"syslog": False}
    results = PUT("/smb/", payload)
    assert results.status_code == 200, results.text


def test_063_update_cifs_share():
    results = PUT(f"/sharing/smb/id/{smb_id}/", {"home": False})
    assert results.status_code == 200, results.text


def test_064_starting_cifs_service():
    payload = {"service": "cifs", "service-control": {"onetime": True}}
    results = POST("/service/start/", payload)
    assert results.status_code == 200, results.text
    sleep(1)


def test_065_checking_to_see_if_nfs_service_is_running():
    results = GET("/service?service=cifs")
    assert results.json()[0]["state"] == "RUNNING", results.text


# starting ssh test for OSX
@osx_host_cfg
def test_066_create_mount_point_for_smb_on_osx():
    cmd = f'mkdir -p "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_067_mount_smb_share_on_osx():
    cmd = f'mount -t smbfs "smb://guest@{ip}/{SMB_NAME}" "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_068_verify_testfile_exist_on_osx_mountpoint():
    cmd = f'test -f "{MOUNTPOINT}/testfile.txt"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_069_create_tmp_directory_on_osx():
    cmd = f'mkdir -p "{MOUNTPOINT}/tmp"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_070_verify_tmp_directory_exist_on_freenas():
    cmd = f'test -d "{smb_path}/tmp"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_071_get_filesystem_stat_from_tmp_dirctory_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/tmp')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@osx_host_cfg
def test_072_moving_smb_test_0file_into_a_tmp_directory_on_osx():
    cmd = f'mv "{MOUNTPOINT}/testfile.txt" "{MOUNTPOINT}/tmp/testfile.txt"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_073_verify_testfile_is_in_tmp_directory_on_freenas():
    cmd = f'test -f {smb_path}/tmp/testfile.txt'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_074_get_filesystem_stat_from_testfile_in_tmp_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/tmp/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


@osx_host_cfg
def test_075_deleting_test_0file_and_directory_from_smb_share_on_osx():
    cmd = f'rm -f "{MOUNTPOINT}/tmp/testfile.txt" && rmdir "{MOUNTPOINT}/tmp"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_076_verifying_test_0file_directory_were_successfully_removed_on_osx():
    cmd = f'find -- "{MOUNTPOINT}/" -prune -type d -empty | grep -q .'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_077_verify_the_mount_directory_is_empty_on_freenas():
    cmd = f'find -- "{smb_path}/" -prune -type d -empty | grep -q .'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_078_unmount_smb_share_on_osx():
    cmd = f'umount -f "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


def test_079_change_timemachine_to_true():
    global vuid
    payload = {
        'timemachine': True,
    }
    results = PUT(f"/sharing/smb/id/{smb_id}/", payload)
    assert results.status_code == 200, results.text
    vuid = results.json()['vuid']


def test_080_verify_that_timemachine_is_true():
    results = GET(f"/sharing/smb/id/{smb_id}/")
    assert results.status_code == 200, results.text
    assert results.json()['timemachine'] is True, results.text


@pytest.mark.parametrize('vfs_object', ["ixnas", "fruit", "streams_xattr"])
def test_081_verify_smb_getparm_vfs_objects_share(vfs_object):
    cmd = f'midclt call smb.getparm "vfs objects" {SMB_NAME}'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert vfs_object in results['output'], results['output']


def test_082_verify_smb_getparm_fruit_volume_uuid_share():
    cmd = f'midclt call smb.getparm "fruit:volume_uuid" {SMB_NAME}'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert results['output'].strip() == vuid, results['output']


def test_083_verify_smb_getparm_fruit_time_machine_is_yes():
    cmd = f'midclt call smb.getparm "fruit:time machine" {SMB_NAME}'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert results['output'].strip() == 'yes', results['output']


def test_084_change_recyclebin_to_true():
    global vuid
    payload = {
        "recyclebin": True,
    }
    results = PUT(f"/sharing/smb/id/{smb_id}", payload)
    assert results.status_code == 200, results.text
    vuid = results.json()['vuid']


def test_085_verify_that_recyclebin_is_true():
    results = GET(f"/sharing/smb/id/{smb_id}/")
    assert results.status_code == 200, results.text
    assert results.json()['recyclebin'] is True, results.text


@pytest.mark.parametrize('vfs_object', ["ixnas", "crossrename", "recycle"])
def test_086_verify_smb_getparm_vfs_objects_share(vfs_object):
    cmd = f'midclt call smb.getparm "vfs objects" {SMB_NAME}'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert vfs_object in results['output'], results['output']


# Update tests
@osx_host_cfg
def test_087_mount_smb_share_on_osx():
    cmd = f'mount -t smbfs "smb://guest@{ip}/{SMB_NAME}" "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_088_create_testfile_on_smb_share_via_osx():
    cmd = f'touch "{MOUNTPOINT}/testfile.txt"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_089_verify_testfile_exist_on_freenas():
    cmd = f'test -f "{smb_path}/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_090_get_filesystem_stat_from_testfile_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


# Delete test file and test directory from SMB share
@osx_host_cfg
def test_091_deleting_test_0file_and_directory_from_smb_share_on_osx():
    cmd = f'rm -f "{MOUNTPOINT}/testfile.txt"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_092_get_filesystem_stat_from_testfile_verify_it_is_not_there():
    results = POST('/filesystem/stat/', f'{smb_path}/testfile.txt')
    assert results.status_code == 422, results.text
    message = f"Path {smb_path}/testfile.txt not found"
    assert results.json()['message'] == message, results.text


@osx_host_cfg
def test_093_verify_recycle_directory_exist_on_freenas():
    cmd = f'test -d "{smb_path}/.recycle"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
@pytest.mark.parametrize('stat', list(root_path_verification.keys()))
def test_095_get_filesystem_stat_from_recycle_directory_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/.recycle')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == root_path_verification[stat], results.text


@osx_host_cfg
def test_096_verify_guest_directory_exist_in_recycle_directory_on_freenas():
    cmd = f'test -d "{smb_path}/.recycle/guest"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
@pytest.mark.parametrize('stat', list(root_path_verification.keys()))
def test_097_get_filesystem_stat_from_guest_directory_recycle_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/.recycle/guest')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == root_path_verification[stat], results.text


@osx_host_cfg
def test_098_verify_testfile_exist_in_recycle_guest_dirctory_on_freenas():
    cmd = f'test -f "{smb_path}/.recycle/guest/testfile.txt"'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']


@osx_host_cfg
@pytest.mark.parametrize('stat', list(guest_path_verification.keys()))
def test_099_get_filesystem_stat_from_testfile_in_recycle_and_verify(stat):
    results = POST('/filesystem/stat/', f'{smb_path}/.recycle/guest/testfile.txt')
    assert results.status_code == 200, results.text
    assert results.json()[stat] == guest_path_verification[stat], results.text


# Clean up mounted SMB share
@osx_host_cfg
def test_100_Unmount_smb_share_on_osx():
    cmd = f'umount -f "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


@osx_host_cfg
def test_102_Removing_smb_mountpoint_on_osx():
    cmd = f'rm -r "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, OSX_USERNAME, OSX_PASSWORD, OSX_HOST)
    assert results['result'] is True, results['output']


def test_103_get_smb_sharesec_id_and_set_smb_sharesec_share_acl():
    global share_id, payload
    share_id = GET(f"/smb/sharesec/?share_name={SMB_NAME}").json()[0]['id']
    payload = {
        'share_acl': [
            {
                'ae_who_sid': 'S-1-5-32-544',
                'ae_perm': 'FULL',
                'ae_type': 'ALLOWED'
            }
        ]
    }
    results = PUT(f"/smb/sharesec/id/{share_id}/", payload)
    assert results.status_code == 200, results.text


@pytest.mark.parametrize('ae', ['ae_who_sid', 'ae_perm', 'ae_type'])
def test_104_verify_smb_sharesec_change_for(ae):
    results = GET(f"/smb/sharesec/id/{share_id}/")
    assert results.status_code == 200, results.text
    ae_result = results.json()['share_acl'][0][ae]
    assert ae_result == payload['share_acl'][0][ae], results.text


def test_105_verify_smbclient_127_0_0_1_connection():
    cmd = 'smbclient -NL //127.0.0.1'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert 'TestCifsSMB' in results['output'], results['output']
    assert 'My Test SMB Share' in results['output'], results['output']


def test_106_verify_midclt_call_smb_getparm_access_based_share_enum_is_true():
    cmd = f'midclt call smb.getparm "access based share enum" {SMB_NAME}'
    results = SSH_TEST(cmd, user, password, ip)
    assert results['result'] is True, results['output']
    assert results['output'].strip() == 'False', results['output']


def test_107_delete_cifs_share():
    results = DELETE(f"/sharing/smb/id/{smb_id}")
    assert results.status_code == 200, results.text


# Now stop the service
def test_108_disable_cifs_service_at_boot():
    results = PUT("/service/id/cifs/", {"enable": False})
    assert results.status_code == 200, results.text


def test_109_checking_to_see_if_clif_service_is_enabled_at_boot():
    results = GET("/service?service=cifs")
    assert results.json()[0]["enable"] is False, results.text


def test_110_stoping_clif_service():
    payload = {"service": "cifs", "service-control": {"onetime": True}}
    results = POST("/service/stop/", payload)
    assert results.status_code == 200, results.text
    sleep(1)


def test_111_checking_if_cifs_is_stop():
    results = GET("/service?service=cifs")
    assert results.json()[0]['state'] == "STOPPED", results.text


# Check destroying a SMB dataset
def test_112_destroying_smb_dataset():
    results = DELETE(f"/pool/dataset/id/{dataset_url}/")
    assert results.status_code == 200, results.text
