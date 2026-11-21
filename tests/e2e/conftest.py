"""Fixtures for the live web-service tests.

A real platform is started in a temporary VOLTTRON_HOME with the web service enabled, one web user, the Platform
Driver with a fake device, and a listener agent, built on volttron-testing's ``web_platform`` fixture (one platform
for the session: ``volttron_platform_scope = session`` in pyproject.toml). Set VOLTTRON_TESTING_SKIP_PLATFORM=1 to
skip these tests.
"""
import json
import time
from pathlib import Path

import pytest
import requests

from volttrontesting.configs import AgentSources
from volttrontesting.plugin import WebUser, install_platform_driver

# Checkouts when they can be found (an editable install, VOLTTRON_MODULAR_DRIVERS, or this repository's place in the
# modular tree); the Platform Driver falls back to its PyPI name.
SOURCES = AgentSources(anchor=__file__)
PLATFORM_DRIVER_SOURCE = SOURCES.resolve('platform-driver')
# fake.config and fake.csv ship at the root of the fake driver's source tree, not in its package.
FAKE_DRIVER_DIR = SOURCES.checkout('fake-driver')
REGISTRY_CONFIG = 'registry_configs/fake.csv'

PLATFORM_DRIVER = 'platform.driver'
LISTENER = 'listener'
CONTROL = 'platform.control'
DEVICE_TOPIC = 'fake-campus/fake-building/fake-device'
USERNAME, PASSWORD = 'admin', 'correct horse battery staple'

RPC_ALLOW_LIST = [
    f'{CONTROL}: peerlist, list_agents',
    f'{LISTENER}: *',
]


def _device_config(tmp_path_factory) -> Path:
    """The fake driver's sample device config, pointed at the registry stored by the platform fixture, with duplicate
    remotes allowed.

    The registry path is set explicitly because the sample ``fake.config`` differs between fake-driver releases
    (``config://fake.csv`` upstream, ``config://registry_configs/fake.csv`` in newer checkouts); the fixture must match
    wherever ``platform`` stores the CSV.

    Workaround (2026-10-14): platform_driver.agent._get_or_create_remote passes the generic RemoteConfig to the
    interface's unique_remote_id, but the fake interface reads ``remote_id`` from its own FakeRemoteConfig, so
    configuring the device raises AttributeError and the device is never polled. With allow_duplicate_remotes the
    base implementation is used instead.
    """
    config = json.loads((FAKE_DRIVER_DIR / 'fake.config').read_text())
    config['registry_config'] = f'config://{REGISTRY_CONFIG}'
    config['allow_duplicate_remotes'] = True
    path = tmp_path_factory.mktemp('configs') / 'fake-device.json'
    path.write_text(json.dumps(config))
    return path


@pytest.fixture(scope='session')
def web_platform_options():
    return dict(instance_name='web-e2e', rpc_allow_list=RPC_ALLOW_LIST)


@pytest.fixture(scope='session')
def web_user():
    return WebUser(USERNAME, PASSWORD)


@pytest.fixture(scope='session')
def platform(web_platform, tmp_path_factory):
    if FAKE_DRIVER_DIR is None:
        pytest.skip('the fake driver source tree (fake.config, fake.csv) was not found; install volttron-lib-fake-driver '
                    'editable from its checkout')
    driver = install_platform_driver(web_platform, PLATFORM_DRIVER_SOURCE, identity=PLATFORM_DRIVER)
    driver.store_registry(REGISTRY_CONFIG, FAKE_DRIVER_DIR / 'fake.csv')
    driver.store_device(DEVICE_TOPIC, _device_config(tmp_path_factory))
    web_platform.install_agent('volttron-listener', vip_identity=LISTENER, tag='listener')
    return web_platform


@pytest.fixture(scope='session')
def config_write_capability(platform):
    """Grant platform.web the config-store write capabilities. libauth grants it only control.* by default, so an
    operator must do this before the config write endpoints work; tests that write configs request this fixture, and
    a test that runs before any of them checks the 403 given without it."""
    platform.grant('platform.web', 'platform.config_store.set_config', 'platform.config_store.delete_config',
                   'platform.config_store.delete_store')
    return True


@pytest.fixture(scope='session')
def base_url(platform):
    return platform.bind_address


@pytest.fixture(scope='session')
def tokens(base_url):
    r = requests.post(f'{base_url}/authenticate', json={'username': USERNAME, 'password': PASSWORD}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope='session')
def api(base_url, tokens):
    """A requests session carrying the access token, with the base URL prepended to relative paths."""
    s = requests.Session()
    s.headers['Authorization'] = f"Bearer {tokens['access_token']}"
    base = base_url

    class Api:
        def request(self, method, path, **kw):
            kw.setdefault('timeout', 30)
            return s.request(method, base + path, **kw)

        def __getattr__(self, name):
            if name in ('get', 'put', 'post', 'delete', 'head', 'options', 'patch'):
                return lambda path, **kw: self.request(name.upper(), path, **kw)
            raise AttributeError(name)

        url = base
        session = s
        access_token = tokens['access_token']

    return Api()


@pytest.fixture(scope='session')
def platform_name(platform):
    return platform.instance_name


@pytest.fixture(scope='session')
def P(platform_name):
    """Prefix for the local platform's VUI routes."""
    return f'/vui/platforms/{platform_name}'


def wait_until(predicate, timeout=30.0, interval=1.0, message='condition not met'):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f'{message} within {timeout}s (last value: {last!r})')
