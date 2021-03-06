# Copyright 2017 Hewlett Packard Enterprise Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import fnmatch
import os
import re
import shutil
import tempfile
import time

from oslo_concurrency import processutils

from proliantutils import exception
from proliantutils.ilo import client
from proliantutils import utils


OUTPUT_FILE = '/var/hp/log/localhost/hpsum_log.txt'

HPSUM_LOCATION = 'hp/swpackages/hpsum'

EXIT_CODE_TO_STRING = {
    0: "The smart component was installed successfully.",
    1: ("The smart component was installed successfully, but the system "
        "must be restarted."),
    3: ("The smart component was not installed. Node is already "
        "up-to-date."),
    253: "The installation of the component failed."
    }


def _execute_hpsum(hpsum_file_path, components=None):
    """Executes the hpsum firmware update command.

    This method executes the hpsum firmware update command to update the
    components specified, if not, it performs update on all the firmware
    components on th server.

    :param hpsum_file_path: A string with the path to the hpsum binary to be
        executed
    :param components: A list of components to be updated. If it is None, all
        the firmware components are updated.
    :returns: A string with the statistics of the updated/failed components.
    :raises: HpsumOperationError, when the hpsum firmware update operation on
        the node fails.
    """
    cmd = ' --c ' + ' --c '.join(components) if components else ''

    try:
        processutils.execute(hpsum_file_path, "--s", "--romonly", cmd)
    except processutils.ProcessExecutionError as e:
        result = _parse_hpsum_ouput(e.exit_code)
        if result:
            return result
        else:
            msg = ("Unable to perform hpsum firmware update on the node. "
                   "Error: " + str(e))
            raise exception.HpsumOperationError(reason=msg)


def _parse_hpsum_ouput(exit_code):
    """Parse the hpsum output log file.

    This method parses through the hpsum log file in the
    default location to return the hpsum update status. Sample return
    string:

    "Summary: The installation of the component failed. Status of updated
     components: Total: 5 Success: 4 Failed: 1"

    :param exit_code: A integer returned by the hpsum after command execution.
    :returns: A string with the statistics of the updated/failed
        components and 'None' when the exit_code is not 0, 1, 3 or 253.
    """
    if exit_code == 3:
        return "Summary: %s" % EXIT_CODE_TO_STRING.get(exit_code)

    if exit_code in (0, 1, 253):
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, 'r') as f:
                output_data = f.read()

            ret_data = output_data[(output_data.find('Deployed Components:') +
                                    len('Deployed Components:')):
                                   output_data.find('Exit status:')]

            failed = 0
            success = 0
            for line in re.split('\n\n', ret_data):
                if line:
                    if 'Success' not in line:
                        failed += 1
                    else:
                        success += 1

            return ("Summary: %(return_string)s Status of updated components:"
                    " Total: %(total)s Success: %(success)s Failed: "
                    "%(failed)s." %
                    {'return_string': EXIT_CODE_TO_STRING.get(exit_code),
                     'total': (success + failed), 'success': success,
                     'failed': failed})

        return "UPDATE STATUS: UNKNOWN"


def update_firmware(node):
    """Performs hpsum firmware update on the node.

    This method performs hpsum firmware update by mounting the
    SPP ISO on the node. It performs firmware update on all or
    some of the firmware components.

    :param node: A node object of type dict.
    :returns: Operation Status string.
    :raises: HpsumOperationError, when the vmedia device is not found or
        when the mount operation fails or when the image validation fails.
    :raises: IloConnectionError, when the iLO connection fails.
    :raises: IloError, when vmedia eject or insert operation fails.
    """
    hpsum_update_iso = node['clean_step']['args']['firmware_images'][0].get(
        'url')

    # Validates the http image reference for hpsum update ISO.
    try:
        utils.validate_href(hpsum_update_iso)
    except exception.ImageRefValidationFailed as e:
        raise exception.HpsumOperationError(reason=e)

    # Ejects the CDROM device in the iLO and inserts the hpsum update ISO
    # to the CDROM device.
    info = node.get('driver_info')
    ilo_object = client.IloClient(info.get('ilo_address'),
                                  info.get('ilo_username'),
                                  info.get('ilo_password'))

    ilo_object.eject_virtual_media('CDROM')
    ilo_object.insert_virtual_media(hpsum_update_iso, 'CDROM')

    # Waits for the OS to detect the disk and update the label file. SPP ISO
    # is identified by matching its label.
    time.sleep(5)
    vmedia_device_dir = "/dev/disk/by-label/"
    for file in os.listdir(vmedia_device_dir):
        if fnmatch.fnmatch(file, 'SPP*'):
            vmedia_device_file = os.path.join(vmedia_device_dir, file)

    if not os.path.exists(vmedia_device_file):
        msg = "Unable to find the virtual media device for HPSUM"
        raise exception.HpsumOperationError(reason=msg)

    # Validates the SPP ISO image for any file corruption using the checksum
    # of the ISO file.
    expected_checksum = node['clean_step']['args']['firmware_images'][0].get(
        'checksum')
    try:
        utils.verify_image_checksum(vmedia_device_file, expected_checksum)
    except exception.ImageRefValidationFailed as e:
        raise exception.HpsumOperationError(reason=e)

    # Mounts SPP ISO on a temporary directory.
    vmedia_mount_point = tempfile.mkdtemp()
    try:
        try:
            processutils.execute("mount", vmedia_device_file,
                                 vmedia_mount_point)
        except processutils.ProcessExecutionError as e:
            msg = ("Unable to mount virtual media device %(device)s: "
                   "%(error)s" % {'device': vmedia_device_file, 'error': e})
            raise exception.HpsumOperationError(reason=msg)

        # Executes the hpsum based firmware update by passing the default hpsum
        # executable path and the components specified, if any.
        hpsum_file_path = os.path.join(vmedia_mount_point, HPSUM_LOCATION)
        components = node['clean_step']['args']['firmware_images'][0].get(
            'component')
        if components:
            components = components.strip().split(',')

        result = _execute_hpsum(hpsum_file_path, components=components)

        processutils.trycmd("umount", vmedia_mount_point)
    finally:
        shutil.rmtree(vmedia_mount_point, ignore_errors=True)

    return result
