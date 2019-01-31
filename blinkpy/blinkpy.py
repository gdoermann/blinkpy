# -*- coding: utf-8 -*-
"""
blinkpy is an unofficial api for the Blink security camera system.

repo url: https://github.com/fronzbot/blinkpy

Original protocol hacking by MattTW :
https://github.com/MattTW/BlinkMonitorProtocol

Published under the MIT license - See LICENSE file for more details.
"Blink Wire-Free HS Home Monitoring & Alert Systems" is a trademark
owned by Immedia Inc., see www.blinkforhome.com for more information.
blinkpy is in no way affiliated with Blink, nor Immedia Inc.
"""

import os.path
import time
import getpass
import logging
from shutil import copyfileobj

from requests.structures import CaseInsensitiveDict
from dateutil.parser import parse

from blinkpy import api
from blinkpy.sync_module import BlinkSyncModule
from blinkpy.helpers import errors as ERROR
from blinkpy.helpers.util import (
    create_session, merge_dicts, get_time, BlinkURLHandler,
    BlinkAuthenticationException, RepeatLogHandler)
from blinkpy.helpers.constants import (
    BLINK_URL, LOGIN_URL, LOGIN_BACKUP_URL)
from blinkpy.helpers.constants import __version__

REFRESH_RATE = 30

_LOGGER = logging.getLogger('blinkpy')


class Blink():
    """Class to initialize communication."""

    def __init__(self, username=None, password=None,
                 refresh_rate=REFRESH_RATE, loglevel=logging.INFO,
                 allow_duplicate_logs=True):
        """
        Initialize Blink system.

        :param username: Blink username (usually email address)
        :param password: Blink password
        :param refresh_rate: Refresh rate of blink information.
                             Defaults to 15 (seconds)
        :param loglevel: Sets the log level for the logger.
        :param allow_duplicate_logs: Set to 'False' to only allow a log
                                     message to be logged once.
        """
        self._username = username
        self._password = password
        self._token = None
        self._auth_header = None
        self._host = None
        self.account_id = None
        self.network_ids = []
        self.urls = None
        self.sync = CaseInsensitiveDict({})
        self.region = None
        self.region_id = None
        self.last_refresh = None
        self.refresh_rate = refresh_rate
        self.session = create_session()
        self.networks = []
        self.cameras = CaseInsensitiveDict({})
        self.video_list = CaseInsensitiveDict({})
        self._login_url = LOGIN_URL
        self.version = __version__
        self.allow_duplicate_logs = allow_duplicate_logs

        _LOGGER.addHandler(RepeatLogHandler())
        _LOGGER.setLevel(loglevel)

    @property
    def auth_header(self):
        """Return the authentication header."""
        return self._auth_header

    def start(self):
        """
        Perform full system setup.

        Method logs in and sets auth token, urls, and ids for future requests.
        Essentially this is just a wrapper function for ease of use.
        """
        if not self.allow_duplicate_logs:
            self._reset_handler()
        if self._username is None or self._password is None:
            if not self.login():
                return
        elif not self.get_auth_token():
            return

        networks = self.get_ids()
        for network_name, network_id in networks.items():
            sync_module = BlinkSyncModule(self, network_name, network_id)
            sync_module.start()
            self.sync[network_name] = sync_module
        self.cameras = self.merge_cameras()

    def login(self):
        """Prompt user for username and password."""
        self._username = input("Username:")
        self._password = getpass.getpass("Password:")
        if self.get_auth_token():
            _LOGGER.info("Login successful!")
            return True
        _LOGGER.warning("Unable to login with %s.", self._username)
        return False

    def get_auth_token(self, is_retry=False):
        """Retrieve the authentication token from Blink."""
        if not isinstance(self._username, str):
            raise BlinkAuthenticationException(ERROR.USERNAME)
        if not isinstance(self._password, str):
            raise BlinkAuthenticationException(ERROR.PASSWORD)

        login_url = LOGIN_URL
        response = api.request_login(self,
                                     login_url,
                                     self._username,
                                     self._password,
                                     is_retry=is_retry)
        try:
            if response.status_code != 200:
                _LOGGER.debug("Received response code %s during login.",
                              response.status_code)
                login_url = LOGIN_BACKUP_URL
                response = api.request_login(self,
                                             login_url,
                                             self._username,
                                             self._password,
                                             is_retry=is_retry)
            response = response.json()
            (self.region_id, self.region), = response['region'].items()
        except AttributeError:
            _LOGGER.error("Login API endpoint failed with response %s",
                          response,
                          exc_info=True)
            return False
        except KeyError:
            _LOGGER.warning("Could not extract region info.")
            self.region_id = 'piri'
            self.region = 'UNKNOWN'

        self._host = "{}.{}".format(self.region_id, BLINK_URL)
        self._token = response['authtoken']['authtoken']
        self.networks = response['networks']

        self._auth_header = {'Host': self._host,
                             'TOKEN_AUTH': self._token}
        self.urls = BlinkURLHandler(self.region_id)
        self._login_url = login_url

        return self._auth_header

    def get_ids(self):
        """Set the network ID and Account ID."""
        response = api.request_networks(self)
        all_networks = []
        network_dict = {}
        for network, status in self.networks.items():
            if status['onboarded']:
                all_networks.append('{}'.format(network))
                network_dict[status['name']] = network

        # For the first onboarded network we find, grab the account id
        for resp in response['networks']:
            if str(resp['id']) in all_networks:
                self.account_id = resp['account_id']
                break

        self.network_ids = all_networks
        return network_dict

    def refresh(self, force_cache=False):
        """
        Perform a system refresh.

        :param force_cache: Force an update of the camera cache
        """
        if self.check_if_ok_to_update() or force_cache:
            for sync_name, sync_module in self.sync.items():
                _LOGGER.debug("Attempting refresh of sync %s", sync_name)
                sync_module.refresh(force_cache=force_cache)
            if not force_cache:
                # Prevents rapid clearing of motion detect property
                self.last_refresh = int(time.time())

    def check_if_ok_to_update(self):
        """Check if it is ok to perform an http request."""
        current_time = int(time.time())
        last_refresh = self.last_refresh
        if last_refresh is None:
            last_refresh = 0
        if current_time >= (last_refresh + self.refresh_rate):
            return True
        return False

    def merge_cameras(self):
        """Merge all sync camera dicts into one."""
        combined = CaseInsensitiveDict({})
        for sync in self.sync:
            combined = merge_dicts(combined, self.sync[sync].cameras)
        return combined

    def download_videos(self, path, since=None, camera='all', stop=10):
        """
        Download all videos from server since specified time.

        :param path: Path to write files.  /path/<cameraname>_<recorddate>.mp4
        :param since: Date and time to get videos from.
                      Ex: "2018/07/28 12:33:00" to retrieve videos since
                           July 28th 2018 at 12:33:00
        :param camera: Camera name to retrieve.  Defaults to "all".
                       Use a list for multiple cameras.
        :param stop: Page to stop on (~25 items per page. Default page 10).
        """
        # Reset the handler so we don't filter out messages during this method.
        if not self.allow_duplicate_logs:
            self._reset_logger()

        if since is None:
            since_epochs = self.last_refresh
        else:
            parsed_datetime = parse(since, fuzzy=True)
            since_epochs = parsed_datetime.timestamp()

        formatted_date = get_time(time_to_convert=since_epochs)
        _LOGGER.info("Retrieving videos since %s", formatted_date)

        if not isinstance(camera, list):
            camera = [camera]

        for page in range(1, stop):
            response = api.request_videos(self, time=since_epochs, page=page)
            _LOGGER.debug("Processing page %s", page)
            try:
                result = response['videos']
                if not result:
                    raise IndexError
            except (KeyError, IndexError):
                _LOGGER.info("No videos found on page %s. Exiting.", page)
                break

            self._parse_downloaded_items(result, camera, path)

    def _parse_downloaded_items(self, result, camera, path):
        """Parse downloaded videos."""
        for item in result:
            try:
                created_at = item['created_at']
                camera_name = item['camera_name']
                is_deleted = item['deleted']
                address = item['address']
            except KeyError:
                _LOGGER.info("Missing clip information, skipping...")
                continue

            if camera_name not in camera and 'all' not in camera:
                _LOGGER.debug("Skipping videos for %s.", camera_name)
                continue

            if is_deleted:
                _LOGGER.debug("%s: %s is marked as deleted.",
                              camera_name,
                              address)
                continue

            clip_address = "{}{}".format(self.urls.base_url, address)
            filename = "{}_{}.mp4".format(camera_name, created_at)
            filename = os.path.join(path, filename)

            if os.path.isfile(filename):
                _LOGGER.info("%s already exists, skipping...", filename)
                continue

            response = api.http_get(self, url=clip_address,
                                    stream=True, json=False)
            with open(filename, 'wb') as vidfile:
                copyfileobj(response.raw, vidfile)

            _LOGGER.info("Downloaded video to %s", filename)

    def _reset_logger(self):
        """Reset the log handler."""
        for handler in _LOGGER.handlers:
            handler.close()
            _LOGGER.removeHandler(handler)
        _LOGGER.addHandler(RepeatLogHandler())
