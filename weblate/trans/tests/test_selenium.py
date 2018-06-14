# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import print_function
from unittest import SkipTest
import math
import time
import tempfile
import os
import json
from contextlib import contextmanager
from base64 import b64encode
from six.moves.http_client import HTTPConnection
import django
from django.conf import settings
from django.test.utils import override_settings
from django.urls import reverse
from django.core import mail

from PIL import Image

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        WebDriverException, ElementNotVisibleException
    )
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support.expected_conditions import staleness_of
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

import six

from weblate.trans.tests.test_views import RegistrationTestMixin
from weblate.trans.tests.test_models import BaseLiveServerTestCase
from weblate.trans.tests.utils import create_test_user
from weblate.vcs.ssh import get_key_data

# Check whether we should run Selenium tests
DO_SELENIUM = (
    'DO_SELENIUM' in os.environ and
    'SAUCE_USERNAME' in os.environ and
    'SAUCE_ACCESS_KEY' in os.environ and
    HAS_SELENIUM
)


class SeleniumTests(BaseLiveServerTestCase, RegistrationTestMixin):
    caps = {
        'browserName': 'firefox',
        'platform': 'Windows 10',
    }
    driver = None
    image_path = None

    def set_test_status(self, passed=True):
        connection = HTTPConnection("saucelabs.com")
        connection.request(
            'PUT',
            '/rest/v1/{0}/jobs/{1}'.format(
                self.username, self.driver.session_id
            ),
            json.dumps({"passed": passed}),
            headers={"Authorization": "Basic {0}".format(self.sauce_auth)}
        )
        result = connection.getresponse()
        return result.status == 200

    def run(self, result=None):
        if result is None:
            result = self.defaultTestResult()

        errors = len(result.errors)
        failures = len(result.failures)
        super(SeleniumTests, self).run(result)

        if DO_SELENIUM:
            self.set_test_status(
                errors == len(result.errors) and
                failures == len(result.failures)
            )

    @contextmanager
    def wait_for_page_load(self, timeout=30):
        old_page = self.driver.find_element_by_tag_name('html')
        yield
        WebDriverWait(self.driver, timeout).until(
            staleness_of(old_page)
        )

    @classmethod
    def setUpClass(cls):
        if DO_SELENIUM:
            cls.caps['name'] = 'Weblate CI build'
            cls.caps['screenResolution'] = '1024x768'
            # Fill in Travis details in caps
            if 'TRAVIS_JOB_NUMBER' in os.environ:
                cls.caps['tunnel-identifier'] = os.environ['TRAVIS_JOB_NUMBER']
                cls.caps['build'] = os.environ['TRAVIS_BUILD_NUMBER']
                cls.caps['tags'] = [
                    'python-{0}'.format(os.environ['TRAVIS_PYTHON_VERSION']),
                    'django-{0}'.format(django.get_version()),
                    'CI'
                ]

            # Use Sauce connect
            cls.username = os.environ['SAUCE_USERNAME']
            cls.key = os.environ['SAUCE_ACCESS_KEY']
            cls.sauce_auth = b64encode(
                '{}:{}'.format(cls.username, cls.key).encode('utf-8')
            )
            cls.driver = webdriver.Remote(
                desired_capabilities=cls.caps,
                command_executor="http://{0}:{1}@{2}/wd/hub".format(
                    cls.username,
                    cls.key,
                    'ondemand.saucelabs.com',
                )
            )
            cls.driver.implicitly_wait(10)
            cls.actions = webdriver.ActionChains(cls.driver)
            jobid = cls.driver.session_id
            print(
                'Sauce Labs job: https://saucelabs.com/jobs/{0}'.format(jobid)
            )
            cls.image_path = os.path.join(settings.BASE_DIR, 'test-images')
            if not os.path.exists(cls.image_path):
                os.makedirs(cls.image_path)
        super(SeleniumTests, cls).setUpClass()

    def setUp(self):
        if self.driver is None:
            raise SkipTest('Selenium Tests disabled')
        super(SeleniumTests, self).setUp()
        self.driver.get('{0}{1}'.format(self.live_server_url, reverse('home')))
        self.driver.set_window_size(1024, 768)
        time.sleep(1)

    @classmethod
    def tearDownClass(cls):
        super(SeleniumTests, cls).tearDownClass()
        if cls.driver is not None:
            cls.driver.quit()
            cls.driver = None

    def scroll_top(self):
        self.driver.execute_script('window.scrollTo(0, 0)')

    def screenshot(self, name):
        """Captures named full page screenshot."""
        self.scroll_top()
        # Get window and document dimensions
        window_height = self.driver.execute_script(
            'return window.innerHeight'
        )
        scroll_height = self.driver.execute_script(
            'return document.body.parentNode.scrollHeight'
        )
        # Calculate number of screnshots
        num = int(math.ceil(float(scroll_height) / float(window_height)))

        # Create temporary files
        tempfiles = []
        for i in xrange( num ):
            fd, path = tempfile.mkstemp(
                prefix='wl-shot-{0:02}-'.format(i), suffix='.png'
            )
            os.close(fd)
            tempfiles.append(path)

        try:
            # take screenshots
            for i, path in enumerate(tempfiles):
                if i > 0:
                    self.driver.execute_script(
                        'window.scrollBy(%d,%d)' % (0, window_height)
                    )

                self.driver.save_screenshot(path)

            # Stitch images together
            stiched = None
            for i, path in enumerate(tempfiles):
                img = Image.open(path)

                w, h = img.size
                y = i * window_height

                if i == (len(tempfiles) - 1):
                    crop_height = scroll_height % h
                    if crop_height > 0:
                        img = img.crop((0, h - crop_height, w, h))
                    w, h = img.size

                if stiched is None:
                    stiched = Image.new('RGB', (w, scroll_height))

                stiched.paste(img, (0, y, w, y + h))

            stiched.save(os.path.join(self.image_path, name))
        finally:
            # Temp files cleanup
            for path in tempfiles:
                if os.path.isfile(path):
                    os.remove(path)
        self.scroll_top()

    def click(self, element):
        """Wrapper to scroll into element for click"""
        if isinstance(element, six.string_types):
            element = self.driver.find_element_by_link_text(element)

        try:
            element.click()
        except ElementNotVisibleException:
            self.actions.move_to_element(element).perform()
            element.click()

    def clear_field(self, element):
        element.send_keys(Keys.CONTROL + 'a')
        element.send_keys(Keys.DELETE)
        return element

    def do_login(self, create=True, superuser=False):
        # login page
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_id('login-button'),
            )

        # Create user
        if create:
            user = create_test_user()
            if superuser:
                user.is_superuser = True
                user.save()

        # Login
        username_input = self.driver.find_element_by_id('id_username')
        username_input.send_keys('weblate@example.org')
        password_input = self.driver.find_element_by_id('id_password')
        password_input.send_keys('testpassword')

        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_xpath('//input[@value="Login"]')
            )

    def open_admin(self):
        # Login as superuser
        self.do_login(superuser=True)

        # Open admin page
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_id('admin-button'),
            )

    def test_failed_login(self):
        self.do_login(create=False)

        # We should end up on login page as user was invalid
        self.driver.find_element_by_id('id_username')

    def test_login(self):
        # Do proper login with new user
        self.do_login()

        # Load profile
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_id('profile-button')
            )

        # Wait for profile to load
        self.driver.find_element_by_id('subscriptions')

        # Finally logout
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_id('logout-button')
            )

        # We should be back on home page
        self.driver.find_element_by_id('suggestions')

    def register_user(self):
        # registration page
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_id('register-button'),
            )

        # Fill in registration form
        self.driver.find_element_by_id(
            'id_email'
        ).send_keys(
            'weblate@example.org'
        )
        self.driver.find_element_by_id(
            'id_username'
        ).send_keys(
            'test-example'
        )
        self.driver.find_element_by_id(
            'id_fullname'
        ).send_keys(
            'Test Example'
        )
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_xpath('//input[@value="Register"]')
            )

        # Wait for registration email
        loops = 0
        while not mail.outbox:
            time.sleep(1)
            loops += 1
            if loops > 20:
                break

        return ''.join(
            (self.live_server_url, self.assert_registration_mailbox())
        )

    @override_settings(REGISTRATION_CAPTCHA=False)
    def test_register(self, clear=False):
        """Test registration."""
        url = self.register_user()

        # Delete all cookies
        if clear:
            try:
                self.driver.delete_all_cookies()
            except WebDriverException as error:
                # This usually happens when browser fails to delete some
                # of the cookies for whatever reason.
                print('Ignoring: {0}'.format(error))

        # Confirm account
        self.driver.get(url)

        # Check we're logged in
        self.assertTrue(
            'Test Example' in
            self.driver.find_element_by_id('profile-button').text
        )

        # Check we got message
        self.assertTrue(
            'You have activated' in
            self.driver.find_element_by_tag_name('body').text
        )

    def test_register_nocookie(self):
        """Test registration without cookies."""
        self.test_register(True)

    def test_admin_ssh(self):
        """Test admin interface."""
        self.open_admin()

        self.screenshot('admin.png')

        # Open SSH page
        with self.wait_for_page_load():
            self.click('SSH keys')

        # Generate SSH key
        if get_key_data() is None:
            with self.wait_for_page_load():
                self.click(
                    self.driver.find_element_by_id('generate-ssh-button'),
                )

        # Add SSH host key
        self.driver.find_element_by_id(
            'ssh-host'
        ).send_keys(
            'github.com'
        )
        with self.wait_for_page_load():
            self.click(
                self.driver.find_element_by_id('ssh-add-button'),
            )

        self.screenshot('ssh-keys-added.png')


        # Open SSH page for final screenshot
        with self.wait_for_page_load():
            self.click('Home')
        with self.wait_for_page_load():
            self.click('SSH keys')
        self.screenshot('ssh-keys.png')

    def test_admin_componentlist(self):
        """Test admin interface."""
        self.open_admin()

        with self.wait_for_page_load():
            self.click('Component lists')

        with self.wait_for_page_load():
            self.click('Add Component list')
        self.driver.find_element_by_id('id_name').send_keys('All components')

        self.click('Add another Automatic component list assignment')
        self.clear_field(
            self.driver.find_element_by_id(
                'id_autocomponentlist_set-0-project_match'
            )
        ).send_keys('^.*$')
        self.clear_field(
            self.driver.find_element_by_id(
                'id_autocomponentlist_set-0-component_match'
            )
        ).send_keys('^.*$')
        self.screenshot('componentlist-add.png')

        with self.wait_for_page_load():
            self.driver.find_element_by_id('id_name').submit()

        # Ensure the component list is there
        self.click('All components')


# What other platforms we want to test
EXTRA_PLATFORMS = {
    'Chrome': {
        'browserName': 'chrome',
        'platform': 'Windows 10',
    },
}


def create_extra_classes():
    """Create classes for testing with other browsers"""
    classes = {}
    for platform, caps in EXTRA_PLATFORMS.items():
        name = '{0}_{1}'.format(
            platform,
            SeleniumTests.__name__,
        )
        classdict = dict(SeleniumTests.__dict__)
        classdict.update({
            'caps': caps,
        })
        classes[name] = type(name, (SeleniumTests,), classdict)

    globals().update(classes)


create_extra_classes()
